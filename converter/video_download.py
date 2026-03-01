#!/usr/bin/env python3
"""
video_download.py - Download videos from URLs using yt-dlp

Provides a helper to download a video from a YouTube (or compatible) URL
to a local file for use in the SNES-VideoPlayer converter pipeline.

Requires: pip install yt-dlp
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
    """Download a video from a URL using yt-dlp.

    Args:
        url: Video URL (YouTube, etc.)
        output_dir: Directory to save to (default: temp dir)
        on_progress: Optional callable(bytes_downloaded, total_bytes, percent)

    Returns:
        Path to the downloaded video file.

    Raises:
        ImportError: If yt-dlp is not installed.
        Exception: On download failure.
    """
    try:
        import yt_dlp
    except ImportError:
        raise ImportError(
            "yt-dlp is required for URL downloads.\n"
            "Install it with: pip install yt-dlp"
        )

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix='snes_vp_dl_')

    # Track the final filename via a hook
    downloaded_path = {}

    def _progress_hook(d):
        if d['status'] == 'downloading' and on_progress:
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                pct = downloaded / total * 100
                on_progress(downloaded, total, pct)
        elif d['status'] == 'finished':
            downloaded_path['path'] = d.get('filename', '')

    logger.info("Connecting to: %s", url)

    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        'progress_hooks': [_progress_hook],
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get('title', 'unknown')
        logger.info("Video title: %s", title)

    # Determine the output path
    path = downloaded_path.get('path', '')
    if not path or not os.path.isfile(path):
        # Fallback: find the file yt-dlp wrote
        path = ydl.prepare_filename(info)

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
