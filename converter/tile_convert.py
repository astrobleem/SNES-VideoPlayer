#!/usr/bin/env python3
"""
tile_convert.py - SNES 4BPP tile conversion for SNES-VideoPlayer

Converts 256x160 full-color PNG frames to SNES tile/tilemap/palette data:
  - K-means clusters 8x8 tiles into sub-palette groups
  - Builds per-cluster 15-color sub-palettes (color 0 reserved)
  - Floyd-Steinberg error diffusion across tile boundaries
  - Reduces unique tiles to 384 max (VRAM limit at 4BPP)

OPENBLAS_NUM_THREADS must be set to 1 before importing numpy to prevent
corruption when called from concurrent threads.
"""

import os
import sys

# CRITICAL: Set BLAS to single-threaded BEFORE importing numpy.
# reduce_tiles() uses numpy matrix multiplication (pixels @ pixels.T) which calls
# multi-threaded BLAS internally. When multiple Python threads in ThreadPoolExecutor
# call BLAS concurrently, the shared BLAS thread pool corrupts results.
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

import struct
import numpy as np
from PIL import Image

# ---------- Constants ----------
BPP = 4
MAX_PALETTES = 2          # 2 sub-palettes per frame (32 colors max)
MAX_COLORS = MAX_PALETTES * (2 ** BPP)  # 2 * 16 = 32 colors
MAX_TILES = 384           # VRAM tile buffer: $3000 bytes = 384 tiles at 4BPP
FRAME_WIDTH = 256
FRAME_HEIGHT = 160
TILEMAP_WIDTH = 32        # tiles per row (256 / 8)
TILEMAP_HEIGHT = 20       # tiles per column (160 / 8)
TILEMAP_TARGET_SIZE = TILEMAP_WIDTH * TILEMAP_HEIGHT * 2  # 1280 bytes
BYTES_PER_TILE = 8 * BPP  # 32 bytes per 4BPP tile

# Dithering method constants
DITHER_NONE = 'none'
DITHER_FLOYD_STEINBERG = 'floyd-steinberg'
DITHER_ORDERED = 'ordered'
DEFAULT_DITHER = DITHER_FLOYD_STEINBERG

# Conversion engine constants
ENGINE_BUILTIN = 'builtin'
ENGINE_SUPERFAMICONV = 'superfamiconv'
DEFAULT_ENGINE = ENGINE_BUILTIN

# 4x4 Bayer threshold matrix normalized to BGR555 quantization step (~+-4.1 in RGB-255 space)
_BAYER_4x4 = np.array([
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5],
], dtype=np.float32)
_BAYER_4x4_SCALED = (_BAYER_4x4 / 16.0 - 0.5) * (255.0 / 31.0)


