#!/usr/bin/env python3
"""
pipeline.py - Orchestration pipeline for SNES-VideoPlayer conversion

Coordinates the full video-to-MSU-1 pipeline:
  1. Extract frames from video (ffmpeg)
  2. Extract audio from video (ffmpeg -> MSU-1 PCM)
  3. Convert frames to SNES tiles (parallel with ThreadPoolExecutor)
  4. Package into .msu file

Provides progress callbacks for GUI integration.
"""

import os
import sys
import time
import glob
import tempfile
import shutil
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from frame_extract import (extract_frames, get_extracted_frames, get_video_info,
                           find_ffmpeg, SCALE_STRETCH)
from audio_extract import extract_audio_from_video
from tile_convert import (convert_frame, compute_shared_palette,
                          FRAME_WIDTH, FRAME_HEIGHT,
                          MAX_PALETTES, MAX_TILES,
                          DEFAULT_DITHER, DEFAULT_ENGINE)
from msu_package import package_single_chapter, MSU_TITLE
from preview import reconstruct_to_pil

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)


class PipelineProgress:
    """Progress tracker with callbacks for each pipeline phase."""

    def __init__(self):
        self.phase = ""
        self.phase_progress = 0.0  # 0.0 to 1.0
        self.overall_progress = 0.0
        self.message = ""
        self.error = None
        self.cancelled = False

        # Callbacks
        self.on_phase_change = None      # callable(phase_name, message)
        self.on_progress = None          # callable(phase_progress, overall_progress, message)
        self.on_frame_converted = None   # callable(frame_index, total_frames, tile_bytes, map_bytes, pal_bytes, source_path)
        self.on_complete = None          # callable(output_path)
        self.on_error = None             # callable(error_message)

    def set_phase(self, phase, message=""):
        self.phase = phase
        self.message = message
        if self.on_phase_change:
            self.on_phase_change(phase, message)

    def update(self, phase_progress, overall_progress, message=""):
        self.phase_progress = phase_progress
        self.overall_progress = overall_progress
        self.message = message
        if self.on_progress:
            self.on_progress(phase_progress, overall_progress, message)

    def report_error(self, message):
        self.error = message
        if self.on_error:
            self.on_error(message)

    def cancel(self):
        self.cancelled = True


