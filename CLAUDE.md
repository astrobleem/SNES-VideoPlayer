# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SNES-VideoPlayer converts standard video files into packages playable on real Super Nintendo hardware via MSU-1. Two-part system: a pre-built 65816 SNES ROM engine + a Python converter pipeline.

## Common Commands

### Run the converter (GUI)
```bash
cd converter
uv run videoplayer_converter.py
```

### Run the converter (CLI)
```bash
cd converter
uv run videoplayer_converter.py --cli -i video.mp4 -o SNESVideoPlayer.msu --workers 8

# Preserve aspect ratio for portrait/vertical video (black pillarbox bars)
uv run videoplayer_converter.py --cli -i short.mp4 -o SNESVideoPlayer.msu --scale-mode fit

# Crop to fill (no bars, center-crops overflow)
uv run videoplayer_converter.py --cli -i video.mp4 -o SNESVideoPlayer.msu --scale-mode crop

# Override aspect ratio
uv run videoplayer_converter.py --cli -i video.mp4 -o SNESVideoPlayer.msu --scale-mode fit --aspect-ratio 4:3

# Grayscale with shared palette
uv run videoplayer_converter.py --cli -i video.mp4 -o SNESVideoPlayer.msu --grayscale --shared-palette

# With per-segment quality settings
uv run videoplayer_converter.py --cli -i video.mp4 -o SNESVideoPlayer.msu --segments-file segments.json
```

### Install Python dependencies
```bash
cd converter
uv sync
```

### Build the ROM (only if modifying 65816 source)
```bash
cd rom
wsl -e bash -c "make clean && make"
# Output: build/SNESVideoPlayer.sfc → distribution/SNESVideoPlayer.sfc
```
Requires WSL on Windows. Uses WLA-DX 9.5 assembler bundled in `rom/tools/`.

## Architecture

### Converter Pipeline (Python, `converter/`)

Entry point: `videoplayer_converter.py` — dispatches to GUI (no args) or CLI (`--cli`).

Pipeline phases orchestrated by `pipeline.py`:
1. **frame_extract.py** — ffmpeg extracts 256x160 PNG frames at 24fps with configurable scale mode (stretch/fit/crop) and optional aspect ratio override
2. **audio_extract.py** — ffmpeg extracts audio → 44100Hz stereo 16-bit LE PCM with MSU-1 header
3. **tile_convert.py** — core algorithm (K-means clustering, sub-palette building, Floyd-Steinberg dithering, 4BPP encoding, greedy tile merging to ≤384 unique tiles). Runs in parallel via ThreadPoolExecutor with BLAS pinned to single-thread per worker.
4. **msu_package.py** — writes binary `.msu` file (header, chapter pointers, frame data)

Supporting modules:
- `segments.py` — `Segment` dataclass + `SegmentList` manager for per-segment quality settings (split/delete/lookup/JSON serialization). Each segment has its own dither method, engine, palette count, and max tiles. Pipeline does per-frame lookup via `settings_for_frame()`.
- `preview.py` — reconstructs PIL Image from tile data for GUI preview
- `gui.py` — tkinter interface with file picker, preview panels, segment strip, progress callbacks

### ROM Engine (65816 Assembly, `rom/src/`)

Custom OOP framework with 48 concurrent object slots (`core/oop.65816`). Uses coroutine-style scripts via `SavePC`/`DIE` macros for frame-based control flow.

Key flow: `boot.65816` → `main.script` → `videoplayer.script`

Important subsystems:
- `object/msu1/` — MSU-1 hardware controller, video frame streaming and DMA uploads
- `object/background/` — double-buffered BG layer (`framebuffer`) and 8x8 text layer
- `object/brightness/` — screen fade controller (singleton)
- `core/` — hardware init, DMA, NMI/IRQ handlers, input polling, memory allocation
- `definition/` — SNES and MSU-1 register definitions, 65816 opcode definitions

## Key Technical Constraints

- **Display**: Mode 1, 4BPP, 256x160 pixels (32x20 tiles), letterboxed with 32px borders
- **VRAM limit**: 384 unique tiles per frame ($3000 bytes at 4BPP)
- **Palettes**: 2 sub-palettes × 16 colors = 32 colors max per frame (color 0 = transparent)
- **Palette format**: BGR555 (5 bits/channel, 2 bytes/color)
- **Audio**: 44100Hz stereo 16-bit signed little-endian PCM
- **Frame rate**: 24fps
- **MSU-1 title**: exactly 21 characters, space-padded, must match between ROM and .msu file

## Scale Mode & Aspect Ratio

The converter supports three video scaling modes controlled by `scale_mode` (CLI: `--scale-mode`, GUI: Scale Mode combo):

- **`stretch`** (default): `scale=W:H` — fills 256x160 exactly, may distort non-16:10 sources
- **`fit`**: `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:...:color=black` — preserves aspect ratio, pads with black
- **`crop`**: `scale=W:H:force_original_aspect_ratio=increase,crop=W:H` — preserves aspect ratio, center-crops overflow

Optional `aspect_ratio` override (CLI: `--aspect-ratio`, e.g. `"16:9"`) pre-scales pixels to the desired AR before fit/crop using `scale=trunc(ih*ar_w/ar_h/2)*2:ih,setsar=1`. Ignored in stretch mode since it forces exact dimensions. GUI auto-switches from Stretch to Fit when an AR is entered.

Implementation: `_build_scale_filter()` in `frame_extract.py` returns the filter list. Both `extract_frames()` and `extract_single_frame()` accept `scale_mode` and `aspect_ratio` params. These are global pipeline settings (not per-segment) threaded through `ConversionPipeline` and `run_pipeline()`.

## Per-Segment Quality

The converter supports per-segment quality settings, allowing different parts of a video to use different dithering, engine, palette count, and max tile settings. Segments are managed by `SegmentList` in `converter/segments.py`.

- **GUI**: Segment strip canvas between scrubber and progress bar. Split/Delete buttons. Selecting a segment loads its settings into the quality widgets; changing widgets writes back to the selected segment. Scrubbing auto-selects the segment at that time.
- **CLI**: `--segments-file segments.json` loads a JSON array of segment objects. Overrides global `--dither`/`--engine`/`--max-tiles`/`--num-palettes`.
- **Pipeline**: `ConversionPipeline` accepts optional `segments` parameter. In `_convert_tiles()`, each frame computes its video time as `start_offset + frame_index / fps` and looks up the segment for per-frame settings.

## External Dependencies

- **uv**: Python package manager. Auto-installed by `SNESVideoPlayer.bat` if missing. Manages venv and dependencies via `converter/pyproject.toml`.
- **ffmpeg**: must be in PATH (or specified via `--ffmpeg`). Used for frame and audio extraction.
- **Python 3.8+** with numpy, Pillow, and yt-dlp (managed by uv)
- **WLA-DX 9.5**: bundled in `rom/tools/`, only needed for ROM builds
