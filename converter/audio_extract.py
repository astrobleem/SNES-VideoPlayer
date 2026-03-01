#!/usr/bin/env python3
"""
audio_extract.py - ffmpeg audio extraction to MSU-1 PCM format for SNES-VideoPlayer

Extracts audio from video files and converts to MSU-1 PCM format:
  - 44100 Hz sample rate
  - Stereo (2 channels)
  - 16-bit signed little-endian samples
  - "MSU1" magic header + 4-byte loop point (LE)
"""

import os
import struct
import subprocess
import shutil

# MSU-1 audio constants
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
MSU1_MAGIC = b"MSU1"
MSU1_AUDIO_HEADER = MSU1_MAGIC + struct.pack('<I', 0)  # "MSU1" + loop_point=0 (no loop)


def find_ffmpeg():
    """Find ffmpeg executable."""
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        return ffmpeg
    return 'ffmpeg'


def extract_audio(input_path, output_pcm_path, ffmpeg='ffmpeg',
                  start_time=None, duration=None, loop_point=0,
                  sample_rate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS):
    """Extract audio from a video/audio file and convert to MSU-1 PCM format.

    Args:
        input_path: Path to source video or audio file
        output_pcm_path: Path to output .pcm file
        ffmpeg: Path to ffmpeg executable
        start_time: Start time in seconds (None = from beginning)
        duration: Duration in seconds (None = entire file)
        loop_point: MSU-1 loop point in samples (0 = no loop)
        sample_rate: Output sample rate (default 44100)
        channels: Output channel count (default 2 = stereo)

    Returns:
        True on success, False on error
    """
    # Build ffmpeg command for raw PCM extraction
    raw_path = output_pcm_path + '.raw'

    cmd = [ffmpeg, '-y']

    # Input seeking (coarse + precise for large files)
    if start_time is not None and start_time > 0:
        pre_seek = max(0, start_time - 5)
        precise_offset = start_time - pre_seek
        cmd.extend(['-ss', f'{pre_seek:.3f}'])
        cmd.extend(['-i', input_path])
        cmd.extend(['-ss', f'{precise_offset:.3f}'])
    else:
        cmd.extend(['-i', input_path])

    if duration is not None:
        cmd.extend(['-t', f'{duration:.3f}'])

    cmd.extend([
        '-vn',                        # No video
        '-ar', str(sample_rate),      # Sample rate
        '-ac', str(channels),         # Channels
        '-f', 's16le',                # Raw signed 16-bit LE
        '-acodec', 'pcm_s16le',       # PCM codec
        raw_path
    ])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            error_msg = result.stderr[-300:] if result.stderr else 'unknown'
            raise RuntimeError(f"ffmpeg audio extraction failed: {error_msg}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg audio extraction timed out (>5 min)")

    # Wrap raw PCM with MSU-1 header
    try:
        header = MSU1_MAGIC + struct.pack('<I', loop_point)

        with open(raw_path, 'rb') as f:
            raw_pcm = f.read()

        with open(output_pcm_path, 'wb') as f:
            f.write(header)
            f.write(raw_pcm)

        # Clean up raw file
        os.remove(raw_path)
        return True

    except IOError as e:
        raise RuntimeError(f"Failed to write PCM file: {e}")


def extract_audio_from_video(video_path, output_pcm_path, ffmpeg='ffmpeg',
                             duration=None, loop_point=0):
    """Convenience: extract full audio from a video file to MSU-1 PCM.

    Args:
        video_path: Path to source video file
        output_pcm_path: Path to output .pcm file
        ffmpeg: Path to ffmpeg executable
        duration: Max duration in seconds (None = full video)
        loop_point: MSU-1 loop point in samples (0 = no loop)

    Returns:
        True on success
    """
    return extract_audio(
        video_path, output_pcm_path,
        ffmpeg=ffmpeg, duration=duration, loop_point=loop_point
    )


def get_audio_duration_samples(pcm_path):
    """Read an MSU-1 PCM file and return duration in samples.

    MSU-1 PCM format: 8-byte header + raw 16-bit stereo samples.
    Each sample frame = 4 bytes (2 channels x 2 bytes).
    """
    if not os.path.isfile(pcm_path):
        return 0
    file_size = os.path.getsize(pcm_path)
    if file_size <= 8:
        return 0
    # Subtract 8-byte header, divide by 4 (stereo 16-bit = 4 bytes per sample frame)
    return (file_size - 8) // 4


def get_audio_duration_seconds(pcm_path, sample_rate=AUDIO_SAMPLE_RATE):
    """Read an MSU-1 PCM file and return duration in seconds."""
    samples = get_audio_duration_samples(pcm_path)
    if samples == 0:
        return 0.0
    return samples / sample_rate