class ConversionPipeline:
    """Full video-to-MSU-1 conversion pipeline."""

    # Phase weights for overall progress calculation
    PHASE_WEIGHTS = {
        'extract_frames': 0.10,
        'extract_audio': 0.05,
        'convert_tiles': 0.75,
        'package_msu': 0.10,
    }

    def __init__(self, video_path, output_path, workers=4,
                 ffmpeg=None, title=MSU_TITLE, fps=24,
                 num_palettes=MAX_PALETTES, max_tiles=MAX_TILES,
                 dither_method=DEFAULT_DITHER, engine=DEFAULT_ENGINE,
                 deinterlace=False, start_time=None, duration=None,
                 segments=None, scale_mode=SCALE_STRETCH, aspect_ratio=None):
        """Initialize the conversion pipeline.

        Args:
            video_path: Path to source video file
            output_path: Path to output .msu file
            workers: Number of parallel workers for tile conversion
            ffmpeg: Path to ffmpeg (None = auto-detect)
            title: MSU-1 title string (21 chars)
            fps: Playback frame rate
            num_palettes: Number of sub-palettes per frame (default 2)
            max_tiles: Maximum tiles per frame (default 384)
            dither_method: Dithering method ('none', 'floyd-steinberg', 'ordered')
            engine: Conversion engine ('builtin' or 'superfamiconv')
            deinterlace: Apply deinterlace filter
            start_time: Start time in seconds (None = beginning)
            duration: Duration in seconds (None = full video)
            segments: SegmentList for per-segment quality settings (None = use global settings)
            scale_mode: Video scaling mode ('stretch', 'fit', 'crop')
            aspect_ratio: Optional aspect ratio override (e.g. '16:9')
        """
        self.video_path = video_path
        self.output_path = output_path
        self.workers = workers
        self.ffmpeg = ffmpeg or find_ffmpeg()
        self.title = title
        self.fps = fps
        self.num_palettes = num_palettes
        self.max_tiles = max_tiles
        self.dither_method = dither_method
        self.engine = engine
        self.deinterlace = deinterlace
        self.start_time = start_time
        self.duration = duration
        self.segments = segments
        # Scale mode and aspect ratio are global settings that affect how
        # ffmpeg extracts frames — not per-segment because AR is a property
        # of the source video. Passed through to extract_frames() in phase 1.
        self.scale_mode = scale_mode
        self.aspect_ratio = aspect_ratio

        self.progress = PipelineProgress()
        self.temp_dir = None
        self.frame_dir = None

    def run(self):
        """Run the full conversion pipeline.

        Returns:
            True on success, False on error
        """
        start_time_wall = time.time()

        try:
            # Create temp directory for intermediate files
            self.temp_dir = tempfile.mkdtemp(prefix='snes_vp_')
            self.frame_dir = os.path.join(self.temp_dir, 'frames')
            os.makedirs(self.frame_dir, exist_ok=True)

            logger.info("Starting conversion pipeline")
            logger.info("  Video: %s", self.video_path)
            logger.info("  Output: %s", self.output_path)
            logger.info("  Workers: %d", self.workers)
            logger.info("  Temp dir: %s", self.temp_dir)

            # Phase 1: Extract frames
            if self.progress.cancelled:
                return False
            frame_count = self._extract_frames()
            if frame_count <= 0:
                self.progress.report_error("No frames extracted from video")
                return False

            # Phase 2: Extract audio
            if self.progress.cancelled:
                return False
            audio_path = self._extract_audio()

            # Phase 3: Convert tiles
            if self.progress.cancelled:
                return False
            frame_data = self._convert_tiles()
            if not frame_data:
                self.progress.report_error("No frames converted successfully")
                return False

            # Phase 4: Package MSU
            if self.progress.cancelled:
                return False
            self._package_msu(frame_data, audio_path)

            # Phase 5: Create zip
            if self.progress.cancelled:
                return False
            zip_path = self._create_zip(len(frame_data))

            elapsed = time.time() - start_time_wall
            msg = f"Conversion complete in {elapsed:.1f}s ({len(frame_data)} frames)"
            logger.info(msg)
            self.progress.update(1.0, 1.0, msg)

            if self.progress.on_complete:
                self.progress.on_complete(zip_path or self.output_path)

            return True

        except Exception as e:
            self.progress.report_error(str(e))
            logger.error("Pipeline error: %s", e)
            return False

        finally:
            # Clean up temp directory
            if self.temp_dir and os.path.isdir(self.temp_dir):
                try:
                    shutil.rmtree(self.temp_dir)
                except OSError:
                    pass

    def _extract_frames(self):
        """Phase 1: Extract video frames."""
        self.progress.set_phase('extract_frames', 'Extracting video frames...')

        # Get video info for progress estimation
        info = get_video_info(self.video_path, self.ffmpeg)
        estimated_frames = info['num_frames'] if info else 0

        frame_count = extract_frames(
            self.video_path, self.frame_dir,
            ffmpeg=self.ffmpeg,
            start_time=self.start_time,
            duration=self.duration,
            deinterlace=self.deinterlace,
            width=FRAME_WIDTH,
            height=FRAME_HEIGHT,
            scale_mode=self.scale_mode,
            aspect_ratio=self.aspect_ratio,
        )

        weight = self.PHASE_WEIGHTS['extract_frames']
        self.progress.update(1.0, weight, f"Extracted {frame_count} frames")
        logger.info("Extracted %d frames", frame_count)
        return frame_count

    def _extract_audio(self):
        """Phase 2: Extract audio."""
        self.progress.set_phase('extract_audio', 'Extracting audio...')

        audio_path = os.path.join(self.temp_dir, 'audio.pcm')

        try:
            extract_audio_from_video(
                self.video_path, audio_path,
                ffmpeg=self.ffmpeg,
                duration=self.duration,
            )
            logger.info("Audio extracted: %s", audio_path)
        except Exception as e:
            logger.warning("Audio extraction failed (continuing without audio): %s", e)
            audio_path = None

        weight = self.PHASE_WEIGHTS['extract_frames'] + self.PHASE_WEIGHTS['extract_audio']
        self.progress.update(1.0, weight, "Audio extraction complete")
        return audio_path

    def _convert_tiles(self):
        """Phase 3: Convert PNG frames to SNES tiles."""
        self.progress.set_phase('convert_tiles', 'Converting frames to SNES tiles...')

        frames = get_extracted_frames(self.frame_dir)
        total = len(frames)
        if total == 0:
            return []

        # Pre-compute shared palettes for segments that request it
        shared_palettes = {}  # seg_idx -> list of sub-palettes
        if self.segments is not None:
            start_offset = self.start_time or 0.0
            for seg_idx, seg in enumerate(self.segments.segments):
                if not seg.shared_palette or seg.engine != 'builtin':
                    continue
                # Find frame index range for this segment
                seg_frames = []
                for i, png_path in enumerate(frames):
                    t = start_offset + i / self.fps
                    if seg.start_time <= t < seg.end_time:
                        seg_frames.append(png_path)
                if not seg_frames:
                    continue
                # Sample up to 20 evenly spaced frames
                n_sample = min(20, len(seg_frames))
                step = max(1, len(seg_frames) // n_sample)
                sample_paths = [seg_frames[i * step] for i in range(n_sample)
                                if i * step < len(seg_frames)]
                logger.info("Computing shared palette for segment %d (%d sample frames)",
                            seg_idx, len(sample_paths))
                shared_palettes[seg_idx] = compute_shared_palette(
                    sample_paths, seg.num_palettes, seg.grayscale)

        frame_data = [None] * total  # Preserve order
        completed = 0
        failed = 0

        base_weight = self.PHASE_WEIGHTS['extract_frames'] + self.PHASE_WEIGHTS['extract_audio']
        tile_weight = self.PHASE_WEIGHTS['convert_tiles']

        def convert_one(idx_path):
            idx, png_path = idx_path

            # Per-frame segment lookup: use segment settings if available
            f_grayscale = False
            f_shared_palette = None
            if self.segments is not None:
                start_offset = self.start_time or 0.0
                t = start_offset + idx / self.fps
                seg = self.segments.settings_for_frame(idx, self.fps,
                                                       start_offset=start_offset)
                if seg is not None:
                    f_num_palettes = seg.num_palettes
                    f_max_tiles = seg.max_tiles
                    f_dither = seg.dither_method
                    f_engine = seg.engine
                    f_grayscale = seg.grayscale
                    seg_idx = self.segments.segment_index_for_time(t)
                    f_shared_palette = shared_palettes.get(seg_idx)
                else:
                    f_num_palettes = self.num_palettes
                    f_max_tiles = self.max_tiles
                    f_dither = self.dither_method
                    f_engine = self.engine
            else:
                f_num_palettes = self.num_palettes
                f_max_tiles = self.max_tiles
                f_dither = self.dither_method
                f_engine = self.engine

            success, err = convert_frame(png_path, num_palettes=f_num_palettes,
                                         max_tiles=f_max_tiles,
                                         dither_method=f_dither,
                                         engine=f_engine,
                                         grayscale=f_grayscale,
                                         shared_palette=f_shared_palette)
            if not success:
                return idx, None, err

            base = png_path[:-4]
            with open(base + '.tiles', 'rb') as f:
                tiles = f.read()
            with open(base + '.tilemap', 'rb') as f:
                tilemap = f.read()
            with open(base + '.palette', 'rb') as f:
                palette = f.read()

            return idx, (tiles, tilemap, palette), ""

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(convert_one, (i, p)): i
                       for i, p in enumerate(frames)}

            for future in as_completed(futures):
                if self.progress.cancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return []

                idx, data, err = future.result()
                if data is not None:
                    frame_data[idx] = data
                    completed += 1

                    # Notify with preview data
                    if self.progress.on_frame_converted:
                        self.progress.on_frame_converted(
                            completed, total, data[0], data[1], data[2],
                            frames[idx]
                        )
                else:
                    failed += 1
                    if failed <= 3:
                        logger.warning("Frame %d failed: %s", idx, err)

                progress = completed / total
                overall = base_weight + tile_weight * progress
                self.progress.update(
                    progress, overall,
                    f"Converted {completed}/{total} frames ({failed} failed)"
                )

        # Filter out None entries (failed frames)
        frame_data = [fd for fd in frame_data if fd is not None]
        logger.info("Converted %d/%d frames (%d failed)", completed, total, failed)
        return frame_data

    def _package_msu(self, frame_data, audio_path):
        """Phase 4: Package into .msu file."""
        self.progress.set_phase('package_msu', 'Packaging MSU-1 file...')

        audio_data = b''
        if audio_path and os.path.isfile(audio_path):
            with open(audio_path, 'rb') as f:
                audio_data = f.read()

        # Ensure output directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)

        package_single_chapter(
            self.output_path, frame_data,
            audio_data=audio_data,
            title=self.title,
            fps=self.fps,
        )

        overall = (self.PHASE_WEIGHTS['extract_frames'] +
                   self.PHASE_WEIGHTS['extract_audio'] +
                   self.PHASE_WEIGHTS['convert_tiles'] +
                   self.PHASE_WEIGHTS['package_msu'])
        self.progress.update(1.0, overall, "MSU-1 file packaged")
        logger.info("MSU-1 file written: %s", self.output_path)

    def _create_zip(self, num_frames):
        """Phase 5: Zip the ROM, MSU, PCM, and FILE_ID.DIZ into a ready-to-play archive."""
        self.progress.set_phase('create_zip', 'Creating zip archive...')

        output_dir = os.path.dirname(os.path.abspath(self.output_path))
        base = os.path.splitext(os.path.abspath(self.output_path))[0]
        msu_path = self.output_path
        pcm_path = base + '-0.pcm'

        # Find the prebuilt ROM
        converter_dir = os.path.dirname(os.path.abspath(__file__))
        rom_path = os.path.join(converter_dir, '..', 'prebuilt', 'SNESVideoPlayer.sfc')
        rom_path = os.path.normpath(rom_path)

        if not os.path.isfile(rom_path):
            logger.warning("Prebuilt ROM not found at %s, zip will not include .sfc", rom_path)
            rom_path = None

        # Build FILE_ID.DIZ
        video_name = os.path.basename(self.video_path)
        diz = (
            f"Made with SNES Video Player Converter tool\n"
            f"Source: {video_name}\n"
            f"{num_frames} frames @ {self.fps}fps\n"
        )

        zip_path = base + '.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if rom_path:
                zf.write(rom_path, 'SNESVideoPlayer.sfc')
            if os.path.isfile(msu_path):
                zf.write(msu_path, os.path.basename(msu_path))
            if os.path.isfile(pcm_path):
                zf.write(pcm_path, os.path.basename(pcm_path))
            zf.writestr('FILE_ID.DIZ', diz)

        zip_size = os.path.getsize(zip_path) / 1024 / 1024
        logger.info("Zip archive written: %s (%.1f MB)", zip_path, zip_size)
        self.progress.update(1.0, 1.0, f"Zip archive created ({zip_size:.1f} MB)")
        return zip_path


