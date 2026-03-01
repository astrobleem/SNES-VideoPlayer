#!/usr/bin/env python3
"""
FXPAK Pro USB debugger for SNES-VideoPlayer - reads SNES memory via QUsb2Snes.
Dumps OOP stack, allocation tables, exception state for crash diagnosis.

Usage: python tools/fxpak_debug.py
Requires: QUsb2Snes running and FXPAK connected via USB.
"""

import asyncio
import struct
import sys
import os

try:
    import websockets
except ImportError:
    print("ERROR: pip install websockets")
    sys.exit(1)

WS_URL = "ws://localhost:23074"

# Sym file path (relative to project root)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SYM_FILE = os.path.join(PROJECT_ROOT, 'rom', 'build', 'SNESVideoPlayer.sym')

def snes_to_usb(snes_addr):
    if snes_addr >= 0x7E0000:
        return 0xF50000 + (snes_addr - 0x7E0000)
    if snes_addr < 0x2000:
        return 0xF50000 + snes_addr
    raise ValueError(f"Cannot map SNES address ${snes_addr:06X}")

# WRAM addresses from SNESVideoPlayer.sym
ADDR = {
    'OopStack':             0x7E6988,
    'VRAM_alloc_id':        0x7E6E8F,
    'VRAM_alloc_blocks':    0x7E6E9A,
    'HdmaSpcBuffer':        0x7E6F9A,
    'DMA_QUEUE_start':      0x7E709A,
    'CGRAM_alloc_id':       0x7E711F,
    'CGRAM_alloc_blocks':   0x7E712C,
    'WRAM_alloc_id':        0x7E716C,
    'WRAM_alloc_blocks':    0x7E7175,
    'currentObject':        0x7E7211,
    'currentMethod':        0x7E7213,
    'currentClass':         0x7E7215,
    'currentObjectStr':     0x7E7217,
    'currentMethodStr':     0x7E721A,
    'currentClassStr':      0x7E721D,
    'excStack':             0x001993,
    'excA':                 0x001995,
    'excY':                 0x001997,
    'excX':                 0x001999,
    'excDp':                0x00199B,
    'excDb':                0x00199D,
    'excPb':                0x00199E,
    'excFlags':             0x00199F,
    'excPc':                0x0019A0,
    'excErr':               0x0019A2,
    'excArgs':              0x0019A4,
    'crashSP':              0x0019AC,
    'crashPC':              0x0019AE,
    'crashPB':              0x0019B0,
    'crashP':               0x0019B1,
    'crashA':               0x0019B2,
    'crashX':               0x0019B4,
    'crashY':               0x0019B6,
    'crashDP':              0x0019B8,
    'crashTmp':             0x0019BA,
    'fpExpectedId':         0x0019BC,
    'fpExpectedNum':        0x0019BE,
    'fpActualId':           0x0019C0,
    'fpActualNum':          0x0019C2,
    'fpSlotIndex':          0x0019C4,
    'fpCrashSP':            0x0019C6,
    'OopObjRam':            0x000010,
}

