#!/usr/bin/env python3
"""
frame_extract.py - ffmpeg frame extraction wrapper for SNES-VideoPlayer

Extracts video frames from any video file as 256x160 PNG images at
24000/1001 fps (~23.976 fps) suitable for MSU-1 playback on SNES.

Supports optional deinterlacing, custom time ranges, progress callbacks,
and three video scaling modes (stretch/fit/crop) with optional aspect
ratio override for handling non-standard source aspect ratios.
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


# --- Scale mode constants ---
# These control how source video is fitted to the target resolution (e.g. 256x160).
# Important for non-standard aspect ratios like portrait/vertical video (YouTube Shorts).
SCALE_STRETCH = 'stretch'  # Force-fill target dimensions; distorts if AR doesn't match
SCALE_FIT = 'fit'          # Fit inside target, preserving AR; pad remainder with black
SCALE_CROP = 'crop'        # Cover target, preserving AR; center-crop the overflow
SCALE_MODES = [SCALE_STRETCH, SCALE_FIT, SCALE_CROP]


def _build_scale_filter(width, height, scale_mode, aspect_ratio=None):
    """Build the ffmpeg video filter chain for scaling to the target resolution.

    Returns a list of ffmpeg filter strings (to be comma-joined into a -vf chain).
    The caller appends these after any deinterlace/fps/trim filters.

    Scale modes produce different ffmpeg filter chains:
      stretch: scale=W:H
        Forces exact dimensions. Simple but distorts non-matching aspect ratios.

      fit:    scale=W:H:force_original_aspect_ratio=decrease, pad=W:H:centered:black
        Scales the largest dimension to fit, then pads the shorter dimension
        with black bars (pillarbox for tall video, letterbox for ultra-wide).

      crop:   scale=W:H:force_original_aspect_ratio=increase, crop=W:H
        Scales the smallest dimension to fill, then center-crops the overflow.
        No black bars, but some content is lost at the edges.

    Aspect ratio override (e.g. '16:9'):
      Ignored for stretch mode (exact dimensions override any AR).
      For fit/crop, we pre-scale the source pixels to match the desired AR
      before applying the fit/crop logic. This is necessary because ffmpeg's
      force_original_aspect_ratio works on pixel dimensions, not metadata —
      setdar alone has no effect. Instead we reshape via scale + setsar=1.

    Args:
        width: Target width in pixels (e.g. 256 for SNES)
        height: Target height in pixels (e.g. 160 for SNES)
        scale_mode: One of SCALE_STRETCH, SCALE_FIT, SCALE_CROP
        aspect_ratio: Optional AR override string like '16:9' or '4:3'.
                      Accepts colon or slash separators ('16:9' or '16/9').

    Returns:
        List of ffmpeg filter strings to join with ','
    """
    filters = []

    # --- Aspect ratio override (pre-scale) ---
    # Only meaningful for fit/crop — stretch forces exact dims regardless of AR.
    # We reshape the source pixels so their width/height ratio matches the
    # desired AR, then set SAR=1 (square pixels) so the subsequent
    # force_original_aspect_ratio scale sees the correct proportions.
    # trunc(.../2)*2 ensures even dimensions (required by many ffmpeg codecs).
    if aspect_ratio and scale_mode != SCALE_STRETCH:
        ar = aspect_ratio.replace('/', ':')
        ar_w, ar_h = ar.split(':')
        filters.append(f'scale=trunc(ih*{ar_w}/{ar_h}/2)*2:ih')
        filters.append('setsar=1')

    # --- Main scale filter ---
    if scale_mode == SCALE_FIT:
        # force_original_aspect_ratio=decrease: scale to fit INSIDE WxH
        # (one dimension matches, the other is smaller)
        filters.append(f'scale={width}:{height}:force_original_aspect_ratio=decrease')
        # pad: center the smaller image on a WxH black canvas
        # (ow-iw)/2 and (oh-ih)/2 compute the centering offsets
        filters.append(f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black')
    elif scale_mode == SCALE_CROP:
        # force_original_aspect_ratio=increase: scale to COVER WxH
        # (one dimension matches, the other overflows)
        filters.append(f'scale={width}:{height}:force_original_aspect_ratio=increase')
        # crop: center-crop to exact WxH, discarding overflow
        filters.append(f'crop={width}:{height}')
    else:
        # stretch (default): force exact dimensions, ignoring source AR
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

    # Append scale/fit/crop filters (replaces the old bare 'scale=W:H')
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
