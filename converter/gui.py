#!/usr/bin/env python3
"""
gui.py - tkinter GUI for SNES-VideoPlayer converter

Provides a graphical interface for video-to-MSU-1 conversion with:
  - File picker for input video and output .msu file
  - Preview panels (source frame + SNES reconstruction)
  - Progress bar with phase status
  - Scrollable log output
  - Settings for workers, deinterlace, start/duration
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import logging
from io import StringIO

# Ensure proper imports from the converter package
CONVERTER_DIR = os.path.dirname(os.path.abspath(__file__))
if CONVERTER_DIR not in sys.path:
    sys.path.insert(0, CONVERTER_DIR)

from pipeline import ConversionPipeline, PipelineProgress
from msu_package import MSU_TITLE
from preview import reconstruct_to_pil
from frame_extract import find_ffmpeg, get_video_info, extract_single_frame
from tile_convert import (DITHER_NONE, DITHER_FLOYD_STEINBERG, DITHER_ORDERED,
                          DEFAULT_DITHER, MAX_TILES, MAX_PALETTES,
                          ENGINE_BUILTIN, ENGINE_SUPERFAMICONV, DEFAULT_ENGINE)
from segments import SegmentList, Segment


class Tooltip:
    """Simple hover tooltip for tkinter widgets."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        widget.bind('<Enter>', self._show)
        widget.bind('<Leave>', self._hide)

    def _show(self, event=None):
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                         font=("Segoe UI", 9))
        label.pack(ipadx=4, ipady=2)

    def _hide(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


class TextHandler(logging.Handler):
    """Logging handler that writes to a tkinter Text widget."""

    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record) + '\n'
        self.text_widget.after(0, self._append, msg)

    def _append(self, msg):
        self.text_widget.configure(state='normal')
        self.text_widget.insert(tk.END, msg)
        self.text_widget.see(tk.END)
        self.text_widget.configure(state='disabled')