ERROR_NAMES = {
    10: 'E_ObjLstFull', 11: 'E_ObjRamFull', 12: 'E_StackTrash',
    13: 'E_Brk', 14: 'E_StackOver',
    15: 'E_Sa1IramCode', 16: 'E_Sa1IramClear', 17: 'E_Sa1Test',
    18: 'E_Sa1NoIrq', 19: 'E_Todo', 20: 'E_SpcTimeout',
    21: 'E_ObjBadHash', 22: 'E_ObjBadMethod', 23: 'E_BadScript',
    24: 'E_StackUnder', 25: 'E_Cop', 26: 'E_ScriptStackTrash',
    27: 'E_UnhandledIrq',
    28: 'E_Sa1BWramClear', 29: 'E_Sa1NoBWram', 30: 'E_Sa1BWramToSmall',
    31: 'E_Sa1DoubleIrq', 32: 'E_SpcNoStimulusCallback',
    33: 'E_Msu1NotPresent', 34: 'E_Msu1FileNotPresent',
    35: 'E_Msu1SeekTimeout', 36: 'E_Msu1InvalidFrameRequested',
    37: 'E_DmaQueueFull', 38: 'E_InvalidDmaTransferType',
    39: 'E_InvalidDmaTransferLength',
    40: 'E_VallocBadStepsize', 41: 'E_VallocEmptyDeallocation',
    42: 'E_UnitTestComplete', 43: 'E_UnitTestFail',
    44: 'E_VallocInvalidLength',
    45: 'E_CGallocInvalidLength', 46: 'E_CGallocBadStepsize',
    47: 'E_CGallocInvalidStart', 48: 'E_CGallocEmptyDeallocation',
    49: 'E_ObjNotFound', 50: 'E_BadParameters',
    51: 'E_OutOfVram', 52: 'E_OutOfCgram', 53: 'E_InvalidException',
    54: 'E_Msu1InvalidFrameCycle', 55: 'E_Msu1InvalidChapterRequested',
    56: 'E_Msu1InvalidChapter',
    57: 'E_Msu1AudioSeekTimeout', 58: 'E_Msu1AudioPlayError',
    59: 'E_ObjStackCorrupted', 60: 'E_BadEventResult',
    61: 'E_abstractClass',
    62: 'E_NoChapterFound', 63: 'E_NoCheckpointFound',
    64: 'E_BadSpriteAnimation',
    65: 'E_AllocatedVramExceeded', 66: 'E_AllocatedCgramExceeded',
    67: 'E_InvalidDmaChannel', 68: 'E_DmaChannelEmpty', 69: 'E_NoDmaChannel',
    70: 'E_VideoMode', 71: 'E_BadBgAnimation', 72: 'E_BadBgLayer',
    73: 'E_NtscUnsupported',
    74: 'E_WallocBadStepsize', 75: 'E_WallocEmptyDeallocation', 76: 'E_OutOfWram',
    77: 'E_BadInputDevice', 78: 'E_ScoreTest',
    79: 'E_Msu1FrameBad', 80: 'E_BadIrq', 81: 'E_NoIrqCallback',
    82: 'E_BadIrqCallback', 83: 'E_SramBad',
}

OOP_SLOT_SIZE = 16
OOP_NUM_SLOTS = 48


async def read_memory(ws, snes_addr, size):
    usb_addr = snes_to_usb(snes_addr)
    cmd = {
        "Opcode": "GetAddress",
        "Space": "SNES",
        "Operands": [format(usb_addr, 'X'), format(size, 'X')]
    }
    await ws.send(str(cmd).replace("'", '"'))
    data = b""
    while len(data) < size:
        chunk = await ws.recv()
        if isinstance(chunk, str):
            print(f"  Unexpected text response: {chunk}")
            break
        data += chunk
    return data[:size]


def parse_oop_slot(data, slot_num):
    if len(data) < 16:
        return None
    return {
        'slot': slot_num,
        'flags': data[0],
        'id': data[1],
        'num': struct.unpack_from('<H', data, 2)[0],
        'void': struct.unpack_from('<H', data, 4)[0],
        'properties': struct.unpack_from('<H', data, 6)[0],
        'dp': struct.unpack_from('<H', data, 8)[0],
        'init': struct.unpack_from('<H', data, 10)[0],
        'play': struct.unpack_from('<H', data, 12)[0],
        'kill': struct.unpack_from('<H', data, 14)[0],
    }


def format_properties(props):
    names = []
    if props & 0x0001: names.append('isScript')
    if props & 0x0002: names.append('isChapter')
    if props & 0x0004: names.append('isEvent')
    if props & 0x0008: names.append('isHdma')
    if props & 0x0010: names.append('isSprite')
    if props & 0x0020: names.append('isBackground')
    if props & 0x0040: names.append('isAnimation')
    if props & 0x0080: names.append('isCheckpoint')
    if props & 0x1000: names.append('isSerializable')
    return '|'.join(names) if names else f'${props:04X}'


