"""
Native 16-bit / 24-bit LPCM Menu Toggle without DMW-XLR1 (`program` only).

UX & Safety Architecture:
  1. Un-greys the GH5S's existing multilingual `XLR Mic Adaptor Setting` menu item (0x8e)
     at 0x062721b6 WITHOUT spoofing DMW-XLR1 hardware detection (0x0634b8f6 stays stock):
       - [0x0837369c] stays 0 when no XLR1 is attached -> Mic Level, Wind Cut, Limiter,
         Audio Level Display, Internal Mic & 3.5mm Jack all stay 100% enabled!
       - 96kHz/24bit (choice 3) stays cleanly greyed out without a real XLR1 (0x0634b8d2).
       - Changing the menu saves persistently to EEPROM [0x05fcf000 + 0x25c] and triggers
         Panasonic's own guarded LiveView AudioReconfig (0x062e2e6e -> 0x061b0d80).
  2. GetAudioBitDepth (0x063e6b0a):
       - Non-LPCM (AAC/AC3): always 0 (untouched).
       - Real XLR1 attached ([0x0837369c] in {2,3}): 1 (100% factory stock XLR1, incl. 96kHz).
       - No XLR1: reads [0x05fcf25c] -> 24-bit ONLY when set to 48kHz/24bit (2) AND in MOV
         container ([0x08373512] == 6); OFF (0) or 48kHz/16bit (1) returns 0 (16-bit stock).
  3. Conditional 2ch DMA & TDM Crossbar Router (ONLY active when is_24bit == 1):
       - 16-bit selected -> 100% factory stock 4ch DMA / ZNC (Wind Cut) / port 4!
       - 24-bit selected -> proven 2ch DMA + ZNC 2ch bypass + conditional TDM crossbar
         (internal mic port 3; 3.5mm ext_type 3 / plug-in power port 7 slots 2,3;
          TRS/LINE IN port 7 slots 0,1; real XLR1 port 7 slots 0x14,0x15).
"""
from __future__ import annotations

import struct

from .asm import ThumbBuilder, thumb_b_w, thumb_bl
from .common import G_LPCM_CODE_BASE, PatchSet

MODULE = "lpcm24_menu"

# Verified stock V2.5 addresses in `program`
ADDR_MENU_8E_BL = 0x062721B6       # bl 0x0634b8f6 (f0d9 fb9e)
ADDR_XLR_CHOICE_OK = 0x0634B8D2    # cmp r1, #3; bne 0x634b8e8 (2903 d108)
ADDR_SET_XLR_TAIL = 0x0635742E     # b.w 0x06352988 (f7fb baab)
ADDR_GET_BITDEPTH = 0x063E6B12     # ldr r1, [pc, #0x3e4]; ldrb r1, [r1] (49f9 7809)
ADDR_GET_DMA_CH = 0x0614DF4E       # bl 0x0614dcfa (f7ff fed4) — replaces single 32-bit insn, avoids tbb[1]/bhs -> 0x614df62!
ADDR_REC_START_4CH = 0x062F2208    # movs r0, #0; cbnz r1, 0x62f220e (2000 b901)
ADDR_ZNC_START_BL = 0x061A05D6     # bl 0x0614dcfa (f7ad fb90)
ADDR_PATHSET_PORT4 = 0x06339D84    # cbz r4, 0x6339d92; movs r0, #20 (b12c 2014) — avoids b 0x6339d94 from 0x6339d82!

# Stock helper functions & state addresses
FN_IS_XLR_ATTACHED = 0x0628239E
FN_RECALC_SETTINGS = 0x06352988
FN_IS_AUDIO_LV_ON = 0x062E2E6E
FN_AUDIO_RECONFIG = 0x061B0D80
FN_GET_AUDIO_BITDEPTH = 0x063E6B0A
FN_GET_MIC_MODE = 0x0614DCFA

RAM_XLR_EFF_FMT = 0x0837369C       # u8: effective XLR format (0 if no physical XLR1)
EEP_XLR_SETTING = 0x05FCF25C       # u8: persistent user setting [0x05fcf000 + 0x25c]
RAM_CONTAINER_FMT = 0x08373512     # u8: 6 == MOV container


