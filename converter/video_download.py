#!/usr/bin/env python3
"""
video_download.py - Download videos from URLs using pytube2

Provides a helper to download a video from a YouTube (or compatible) URL
to a local file for use in the SNES-VideoPlayer converter pipeline.

Requires: pip install pytube2
"""

import os
import tempfile
import logging

logger = logging.getLogger(__name__)


def is_url(text):
    """Check if text looks like a URL."""
    text = text.strip()
    return text.startswith(('http://', 'https://', 'www.'))


def download_video(url, output_dir=None, on_progress=None):
    """Download a video from a URL using pytube2.

    Args:
        url: Video URL (YouTube, etc.)
        output_dir: Directory to save to (default: temp dir)
        on_progress: Optional callable(bytes_downloaded, total_bytes, percent)

    Returns:
        Path to the downloaded video file.

    Raises:
        ImportError: If pytube2 is not installed.
        Exception: On download failure.
    """
    try:
        from pytube import YouTube
    except ImportError:
        raise ImportError(
            "pytube2 is required for URL downloads.\n"
            "Install it with: pip install pytube2"
        )

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='snes_vp_dl_')

    def _pytube_progress(stream, chunk, bytes_remaining):
        if on_progress and stream.filesize:
            downloaded = stream.filesize - bytes_remaining
            pct = downloaded / stream.filesize * 100
            on_progress(downloaded, stream.filesize, pct)

    logger.info("Connecting to: %s", url)
    yt = YouTube(url, on_progress_callback=_pytube_progress)

    title = yt.title
    logger.info("Video title: %s", title)

    # Prefer progressive MP4 (has audio+video in one file) at highest resolution
    stream = (yt.streams
              .filter(progressive=True, file_extension='mp4')
              .order_by('resolution')
              .desc()
              .first())

    if stream is None:
        # Fall back to any MP4 stream
        stream = (yt.streams
                  .filter(file_extension='mp4')
                  .order_by('resolution')
                  .desc()
                  .first())

    if stream is None:
        # Last resort: any available stream
        stream = yt.streams.first()

    if stream is None:
        raise RuntimeError("No downloadable streams found for this URL")

    logger.info("Downloading: %s (%s, %s)",
                stream.resolution or 'unknown res',
                stream.mime_type,
                _fmt_size(stream.filesize) if stream.filesize else '? MB')

    path = stream.download(output_path=output_dir)
    logger.info("Downloaded: %s", path)
    return path


def _fmt_size(nbytes):
    """Format byte count as human-readable string."""
    if nbytes < 1024:
        return f"{nbytes} B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes / 1024 / 1024:.1f} MB"