def format_flags(flags):
    names = []
    if flags & 0x80: names.append('Present')
    if flags & 0x08: names.append('DeleteScheduled')
    if flags & 0x04: names.append('InitOk')
    if flags & 0x02: names.append('Persistent')
    if flags & 0x01: names.append('Singleton')
    return '|'.join(names) if names else 'None'


def format_p_register(p):
    flags = []
    if p & 0x80: flags.append('N')
    if p & 0x40: flags.append('V')
    if p & 0x20: flags.append('M(8bit-A)')
    if p & 0x10: flags.append('X(8bit-XY)')
    if p & 0x08: flags.append('D')
    if p & 0x04: flags.append('I')
    if p & 0x02: flags.append('Z')
    if p & 0x01: flags.append('C')
    return '|'.join(flags) if flags else 'none'


def load_class_names():
    names = {}
    try:
        with open(SYM_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if 'OBJID.' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        val = int(parts[0].split(':')[1], 16)
                        class_name = parts[1].replace('OBJID.', '')
                        names[val] = class_name
    except Exception as e:
        print(f"  Warning: Could not load class names: {e}")
    return names


def load_kernel_zp():
    try:
        with open(SYM_FILE, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == 'ZP':
                    return int(parts[0].split(':')[1], 16)
    except Exception:
        pass
    return None


def load_sym_addresses():
    syms = {}
    try:
        with open(SYM_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(';') or line.startswith('['):
                    continue
                parts = line.split()
                if len(parts) >= 2 and ':' in parts[0]:
                    try:
                        addr = int(parts[0].split(':')[1], 16)
                        syms[addr] = parts[1]
                    except ValueError:
                        pass
    except Exception:
        pass
    return syms


def load_method_names():
    methods = {}
    try:
        with open(SYM_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if '.MTD' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        val = int(parts[0].split(':')[1], 16)
                        base = parts[1].replace('.MTD', '')
                        dot_idx = base.rfind('.')
                        if dot_idx > 0:
                            class_name = base[:dot_idx]
                            method_name = base[dot_idx+1:]
                            if class_name not in methods:
                                methods[class_name] = {}
                            methods[class_name][val] = method_name
    except Exception:
        pass
    return methods


async def main():
    class_names = load_class_names()
    method_names = load_method_names()
    kernel_zp = load_kernel_zp()
    sym_addrs = load_sym_addresses()
    print(f"Loaded {len(class_names)} class name mappings from {SYM_FILE}")
    if kernel_zp is not None:
        print(f"Kernel ZP base: ${kernel_zp:04X}")
    print(f"Connecting to QUsb2Snes at {WS_URL}...")

    try:
        async with websockets.connect(WS_URL) as ws:
            # List & attach
            await ws.send('{"Opcode":"DeviceList","Space":"SNES"}')
            resp = await ws.recv()
            print(f"Devices: {resp}")

            import json
            devices = json.loads(resp)
            if not devices.get('Results'):
                print("ERROR: No devices found. Is FXPAK connected?")
                return
            device = devices['Results'][0]
            print(f"Attaching to: {device}")

            await ws.send(f'{{"Opcode":"Attach","Space":"SNES","Operands":["{device}"]}}')
            await asyncio.sleep(0.5)

            await ws.send('{"Opcode":"Info","Space":"SNES"}')
            info = await ws.recv()
            print(f"Device info: {info}")

            print("\n" + "="*80)
            print("  FXPAK CRASH STATE DUMP — SNES-VideoPlayer")
            print("="*80)

            # ========== EXCEPTION STATE ==========
            print("\n--- EXCEPTION STATE ---")
            exc_data = await read_memory(ws, ADDR['excStack'], 0x20)
            exc_stack = struct.unpack_from('<H', exc_data, 0)[0]
            exc_a     = struct.unpack_from('<H', exc_data, 2)[0]
            exc_y     = struct.unpack_from('<H', exc_data, 4)[0]
            exc_x     = struct.unpack_from('<H', exc_data, 6)[0]
            exc_dp    = struct.unpack_from('<H', exc_data, 8)[0]
            exc_db    = exc_data[10]
            exc_pb    = exc_data[11]
            exc_flags = exc_data[12]
            exc_pc    = struct.unpack_from('<H', exc_data, 13)[0]
            exc_err   = struct.unpack_from('<H', exc_data, 15)[0]
            exc_args  = exc_data[17:25]

            err_name = ERROR_NAMES.get(exc_err & 0xFF, f'Unknown(${exc_err:04X})')
            print(f"  Error code:     {exc_err} = {err_name}")
            print(f"  TRIGGER_ERROR PC: ${exc_pc:04X}")
            print(f"  CPU at crash:   A=${exc_a:04X} X=${exc_x:04X} Y=${exc_y:04X}")
            print(f"  Direct Page:    DP=${exc_dp:04X}")
            print(f"  Banks:          DB=${exc_db:02X} PB=${exc_pb:02X}")
            print(f"  Flags (P):      ${exc_flags:02X} = {format_p_register(exc_flags)}")
            print(f"  Stack at crash: SP=${exc_stack:04X}")
            print(f"  excArgs:        {' '.join(f'{b:02X}' for b in exc_args)}")

            # BRK/COP diagnostics
            if (exc_err & 0xFF) in (13, 25):
                brk_p = exc_args[0]
                brk_pc_plus2 = exc_args[1] | (exc_args[2] << 8)
                brk_pbr = exc_args[3]
                brk_pc = (brk_pc_plus2 - 2) & 0xFFFF
                kind = 'BRK' if (exc_err & 0xFF) == 13 else 'COP'
                print(f"\n  *** {kind} CRASH LOCATION ***")
                print(f"  {kind} instruction at: ${brk_pbr:02X}:{brk_pc:04X}")
                print(f"  {kind} P register: ${brk_p:02X} = {format_p_register(brk_p)}")

                crash_data = await read_memory(ws, ADDR['crashSP'], 16)
                crash_sp = struct.unpack_from('<H', crash_data, 0)[0]
                crash_pc = struct.unpack_from('<H', crash_data, 2)[0]
                crash_pb = crash_data[4]
                crash_p  = crash_data[5]
                crash_a  = struct.unpack_from('<H', crash_data, 6)[0]
                crash_x  = struct.unpack_from('<H', crash_data, 8)[0]
                crash_y  = struct.unpack_from('<H', crash_data, 10)[0]
                crash_dp_val = struct.unpack_from('<H', crash_data, 12)[0]
                crash_tmp = struct.unpack_from('<H', crash_data, 14)[0]
                pre_brk_sp = (crash_sp + 4) & 0xFFFF
                crash_pc_actual = (crash_pc - 2) & 0xFFFF

                print(f"\n  *** {kind} CRASH DIAGNOSTICS ***")
                print(f"  Crash at:         ${crash_pb:02X}:{crash_pc_actual:04X}")
                print(f"  Pre-{kind} SP:      ${pre_brk_sp:04X}")
                print(f"  Crash P:          ${crash_p:02X} = {format_p_register(crash_p)}")
                print(f"  Crash regs:       A=${crash_a:04X} X=${crash_x:04X} Y=${crash_y:04X}")
                print(f"  Crash DP:         ${crash_dp_val:04X}")
                print(f"  Kernel ZP tmp:    ${crash_tmp:04X}")

                if kernel_zp is not None and crash_dp_val == kernel_zp:
                    print(f"  DP analysis:      DP = kernel ZP -> crash in dispatch code")
                elif 0x0010 <= crash_dp_val < 0x1810:
                    print(f"  DP analysis:      DP in OOP ZP pool -> crash inside object method")

                sym_name = sym_addrs.get(crash_pc_actual, None)
                if sym_name:
                    print(f"  Symbol:           {sym_name}")
                sym_tmp = sym_addrs.get(crash_tmp, None)
                if sym_tmp:
                    print(f"  tmp symbol:       {sym_tmp}")

            # E_ObjStackCorrupted diagnostics
            if (exc_err & 0xFF) == 59:
                fp_data = await read_memory(ws, ADDR['fpExpectedId'], 12)
                fp_exp_id   = struct.unpack_from('<H', fp_data, 0)[0]
                fp_exp_num  = struct.unpack_from('<H', fp_data, 2)[0]
                fp_act_id   = struct.unpack_from('<H', fp_data, 4)[0]
                fp_act_num  = struct.unpack_from('<H', fp_data, 6)[0]
                fp_slot_idx = struct.unpack_from('<H', fp_data, 8)[0]
                fp_crash_sp = struct.unpack_from('<H', fp_data, 10)[0]
                slot_num = fp_slot_idx // OOP_SLOT_SIZE
                exp_name = class_names.get(fp_exp_id & 0xFF, f'?${fp_exp_id:02X}')
                act_name = class_names.get(fp_act_id & 0xFF, f'?${fp_act_id:02X}')
                print(f"\n  *** E_ObjStackCorrupted FINGERPRINT ***")
                print(f"  Slot offset X=${fp_slot_idx:04X} (slot #{slot_num})")
                print(f"  Expected: id=${fp_exp_id:04X}({exp_name}) num=${fp_exp_num:04X}")
                print(f"  Actual:   id=${fp_act_id:04X}({act_name}) num=${fp_act_num:04X}")

            # ========== OOP DISPATCH STATE ==========
            print("\n--- OOP DISPATCH STATE ---")
            dispatch_data = await read_memory(ws, ADDR['currentObject'], 18)
            cur_object = struct.unpack_from('<H', dispatch_data, 0)[0]
            cur_method = struct.unpack_from('<H', dispatch_data, 2)[0]
            cur_class  = struct.unpack_from('<H', dispatch_data, 4)[0]

            obj_name = class_names.get(cur_object & 0xFF, f'?${cur_object:02X}')
            cls_name = class_names.get(cur_class & 0xFF, f'?${cur_class:02X}')

            meth_name = {0: 'init', 1: 'play', 2: 'kill'}.get(cur_method, f'method#{cur_method}')
            cls_methods = method_names.get(obj_name, {})
            if cur_method in cls_methods:
                meth_name = cls_methods[cur_method]

            print(f"  Last dispatched: {cls_name}::{meth_name}()")
            print(f"  currentObject = ${cur_object:04X} ({obj_name})")
            print(f"  currentClass  = ${cur_class:04X} ({cls_name})")
            print(f"  currentMethod = ${cur_method:04X} ({meth_name})")

            # ========== OOP STACK ==========
            print("\n--- OOP STACK (48 slots) ---")
            oop_data = await read_memory(ws, ADDR['OopStack'], OOP_SLOT_SIZE * OOP_NUM_SLOTS)
            active_count = 0
            for i in range(OOP_NUM_SLOTS):
                offset = i * OOP_SLOT_SIZE
                slot = parse_oop_slot(oop_data[offset:offset+OOP_SLOT_SIZE], i+1)
                if slot and slot['flags'] != 0:
                    active_count += 1
                    cname = class_names.get(slot['id'], f'?${slot["id"]:02X}')
                    print(f"  Slot {slot['slot']:2d}: flags={format_flags(slot['flags']):28s} "
                          f"id=${slot['id']:02X}({cname:30s}) "
                          f"props={format_properties(slot['properties']):20s} "
                          f"dp=${slot['dp']:04X} "
                          f"init=${slot['init']:04X} play=${slot['play']:04X} kill=${slot['kill']:04X}")
            print(f"  Active slots: {active_count}/{OOP_NUM_SLOTS}")

            # ========== VRAM ALLOCATION ==========
            print("\n--- VRAM ALLOCATION TABLE ---")
            vram_id_data = await read_memory(ws, ADDR['VRAM_alloc_id'], 1)
            vram_data = await read_memory(ws, ADDR['VRAM_alloc_blocks'], 256)
            print(f"  currentVramAllocationId = ${vram_id_data[0]:02X}")
            used_blocks = [(i, b) for i, b in enumerate(vram_data) if b != 0]
            if used_blocks:
                groups = []
                current_id, start, prev_idx = None, None, 0
                for idx, bid in used_blocks:
                    if bid != current_id:
                        if current_id is not None:
                            groups.append((start, prev_idx, current_id))
                        current_id, start = bid, idx
                    prev_idx = idx
                if current_id is not None:
                    groups.append((start, prev_idx, current_id))
                for gs, ge, gid in groups:
                    print(f"    blocks {gs:3d}-{ge:3d} (VRAM ${gs*0x100:04X}-${(ge+1)*0x100:04X}): id=${gid:02X}")
            else:
                print("  All blocks free")

            # ========== DMA QUEUE ==========
            print("\n--- DMA QUEUE STATE ---")
            dma_data = await read_memory(ws, ADDR['DMA_QUEUE_start'], 133)
            dma_slot_ptr = dma_data[0]
            print(f"  currentDmaQueueSlot = ${dma_slot_ptr:02X}")
            for i in range(16):
                slot_off = 5 + i * 8
                if slot_off + 8 <= len(dma_data):
                    xfer_type = dma_data[slot_off + 4]
                    if xfer_type & 0x40:
                        xfer_len = struct.unpack_from('<H', dma_data, slot_off)[0]
                        tgt_addr = struct.unpack_from('<H', dma_data, slot_off + 2)[0]
                        src_lo = struct.unpack_from('<H', dma_data, slot_off + 5)[0]
                        src_hi = dma_data[slot_off + 7]
                        type_name = {0: 'VRAM', 1: 'OAM', 2: 'CGRAM'}.get(xfer_type & 0x1F, f'?${xfer_type & 0x1F:02X}')
                        print(f"  Slot {i:2d}: ACTIVE {type_name} src=${src_hi:02X}:{src_lo:04X} "
                              f"tgt=${tgt_addr:04X} len=${xfer_len:04X}")

            # ========== STACK PAGE ==========
            print("\n--- STACK PAGE ($0100-$01FF) ---")
            stack_data = await read_memory(ws, 0x0100, 256)
            sp_guess = 255
            while sp_guess > 0 and stack_data[sp_guess] == 0:
                sp_guess -= 1
            print(f"  Apparent stack top: ~$01{sp_guess+1:02X}")
            start_row = max(0, sp_guess - 31)
            end_row = min(256, sp_guess + 17)
            for row_start in range(start_row, end_row, 16):
                row_end = min(row_start + 16, 256)
                hex_str = ' '.join(f'{stack_data[i]:02X}' for i in range(row_start, row_end))
                ascii_str = ''.join(chr(stack_data[i]) if 32 <= stack_data[i] < 127 else '.' for i in range(row_start, row_end))
                print(f"  ${0x0100 + row_start:04X}: {hex_str}  {ascii_str}")

            # ========== DIRECT PAGE of crashed object ==========
            if 0x0010 <= exc_dp < 0x1810:
                print(f"\n--- DIRECT PAGE at DP=${exc_dp:04X} (108 bytes) ---")
                zp_data = await read_memory(ws, exc_dp, 108)
                for row_start in range(0, 108, 16):
                    row_end = min(row_start + 16, 108)
                    hex_str = ' '.join(f'{zp_data[i]:02X}' for i in range(row_start, row_end))
                    print(f"  ${exc_dp + row_start:04X}: {hex_str}")

            # ========== SUMMARY ==========
            print("\n" + "="*80)
            print("  CRASH SUMMARY")
            print("="*80)
            print(f"  Error: {err_name} (code {exc_err})")
            print(f"  Last dispatched: {cls_name}::{meth_name}()")
            print(f"  CPU: A=${exc_a:04X} X=${exc_x:04X} Y=${exc_y:04X} DP=${exc_dp:04X} SP=${exc_stack:04X}")
            print(f"  Active OOP objects: {active_count}/{OOP_NUM_SLOTS}")
            print(f"  VRAM alloc blocks: {len(used_blocks)}/256")

    except ConnectionRefusedError:
        print("ERROR: Cannot connect to QUsb2Snes.")
        print("Make sure QUsb2Snes.exe is running and FXPAK is connected.")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())
