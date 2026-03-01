#!/usr/bin/env python3
"""
msu_package.py - MSU-1 file writer for SNES-VideoPlayer

Packages tile/tilemap/palette frame data and audio into a single .msu file
using the S-MSU1 binary format. Designed for single-chapter video playback.

Binary format:
  Header (0x20 bytes):
    +0x00: "S-MSU1" (6 bytes magic)
    +0x06: Title (21 bytes, uppercase ASCII, space-padded)
    +0x1B: Color depth code (5 = 4BPP)
    +0x1C: FPS (1 byte)
    +0x1D: Chapter count (2 bytes LE)
    +0x1F: Padding to 0x20

  Chapter pointer table:
    N x 4-byte LE pointers to chapter data

  Per chapter:
    +0x00: Chapter ID (1 byte)
    +0x01: Frame count (3 bytes LE)
    Then: N x 4-byte LE pointers to frame data

  Per frame:
    +0x00: Frame ID (2 bytes LE)
    +0x02: Length header (4 bytes LE, packed tilemap/tile/palette sizes)
    +0x06: Tilemap data
    +0x06+tm_len: Tile data
    +0x06+tm_len+tl_len: Palette data
"""

import os
import struct
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')

# Constants matching msu1blockwriter.py
HEADER_SIZE = 0x20
CHAPTER_SIZE = 0x4
POINTER_SIZE = 0x4
FRAME_SIZE = 0x6

MSU_TITLE = "SNES VIDEO PLAYER    "  # 21 chars, space-padded
MSU_MAGIC = b"S-MSU1"
BPP = 4
FPS = 24
COLOR_DEPTH_CODE = 5  # 4BPP


class MsuFrame:
    """A single video frame with tile/tilemap/palette data."""

    def __init__(self, tiles, tilemap, palette):
        self.tiles = tiles
        self.tilemap = tilemap
        self.palette = palette
        self.length = len(tiles) + len(tilemap) + len(palette)


class MsuChapter:
    """A chapter containing a sequence of video frames and optional audio."""

    def __init__(self, chapter_id=0, name="chapter"):
        self.id = chapter_id
        self.name = name
        self.frames = []
        self.audio = b''

    def add_frame(self, tiles, tilemap, palette):
        """Add a frame to this chapter."""
        self.frames.append(MsuFrame(tiles, tilemap, palette))

    def set_audio(self, audio_data):
        """Set the MSU-1 PCM audio data (with MSU1 header) for this chapter."""
        self.audio = audio_data


def write_pointer(f, pointer):
    """Write a 4-byte LE pointer."""
    f.write(struct.pack('<I', pointer))


