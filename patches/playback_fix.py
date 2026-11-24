"""
Live-verified In-Camera Playback Green Vertical Stripe Fix (42 bytes in `postboot2`).

Root cause (proven empirically in DDR RAM):
  1. Stock DPB[5] (0x07391f2c) placed 10-bit 4:2:2 chroma at +0x00a96800 (3328px stride),
     while GAIA decoded 3744px luma across 0x000000..0x00be1fff (4992 * 2496 B), overwriting
     the first 0x14b800 B (416 columns = 3744 - 3328) of chroma with luma (Cb=Cr=-65 -> green).
  2. Stock StartDec MV pool (0x00110000) overflowed 234*156*32 = 0x0011d300 at y >= 2368.

Fix (100% backward-compatible with stock 3328x2496, 3744x2496, and 2496x2496 clips):
  - StartDec (0x072e2bf8..0x072e3197): MV 0x00110000 -> 0x00120000 (24.00 MiB spare in GaiaWork)
  - DPB[5] (0x07391f2c): 3744x2496, y_sz = c_sz = 0x00be2000, step = 9*0x2d8000 = 0x01998000
    (3 slots = 76.78 MiB <= 114.00 MiB available -> 37.22 MiB spare)
"""
from __future__ import annotations

from .common import PatchSet

MODULE = "playback_fix"

# Exact 19 ARM32/NEON/table u32 patches (21 modified halfwords = 42 bytes), live-verified on GH5S!
PLAYBACK_FIX_U32S: list[tuple[int, int, int, str]] = [
    # StartDec MV buffer: 0x00110000 -> 0x00120000 (4*MV = 0x480000, MV/4 = 0x48000 = 9<<15)
    (0x072E2BF8, 0xE3A03711, 0xE3A03712, "StartDec 4*MV size (0x480000)"),
    (0x072E2C18, 0x02433711, 0x02433712, "StartDec zero r3 (subeq r3, #0x480000)"),
    (0x072E2D28, 0xE2800811, 0xE2800812, "StartDec MV base +0x120000"),
    (0x072E2D94, 0xF2C14451, 0xF2C14452, "StartDec NEON vmov.i32 q10, #0x120000"),
    (0x072E2DC4, 0xE0830203, 0xE0830183, "StartDec MV mul 17->9 (add r0, r3, r3, lsl #3)"),
    (0x072E2DC8, 0xE0811800, 0xE0811880, "StartDec MV shift 16->17 (lsl #17 -> *0x120000)"),
    (0x072E2DD8, 0xE2811811, 0xE2811812, "StartDec MV loop +0x120000"),
    (0x072E2E1C, 0xE2800911, 0xE2800912, "StartDec MV/4 step1 +0x48000"),
    (0x072E2EB8, 0xE0830203, 0xE0830183, "StartDec MV/4 mul 17->9 (add r0, r3, r3, lsl #3)"),
    (0x072E2EBC, 0xE0811700, 0xE0811780, "StartDec MV/4 shift 14->15 (lsl #15 -> *0x48000)"),
    (0x072E2ECC, 0xE2811911, 0xE2811912, "StartDec MV/4 step2 +0x48000"),
    (0x072E3188, 0x00044000, 0x00048000, "StartDec NEON pool[0] MV/4 (0x48000)"),
    (0x072E318C, 0x00044000, 0x00048000, "StartDec NEON pool[1] MV/4 (0x48000)"),
    (0x072E3190, 0x00044000, 0x00048000, "StartDec NEON pool[2] MV/4 (0x48000)"),
    (0x072E3194, 0x00044000, 0x00048000, "StartDec NEON pool[3] MV/4 (0x48000)"),
    # DPB[5] (Dec-only descriptor at 0x07391f2c, verified untouched by Enc)
    (0x07391F2C, 0x09C00D00, 0x09C00EA0, "DPB[5] max WxH (3744x2496)"),
    (0x07391F3C, 0x00A96800, 0x00BE2000, "DPB[5] y_sz = 4992*2496 (0x00be2000)"),
    (0x07391F40, 0x00A96800, 0x00BE2000, "DPB[5] c_sz = 4992*2496 (0x00be2000)"),
    (0x07391F44, 0x00000803, 0x00000903, "DPB[5] dpb_cnt=3, step=9*0x2d8000 (0x01998000)"),
]


def apply_playback_fix(ps: PatchSet) -> None:
    for va, old_v, new_v, desc in PLAYBACK_FIX_U32S:
        ps.add_u32(va, old_v, new_v, desc, MODULE)
