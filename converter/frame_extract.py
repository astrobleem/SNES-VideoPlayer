#!/usr/bin/env python3
"""
frame_extract.py - ffmpeg frame extraction wrapper for SNES-VideoPlayer

Extracts video frames from any video file as 256x160 PNG images at
24000/1001 fps (~23.976 fps) suitable for MSU-1 playback on SNES.

Supports optional deinterlacing, custom time ranges, and progress callbacks.
"""

import os
import subprocess
import glob
import shutil


# ---------- Constants ----------
FRAME_WIDTH = 256
FRAME_HEIGHT = 160
TARGET_FPS = "24000/1001"
TARGET_FPS_FLOAT = 24000.0 / 1001.0  # ~23.976


SCALE_STRETCH = 'stretch'
SCALE_FIT = 'fit'
SCALE_CROP = 'crop'
SCALE_MODES = [SCALE_STRETCH, SCALE_FIT, SCALE_CROP]


def _build_scale_filter(width, height, scale_mode, aspect_ratio=None):
    """Build ffmpeg filter string for scaling with the given mode.

    Args:
        width: Target width in pixels
        height: Target height in pixels
        scale_mode: 'stretch', 'fit', or 'crop'
        aspect_ratio: Optional aspect ratio override (e.g. '16:9', '4:3')

    Returns:
        List of filter strings to join with ','
    """
    filters = []

    if aspect_ratio and scale_mode != SCALE_STRETCH:
        # Reshape pixels to match the desired aspect ratio before fit/crop.
        # scale expressions: ih*ar_w/ar_h gives the width that produces
        # the desired AR at the current height. setsar=1 ensures the
        # subsequent scale sees square pixels.
        ar = aspect_ratio.replace('/', ':')
        ar_w, ar_h = ar.split(':')
        filters.append(f'scale=trunc(ih*{ar_w}/{ar_h}/2)*2:ih')
        filters.append('setsar=1')

    if scale_mode == SCALE_FIT:
        filters.append(f'scale={width}:{height}:force_original_aspect_ratio=decrease')
        filters.append(f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black')
    elif scale_mode == SCALE_CROP:
        filters.append(f'scale={width}:{height}:force_original_aspect_ratio=increase')
        filters.append(f'crop={width}:{height}')
    else:
        # stretch (default)
        filters.append(f'scale={width}:{height}')

    return filters


def find_ffmpeg():
    """Find ffmpeg executable. Checks PATH first, then common locations."""
    # Check PATH
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        return ffmpeg

    # Common Windows locations
    common_paths = [
        r'C:\ffmpeg\bin\ffmpeg.exe',
        r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
        os.path.expanduser('~/ffmpeg/bin/ffmpeg.exe'),
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p

    return 'ffmpeg'  # Hope it is in PATH


def get_video_info(video_path, ffmpeg='ffmpeg'):
    """Get video duration and frame count using ffprobe.

    Returns dict with: duration_s, fps, width, height, num_frames (estimated)
    """
    ffprobe = ffmpeg.replace('ffmpeg', 'ffprobe')
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which('ffprobe') or 'ffprobe'

    cmd = [
        ffprobe, '-v', 'quiet',
        '-print_format', 'json',
        '-show_format', '-show_streams',
        video_path
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None

        import json
        data = json.loads(result.stdout)

        info = {
            'duration_s': 0.0,
            'fps': 0.0,
            'width': 0,
            'height': 0,
            'num_frames': 0,
        }

        # Get duration from format
        if 'format' in data and 'duration' in data['format']:
            info['duration_s'] = float(data['format']['duration'])

        # Get video stream info
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                info['width'] = int(stream.get('width', 0))
                info['height'] = int(stream.get('height', 0))

                # Parse fps from r_frame_rate (e.g., "30000/1001")
                r_frame_rate = stream.get('r_frame_rate', '0/1')
                if '/' in r_frame_rate:
                    num, den = r_frame_rate.split('/')
                    if int(den) > 0:
                        info['fps'] = float(num) / float(den)

                # Get duration from stream if not in format
                if info['duration_s'] == 0.0 and 'duration' in stream:
                    info['duration_s'] = float(stream['duration'])

                # Estimate output frames at target FPS
                if info['duration_s'] > 0:
                    info['num_frames'] = int(info['duration_s'] * TARGET_FPS_FLOAT)
                break

        return info

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def extract_frames(video_path, output_dir, ffmpeg='ffmpeg',
                   start_time=None, duration=None, deinterlace=False,
                   width=FRAME_WIDTH, height=FRAME_HEIGHT,
                   scale_mode=SCALE_STRETCH, aspect_ratio=None,
                   progress_callback=None):
    """Extract video frames as 256x160 PNG images.

    Args:
        video_path: Path to source video file
        output_dir: Directory to write PNG frames
        ffmpeg: Path to ffmpeg executable
        start_time: Start time in seconds (None = from beginning)
        duration: Duration in seconds (None = entire video)
        deinterlace: Apply yadif deinterlace filter
        width: Output frame width (default 256)
        height: Output frame height (default 160)
        scale_mode: 'stretch', 'fit', or 'crop'
        aspect_ratio: Optional aspect ratio override (e.g. '16:9')
        progress_callback: Optional callable(current_frame, total_estimated)

    Returns:
        frame_count on success, -1 on error
    """
    os.makedirs(output_dir, exist_ok=True)

    # Build filter chain
    filters = []
    if deinterlace:
        filters.append('yadif')
    filters.append(f'fps={TARGET_FPS}')

    if start_time is not None and start_time > 0:
        if duration is not None:
            filters.append(f'trim=start={start_time:.6f}:duration={duration:.6f}')
        else:
            filters.append(f'trim=start={start_time:.6f}')
        filters.append('setpts=PTS-STARTPTS')
    elif duration is not None:
        filters.append(f'trim=duration={duration:.6f}')
        filters.append('setpts=PTS-STARTPTS')

    filters.extend(_build_scale_filter(width, height, scale_mode, aspect_ratio))

    filter_str = ','.join(filters)

    out_pattern = os.path.join(output_dir, "frame_%06d.png")

    cmd = [
        ffmpeg, '-y',
        '-i', video_path,
        '-vf', filter_str,
        '-f', 'image2',
        out_pattern
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            error_msg = result.stderr[-500:] if result.stderr else 'unknown error'
            raise RuntimeError(f"ffmpeg error: {error_msg}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg timed out (>1 hour)")

    frame_count = len(glob.glob(os.path.join(output_dir, "frame_*.png")))

    if progress_callback and frame_count > 0:
        progress_callback(frame_count, frame_count)

    return frame_count


def extract_single_frame(video_path, output_path, ffmpeg='ffmpeg',
                         seek_time=0, width=FRAME_WIDTH, height=FRAME_HEIGHT,
                         scale_mode=SCALE_STRETCH, aspect_ratio=None):
    """Extract a single frame from a video at a given time offset.

    Args:
        video_path: Path to source video
        output_path: Path to write the output PNG
        ffmpeg: Path to ffmpeg executable
        seek_time: Time in seconds to seek to
        width: Output width in pixels
        height: Output height in pixels
        scale_mode: 'stretch', 'fit', or 'crop'
        aspect_ratio: Optional aspect ratio override (e.g. '16:9')

    Returns:
        True if frame was successfully extracted
    """
    scale_filters = _build_scale_filter(width, height, scale_mode, aspect_ratio)
    cmd = [
        ffmpeg, '-y',
        '-ss', f'{seek_time:.3f}',
        '-i', video_path,
        '-vf', ','.join(scale_filters),
        '-frames:v', '1',
        output_path
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and os.path.isfile(output_path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_extracted_frames(output_dir):
    """Return sorted list of extracted PNG frame paths."""
    return sorted(glob.glob(os.path.join(output_dir, "frame_*.png")))