class ConverterGUI:
    """Main GUI application for SNES Video Player converter."""

    def __init__(self, root):
        self.root = root
        self.root.title("SNES Video Player Converter")
        self.root.geometry("900x700")
        self.root.minsize(750, 550)

        self.pipeline = None
        self.conversion_thread = None
        self.is_converting = False
        self._preview_path = None       # cached path for re-conversion on settings change
        self._preview_timer = None      # debounce timer for quality setting changes
        self._video_path = None         # loaded video file for scrubber seeking
        self._video_duration = 0.0      # video duration in seconds
        self._scrub_timer = None        # debounce timer for scrubber
        self._clip_frames = []          # list of (src_pil, snes_pil) for animation
        self._clip_playing = False      # animation loop active
        self._clip_frame_idx = 0        # current animation frame
        self._clip_timer = None         # after() id for animation tick
        self._clip_cancel = None        # threading.Event to signal cancel

        # Per-segment quality settings
        self._segment_list = None           # SegmentList, created when video loads
        self._selected_segment_idx = 0      # which segment is selected
        self._updating_controls = False     # suppresses feedback loop

        self._build_ui()
        self._setup_logging()

    def _build_ui(self):
        """Build the GUI layout."""
        # Main container with padding
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- File Selection ---
        file_frame = ttk.LabelFrame(main_frame, text="Files", padding=5)
        file_frame.pack(fill=tk.X, pady=(0, 5))

        # Input video
        ttk.Label(file_frame, text="Input Video:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.input_var = tk.StringVar()
        input_entry = ttk.Entry(file_frame, textvariable=self.input_var, width=60)
        input_entry.grid(row=0, column=1, sticky=tk.EW, padx=5)
        ttk.Button(file_frame, text="Browse...", command=self._browse_input).grid(
            row=0, column=2, padx=5)

        # Video URL
        url_label = ttk.Label(file_frame, text="Video URL:")
        url_label.grid(row=1, column=0, sticky=tk.W, padx=5)
        Tooltip(url_label, "Paste a YouTube or video URL to download.\n"
                           "Requires yt-dlp: pip install yt-dlp")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(file_frame, textvariable=self.url_var, width=60)
        url_entry.grid(row=1, column=1, sticky=tk.EW, padx=5)
        self.download_btn = ttk.Button(file_frame, text="Download",
                                       command=self._download_url)
        self.download_btn.grid(row=1, column=2, padx=5)

        # Output MSU
        ttk.Label(file_frame, text="Output MSU:").grid(row=2, column=0, sticky=tk.W, padx=5)
        self.output_var = tk.StringVar()
        output_entry = ttk.Entry(file_frame, textvariable=self.output_var, width=60)
        output_entry.grid(row=2, column=1, sticky=tk.EW, padx=5)
        ttk.Button(file_frame, text="Browse...", command=self._browse_output).grid(
            row=2, column=2, padx=5)

        file_frame.columnconfigure(1, weight=1)

        # --- Settings ---
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding=5)
        settings_frame.pack(fill=tk.X, pady=(0, 5))

        # Row 1
        ttk.Label(settings_frame, text="Workers:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.workers_var = tk.IntVar(value=4)
        workers_spin = ttk.Spinbox(settings_frame, from_=1, to=16,
                                   textvariable=self.workers_var, width=5)
        workers_spin.grid(row=0, column=1, sticky=tk.W, padx=5)

        self.deinterlace_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings_frame, text="Deinterlace",
                        variable=self.deinterlace_var).grid(row=0, column=2, padx=15)

        # Row 2: Time range
        ttk.Label(settings_frame, text="Start (sec):").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.start_var = tk.StringVar(value="")
        ttk.Entry(settings_frame, textvariable=self.start_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(settings_frame, text="Duration (sec):").grid(row=1, column=2, sticky=tk.W, padx=5)
        self.duration_var = tk.StringVar(value="")
        ttk.Entry(settings_frame, textvariable=self.duration_var, width=8).grid(
            row=1, column=3, sticky=tk.W, padx=5)

        ttk.Label(settings_frame, text="FPS:").grid(row=1, column=4, sticky=tk.W, padx=5)
        self.fps_var = tk.IntVar(value=24)
        ttk.Spinbox(settings_frame, from_=1, to=60,
                     textvariable=self.fps_var, width=5).grid(
            row=1, column=5, sticky=tk.W, padx=5)

        # Row 3: Quality controls
        dither_label = ttk.Label(settings_frame, text="Dithering:")
        dither_label.grid(row=2, column=0, sticky=tk.W, padx=5)
        Tooltip(dither_label,
                "How color quantization error is handled.\n"
                "None: direct nearest-color, flat banding on gradients.\n"
                "Floyd-Steinberg: error diffusion, smooth gradients (default).\n"
                "Ordered (Bayer): threshold stipple pattern, retro look.")
        self.dither_var = tk.StringVar(value="Floyd-Steinberg")
        dither_combo = ttk.Combobox(settings_frame, textvariable=self.dither_var,
                                    values=["None", "Floyd-Steinberg", "Ordered (Bayer)"],
                                    state='readonly', width=16)
        dither_combo.grid(row=2, column=1, sticky=tk.W, padx=5)
        dither_combo.bind('<<ComboboxSelected>>', lambda e: self._on_quality_changed())

        tiles_label = ttk.Label(settings_frame, text="Max Tiles:")
        tiles_label.grid(row=2, column=2, sticky=tk.W, padx=5)
        Tooltip(tiles_label,
                "Maximum unique 8x8 tiles per frame (VRAM limit).\n"
                "384 = full quality. Lower values force more tile\n"
                "merging, increasing blockiness but saving VRAM.")
        self.max_tiles_var = tk.IntVar(value=MAX_TILES)
        max_tiles_spin = ttk.Spinbox(settings_frame, from_=1, to=384,
                                     textvariable=self.max_tiles_var, width=5)
        max_tiles_spin.grid(row=2, column=3, sticky=tk.W, padx=5)
        self.max_tiles_var.trace_add('write', lambda *_: self._on_quality_changed())

        palettes_label = ttk.Label(settings_frame, text="Palettes:")
        palettes_label.grid(row=2, column=4, sticky=tk.W, padx=5)
        Tooltip(palettes_label,
                "Number of 16-color sub-palettes per frame.\n"
                "2 = 32 colors max (default). 1 = 16 colors,\n"
                "more color banding but simpler palette.")
        self.palettes_var = tk.StringVar(value="2")
        palettes_combo = ttk.Combobox(settings_frame, textvariable=self.palettes_var,
                                      values=["1", "2"],
                                      state='readonly', width=5)
        palettes_combo.grid(row=2, column=5, sticky=tk.W, padx=5)
        palettes_combo.bind('<<ComboboxSelected>>', lambda e: self._on_quality_changed())

        engine_label = ttk.Label(settings_frame, text="Engine:")
        engine_label.grid(row=3, column=0, sticky=tk.W, padx=5)
        Tooltip(engine_label,
                "Tile conversion backend.\n"
                "Built-in: pure Python/NumPy (default).\n"
                "SuperFamiconv: external tool by Optiroc.\n"
                "  Requires superfamiconv in PATH.")
        self.engine_var = tk.StringVar(value="Built-in")
        engine_combo = ttk.Combobox(settings_frame, textvariable=self.engine_var,
                                    values=["Built-in", "SuperFamiconv"],
                                    state='readonly', width=16)
        engine_combo.grid(row=3, column=1, sticky=tk.W, padx=5)
        engine_combo.bind('<<ComboboxSelected>>', lambda e: self._on_quality_changed())

        self.grayscale_var = tk.BooleanVar(value=False)
        grayscale_cb = ttk.Checkbutton(settings_frame, text="Grayscale",
                                        variable=self.grayscale_var,
                                        command=self._on_quality_changed)
        grayscale_cb.grid(row=3, column=2, sticky=tk.W, padx=5)
        Tooltip(grayscale_cb,
                "Convert frames to grayscale before processing.\n"
                "Useful for B&W source material to avoid\n"
                "wasting palette entries on color noise.")

        self.shared_palette_var = tk.BooleanVar(value=False)
        shared_pal_cb = ttk.Checkbutton(settings_frame, text="Shared Palette",
                                         variable=self.shared_palette_var,
                                         command=self._on_quality_changed)
        shared_pal_cb.grid(row=3, column=3, columnspan=2, sticky=tk.W, padx=5)
        Tooltip(shared_pal_cb,
                "Compute one palette from sampled frames and reuse\n"
                "it for all frames in this segment. Reduces dither\n"
                "'swimming' between frames. Only used during full\n"
                "conversion (not single-frame preview).")

        # --- Preview panels ---
        preview_frame = ttk.LabelFrame(main_frame, text="Preview", padding=5)
        preview_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # Source preview
        src_frame = ttk.Frame(preview_frame)
        src_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        ttk.Label(src_frame, text="Source Frame").pack()
        self.src_canvas = tk.Canvas(src_frame, width=256, height=160, bg='black')
        self.src_canvas.pack(fill=tk.BOTH, expand=True)
        self.src_photo = None

        # SNES preview
        snes_frame = ttk.Frame(preview_frame)
        snes_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        ttk.Label(snes_frame, text="SNES Reconstruction").pack()
        self.snes_canvas = tk.Canvas(snes_frame, width=256, height=160, bg='black')
        self.snes_canvas.pack(fill=tk.BOTH, expand=True)
        self.snes_photo = None

        # --- Preview scrubber ---
        scrubber_frame = ttk.Frame(main_frame)
        scrubber_frame.pack(fill=tk.X, pady=(0, 5))

        self.scrub_var = tk.DoubleVar(value=0.0)
        self.scrub_scale = ttk.Scale(scrubber_frame, from_=0.0, to=1.0,
                                     orient=tk.HORIZONTAL,
                                     variable=self.scrub_var,
                                     command=self._on_scrub)
        self.scrub_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.scrub_scale.configure(state=tk.DISABLED)

        self.clip_btn = ttk.Button(scrubber_frame, text="Preview Clip",
                                   command=self._toggle_clip, state=tk.DISABLED)
        self.clip_btn.pack(side=tk.RIGHT, padx=(0, 8))
        Tooltip(self.clip_btn,
                "Render ~2 seconds of video from the scrubber\n"
                "position and play it back as animation.")

        self.scrub_label = ttk.Label(scrubber_frame, text="0:00 / 0:00", width=16,
                                     anchor=tk.E)
        self.scrub_label.pack(side=tk.RIGHT)

        # --- Segments ---
        seg_frame = ttk.LabelFrame(main_frame, text="Segments", padding=5)
        seg_frame.pack(fill=tk.X, pady=(0, 5))

        self.seg_canvas = tk.Canvas(seg_frame, height=24, bg='#2b2b2b',
                                    highlightthickness=0)
        self.seg_canvas.pack(fill=tk.X, pady=(0, 4))
        self.seg_canvas.bind('<Button-1>', self._on_seg_canvas_click)
        self.seg_canvas.bind('<Configure>', lambda e: self._redraw_seg_canvas())

        seg_btn_frame = ttk.Frame(seg_frame)
        seg_btn_frame.pack(fill=tk.X)

        self.split_btn = ttk.Button(seg_btn_frame, text="Split Here",
                                    command=self._split_segment, state=tk.DISABLED)
        self.split_btn.pack(side=tk.LEFT, padx=(0, 5))
        Tooltip(self.split_btn,
                "Split the current segment at the scrubber position.\n"
                "Each segment can have its own quality settings.")

        self.delete_seg_btn = ttk.Button(seg_btn_frame, text="Delete Segment",
                                         command=self._delete_segment, state=tk.DISABLED)
        self.delete_seg_btn.pack(side=tk.LEFT, padx=(0, 5))
        Tooltip(self.delete_seg_btn,
                "Delete the selected segment, merging it\n"
                "into its left neighbor.")

        self.seg_info_label = ttk.Label(seg_btn_frame, text="No segments")
        self.seg_info_label.pack(side=tk.LEFT, padx=(10, 0))

        # --- Progress ---
        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=(0, 5))

        self.phase_label = ttk.Label(progress_frame, text="Ready")
        self.phase_label.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=1.0, mode='determinate')
        self.progress_bar.pack(fill=tk.X, pady=2)

        self.status_label = ttk.Label(progress_frame, text="")
        self.status_label.pack(fill=tk.X)

        # --- Buttons ---
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(0, 5))

        self.convert_btn = ttk.Button(button_frame, text="Convert",
                                      command=self._start_conversion)
        self.convert_btn.pack(side=tk.LEFT, padx=5)

        self.cancel_btn = ttk.Button(button_frame, text="Cancel",
                                     command=self._cancel_conversion, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

        # --- Log ---
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state='disabled',
                                                  font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _setup_logging(self):
        """Set up logging to the log text widget."""
        handler = TextHandler(self.log_text)
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def _browse_input(self):
        """Open file dialog for input video."""
        filetypes = [
            ("Video files", "*.mp4 *.avi *.mkv *.m2v *.mpg *.mpeg *.mov *.webm *.flv *.wmv"),
            ("All files", "*.*"),
        ]
        path = filedialog.askopenfilename(title="Select Input Video", filetypes=filetypes)
        if path:
            self.input_var.set(path)
            # Auto-fill output: SNESVideoPlayer.msu in same directory as input
            # so filenames match the ROM and it "just works"
            if not self.output_var.get():
                self.output_var.set(
                    os.path.join(os.path.dirname(path), 'SNESVideoPlayer.msu'))

            # Show video info and populate settings
            ffmpeg = find_ffmpeg()
            info = get_video_info(path, ffmpeg)
            if info:
                self._log(f"Video: {info['width']}x{info['height']} "
                          f"@ {info['fps']:.2f}fps, "
                          f"{info['duration_s']:.1f}s "
                          f"(~{info['num_frames']} output frames)")

                # Auto-populate start and duration
                self.start_var.set("0")
                self.duration_var.set(f"{info['duration_s']:.1f}")

                self._setup_scrubber(path, info['duration_s'])

    def _browse_output(self):
        """Open file dialog for output MSU file."""
        filetypes = [
            ("MSU-1 files", "*.msu"),
            ("All files", "*.*"),
        ]
        path = filedialog.asksaveasfilename(title="Save MSU File", filetypes=filetypes,
                                            defaultextension='.msu')
        if path:
            self.output_var.set(path)

    def _download_url(self):
        """Download video from URL and set it as input."""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a video URL.")
            return

        self.download_btn.configure(state=tk.DISABLED)
        self.phase_label.configure(text="Downloading video...")
        self.progress_bar.configure(mode='indeterminate')
        self.progress_bar.start(15)
        self._log(f"Downloading: {url}")

        def _do_download():
            try:
                from video_download import download_video

                def _on_progress(downloaded, total, pct):
                    self.root.after(0, self._on_download_progress, downloaded, total, pct)

                path = download_video(url, on_progress=_on_progress)
                self.root.after(0, self._on_download_complete, path)
            except ImportError as e:
                self.root.after(0, self._on_download_error, str(e))
            except Exception as e:
                self.root.after(0, self._on_download_error, str(e))

        threading.Thread(target=_do_download, daemon=True).start()

    def _on_download_progress(self, downloaded, total, pct):
        """Update progress bar during download."""
        if self.progress_bar.cget('mode') == 'indeterminate':
            self.progress_bar.stop()
            self.progress_bar.configure(mode='determinate')
        self.progress_var.set(pct / 100.0)
        from video_download import _fmt_size
        self.status_label.configure(
            text=f"Downloading: {_fmt_size(downloaded)} / {_fmt_size(total)} ({pct:.0f}%)")

    def _on_download_complete(self, path):
        """Handle successful download — set as input and show preview."""
        self.progress_bar.stop()
        self.progress_bar.configure(mode='determinate')
        self.progress_var.set(0.0)
        self.download_btn.configure(state=tk.NORMAL)
        self.phase_label.configure(text="Ready")
        self.status_label.configure(text="")

        self._log(f"Downloaded: {path}")
        self.input_var.set(path)

        # Auto-fill output in same directory
        if not self.output_var.get():
            self.output_var.set(
                os.path.join(os.path.dirname(path), 'SNESVideoPlayer.msu'))

        # Show video info and preview
        ffmpeg = find_ffmpeg()
        info = get_video_info(path, ffmpeg)
        if info:
            self._log(f"Video: {info['width']}x{info['height']} "
                      f"@ {info['fps']:.2f}fps, "
                      f"{info['duration_s']:.1f}s "
                      f"(~{info['num_frames']} output frames)")
            self.start_var.set("0")
            self.duration_var.set(f"{info['duration_s']:.1f}")

            self._setup_scrubber(path, info['duration_s'])

    def _on_download_error(self, message):
        """Handle download failure."""
        self.progress_bar.stop()
        self.progress_bar.configure(mode='determinate')
        self.progress_var.set(0.0)
        self.download_btn.configure(state=tk.NORMAL)
        self.phase_label.configure(text="Ready")
        self.status_label.configure(text="")
        self._log(f"Download failed: {message}")
        messagebox.showerror("Download Error", message)

    def _display_on_canvas(self, canvas, pil_img, photo_attr):
        """Scale a PIL image to fit the canvas and display it centered."""
        from PIL import Image, ImageTk

        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            cw = int(canvas.cget('width'))
            ch = int(canvas.cget('height'))

        iw, ih = pil_img.size
        scale = min(cw / iw, ch / ih)
        new_w = max(1, int(iw * scale))
        new_h = max(1, int(ih * scale))

        if new_w != iw or new_h != ih:
            scaled = pil_img.resize((new_w, new_h), Image.NEAREST)
        else:
            scaled = pil_img

        photo = ImageTk.PhotoImage(scaled)
        setattr(self, photo_attr, photo)
        canvas.delete("all")
        canvas.create_image(cw // 2, ch // 2, image=photo)

    def _get_dither_method(self):
        """Map GUI dither display string to engine constant."""
        mapping = {
            "None": DITHER_NONE,
            "Floyd-Steinberg": DITHER_FLOYD_STEINBERG,
            "Ordered (Bayer)": DITHER_ORDERED,
        }
        return mapping.get(self.dither_var.get(), DEFAULT_DITHER)

    def _get_engine(self):
        """Map GUI engine display string to engine constant."""
        mapping = {
            "Built-in": ENGINE_BUILTIN,
            "SuperFamiconv": ENGINE_SUPERFAMICONV,
        }
        return mapping.get(self.engine_var.get(), DEFAULT_ENGINE)

    def _on_quality_changed(self):
        """Re-convert the cached preview frame when quality settings change."""
        if self._updating_controls:
            return

        # Write current widget values into the selected segment
        if self._segment_list and 0 <= self._selected_segment_idx < len(self._segment_list):
            seg = self._segment_list.segments[self._selected_segment_idx]
            seg.dither_method = self._get_dither_method()
            seg.engine = self._get_engine()
            seg.grayscale = self.grayscale_var.get()
            seg.shared_palette = self.shared_palette_var.get()
            try:
                seg.num_palettes = int(self.palettes_var.get())
            except (ValueError, tk.TclError):
                pass
            try:
                seg.max_tiles = self.max_tiles_var.get()
            except (ValueError, tk.TclError):
                pass
            self._redraw_seg_canvas()

        if self._preview_timer is not None:
            self.root.after_cancel(self._preview_timer)
        self._preview_timer = self.root.after(300, self._reconvert_preview)

    def _setup_scrubber(self, video_path, duration):
        """Configure the scrubber for a loaded video and seek to the middle."""
        self._video_path = video_path
        self._video_duration = duration
        self.scrub_scale.configure(from_=0.0, to=max(duration, 0.1),
                                   state=tk.NORMAL)
        self.clip_btn.configure(state=tk.NORMAL)
        mid = duration * 0.5 if duration > 0 else 0.0
        self.scrub_var.set(mid)
        self.scrub_label.configure(
            text=f"{self._fmt_time(mid)} / {self._fmt_time(duration)}")

        # Initialize segments with a single segment using current widget settings
        self._segment_list = SegmentList.create_default(
            duration,
            dither_method=self._get_dither_method(),
            engine=self._get_engine(),
            num_palettes=int(self.palettes_var.get()),
            max_tiles=self.max_tiles_var.get(),
        )
        self._selected_segment_idx = 0
        self.split_btn.configure(state=tk.NORMAL)
        self.delete_seg_btn.configure(state=tk.DISABLED)
        self._redraw_seg_canvas()
        self._update_seg_info()

        # Show the initial preview at the midpoint
        ffmpeg = find_ffmpeg()
        self._show_preview_frame(video_path, ffmpeg, mid)

    def _on_scrub(self, value):
        """Handle scrubber slider movement with debounce."""
        seek = float(value)
        self.scrub_label.configure(
            text=f"{self._fmt_time(seek)} / {self._fmt_time(self._video_duration)}")

        # Auto-select the segment at the scrubber position
        if self._segment_list:
            new_idx = self._segment_list.segment_index_for_time(seek)
            if new_idx != self._selected_segment_idx:
                self._selected_segment_idx = new_idx
                self._load_segment_to_controls()
                self._redraw_seg_canvas()
                self._update_seg_info()

        if self._scrub_timer is not None:
            self.root.after_cancel(self._scrub_timer)
        self._scrub_timer = self.root.after(250, self._scrub_seek)

    def _scrub_seek(self):
        """Extract and preview the frame at the current scrubber position."""
        self._scrub_timer = None
        if not self._video_path or self.is_converting:
            return
        # Stop any running clip when the user scrubs
        if self._clip_playing or self._clip_cancel is not None:
            self._stop_clip()
        seek_time = self.scrub_var.get()
        ffmpeg = find_ffmpeg()
        self._show_preview_frame(self._video_path, ffmpeg, seek_time)

    @staticmethod
    def _fmt_time(seconds):
        """Format seconds as M:SS or H:MM:SS."""
        s = max(0.0, seconds)
        m, sec = divmod(int(s), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"

    def _toggle_clip(self):
        """Start or stop the animated preview clip."""
        if self._clip_playing or self._clip_cancel is not None:
            self._stop_clip()
        else:
            self._start_clip()

    def _start_clip(self):
        """Render a short clip from the scrubber position and play it back."""
        if not self._video_path or self.is_converting:
            return

        CLIP_SECONDS = 2.0
        seek = self.scrub_var.get()
        # Clamp duration so we don't go past the end
        clip_dur = min(CLIP_SECONDS, max(0.1, self._video_duration - seek))

        # Capture fallback settings from widgets
        try:
            fb_num_palettes = int(self.palettes_var.get())
            fb_max_tiles = self.max_tiles_var.get()
        except (ValueError, tk.TclError):
            return
        fb_dither_method = self._get_dither_method()
        fb_engine = self._get_engine()
        workers = self.workers_var.get()
        video_path = self._video_path
        ffmpeg = find_ffmpeg()
        fps = self.fps_var.get()
        segment_list = self._segment_list  # capture for thread

        cancel = threading.Event()
        self._clip_cancel = cancel
        self._clip_frames = []
        self._clip_playing = False
        self.clip_btn.configure(text="Stop")
        self.phase_label.configure(text="Rendering preview clip...")
        self.progress_var.set(0.0)

        def _render():
            import tempfile
            import shutil
            from frame_extract import extract_frames, get_extracted_frames
            from tile_convert import convert_frame_to_bytes
            from concurrent.futures import ThreadPoolExecutor, as_completed

            tmp = tempfile.mkdtemp(prefix='snes_vp_clip_')
            try:
                # Phase 1: extract frames
                self.root.after(0, self.status_label.configure,
                                {'text': 'Extracting clip frames...'})
                extract_frames(video_path, tmp, ffmpeg=ffmpeg,
                               start_time=seek, duration=clip_dur)
                if cancel.is_set():
                    return

                frame_paths = get_extracted_frames(tmp)
                total = len(frame_paths)
                if total == 0:
                    return

                # Phase 2: convert each frame in parallel, collect PIL images
                from PIL import Image
                results = [None] * total  # (src_pil, snes_pil) per index
                done = [0]  # mutable counter for closure

                def convert_one(idx, png_path):
                    if cancel.is_set():
                        return idx, None, None
                    # Look up per-frame segment settings
                    if segment_list:
                        frame_time = seek + idx / fps
                        seg = segment_list.settings_for_time(frame_time)
                        if seg:
                            f_np, f_mt = seg.num_palettes, seg.max_tiles
                            f_dm, f_en = seg.dither_method, seg.engine
                        else:
                            f_np, f_mt = fb_num_palettes, fb_max_tiles
                            f_dm, f_en = fb_dither_method, fb_engine
                    else:
                        f_np, f_mt = fb_num_palettes, fb_max_tiles
                        f_dm, f_en = fb_dither_method, fb_engine
                    src = Image.open(png_path).copy()
                    tiles, tilemap, palette = convert_frame_to_bytes(
                        png_path, num_palettes=f_np,
                        max_tiles=f_mt, dither_method=f_dm,
                        engine=f_en)
                    snes = reconstruct_to_pil(tiles, tilemap, palette, 256, 160)
                    return idx, src, snes

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(convert_one, i, p): i
                               for i, p in enumerate(frame_paths)}
                    for future in as_completed(futures):
                        if cancel.is_set():
                            pool.shutdown(wait=False, cancel_futures=True)
                            return
                        idx, src, snes = future.result()
                        if src is not None:
                            results[idx] = (src, snes)
                        done[0] += 1
                        pct = done[0] / total
                        self.root.after(0, self._clip_render_progress,
                                        done[0], total, pct)

                if cancel.is_set():
                    return

                # Filter out any None entries
                frames = [r for r in results if r is not None]
                if frames:
                    self.root.after(0, self._clip_play, frames)

            except Exception as e:
                self.root.after(0, self._log,
                                f"Clip render failed: {e}")
            finally:
                try:
                    shutil.rmtree(tmp)
                except OSError:
                    pass
                self.root.after(0, self._clip_render_done)

        threading.Thread(target=_render, daemon=True).start()

    def _clip_render_progress(self, done, total, pct):
        """Update progress bar during clip rendering."""
        self.progress_var.set(pct)
        self.status_label.configure(text=f"Rendering clip: {done}/{total} frames")

    def _clip_render_done(self):
        """Clean up after clip render thread finishes."""
        if not self._clip_playing:
            # Render finished without starting playback (cancelled or error)
            self._clip_cancel = None
            self.clip_btn.configure(text="Preview Clip")
            self.phase_label.configure(text="Ready")
            self.progress_var.set(0.0)
            self.status_label.configure(text="")

    def _clip_play(self, frames):
        """Start animation playback of rendered clip frames."""
        self._clip_frames = frames
        self._clip_frame_idx = 0
        self._clip_playing = True
        self._clip_cancel = None
        self.phase_label.configure(text="Playing preview clip")
        self.progress_var.set(0.0)
        self.status_label.configure(text="")
        self._clip_tick()

    def _clip_tick(self):
        """Display the next animation frame and schedule the next tick."""
        if not self._clip_playing or not self._clip_frames:
            return

        idx = self._clip_frame_idx
        src_pil, snes_pil = self._clip_frames[idx]
        self._display_on_canvas(self.src_canvas, src_pil, 'src_photo')
        self._display_on_canvas(self.snes_canvas, snes_pil, 'snes_photo')

        self._clip_frame_idx = (idx + 1) % len(self._clip_frames)

        # Schedule next frame (~24fps = 42ms)
        self._clip_timer = self.root.after(42, self._clip_tick)

    def _stop_clip(self):
        """Stop clip rendering or playback."""
        # Signal render thread to abort
        if self._clip_cancel is not None:
            self._clip_cancel.set()
            self._clip_cancel = None

        # Stop animation loop
        self._clip_playing = False
        if self._clip_timer is not None:
            self.root.after_cancel(self._clip_timer)
            self._clip_timer = None

        self._clip_frames = []
        self._clip_frame_idx = 0
        self.clip_btn.configure(text="Preview Clip")
        self.phase_label.configure(text="Ready")
        self.progress_var.set(0.0)
        self.status_label.configure(text="")

    # --- Segment canvas and controls ---

    _SEG_COLORS = ['#4a90d9', '#e07040', '#50b060', '#c050c0',
                   '#d0c040', '#40c0c0', '#d06080', '#8080c0']

    def _redraw_seg_canvas(self):
        """Redraw the colored segment strip on the canvas."""
        c = self.seg_canvas
        c.delete('all')
        if not self._segment_list or len(self._segment_list) == 0:
            return

        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw <= 1:
            cw = 400
        if ch <= 1:
            ch = 24

        total_dur = self._video_duration or 1.0
        segs = self._segment_list.segments

        for i, seg in enumerate(segs):
            x0 = int(seg.start_time / total_dur * cw)
            x1 = int(seg.end_time / total_dur * cw)
            color = self._SEG_COLORS[i % len(self._SEG_COLORS)]
            c.create_rectangle(x0, 0, x1, ch, fill=color, outline='')

            # Selected segment highlight
            if i == self._selected_segment_idx:
                c.create_rectangle(x0, 0, x1, ch, fill='', outline='white', width=2)

            # Segment number label
            mid_x = (x0 + x1) // 2
            if x1 - x0 > 16:
                c.create_text(mid_x, ch // 2, text=str(i + 1),
                              fill='white', font=('Segoe UI', 8, 'bold'))

    def _update_seg_info(self):
        """Update the segment info label."""
        if not self._segment_list or len(self._segment_list) == 0:
            self.seg_info_label.configure(text="No segments")
            return

        idx = self._selected_segment_idx
        total = len(self._segment_list)
        seg = self._segment_list.segments[idx]
        self.seg_info_label.configure(
            text=f"Segment {idx + 1}/{total}: "
                 f"{self._fmt_time(seg.start_time)} - {self._fmt_time(seg.end_time)}")

        # Enable/disable delete button
        self.delete_seg_btn.configure(
            state=tk.NORMAL if total > 1 else tk.DISABLED)

    def _on_seg_canvas_click(self, event):
        """Select the segment under the mouse click."""
        if not self._segment_list or len(self._segment_list) == 0:
            return

        cw = self.seg_canvas.winfo_width()
        if cw <= 1:
            return

        total_dur = self._video_duration or 1.0
        click_time = event.x / cw * total_dur

        new_idx = self._segment_list.segment_index_for_time(click_time)
        if new_idx != self._selected_segment_idx:
            self._selected_segment_idx = new_idx
            self._load_segment_to_controls()
            self._redraw_seg_canvas()
            self._update_seg_info()

    def _load_segment_to_controls(self):
        """Load the selected segment's settings into the quality widgets."""
        if not self._segment_list or self._selected_segment_idx >= len(self._segment_list):
            return

        seg = self._segment_list.segments[self._selected_segment_idx]

        self._updating_controls = True
        try:
            # Dithering
            dither_map = {
                'none': 'None',
                'floyd-steinberg': 'Floyd-Steinberg',
                'ordered': 'Ordered (Bayer)',
            }
            self.dither_var.set(dither_map.get(seg.dither_method, 'Floyd-Steinberg'))

            # Max tiles
            self.max_tiles_var.set(seg.max_tiles)

            # Palettes
            self.palettes_var.set(str(seg.num_palettes))

            # Engine
            engine_map = {
                'builtin': 'Built-in',
                'superfamiconv': 'SuperFamiconv',
            }
            self.engine_var.set(engine_map.get(seg.engine, 'Built-in'))

            # Grayscale / Shared palette
            self.grayscale_var.set(seg.grayscale)
            self.shared_palette_var.set(seg.shared_palette)
        finally:
            self._updating_controls = False

    def _split_segment(self):
        """Split the segment at the current scrubber position."""
        if not self._segment_list:
            return

        split_time = self.scrub_var.get()
        new_idx = self._segment_list.split_at(split_time)

        if new_idx is None:
            self._log("Cannot split: segment would be too short (< 0.1s)")
            return

        self._selected_segment_idx = new_idx
        self._load_segment_to_controls()
        self._redraw_seg_canvas()
        self._update_seg_info()
        self._log(f"Split at {self._fmt_time(split_time)} "
                  f"({len(self._segment_list)} segments)")

    def _delete_segment(self):
        """Delete the selected segment by merging into its neighbor."""
        if not self._segment_list:
            return

        idx = self._selected_segment_idx
        if not self._segment_list.delete_segment(idx):
            self._log("Cannot delete the last remaining segment")
            return

        # Select the left neighbor (or stay at 0)
        self._selected_segment_idx = max(0, idx - 1)
        self._load_segment_to_controls()
        self._redraw_seg_canvas()
        self._update_seg_info()
        self._on_quality_changed()  # trigger preview re-conversion
        self._log(f"Deleted segment ({len(self._segment_list)} segments remaining)")

    def _reconvert_preview(self):
        """Re-run SNES conversion on the cached preview frame with current settings."""
        self._preview_timer = None
        preview_path = self._preview_path
        if not preview_path or not os.path.isfile(preview_path):
            return
        if self.is_converting:
            return

        # Use segment settings at scrubber position if available
        if self._segment_list:
            seek = self.scrub_var.get()
            seg = self._segment_list.settings_for_time(seek)
            if seg:
                num_palettes = seg.num_palettes
                max_tiles = seg.max_tiles
                dither_method = seg.dither_method
                engine = seg.engine
                grayscale = seg.grayscale
            else:
                return
        else:
            try:
                num_palettes = int(self.palettes_var.get())
                max_tiles = self.max_tiles_var.get()
            except (ValueError, tk.TclError):
                return
            dither_method = self._get_dither_method()
            engine = self._get_engine()
            grayscale = self.grayscale_var.get()

        if max_tiles < 1 or max_tiles > 384:
            return

        def _convert():
            try:
                from tile_convert import convert_frame_to_bytes
                tiles, tilemap, palette = convert_frame_to_bytes(
                    preview_path,
                    num_palettes=num_palettes,
                    max_tiles=max_tiles,
                    dither_method=dither_method,
                    engine=engine,
                    grayscale=grayscale)
                pil_img = reconstruct_to_pil(tiles, tilemap, palette, 256, 160)
                self.root.after(0, self._display_on_canvas,
                                self.snes_canvas, pil_img, 'snes_photo')
            except Exception:
                pass

        threading.Thread(target=_convert, daemon=True).start()

    def _show_preview_frame(self, video_path, ffmpeg, seek_time):
        """Extract and display a preview frame from the video, with SNES reconstruction."""
        import tempfile
        preview_path = os.path.join(tempfile.gettempdir(), 'snes_vp_preview.png')
        try:
            if extract_single_frame(video_path, preview_path, ffmpeg,
                                    seek_time=seek_time):
                from PIL import Image
                img = Image.open(preview_path)
                self._display_on_canvas(self.src_canvas, img, 'src_photo')
                self._preview_path = preview_path

                # Convert the preview frame with current quality settings
                self._reconvert_preview()
        except Exception:
            pass

    def _log(self, message):
        """Add a message to the log widget."""
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message + '\n')
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')

    def _start_conversion(self):
        """Start the conversion in a background thread."""
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()

        if not input_path:
            messagebox.showerror("Error", "Please select an input video file.")
            return
        if not output_path:
            messagebox.showerror("Error", "Please specify an output MSU file.")
            return
        if not os.path.isfile(input_path):
            messagebox.showerror("Error", f"Input file not found:\n{input_path}")
            return

        # Parse optional time parameters
        start_time = None
        duration = None
        try:
            if self.start_var.get().strip():
                start_time = float(self.start_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Invalid start time (must be a number in seconds).")
            return
        try:
            if self.duration_var.get().strip():
                duration = float(self.duration_var.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Invalid duration (must be a number in seconds).")
            return

        title = MSU_TITLE

        self.is_converting = True
        self.convert_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)

        self.pipeline = ConversionPipeline(
            input_path, output_path,
            workers=self.workers_var.get(),
            title=title,
            fps=self.fps_var.get(),
            num_palettes=int(self.palettes_var.get()),
            max_tiles=self.max_tiles_var.get(),
            dither_method=self._get_dither_method(),
            engine=self._get_engine(),
            deinterlace=self.deinterlace_var.get(),
            start_time=start_time,
            duration=duration,
            segments=self._segment_list,
        )

        # Set up progress callbacks (thread-safe via root.after)
        self.pipeline.progress.on_progress = self._on_progress_threadsafe
        self.pipeline.progress.on_phase_change = self._on_phase_threadsafe
        self.pipeline.progress.on_frame_converted = self._on_frame_converted_threadsafe
        self.pipeline.progress.on_complete = self._on_complete_threadsafe
        self.pipeline.progress.on_error = self._on_error_threadsafe

        self.conversion_thread = threading.Thread(target=self._run_conversion, daemon=True)
        self.conversion_thread.start()

    def _run_conversion(self):
        """Run the pipeline (called from background thread)."""
        try:
            self.pipeline.run()
        except Exception as e:
            self.root.after(0, self._on_error, str(e))
        finally:
            self.root.after(0, self._conversion_finished)

    def _cancel_conversion(self):
        """Cancel the running conversion."""
        if self.pipeline:
            self.pipeline.progress.cancel()
            self._log("Cancellation requested...")

    def _conversion_finished(self):
        """Called when conversion thread finishes."""
        self.is_converting = False
        self.convert_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)

    # --- Thread-safe callback wrappers ---
    def _on_progress_threadsafe(self, phase_progress, overall_progress, message):
        self.root.after(0, self._on_progress, phase_progress, overall_progress, message)

    def _on_phase_threadsafe(self, phase, message):
        self.root.after(0, self._on_phase, phase, message)

    def _on_frame_converted_threadsafe(self, idx, total, tiles, tilemap, palette, source_path):
        # Only update preview every 10th frame to avoid UI lag
        if idx % 10 == 0 or idx == total:
            self.root.after(0, self._on_frame_converted, idx, total, tiles, tilemap, palette, source_path)

    def _on_complete_threadsafe(self, output_path):
        self.root.after(0, self._on_complete, output_path)

    def _on_error_threadsafe(self, message):
        self.root.after(0, self._on_error, message)

    # --- UI update callbacks (main thread) ---
    def _on_progress(self, phase_progress, overall_progress, message):
        self.progress_var.set(overall_progress)
        self.status_label.configure(text=message)

    def _on_phase(self, phase, message):
        self.phase_label.configure(text=f"Phase: {phase}")
        if message:
            self._log(message)

    def _on_frame_converted(self, idx, total, tiles, tilemap, palette, source_path):
        """Update both preview panels with the latest converted frame."""
        try:
            from PIL import Image

            # Update source frame preview
            if source_path and os.path.isfile(source_path):
                src_img = Image.open(source_path)
                self._display_on_canvas(self.src_canvas, src_img, 'src_photo')

            # Update SNES reconstruction preview
            pil_img = reconstruct_to_pil(tiles, tilemap, palette, 256, 160)
            self._display_on_canvas(self.snes_canvas, pil_img, 'snes_photo')
        except Exception:
            pass  # Preview is best-effort

    def _on_complete(self, output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024 if os.path.exists(output_path) else 0
        msg = f"Conversion complete!\nOutput: {output_path}\nSize: {size_mb:.1f} MB"
        self._log(msg)
        messagebox.showinfo("Complete", msg)

    def _on_error(self, message):
        self._log(f"ERROR: {message}")
        messagebox.showerror("Error", message)


def run_gui():
    """Launch the converter GUI."""
    root = tk.Tk()
    app = ConverterGUI(root)
    root.mainloop()


if __name__ == '__main__':
    run_gui()
