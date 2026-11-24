"""
GH7-inspired UX extras for GH5S V2.5 (`program`).

Currently included (verified against V2.5 disassembly):
  1. Unlock Video Guide Line / Aspect Marker in Open Gate 3:2, Anamorphic 4:3, and 1:1!
     Stock GH5S at 0x06357240..0x06357248 calls GetOsdMovieAspect() (0x0638a9a4) and
     compares against #0x300 (16:9 ONLY), rejecting 4:3 (0x100), 3:2 (0x200), and 1:1 (0x400).
     NOPing the `bne 0x6357258` at 0x06357248 enables the GH5S's Video Guide Line
     (2.39:1 / 2.35:1 / etc.) directly inside Open Gate 3:2 and Anamorphic 4:3!
"""
from __future__ import annotations

from .common import PatchSet

MODULE = "gh7_extras"

# 0x06357240: bl 0x638a9a4 (GetOsdMovieAspect)
# 0x06357244: cmp.w r0, #0x300 (16:9)
# 0x06357248: bne 0x6357258 (0xd106) -> nop (0xbf00)
ADDR_GUIDE_LINE_16X9_GATE = 0x06357248


def apply_gh7_extras(ps: PatchSet) -> None:
    ps.add_u16(
        ADDR_GUIDE_LINE_16X9_GATE,
        0xD106,
        0xBF00,
        "Unlock Video Guide Line / Marker in 3:2 Open Gate & 4:3 Anamorphic",
        MODULE,
    )