def build_lpcm24_cave(base_va: int = G_LPCM_CODE_BASE) -> tuple[bytes, dict[str, int]]:
    """Assemble all 7 Thumb-2 cave handlers for native 16/24-bit LPCM menu toggle."""
    tb = ThumbBuilder(base_va)

    # 1) fn_xlr_choice_ok: called from 0x0634b8d2 (r1=choice != 2, r0=1, lr live)
    tb.align4()
    tb.label("xlr_choice_ok")
    tb.cmp_imm(1, 3)
    tb.b("choice_is_96k", cond="eq")
    tb.b_w(0x0634B8E8)                 # choices 0, 1 -> return r0=1
    tb.label("choice_is_96k")
    tb.push([1], lr=True)
    tb.bl(FN_IS_XLR_ATTACHED)          # r0 = IsXlr1Attached()
    tb.pop([1], lr=True)
    tb.cbz(0, "choice_deny_96k")
    tb.movs(0, 1)                      # restore r0=1 expected by 0x0634b8d6
    tb.b_w(0x0634B8D6)                 # run stock MOV/prohibit check
    tb.label("choice_deny_96k")
    tb.movs(0, 0)                      # grey out 96kHz/24bit when no XLR1
    tb.bx_lr()

    # 2) fn_set_xlr_post: tail-called from 0x0635742e (after saving [0x05fcf25c])
    tb.align4()
    tb.label("set_xlr_post")
    tb.push([4], lr=True)
    tb.bl(FN_RECALC_SETTINGS)          # stock dependent setting recalculation
    tb.mov_reg(4, 0)                   # preserve return value in r4
    tb.bl(FN_IS_AUDIO_LV_ON)           # Panasonic's own guard (0x062cbf68)
    tb.cbz(0, "set_xlr_done")
    tb.bl(FN_AUDIO_RECONFIG)           # immediately reconfigure LiveView audio 16<->24bit!
    tb.label("set_xlr_done")
    tb.mov_reg(0, 4)
    tb.pop([4], pc=True)

    # 3) fn_get_bitdepth: called from 0x063e6b12 (codec==1 LPCM already verified, r0==0)
    tb.align4()
    tb.label("get_bitdepth")
    tb.mov32(1, RAM_XLR_EFF_FMT)       # real XLR1 effective format
    tb.ldrb(1, 1, 0)
    tb.cmp_imm(1, 2)
    tb.b("ret_24bit", cond="eq")
    tb.cmp_imm(1, 3)
    tb.b("ret_24bit", cond="eq")
    # No physical XLR1 24-bit -> check persistent user choice [0x05fcf25c]
    tb.mov32(1, EEP_XLR_SETTING)
    tb.ldrb(1, 1, 0)
    tb.cmp_imm(1, 2)
    tb.b("chk_mov_container", cond="eq")
    tb.cmp_imm(1, 3)
    tb.b("ret_16bit", cond="ne")
    tb.label("chk_mov_container")
    tb.mov32(1, RAM_CONTAINER_FMT)     # only enable 24-bit in MOV (container == 6)
    tb.ldrb(1, 1, 0)
    tb.cmp_imm(1, 6)
    tb.b("ret_16bit", cond="ne")
    tb.label("ret_24bit")
    tb.b_w(0x063E6B1E)                 # movs r0, #1; b 0x63e69dc
    tb.label("ret_16bit")
    tb.b_w(0x063E6B20)                 # b 0x63e69dc (r0 == 0)

    # 4) fn_get_dma_ch: called from 0x0614df4e (replaces 32-bit `bl GetMicInputMode`, r4==2, {r4,lr} on stack).
    #    Safe against tbb[1] & bhs -> 0x0614df62 jumps (which previously collided with a 32-bit b.w at 0x0614df60!).
    tb.align4()
    tb.label("get_dma_ch")
    tb.bl(FN_GET_MIC_MODE)             # r0 = GetMicInputMode()
    tb.cmp_imm(0, 6)
    tb.b("dma_ret_2ch", cond="hs")     # >= 6 (LINE IN / no mic) -> stock 2ch (r4==2)
    tb.cmp_imm(0, 1)
    tb.b("dma_ret_2ch", cond="eq")     # mode 1 (2ch TRS / mic) -> stock 2ch (r4==2)
    tb.movs(0, 1)                      # codec = 1 (LPCM)
    tb.bl(FN_GET_AUDIO_BITDEPTH)
    tb.cbz(0, "dma_ret_2ch", nonzero=True)
    tb.movs(4, 4)                      # 16-bit modes 0, 2..5 -> stock 4ch (preserves Wind Cut ZNC)
    tb.label("dma_ret_2ch")
    tb.mov_reg(0, 4)
    tb.b_w(0x0614DF64)                 # pop {r4, pc}

    # 5) fn_rec_start_2ch: called from 0x062f2208 (r1=mic_mode, [r5,#4]=bitdepth)
    tb.align4()
    tb.label("rec_start_2ch")
    tb.movs(0, 0)
    tb.ldr(2, 5, 4)                    # r2 = bitdepth (0=16b, 1=24b)
    tb.cbz(2, "rec_done", nonzero=True)  # 24-bit -> force r0=0 (2ch DMA)
    tb.cbz(1, "rec_set_4ch")           # 16-bit mode 0 -> stock 4ch DMA
    tb.subs_imm3(1, 1, 2)
    tb.cmp_imm(1, 3)
    tb.b("rec_done", cond="hi")
    tb.label("rec_set_4ch")
    tb.movs(0, 1)                      # 16-bit modes 0, 2..5 -> stock 4ch DMA
    tb.label("rec_done")
    tb.b_w(0x062F2216)

    # 6) fn_znc_start_bypass: called via `bl` from 0x061a05d6 (replaces bl GetMicInputMode)
    tb.align4()
    tb.label("znc_start_bypass")
    tb.push([1], lr=True)              # 8-byte aligned + architectural 2-reg pop.w
    tb.movs(0, 1)
    tb.bl(FN_GET_AUDIO_BITDEPTH)
    tb.cbz(0, "znc_ret_bypass", nonzero=True)
    tb.pop([1], lr=True)
    tb.b_w(FN_GET_MIC_MODE)            # 16-bit -> stock GetMicInputMode()
    tb.label("znc_ret_bypass")
    tb.pop([1], pc=True)               # 24-bit -> returns r0=1 (bypasses 4ch ZNC)

    # 7) fn_pathset_router: called from 0x06339d84 (r4=is_xlr, r5=7, r6=bitdepth, r7==0).
    #    Leaves 0x06339d92..0x06339d94 untouched so 0x06339d82 (`b 0x06339d94` for is_aad!=0 -> port 3)
    #    NEVER lands in the middle of a 32-bit b.w instruction!
    tb.align4()
    tb.label("pathset_router")
    tb.cbz(4, "pathset_not_xlr")
    tb.movs(0, 20)                     # real physical XLR1 -> stock port 7 slots 20, 21!
    tb.b_w(0x06339D88)
    tb.label("pathset_not_xlr")
    tb.cbz(6, "pathset_24bit", nonzero=True)
    tb.b_w(0x06339D92)                 # 16-bit -> stock `movs r0, #4; str r0, [sp, #4]`!
    tb.label("pathset_24bit")
    tb.bl(FN_GET_MIC_MODE)             # r5=7, r6=1 callee-saved
    tb.subs_imm8(0, 2)
    tb.cmp_imm(0, 3)
    tb.b("slots_01", cond="hi")
    tb.movs(0, 2)                      # modes 2..5 (3.5mm ext_type 3 / plug-in power) -> slots 2,3
    tb.strd(5, 0, 13, 4)
    tb.movs(0, 3)
    tb.b_w(0x06339D8E)
    tb.label("slots_01")
    tb.movs(0, 0)                      # modes 0(windcut)/1(TRS)/7(LINE IN) -> slots 0,1
    tb.strd(5, 0, 13, 4)
    tb.movs(0, 1)
    tb.b_w(0x06339D8E)

    tb.align4()
    code = tb.finish()
    assert base_va + len(code) <= 0x06AA1140, f"LPCM cave overflow: {len(code)}B > 0x130"
    return code, dict(tb.labels)