def run_pipeline(video_path, output_path, workers=4, ffmpeg=None,
                 title=MSU_TITLE, fps=24, deinterlace=False,
                 start_time=None, duration=None,
                 num_palettes=MAX_PALETTES, max_tiles=MAX_TILES,
                 dither_method=DEFAULT_DITHER, engine=DEFAULT_ENGINE,
                 segments=None,
                 scale_mode=SCALE_STRETCH, aspect_ratio=None,
                 progress_callback=None, frame_callback=None,
                 complete_callback=None, error_callback=None):
    """Convenience function to run the full pipeline with callbacks.

    Args:
        video_path: Source video file
        output_path: Output .msu file
        workers: Parallel worker count
        ffmpeg: ffmpeg path (None = auto-detect)
        title: MSU-1 title
        fps: Playback FPS
        deinterlace: Enable deinterlace
        start_time: Start time in seconds
        duration: Duration in seconds
        num_palettes: Number of sub-palettes per frame (default 2)
        max_tiles: Maximum tiles per frame (default 384)
        dither_method: Dithering method ('none', 'floyd-steinberg', 'ordered')
        engine: Conversion engine ('builtin' or 'superfamiconv')
        segments: SegmentList for per-segment quality settings (None = use global settings)
        scale_mode: Video scaling mode ('stretch', 'fit', 'crop')
        aspect_ratio: Optional aspect ratio override (e.g. '16:9')
        progress_callback: callable(phase_progress, overall_progress, message)
        frame_callback: callable(frame_idx, total, tiles, tilemap, palette, source_path)
        complete_callback: callable(output_path)
        error_callback: callable(error_message)

    Returns:
        True on success, False on error
    """
    pipeline = ConversionPipeline(
        video_path, output_path,
        workers=workers, ffmpeg=ffmpeg,
        title=title, fps=fps,
        num_palettes=num_palettes, max_tiles=max_tiles,
        dither_method=dither_method, engine=engine,
        deinterlace=deinterlace,
        start_time=start_time, duration=duration,
        segments=segments,
        scale_mode=scale_mode, aspect_ratio=aspect_ratio,
    )

    if progress_callback:
        pipeline.progress.on_progress = progress_callback
    if frame_callback:
        pipeline.progress.on_frame_converted = frame_callback
    if complete_callback:
        pipeline.progress.on_complete = complete_callback
    if error_callback:
        pipeline.progress.on_error = error_callback

    return pipeline.run()