def package_msu(outfile, chapters, title=MSU_TITLE, fps=FPS, bpp=BPP):
    """Package chapters into an MSU-1 .msu file.

    Args:
        outfile: Output file path for the .msu file
        chapters: List of MsuChapter objects
        title: MSU-1 title string (21 chars, space-padded)
        fps: Playback frame rate
        bpp: Bits per pixel (2, 4, or 8)
    """
    if bpp == 2:
        color_depth = 4
    elif bpp == 4:
        color_depth = 5
    elif bpp == 8:
        color_depth = 6
    else:
        raise ValueError(f"Invalid BPP: {bpp}")

    # Only include chapters with frames
    active_chapters = [ch for ch in chapters if ch.frames]
    if not active_chapters:
        raise ValueError("No chapters with frames to package")

    max_chapter_id = max(ch.id for ch in chapters)
    total_chapters = max_chapter_id + 1

    # Build chapter-by-ID lookup
    chapter_by_id = {ch.id: ch for ch in active_chapters}

    with open(outfile, 'wb') as f:
        # --- Header ---
        f.write(MSU_MAGIC)
        f.write(("%-21.21s" % title.upper()).encode('ascii'))
        f.write(bytes((color_depth,)))
        f.write(bytes((fps,)))
        # Chapter count as 2 bytes LE
        f.write(struct.pack('<H', total_chapters))
        # Pad to HEADER_SIZE
        while f.tell() < HEADER_SIZE:
            f.write(b'\x00')

        # --- Compute offsets ---
        scene_pointer_offset = HEADER_SIZE
        scene_offset = scene_pointer_offset + (total_chapters * POINTER_SIZE)

        # Dummy chapter for empty/missing IDs
        dummy_chapter_offset = scene_offset
        dummy_chapter_size = CHAPTER_SIZE

        real_scene_offset = dummy_chapter_offset + dummy_chapter_size
        frame_offset = real_scene_offset
        for ch in active_chapters:
            frame_offset += CHAPTER_SIZE + (len(ch.frames) * POINTER_SIZE)

        # --- Write dense pointer table ---
        f.seek(scene_pointer_offset)
        pointer = real_scene_offset
        for ch_id in range(total_chapters):
            if ch_id in chapter_by_id:
                write_pointer(f, pointer)
                ch = chapter_by_id[ch_id]
                pointer += CHAPTER_SIZE + (len(ch.frames) * POINTER_SIZE)
            else:
                write_pointer(f, dummy_chapter_offset)

        # --- Write dummy chapter entry ---
        f.seek(dummy_chapter_offset)
        f.write(bytes((0xff,)))  # dummy ID
        f.write(bytes((0, 0, 0)))  # 0 frames

        # --- Write chapter data ---
        f.seek(real_scene_offset)
        pointer = frame_offset
        for ch in active_chapters:
            f.write(bytes((ch.id & 0xff,)))
            f.write(bytes((len(ch.frames) & 0xff,)))
            f.write(bytes(((len(ch.frames) & 0xff00) >> 8,)))
            f.write(bytes(((len(ch.frames) & 0xff0000) >> 16,)))
            for frame in ch.frames:
                write_pointer(f, pointer)
                pointer += FRAME_SIZE + frame.length

        # --- Write audio files ---
        for ch in active_chapters:
            if ch.audio:
                audio_filename = "%s-%d.pcm" % (os.path.splitext(outfile)[0], ch.id)
                with open(audio_filename, 'wb') as af:
                    af.write(ch.audio)

        # --- Write frame data ---
        f.seek(frame_offset)
        for ch in active_chapters:
            frame_id = 0
            for frame in ch.frames:
                # Pack length header
                length_header = (
                    ((len(frame.tilemap) // 2) & 0x7ff) |
                    (((len(frame.tiles) >> color_depth) & 0x7ff) << 11) |
                    (((len(frame.palette) // 2) & 0xff) << 22)
                )
                f.write(struct.pack('<H', frame_id))
                f.write(struct.pack('<I', length_header))
                f.write(frame.tilemap)
                f.write(frame.tiles)
                f.write(frame.palette)
                frame_id += 1

    total_frames = sum(len(ch.frames) for ch in active_chapters)
    file_size = os.path.getsize(outfile)
    logging.info(
        'Successfully wrote MSU-1 file %s (%.1f MB), %d chapters, %d frames.',
        outfile, file_size / 1024 / 1024, len(active_chapters), total_frames
    )
    return True


def package_single_chapter(outfile, frame_data_list, audio_data=b'',
                           title=MSU_TITLE, fps=FPS, chapter_id=0):
    """Convenience function: package a single chapter from a list of (tiles, tilemap, palette) tuples.

    Args:
        outfile: Output .msu file path
        frame_data_list: List of (tile_bytes, tilemap_bytes, palette_bytes) tuples
        audio_data: Optional MSU-1 PCM audio bytes (with MSU1 header)
        title: MSU-1 title string
        fps: Playback frame rate
        chapter_id: Chapter ID number
    """
    ch = MsuChapter(chapter_id=chapter_id, name="video")
    for tiles, tilemap, palette in frame_data_list:
        ch.add_frame(tiles, tilemap, palette)

    # Duplicate last frame twice (matches msu1blockwriter.py behavior)
    if ch.frames:
        ch.frames.append(ch.frames[-1])
        ch.frames.append(ch.frames[-1])

    if audio_data:
        ch.set_audio(audio_data)

    return package_msu(outfile, [ch], title=title, fps=fps)
