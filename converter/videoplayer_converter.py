#!/usr/bin/env python3
"""
videoplayer_converter.py - CLI + GUI entry point for SNES-VideoPlayer converter

Converts standard video files to MSU-1 format for playback on SNES hardware
via the SNES-VideoPlayer ROM.

Usage:
  # Launch GUI
  python videoplayer_converter.py

  # CLI mode
  python videoplayer_converter.py --cli -i video.mp4 -o output.msu

  # CLI with options
  python videoplayer_converter.py --cli -i video.mp4 -o output.msu \\
      --workers 8 --deinterlace --start 10.5 --duration 30 \\
      --title "MY VIDEO" --fps 24
"""

import os
import sys
import argparse

# Ensure converter package is importable
CONVERTER_DIR = os.path.dirname(os.path.abspath(__file__))
if CONVERTER_DIR not in sys.path:
    sys.path.insert(0, CONVERTER_DIR)


def run_cli(args):
    """Run conversion in CLI mode."""
    from pipeline import run_pipeline
    from msu_package import MSU_TITLE

    # Download from URL if provided
    if args.url:
        from video_download import download_video

        def _on_dl_progress(downloaded, total, pct):
            mb_done = downloaded / 1024 / 1024
            mb_total = total / 1024 / 1024
            bar = '#' * int(pct // 2) + '-' * (50 - int(pct // 2))
            print(f"\r[{bar}] {pct:5.1f}% {mb_done:.1f}/{mb_total:.1f} MB",
                  end='', flush=True)

        print(f"Downloading: {args.url}")
        try:
            dl_path = download_video(args.url, on_progress=_on_dl_progress)
            print()  # newline after progress bar
            print(f"Downloaded: {dl_path}")
            args.input = dl_path
        except Exception as e:
            print(f"\nDownload failed: {e}")
            sys.exit(1)

    if not args.input:
        print("Error: --input (-i) or --url is required in CLI mode")
        sys.exit(1)
    if not args.output:
        print("Error: --output (-o) is required in CLI mode")
        sys.exit(1)
    if not os.path.isfile(args.input):
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    # Load per-segment quality settings if provided
    segments = None
    if args.segments_file:
        from segments import SegmentList
        try:
            segments = SegmentList.from_json_file(args.segments_file)
            print(f"  Segments: {len(segments)} loaded from {args.segments_file}")
        except Exception as e:
            print(f"Error loading segments file: {e}")
            sys.exit(1)
    elif args.grayscale or args.shared_palette:
        # Create a default segment list with the flags so the pipeline picks them up
        from segments import SegmentList
        from frame_extract import get_video_info, find_ffmpeg
        ffmpeg = args.ffmpeg or find_ffmpeg()
        info = get_video_info(args.input, ffmpeg)
        vid_duration = info['duration'] if info else 60.0
        segments = SegmentList.create_default(
            vid_duration,
            dither_method=args.dither,
            engine=args.engine,
            num_palettes=args.num_palettes,
            max_tiles=max(1, min(384, args.max_tiles)),
            grayscale=args.grayscale,
            shared_palette=args.shared_palette,
        )

    title = args.title or MSU_TITLE.strip()
    title = "%-21.21s" % title

    def on_progress(phase_progress, overall_progress, message):
        pct = int(overall_progress * 100)
        bar = '#' * (pct // 2) + '-' * (50 - pct // 2)
        print(f"\r[{bar}] {pct:3d}% {message[:60]:<60}", end='', flush=True)

    def on_complete(output_path):
        print()
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"Done! Output: {output_path} ({size_mb:.1f} MB)")

    def on_error(message):
        print()
        print(f"Error: {message}")

    # Clamp max-tiles to valid range
    max_tiles = max(1, min(384, args.max_tiles))

    print("SNES Video Player Converter")
    print(f"  Input:  {args.input}")
    print(f"  Output: {args.output}")
    print(f"  Workers: {args.workers}")
    print(f"  FPS: {args.fps}")
    print(f"  Dither: {args.dither}")
    print(f"  Max tiles: {max_tiles}")
    print(f"  Palettes: {args.num_palettes}")
    print(f"  Engine: {args.engine}")
    if args.grayscale:
        print("  Grayscale: enabled")
    if args.shared_palette:
        print("  Shared palette: enabled")
    if args.deinterlace:
        print("  Deinterlace: enabled")
    if args.start is not None:
        print(f"  Start: {args.start}s")
    if args.duration is not None:
        print(f"  Duration: {args.duration}s")
    print()

    success = run_pipeline(
        args.input, args.output,
        workers=args.workers,
        ffmpeg=args.ffmpeg,
        title=title,
        fps=args.fps,
        deinterlace=args.deinterlace,
        start_time=args.start,
        duration=args.duration,
        num_palettes=args.num_palettes,
        max_tiles=max_tiles,
        dither_method=args.dither,
        engine=args.engine,
        segments=segments,
        progress_callback=on_progress,
        complete_callback=on_complete,
        error_callback=on_error,
    )

    sys.exit(0 if success else 1)


def run_gui_mode():
    """Launch the GUI."""
    from gui import run_gui
    run_gui()


def main():
    parser = argparse.ArgumentParser(
        description='SNES Video Player Converter - Convert video to MSU-1 format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          Launch GUI
  %(prog)s --cli -i video.mp4 -o output.msu         CLI conversion
  %(prog)s --cli -i video.mp4 -o output.msu -w 8    CLI with 8 workers
  %(prog)s --cli -i video.mp4 -o out.msu --start 10 --duration 30
  %(prog)s --cli --url https://youtube.com/watch?v=ID -o out.msu
"""
    )

    parser.add_argument('--cli', action='store_true',
                        help='Run in CLI mode (no GUI)')
    parser.add_argument('-i', '--input', type=str,
                        help='Input video file path')
    parser.add_argument('--url', type=str, default=None,
                        help='Download video from URL (requires yt-dlp)')
    parser.add_argument('-o', '--output', type=str,
                        help='Output .msu file path')
    parser.add_argument('-w', '--workers', type=int, default=4,
                        help='Number of parallel conversion workers (default: 4)')
    parser.add_argument('--ffmpeg', type=str, default=None,
                        help='Path to ffmpeg executable (default: auto-detect)')
    parser.add_argument('--title', type=str, default=None,
                        help='MSU-1 title string (max 21 chars, default: "SNES VIDEO PLAYER")')
    parser.add_argument('--fps', type=int, default=24,
                        help='Playback frame rate (default: 24)')
    parser.add_argument('--deinterlace', action='store_true',
                        help='Apply yadif deinterlace filter')
    parser.add_argument('--start', type=float, default=None,
                        help='Start time in seconds')
    parser.add_argument('--duration', type=float, default=None,
                        help='Duration in seconds')
    parser.add_argument('--dither', type=str, default='floyd-steinberg',
                        choices=['none', 'floyd-steinberg', 'ordered'],
                        help='Dithering method (default: floyd-steinberg)')
    parser.add_argument('--max-tiles', type=int, default=384,
                        help='Maximum unique tiles per frame, 1-384 (default: 384)')
    parser.add_argument('--num-palettes', type=int, default=2,
                        choices=[1, 2],
                        help='Number of sub-palettes per frame (default: 2)')
    parser.add_argument('--engine', type=str, default='builtin',
                        choices=['builtin', 'superfamiconv'],
                        help='Tile conversion engine (default: builtin)')
    parser.add_argument('--segments-file', type=str, default=None,
                        help='JSON file with per-segment quality settings '
                             '(overrides --dither/--max-tiles/--num-palettes/--engine)')
    parser.add_argument('--grayscale', action='store_true',
                        help='Convert frames to grayscale before processing')
    parser.add_argument('--shared-palette', action='store_true',
                        help='Compute one shared palette across the segment '
                             'to reduce frame-to-frame dither swimming')

    args = parser.parse_args()

    if args.cli:
        run_cli(args)
    else:
        # If input/output provided without --cli, still use CLI mode
        if (args.input or args.url) and args.output:
            run_cli(args)
        else:
            run_gui_mode()


if __name__ == '__main__':
    main()