def apply_lpcm24_menu(ps: PatchSet) -> dict[str, int]:
    """Add all patches for native 16-bit / 24-bit LPCM menu toggle to PatchSet."""
    code, labels = build_lpcm24_cave(G_LPCM_CODE_BASE)
    ps.add_cave_bytes(G_LPCM_CODE_BASE, code, "LPCM24 menu & conditional 2ch/TDM cave", MODULE)

    # 1. Un-grey menu item 0x8e (XLR Mic Adaptor Setting) without spoofing hardware XLR1
    ps.add_bytes(
        ADDR_MENU_8E_BL,
        struct.pack("<HH", 0xF0D9, 0xFB9E),
        struct.pack("<HH", 0x2001, 0xBF00),  # movs r0, #1; nop
        "Un-grey XLR menu item 0x8e without XLR1",
        MODULE,
    )
    # 2. Grey out 96kHz/24bit (choice 3) when no physical XLR1 is attached
    ps.add_bytes(
        ADDR_XLR_CHOICE_OK,
        struct.pack("<HH", 0x2903, 0xD108),
        thumb_b_w(ADDR_XLR_CHOICE_OK, labels["xlr_choice_ok"]),
        "Gate 96kHz/24bit on IsXlr1Attached",
        MODULE,
    )
    # 3. Reconfigure LiveView audio immediately when user toggles 16/24-bit in menu
    ps.add_bytes(
        ADDR_SET_XLR_TAIL,
        struct.pack("<HH", 0xF7FB, 0xBAAB),
        thumb_b_w(ADDR_SET_XLR_TAIL, labels["set_xlr_post"]),
        "SetXlrMenu guarded AudioReconfig hook",
        MODULE,
    )
    # 4. GetAudioBitDepth: read [0x05fcf25c] when no XLR1 (MOV LPCM only)
    ps.add_bytes(
        ADDR_GET_BITDEPTH,
        struct.pack("<HH", 0x49F9, 0x7809),
        thumb_b_w(ADDR_GET_BITDEPTH, labels["get_bitdepth"]),
        "GetAudioBitDepth menu-controlled 16/24-bit",
        MODULE,
    )
    # 5. Conditional 2ch DMA channels (16-bit=stock 4ch, 24-bit=2ch) — hooks 32-bit `bl GetMicInputMode`
    ps.add_bytes(
        ADDR_GET_DMA_CH,
        struct.pack("<HH", 0xF7FF, 0xFED4),
        thumb_b_w(ADDR_GET_DMA_CH, labels["get_dma_ch"]),
        "GetDmaChannels conditional 2ch/4ch",
        MODULE,
    )
    # 6. Conditional AVIO_AUDIO_Rec_Start DMA width
    ps.add_bytes(
        ADDR_REC_START_4CH,
        struct.pack("<HH", 0x2000, 0xB901),
        thumb_b_w(ADDR_REC_START_4CH, labels["rec_start_2ch"]),
        "AVIO_AUDIO_Rec_Start conditional 2ch/4ch",
        MODULE,
    )
    # 7. Conditional AVIO_ZNC_Start 2ch bypass
    ps.add_bytes(
        ADDR_ZNC_START_BL,
        struct.pack("<HH", 0xF7AD, 0xFB90),
        thumb_bl(ADDR_ZNC_START_BL, labels["znc_start_bypass"]),
        "AVIO_ZNC_Start conditional 2ch bypass",
        MODULE,
    )
    # 8. Conditional avio_audio_rec_pathSet TDM crossbar router — hooks 0x06339d84 (leaves 0x06339d94 stock!)
    ps.add_bytes(
        ADDR_PATHSET_PORT4,
        struct.pack("<HH", 0xB12C, 0x2014),
        thumb_b_w(ADDR_PATHSET_PORT4, labels["pathset_router"]),
        "avio_audio_rec_pathSet conditional TDM crossbar",
        MODULE,
    )
    return labels
