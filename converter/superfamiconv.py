#!/usr/bin/env python3
"""
superfamiconv.py - SuperFamiconv wrapper for SNES-VideoPlayer

Calls the external SuperFamiconv tool (https://github.com/Optiroc/SuperFamiconv)
as an alternative tile conversion backend. Produces the same binary output formats
as the built-in converter (BGR555 palette, 4BPP tiles, SNES tilemap).

Requires: superfamiconv executable in PATH or specified explicitly.
"""

import os
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)

TILEMAP_TARGET_SIZE = 32 * 20 * 2  # 1280 bytes for 256x160


def find_superfamiconv():
    """Find superfamiconv executable. Returns path or None."""
    path = shutil.which('superfamiconv')
    if path:
        return path

    # Check common locations next to the converter
    converter_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ('superfamiconv', 'superfamiconv.exe'):
        candidate = os.path.join(converter_dir, name)
        if os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(converter_dir, '..', 'tools', name)
        if os.path.isfile(os.path.normpath(candidate)):
            return os.path.normpath(candidate)

    return None


def convert_frame_sfc(png_path, pal_file, tile_file, map_file,
                      num_palettes=2, max_tiles=384, sfc_path=None):
    """Convert a PNG frame to SNES tile data using SuperFamiconv.

    Runs three steps: palette → tiles → map.
    Writes output to pal_file, tile_file, map_file.
    """
    if sfc_path is None:
        sfc_path = find_superfamiconv()
    if sfc_path is None:
        raise FileNotFoundError(
            "superfamiconv not found in PATH.\n"
            "Download from: https://github.com/Optiroc/SuperFamiconv/releases")

    # Step 1: palette
    cmd_pal = [
        sfc_path, 'palette',
        '-M', 'snes',
        '-i', png_path,
        '-d', pal_file,
        '-P', str(num_palettes),
        '-C', '16',
    ]
    _run(cmd_pal, "palette")

    # Step 2: tiles
    cmd_tiles = [
        sfc_path, 'tiles',
        '-M', 'snes',
        '-B', '4',
        '-i', png_path,
        '-p', pal_file,
        '-d', tile_file,
        '--max-tiles', str(max_tiles),
        '--no-discard',
    ]
    _run(cmd_tiles, "tiles")

    # Step 3: map
    cmd_map = [
        sfc_path, 'map',
        '-M', 'snes',
        '-B', '4',
        '-i', png_path,
        '-p', pal_file,
        '-t', tile_file,
        '-d', map_file,
    ]
    _run(cmd_map, "map")

    # Pad tilemap to expected size
    _pad_tilemap(map_file)


def _run(cmd, step_name):
    """Run a superfamiconv command and raise on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else 'unknown error'
            raise RuntimeError(f"superfamiconv {step_name} failed: {stderr}")
    except FileNotFoundError:
        raise FileNotFoundError(
            "superfamiconv executable not found.\n"
            "Download from: https://github.com/Optiroc/SuperFamiconv/releases")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"superfamiconv {step_name} timed out")


def _pad_tilemap(map_file):
    """Pad tilemap to target size for SNES compatibility."""
    with open(map_file, 'rb') as f:
        data = f.read()
    if len(data) < TILEMAP_TARGET_SIZE:
        with open(map_file, 'wb') as f:
            f.write(data)
            f.write(b'\x00' * (TILEMAP_TARGET_SIZE - len(data)))