# ---------- K-means clustering ----------
def simple_kmeans(data, k, max_iter=20):
    """K-means++ clustering on (N, D) float32 array. Returns (labels, centers)."""
    N = data.shape[0]
    if N <= k:
        labels = np.arange(N, dtype=np.int32)
        centers = data.copy()
        if N < k:
            centers = np.vstack([centers, np.tile(centers[0], (k - N, 1))])
            labels = np.arange(N, dtype=np.int32)
        return labels, centers

    rng = np.random.RandomState(42)

    # K-means++ initialization
    centers = np.empty((k, data.shape[1]), dtype=data.dtype)
    idx = rng.randint(N)
    centers[0] = data[idx]

    for c in range(1, k):
        dists = np.min(np.sum((data[:, None, :] - centers[None, :c, :]) ** 2, axis=2), axis=1)
        total = dists.sum()
        if total == 0:
            idx = rng.randint(N)
        else:
            probs = dists / total
            probs /= probs.sum()
            idx = rng.choice(N, p=probs)
        centers[c] = data[idx]

    for _ in range(max_iter):
        dists = np.sum((data[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels = np.argmin(dists, axis=1).astype(np.int32)

        new_centers = np.empty_like(centers)
        for c in range(k):
            mask = labels == c
            if mask.any():
                new_centers[c] = data[mask].mean(axis=0)
            else:
                new_centers[c] = centers[c]

        if np.allclose(new_centers, centers):
            break
        centers = new_centers

    return labels, centers


# ---------- Color space conversion ----------
def rgb_to_bgr555(r, g, b):
    """Convert 8-bit RGB to SNES BGR555 (16-bit value)."""
    r5 = int(round(r * 31.0 / 255.0)) & 0x1F
    g5 = int(round(g * 31.0 / 255.0)) & 0x1F
    b5 = int(round(b * 31.0 / 255.0)) & 0x1F
    return r5 | (g5 << 5) | (b5 << 10)


def bgr555_to_rgb_float(bgr555):
    """Convert BGR555 to (R, G, B) as floats in 0-255 range."""
    r = (bgr555 & 0x1F) * (255.0 / 31.0)
    g = ((bgr555 >> 5) & 0x1F) * (255.0 / 31.0)
    b = ((bgr555 >> 10) & 0x1F) * (255.0 / 31.0)
    return (r, g, b)


def read_snes_palette(palette_file):
    """Read SNES BGR555 palette file and return (N, 3) float32 RGB array."""
    with open(palette_file, 'rb') as f:
        data = f.read()
    num_colors = len(data) // 2
    palette = np.zeros((max(16, num_colors), 3), dtype=np.float32)
    for i in range(num_colors):
        bgr555 = struct.unpack_from('<H', data, i * 2)[0]
        palette[i, 0] = (bgr555 & 0x1F) * (255.0 / 31.0)
        palette[i, 1] = ((bgr555 >> 5) & 0x1F) * (255.0 / 31.0)
        palette[i, 2] = ((bgr555 >> 10) & 0x1F) * (255.0 / 31.0)
    return palette


def read_snes_palette_bytes(palette_bytes):
    """Read SNES BGR555 palette from raw bytes and return (N, 3) float32 RGB array."""
    num_colors = len(palette_bytes) // 2
    palette = np.zeros((max(16, num_colors), 3), dtype=np.float32)
    for i in range(num_colors):
        bgr555 = struct.unpack_from('<H', palette_bytes, i * 2)[0]
        palette[i, 0] = (bgr555 & 0x1F) * (255.0 / 31.0)
        palette[i, 1] = ((bgr555 >> 5) & 0x1F) * (255.0 / 31.0)
        palette[i, 2] = ((bgr555 >> 10) & 0x1F) * (255.0 / 31.0)
    return palette


# ---------- 4BPP tile encoding ----------
def encode_tiles_4bpp(pixel_indices, tile_palettes, width, height):
    """Encode pixel index grid to SNES 4BPP tile data + tilemap.

    pixel_indices: (H, W) uint8, values 0-15 (local to each tile's sub-palette)
    tile_palettes: (tiles_h, tiles_w) uint8, sub-palette number per tile
    Returns: (tile_data_bytes, tilemap_bytes)
    """
    tiles_h = height // 8
    tiles_w = width // 8

    # Reshape into tiles: (tiles_h, tiles_w, 8, 8)
    tiles = pixel_indices.reshape(tiles_h, 8, tiles_w, 8)
    tiles = tiles.transpose(0, 2, 1, 3)

    tile_dict = {}
    tile_data_list = []
    tilemap = np.zeros(tiles_h * tiles_w, dtype=np.uint16)

    for tr in range(tiles_h):
        for tc in range(tiles_w):
            tile = tiles[tr, tc]  # (8, 8) uint8

            # Encode SNES 4BPP bitplane format
            encoded = bytearray(32)
            for row in range(8):
                bp0 = bp1 = bp2 = bp3 = 0
                for px in range(8):
                    idx = int(tile[row, px])
                    bit = 7 - px
                    bp0 |= ((idx >> 0) & 1) << bit
                    bp1 |= ((idx >> 1) & 1) << bit
                    bp2 |= ((idx >> 2) & 1) << bit
                    bp3 |= ((idx >> 3) & 1) << bit
                encoded[2 * row] = bp0
                encoded[2 * row + 1] = bp1
                encoded[16 + 2 * row] = bp2
                encoded[16 + 2 * row + 1] = bp3

            encoded_bytes = bytes(encoded)
            if encoded_bytes not in tile_dict:
                tile_dict[encoded_bytes] = len(tile_data_list)
                tile_data_list.append(encoded_bytes)

            tile_idx = tile_dict[encoded_bytes]
            pal_num = int(tile_palettes[tr, tc])
            tilemap[tr * tiles_w + tc] = tile_idx | (pal_num << 10)

    tile_data = b''.join(tile_data_list)
    tilemap_bytes = tilemap.astype('<u2').tobytes()
    return tile_data, tilemap_bytes


# ---------- 4BPP tile decoding ----------
def decode_tiles_4bpp_rgb(tiles_raw, palette_rgb, tile_pal_offsets=None):
    """Decode SNES 4BPP tiles to RGB values using the frame's actual palette.

    tiles_raw: (N, 32) uint8 array of raw SNES 4BPP tile data
    palette_rgb: (C, 3) float32 array of RGB values
    tile_pal_offsets: optional (N,) uint16, per-tile palette base offset
    Returns: (N, 192) float32 array (64 pixels x 3 RGB channels)
    """
    N = tiles_raw.shape[0]
    pixel_indices = np.zeros((N, 8, 8), dtype=np.uint8)
    for row in range(8):
        bp0 = tiles_raw[:, 2 * row].astype(np.uint16)
        bp1 = tiles_raw[:, 2 * row + 1].astype(np.uint16)
        bp2 = tiles_raw[:, 16 + 2 * row].astype(np.uint16)
        bp3 = tiles_raw[:, 16 + 2 * row + 1].astype(np.uint16)
        for px in range(8):
            bit = 7 - px
            pixel_indices[:, row, px] = (
                ((bp0 >> bit) & 1) |
                (((bp1 >> bit) & 1) << 1) |
                (((bp2 >> bit) & 1) << 2) |
                (((bp3 >> bit) & 1) << 3)
            ).astype(np.uint8)
    flat_indices = pixel_indices.reshape(N, 64)

    if tile_pal_offsets is not None:
        flat_indices = flat_indices.astype(np.uint16) + tile_pal_offsets[:, None]

    rgb = palette_rgb[flat_indices]  # (N, 64, 3)
    return rgb.reshape(N, 192)


# ---------- Tilemap padding ----------
def pad_tilemap(tilemap_file):
    """Pad tilemap to target size for SNES compatibility.

    256x160 = 32x20 tiles = 1280 bytes. Pad with zeros if shorter.
    """
    with open(tilemap_file, 'rb') as f:
        data = f.read()
    if len(data) < TILEMAP_TARGET_SIZE:
        with open(tilemap_file, 'wb') as f:
            f.write(data)
            f.write(b'\x00' * (TILEMAP_TARGET_SIZE - len(data)))


def pad_tilemap_bytes(tilemap_bytes):
    """Pad tilemap bytes to target size, returning new bytes."""
    if len(tilemap_bytes) < TILEMAP_TARGET_SIZE:
        return tilemap_bytes + b'\x00' * (TILEMAP_TARGET_SIZE - len(tilemap_bytes))
    return tilemap_bytes


# ---------- Tile reduction ----------
def reduce_tiles(tile_file, tilemap_file, palette_file, max_tiles=MAX_TILES):
    """Reduce tile count to max_tiles using global greedy merge in RGB color space.

    SNES VRAM budget is $3000 bytes = 384 tiles at 4BPP. Video frames at
    256x160 can have up to 640 unique tiles (32x20 grid). This function finds
    the most similar tile pairs across the ENTIRE image and merges them,
    distributing quality loss evenly.

    Uses L2 distance on actual RGB color values (decoded through the frame's
    palette) for accurate visual similarity matching. Only merges tiles that
    share the same sub-palette to preserve color accuracy.
    """
    with open(tile_file, 'rb') as f:
        tile_data = f.read()
    num_tiles = len(tile_data) // BYTES_PER_TILE
    if num_tiles <= max_tiles:
        return

    tiles = np.frombuffer(tile_data, dtype=np.uint8).reshape(num_tiles, BYTES_PER_TILE)
    palette_rgb = read_snes_palette(palette_file)

    with open(tilemap_file, 'rb') as f:
        tilemap_raw = f.read()
    tilemap_arr = np.frombuffer(tilemap_raw, dtype=np.uint16).copy()
    tm_tile_indices = tilemap_arr & 0x3ff
    tm_pal_bits = (tilemap_arr >> 10) & 0x7

    # Determine palette for each unique tile (from first tilemap reference)
    tile_palettes = np.zeros(num_tiles, dtype=np.uint8)
    seen = np.zeros(num_tiles, dtype=bool)
    for i in range(len(tilemap_arr)):
        ti = int(tm_tile_indices[i])
        if ti < num_tiles and not seen[ti]:
            tile_palettes[ti] = tm_pal_bits[i]
            seen[ti] = True

    # Decode tiles to RGB using per-tile sub-palettes
    tile_pal_offsets = tile_palettes.astype(np.uint16) * 16
    pixels = decode_tiles_4bpp_rgb(tiles, palette_rgb, tile_pal_offsets)

    # Compute pairwise L2 squared distance matrix
    sq_norms = np.sum(pixels * pixels, axis=1)
    dot_products = pixels @ pixels.T
    dist = sq_norms[:, None] + sq_norms[None, :] - 2 * dot_products

    # Block cross-palette merges
    same_pal = tile_palettes[:, None] == tile_palettes[None, :]
    dist = np.where(same_pal, dist, np.inf)

    # Get all unique pairs sorted by distance
    rows_idx, cols_idx = np.triu_indices(num_tiles, k=1)
    pair_dists = dist[rows_idx, cols_idx]
    sort_order = np.argsort(pair_dists)

    # Greedy merge
    to_remove = num_tiles - max_tiles
    alive = set(range(num_tiles))
    merge_target = list(range(num_tiles))
    removed = 0

    for idx in sort_order:
        if removed >= to_remove:
            break
        i = int(rows_idx[idx])
        j = int(cols_idx[idx])
        if i not in alive or j not in alive:
            continue
        if not np.isfinite(dist[i, j]):
            break
        alive.discard(j)
        merge_target[j] = i
        removed += 1

    # Resolve transitive merges
    for idx in range(num_tiles):
        target = merge_target[idx]
        while merge_target[target] != target:
            target = merge_target[target]
        merge_target[idx] = target

    # Re-index surviving tiles
    alive_sorted = sorted(alive)
    reindex = {}
    for new_i, old_i in enumerate(alive_sorted):
        reindex[old_i] = new_i

    final_remap = np.array([reindex[merge_target[i]] for i in range(num_tiles)],
                           dtype=np.uint16)

    # Update tilemap
    tile_indices = tilemap_arr & 0x3ff
    flags = tilemap_arr & 0xfc00
    new_indices = final_remap[tile_indices]
    tilemap_arr = flags | new_indices

    # Write reduced tiles
    new_tile_data = tiles[alive_sorted].tobytes()
    with open(tile_file, 'wb') as f:
        f.write(new_tile_data)

    with open(tilemap_file, 'wb') as f:
        f.write(tilemap_arr.tobytes())


def reduce_tiles_bytes(tile_data, tilemap_data, palette_bytes, max_tiles=MAX_TILES):
    """In-memory version of reduce_tiles. Returns (tile_bytes, tilemap_bytes) or originals if no reduction needed."""
    num_tiles = len(tile_data) // BYTES_PER_TILE
    if num_tiles <= max_tiles:
        return tile_data, tilemap_data

    tiles = np.frombuffer(tile_data, dtype=np.uint8).reshape(num_tiles, BYTES_PER_TILE)
    palette_rgb = read_snes_palette_bytes(palette_bytes)

    tilemap_arr = np.frombuffer(tilemap_data, dtype=np.uint16).copy()
    tm_tile_indices = tilemap_arr & 0x3ff
    tm_pal_bits = (tilemap_arr >> 10) & 0x7

    tile_palettes = np.zeros(num_tiles, dtype=np.uint8)
    seen = np.zeros(num_tiles, dtype=bool)
    for i in range(len(tilemap_arr)):
        ti = int(tm_tile_indices[i])
        if ti < num_tiles and not seen[ti]:
            tile_palettes[ti] = tm_pal_bits[i]
            seen[ti] = True

    tile_pal_offsets = tile_palettes.astype(np.uint16) * 16
    pixels = decode_tiles_4bpp_rgb(tiles, palette_rgb, tile_pal_offsets)

    sq_norms = np.sum(pixels * pixels, axis=1)
    dot_products = pixels @ pixels.T
    dist = sq_norms[:, None] + sq_norms[None, :] - 2 * dot_products

    same_pal = tile_palettes[:, None] == tile_palettes[None, :]
    dist = np.where(same_pal, dist, np.inf)

    rows_idx, cols_idx = np.triu_indices(num_tiles, k=1)
    pair_dists = dist[rows_idx, cols_idx]
    sort_order = np.argsort(pair_dists)

    to_remove = num_tiles - max_tiles
    alive = set(range(num_tiles))
    merge_target = list(range(num_tiles))
    removed = 0

    for idx in sort_order:
        if removed >= to_remove:
            break
        i = int(rows_idx[idx])
        j = int(cols_idx[idx])
        if i not in alive or j not in alive:
            continue
        if not np.isfinite(dist[i, j]):
            break
        alive.discard(j)
        merge_target[j] = i
        removed += 1

    for idx in range(num_tiles):
        target = merge_target[idx]
        while merge_target[target] != target:
            target = merge_target[target]
        merge_target[idx] = target

    alive_sorted = sorted(alive)
    reindex = {}
    for new_i, old_i in enumerate(alive_sorted):
        reindex[old_i] = new_i

    final_remap = np.array([reindex[merge_target[i]] for i in range(num_tiles)],
                           dtype=np.uint16)

    tile_indices = tilemap_arr & 0x3ff
    flags = tilemap_arr & 0xfc00
    new_indices = final_remap[tile_indices]
    tilemap_arr = flags | new_indices

    new_tile_data = tiles[alive_sorted].tobytes()
    return new_tile_data, tilemap_arr.tobytes()


# ---------- Per-tile palette optimization with Floyd-Steinberg dithering ----------
def per_tile_palette_optimize(png_path, pal_file, tile_file, map_file,
                              num_palettes=MAX_PALETTES,
                              dither_method=DEFAULT_DITHER):
    """Convert full-color PNG to SNES tiles with sub-palettes + Floyd-Steinberg dithering.

    Algorithm:
    1. Load PNG, quantize pixels to BGR555 color space
    2. Cluster 8x8 tiles into groups by mean color (K-means)
    3. Build 15-color sub-palettes per cluster (color 0 reserved = $0000)
    4. Build BGR555 lookup tables for O(1) nearest-color per sub-palette
    5. Floyd-Steinberg dithering across entire image
    6. Encode to SNES 4BPP tiles + tilemap + palette
    """
    img = Image.open(png_path).convert('RGB')
    rgb = np.array(img, dtype=np.float32)
    H, W = rgb.shape[:2]
    tiles_h, tiles_w = H // 8, W // 8

    # Quantize to BGR555
    rgb5 = np.round(rgb * 31.0 / 255.0).clip(0, 31).astype(np.uint8)
    rgb_q = rgb5.astype(np.float32) * (255.0 / 31.0)

    # Per-tile mean colors for clustering
    tile_blocks = rgb_q.reshape(tiles_h, 8, tiles_w, 8, 3).transpose(0, 2, 1, 3, 4)
    tile_means = tile_blocks.mean(axis=(2, 3))
    flat_means = tile_means.reshape(-1, 3)

    labels, _centers = simple_kmeans(flat_means, num_palettes)
    tile_labels = labels.reshape(tiles_h, tiles_w)

    # Build sub-palettes
    sub_palettes = []
    sub_palette_rgb = np.zeros((num_palettes, 16, 3), dtype=np.float32)

    for p in range(num_palettes):
        mask = tile_labels == p
        positions = np.argwhere(mask)

        bgr555_set = set()
        for tr, tc in positions:
            block = rgb5[tr * 8:(tr + 1) * 8, tc * 8:(tc + 1) * 8]
            for y in range(8):
                for x in range(8):
                    r5, g5, b5 = int(block[y, x, 0]), int(block[y, x, 1]), int(block[y, x, 2])
                    bgr = r5 | (g5 << 5) | (b5 << 10)
                    bgr555_set.add(bgr)

        bgr555_set.discard(0)

        if len(bgr555_set) <= 15:
            colors = sorted(bgr555_set)
        else:
            unique_list = sorted(bgr555_set)
            unique_rgb = np.array([bgr555_to_rgb_float(c) for c in unique_list], dtype=np.float32)
            clabels, ccenters = simple_kmeans(unique_rgb, 15)
            colors = []
            for c in ccenters:
                colors.append(rgb_to_bgr555(int(round(c[0])), int(round(c[1])), int(round(c[2]))))
            colors = sorted(set(colors))
            if 0 in colors:
                colors.remove(0)
            colors = colors[:15]

        full_palette = [0] + colors
        while len(full_palette) < 16:
            full_palette.append(0)
        sub_palettes.append(full_palette)

        for ci, bgr in enumerate(full_palette):
            sub_palette_rgb[p, ci] = bgr555_to_rgb_float(bgr)

    # Build BGR555 LUTs for O(1) nearest-color lookup per sub-palette
    all_bgr = np.arange(32768, dtype=np.uint16)
    all_r = (all_bgr & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_g = ((all_bgr >> 5) & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_b = ((all_bgr >> 10) & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_rgb_lut = np.stack([all_r, all_g, all_b], axis=1)

    lut_index = np.zeros((num_palettes, 32768), dtype=np.uint8)
    lut_rgb = np.zeros((num_palettes, 32768, 3), dtype=np.float32)

    for p in range(num_palettes):
        pal_rgb = sub_palette_rgb[p]
        diffs = all_rgb_lut[:, None, :] - pal_rgb[None, :, :]
        dists = np.sum(diffs * diffs, axis=2)
        nearest = np.argmin(dists, axis=1)
        lut_index[p] = nearest.astype(np.uint8)
        lut_rgb[p] = pal_rgb[nearest]

    # Dithering
    output = np.zeros((H, W), dtype=np.uint8)

    if dither_method == DITHER_NONE:
        # Vectorized nearest-color lookup, no error diffusion
        bgr_grid = (rgb5[:, :, 0].astype(np.uint16) |
                    (rgb5[:, :, 1].astype(np.uint16) << 5) |
                    (rgb5[:, :, 2].astype(np.uint16) << 10))
        tile_pal_map = np.repeat(np.repeat(tile_labels, 8, axis=0), 8, axis=1)
        for p in range(num_palettes):
            mask = tile_pal_map == p
            output[mask] = lut_index[p][bgr_grid[mask]]

    elif dither_method == DITHER_ORDERED:
        # Ordered (Bayer) dithering: add threshold matrix, clamp, quantize
        bayer_tiled = np.tile(_BAYER_4x4_SCALED,
                              ((H + 3) // 4, (W + 3) // 4))[:H, :W]
        rgb_dithered = rgb_q.copy()
        for c in range(3):
            rgb_dithered[:, :, c] = np.clip(rgb_dithered[:, :, c] + bayer_tiled,
                                            0, 255)
        rgb5_d = np.round(rgb_dithered * 31.0 / 255.0).clip(0, 31).astype(np.uint8)
        bgr_grid = (rgb5_d[:, :, 0].astype(np.uint16) |
                    (rgb5_d[:, :, 1].astype(np.uint16) << 5) |
                    (rgb5_d[:, :, 2].astype(np.uint16) << 10))
        tile_pal_map = np.repeat(np.repeat(tile_labels, 8, axis=0), 8, axis=1)
        for p in range(num_palettes):
            mask = tile_pal_map == p
            output[mask] = lut_index[p][bgr_grid[mask]]

    else:
        # Floyd-Steinberg error diffusion (default)
        lut_idx_lists = [lut_index[p].tolist() for p in range(num_palettes)]
        lut_r_lists = [lut_rgb[p, :, 0].tolist() for p in range(num_palettes)]
        lut_g_lists = [lut_rgb[p, :, 1].tolist() for p in range(num_palettes)]
        lut_b_lists = [lut_rgb[p, :, 2].tolist() for p in range(num_palettes)]
        tile_labels_list = tile_labels.tolist()

        err_r = [[0.0] * (W + 2) for _ in range(H + 1)]
        err_g = [[0.0] * (W + 2) for _ in range(H + 1)]
        err_b = [[0.0] * (W + 2) for _ in range(H + 1)]

        src_r = rgb_q[:, :, 0].tolist()
        src_g = rgb_q[:, :, 1].tolist()
        src_b = rgb_q[:, :, 2].tolist()

        for y in range(H):
            xp1 = 1
            tr = y >> 3
            er_row = err_r[y]
            eg_row = err_g[y]
            eb_row = err_b[y]
            er_next = err_r[y + 1]
            eg_next = err_g[y + 1]
            eb_next = err_b[y + 1]
            sr_row = src_r[y]
            sg_row = src_g[y]
            sb_row = src_b[y]

            for x in range(W):
                ex = x + xp1

                ar = sr_row[x] + er_row[ex]
                ag = sg_row[x] + eg_row[ex]
                ab = sb_row[x] + eb_row[ex]

                if ar < 0.0: ar = 0.0
                elif ar > 255.0: ar = 255.0
                if ag < 0.0: ag = 0.0
                elif ag > 255.0: ag = 255.0
                if ab < 0.0: ab = 0.0
                elif ab > 255.0: ab = 255.0

                r5 = int(ar * 31.0 / 255.0 + 0.5)
                g5 = int(ag * 31.0 / 255.0 + 0.5)
                b5 = int(ab * 31.0 / 255.0 + 0.5)
                if r5 > 31: r5 = 31
                if g5 > 31: g5 = 31
                if b5 > 31: b5 = 31
                bgr = r5 | (g5 << 5) | (b5 << 10)

                p = tile_labels_list[tr][x >> 3]

                idx = lut_idx_lists[p][bgr]
                nr = lut_r_lists[p][bgr]
                ng = lut_g_lists[p][bgr]
                nb = lut_b_lists[p][bgr]

                output[y, x] = idx

                qr = ar - nr
                qg = ag - ng
                qb = ab - nb

                er_row[ex + 1] += qr * 0.4375
                eg_row[ex + 1] += qg * 0.4375
                eb_row[ex + 1] += qb * 0.4375
                er_next[ex - 1] += qr * 0.1875
                eg_next[ex - 1] += qg * 0.1875
                eb_next[ex - 1] += qb * 0.1875
                er_next[ex] += qr * 0.3125
                eg_next[ex] += qg * 0.3125
                eb_next[ex] += qb * 0.3125
                er_next[ex + 1] += qr * 0.0625
                eg_next[ex + 1] += qg * 0.0625
                eb_next[ex + 1] += qb * 0.0625

    # Encode to SNES format
    tile_data, tilemap_data = encode_tiles_4bpp(output, tile_labels.astype(np.uint8), W, H)

    # Write palette
    pal_bytes = bytearray(num_palettes * 16 * 2)
    for p in range(num_palettes):
        for ci in range(16):
            bgr = sub_palettes[p][ci]
            offset = (p * 16 + ci) * 2
            pal_bytes[offset] = bgr & 0xFF
            pal_bytes[offset + 1] = (bgr >> 8) & 0xFF

    with open(pal_file, 'wb') as f:
        f.write(pal_bytes)
    with open(tile_file, 'wb') as f:
        f.write(tile_data)
    with open(map_file, 'wb') as f:
        f.write(tilemap_data)


# ---------- High-level conversion ----------
def convert_frame(png_path, num_palettes=MAX_PALETTES, max_tiles=MAX_TILES,
                  dither_method=DEFAULT_DITHER, engine=DEFAULT_ENGINE):
    """Convert one PNG frame to SNES tiles/tilemap/palette.

    Uses per-tile sub-palette optimization with configurable dithering.
    engine='superfamiconv' delegates to the external SuperFamiconv tool.
    Returns (success, error_message).
    """
    base = png_path[:-4]
    pal_file = base + '.palette'
    tile_file = base + '.tiles'
    map_file = base + '.tilemap'

    try:
        if engine == ENGINE_SUPERFAMICONV:
            from superfamiconv import convert_frame_sfc
            convert_frame_sfc(png_path, pal_file, tile_file, map_file,
                              num_palettes=num_palettes, max_tiles=max_tiles)
        else:
            per_tile_palette_optimize(png_path, pal_file, tile_file, map_file,
                                      num_palettes=num_palettes,
                                      dither_method=dither_method)
            # Reduce tiles to fit in VRAM buffer
            reduce_tiles(tile_file, map_file, pal_file, max_tiles=max_tiles)
            # Pad tilemap
            pad_tilemap(map_file)
    except Exception as e:
        return False, str(e)

    return True, ""


def convert_frame_to_bytes(png_path, num_palettes=MAX_PALETTES, max_tiles=MAX_TILES,
                           dither_method=DEFAULT_DITHER, engine=DEFAULT_ENGINE):
    """Convert one PNG frame to SNES data, returning raw bytes.

    Returns (tile_bytes, tilemap_bytes, palette_bytes) on success.
    Raises Exception on failure.
    """
    import tempfile
    import os

    base = os.path.splitext(png_path)[0]
    pal_file = base + '.palette'
    tile_file = base + '.tiles'
    map_file = base + '.tilemap'

    if engine == ENGINE_SUPERFAMICONV:
        from superfamiconv import convert_frame_sfc
        convert_frame_sfc(png_path, pal_file, tile_file, map_file,
                          num_palettes=num_palettes, max_tiles=max_tiles)
    else:
        per_tile_palette_optimize(png_path, pal_file, tile_file, map_file,
                                  num_palettes=num_palettes,
                                  dither_method=dither_method)
        reduce_tiles(tile_file, map_file, pal_file, max_tiles=max_tiles)
        pad_tilemap(map_file)

    with open(tile_file, 'rb') as f:
        tile_bytes = f.read()
    with open(map_file, 'rb') as f:
        map_bytes = f.read()
    with open(pal_file, 'rb') as f:
        pal_bytes = f.read()

    # Clean up intermediate files
    for fp in [tile_file, map_file, pal_file]:
        try:
            os.remove(fp)
        except OSError:
            pass

    return tile_bytes, map_bytes, pal_bytes
