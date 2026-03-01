#!/usr/bin/env python3
"""
Push a ROM to FXPAK Pro via QUsb2Snes and optionally boot it.

Usage:
    python tools/fxpak_push.py                          # push distribution/SNESVideoPlayer.sfc, boot it
    python tools/fxpak_push.py --file path/to/rom.sfc   # push a specific file
    python tools/fxpak_push.py --no-boot                # upload only, don't boot
    python tools/fxpak_push.py --dest /games/my.sfc     # custom SD card destination

Requires: QUsb2Snes running and FXPAK connected via USB.
"""

import asyncio
import json
import os
import sys

try:
    import websockets
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

WS_URL = "ws://localhost:23074"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_ROM = os.path.join(PROJECT_ROOT, "prebuilt", "SNESVideoPlayer.sfc")
DEFAULT_DEST = "/MoviePlayer/SNESVideoPlayer.sfc"


async def push_and_boot(rom_path, dest_path, boot=True):
    if not os.path.isfile(rom_path):
        print(f"ERROR: ROM not found: {rom_path}")
        return False

    rom_size = os.path.getsize(rom_path)
    print(f"ROM: {rom_path} ({rom_size} bytes)")
    print(f"Dest: {dest_path}")
    print(f"Connecting to QUsb2Snes at {WS_URL}...")

    try:
        async with websockets.connect(WS_URL) as ws:
            # DeviceList
            await ws.send(json.dumps({"Opcode": "DeviceList", "Space": "SNES"}))
            resp = json.loads(await ws.recv())
            if not resp.get("Results"):
                print("ERROR: No devices found. Is FXPAK connected?")
                return False
            device = resp["Results"][0]
            print(f"Device: {device}")

            # Attach
            await ws.send(json.dumps({
                "Opcode": "Attach",
                "Space": "SNES",
                "Operands": [device]
            }))
            await asyncio.sleep(0.5)

            # PutFile
            print(f"Uploading ROM to SD card...")
            with open(rom_path, "rb") as f:
                rom_data = f.read()

            await ws.send(json.dumps({
                "Opcode": "PutFile",
                "Space": "SNES",
                "Operands": [dest_path, format(rom_size, "x")]
            }))

            # Send binary data in 4096-byte chunks
            offset = 0
            chunk_size = 4096
            while offset < len(rom_data):
                chunk = rom_data[offset:offset + chunk_size]
                await ws.send(chunk)
                offset += len(chunk)
                pct = min(100, offset * 100 // len(rom_data))
                print(f"\r  Uploaded {offset}/{len(rom_data)} bytes ({pct}%)", end="", flush=True)
            print()

            # Wait a moment for the write to flush
            await asyncio.sleep(1.0)
            print("Upload complete.")

            if boot:
                print(f"Booting {dest_path}...")
                await ws.send(json.dumps({
                    "Opcode": "Boot",
                    "Space": "SNES",
                    "Operands": [dest_path]
                }))
                await asyncio.sleep(0.5)
                print("Boot command sent.")

            return True

    except ConnectionRefusedError:
        print("ERROR: Cannot connect to QUsb2Snes.")
        print("Make sure QUsb2Snes.exe is running and FXPAK is connected via USB.")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Push ROM to FXPAK Pro via QUsb2Snes")
    parser.add_argument("--file", "-f", default=DEFAULT_ROM,
                        help=f"ROM file to upload (default: {DEFAULT_ROM})")
    parser.add_argument("--dest", "-d", default=DEFAULT_DEST,
                        help=f"SD card destination path (default: {DEFAULT_DEST})")
    parser.add_argument("--no-boot", action="store_true",
                        help="Upload only, don't boot the ROM")
    args = parser.parse_args()

    success = asyncio.run(push_and_boot(args.file, args.dest, boot=not args.no_boot))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
