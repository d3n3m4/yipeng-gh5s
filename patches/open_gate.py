"""
Dynamic Open Gate 3:2 (3744x2496) / Anamorphic 4:3 (3328x2496) / optional Square 1:1 (2496x2496)
across ALL 3 System Frequencies (23.98p NTSC, 24.00p Cinema, 25.00p PAL) + Dual Playback + StartEnc resize.

How Dual-Aspect Switching Works on the Camera:
  When `dual_aspect=True` (default), the GH5S's persistent Aspect Ratio setting ([0x05fcf000 + 0xd0])
  controls which geometry is used in 10-bit 4:2:2 4K/A modes (23.98p / 24.00p / 25.00p):
    - 4:3 (0x0100) -> 3328x2496 (stock 4:3 Anamorphic, 3680x2760 sensor crop, 4:3 LV/OSD)
    - 3:2 (0x0200) or 16:9 (0x0300) -> 3744x2496 (Open Gate 3:2, 3840x2560 sensor crop, 3:2 LV/OSD)
    - 1:1 (0x0400, when `enable_1x1=True`) -> 2496x2496 (Square 1:1, 2752x2752 sensor crop, 1:1 LV/OSD)

Safety Isolation:
  - 4K/A 50p/60p (8-bit) and 29.97p (10-bit, unless `allow_2997p=True` or 1:1 mode) remain safely
    at stock 3328x2496 so H.264 Level 5.1 limits are never violated unintentionally.
  - All 5 postboot2 playback/thumbnail/sync validators accept 3328x2496 (4:3), 3744x2496 (3:2),
    and 2496x2496 (1:1) simultaneously with native aspect ratios!
  - StartEnc GaiaWork MV (0x00120000) & Ref (0x00c00000) buffers are resized inside GaiaWork's
    existing 84.00 MiB partition (82.28 MiB used, 1.72 MiB spare) and work for all three widths.
"""
from __future__ import annotations

import struct

from .asm import (
    ArmBuilder,
    ThumbBuilder,
    arm_b,
    arm_movt,
    arm_movw,
    thumb_b_w,
    thumb_bl,
    thumb_movw,
)
from .bitrates import (
    ALLI_BITRATE_CHOICES,
    LONGG_BITRATE_CHOICES,
    apply_bitrate_scaling,
)
from .common import (
    G_ACTIVE_A4K_W,
    G_CONFIG_FLAGS,
    G_OG_CODE_BASE,
    G_VIDEO_MENU_BASE,
    PB2_CAVE_BASE,
    PROG_DATA_DELTA,
    PatchSet,
)
from .playback_fix import apply_playback_fix

MODULE = "open_gate"

# Native Motion Picture (Video) Menu relocation (`0x081831c8` -> `G_VIDEO_MENU_BASE = 0x06AA1800`)
# Inserts GH5S's own multilingual `Aspect Ratio` item (0x000c -> 4:3 / 3:2 / 16:9 / 1:1 submenu 0x08180f38)
# right under Rec Quality on Page 1 of the Motion Picture menu (zero postboot1 changes!).
VIDEO_MENU_ORIG_RAM = 0x081831C8
ASPECT_MENU_ENTRY = (0x000C, 0x0104, 0x08180F38)
STOCK_VIDEO_MENU_ENTRIES: list[tuple[int, int, int]] = [
    (0x0023, 0x0104, 0x081832E0),
    (0x0008, 0x0104, 0x00000000),
    (0x004A, 0x0104, 0x00000000),
    (0x005B, 0x0104, 0x08185E88),
    (0x0025, 0x0104, 0x08182308),
    (0x008B, 0x0104, 0x081884E8),
    (0x0032, 0x0104, 0x00000000),
    (0x001F, 0x0104, 0x08186C20),
    (0x005F, 0x0104, 0x081836E0),
    (0x000A, 0x0104, 0x081825F0),
    (0x0055, 0x0104, 0x00000000),
    (0x0019, 0x0104, 0x08186AA8),
    (0x0029, 0x0104, 0x08184190),
    (0x0092, 0x0104, 0x00000000),
    (0x0099, 0x0104, 0x08184018),
    (0x007E, 0x0104, 0x08184030),
    (0x0007, 0x0102, 0x081827B8),
    (0x0035, 0x0104, 0x08182440),
    (0x002A, 0x0104, 0x081833F8),
    (0x0006, 0x0104, 0x08182758),
    (0x002D, 0x0104, 0x081822F0),
    (0x0098, 0x0104, 0x081822D8),
    (0x0027, 0x0104, 0x08182328),
    (0x0028, 0x0104, 0x00000000),
    (0x005E, 0x0104, 0x081837B0),
    (0x0022, 0x0104, 0x08183310),
    (0x007F, 0x0104, 0x08183340),
    (0x0059, 0x0104, 0x08183360),
    (0x0048, 0x0104, 0x00000000),
    (0x009E, 0x0104, 0x00000000),
    (0x0049, 0x0104, 0x08183550),
    (0x008E, 0x0104, 0x08183568),
    (0x005C, 0x0104, 0x00000000),
    (0x0073, 0x0104, 0x08183390),
]
VIDEO_MENU_CODE_REFS = (
    0x0626C098, 0x0626D9D8, 0x0626DDE8, 0x0626E23C, 0x0626EEAC,
    0x0626F2D8, 0x0626F6F8, 0x0626FB38, 0x06270374, 0x062707F0,
    0x06270C28, 0x06271080, 0x062718B0, 0x06272124, 0x06272520,
    0x06272998, 0x06272DBC, 0x06273240, 0x06273680, 0x06273AF4,
)
VIDEO_MENU_DATA_REFS_RAM = (
    0x06A6C308 + PROG_DATA_DELTA,  # 0x081804f4 (Creative Video M tab 9 -> Motion Picture)
    0x06A6C350 + PROG_DATA_DELTA,  # 0x0818053c (Photo mode tab 9 -> Motion Picture)
    0x06A7C418 + PROG_DATA_DELTA,  # 0x08190604 (Master tab descriptor -> Motion Picture)
)

# Verified stock V2.5 addresses in `program`
EEP_ASPECT_RATIO = 0x05FCF0D0      # u16: 0x0100=4:3, 0x0200=3:2, 0x0300=16:9, 0x0400=1:1
FN_CLEAN_DCACHE_ALL = 0x06395B8B   # Thumb-1 entry: clean L1+L2 D-cache (hardware-proven 4/4 clips in REC!)
FN_GET_RCP_INFO = 0x0621AE80       # GetRcpInfo(sel)
FN_LOOKUP_RECQ = 0x061DC288        # LookupRecQualityEntry(profile_id, &out_ptr)

# All 8 Anamorphic 10-bit 4:2:2 descriptors (selectors 0x200e..0x2015, verified in GH5S V2.5)
# Tuple: (sel, fps_label, rcp_va, chan_ram_va, blob_va, sl_off, is_alli, gop)
A4K_DESCRIPTORS = [
    (0x200E, "29.97p LongG", 0x06538AA8, 0x081949D8, 0x064E4A5C, 0x2FCC, False, 15),
    (0x200F, "29.97p ALL-I", 0x06538B1C, 0x08194518, 0x064D9714, 0x206C, True,   1),
    (0x2010, "23.98p LongG", 0x06538B90, 0x08194648, 0x064DB834, 0x2FCC, False, 12),
    (0x2011, "23.98p ALL-I", 0x06538C04, 0x08194188, 0x064D33B4, 0x206C, True,   1),
    (0x2012, "25.00p LongG", 0x06538C78, 0x081948A8, 0x064E19A4, 0x2FCC, False, 12),
    (0x2013, "25.00p ALL-I", 0x06538CEC, 0x081943E8, 0x064D75F4, 0x206C, True,   1),
    (0x2014, "24.00p LongG", 0x06538D60, 0x08194778, 0x064DE8EC, 0x2FCC, False, 12),
    (0x2015, "24.00p ALL-I", 0x06538DD4, 0x081942B8, 0x064D54D4, 0x206C, True,   1),
]

# StartEnc GaiaWork MV & Ref pool resize constants (82.28 / 84.00 MiB -> 1.72 MiB spare)
OLD_MV, NEW_MV = 0x00110000, 0x00120000
OLD_REF, NEW_REF = 0x00B5B000, 0x00C00000

# 4 postboot2 SPS microcode template pic_width_in_mbs_minus1 addresses (H=2496/H_MB=156 unchanged!)
SPS_W_ADDRS = (0x07375714, 0x07375B34, 0x07381C5C, 0x073825BC)


def build_program_og_cave(base_va: int = G_OG_CODE_BASE) -> tuple[bytes, dict[str, int]]:
    """
    Build all dynamic Open Gate / Dual-Aspect Thumb-2 cave handlers in `program`.

    Mode code returned by `get_og_mode`:
      0 -> 4:3  (3328 x 2496, crop 3680x2760, ExTele 3328x2496, LV 0x11e, OSD 0x100, HDMI 4:3)
      1 -> 3:2  (3744 x 2496, crop 3840x2560, ExTele 3840x2560, LV 0x21b, OSD 0x200, HDMI 3:2)
      2 -> 1:1  (2496 x 2496, crop 2752x2752, ExTele 2752x2752, LV 0x416, OSD 0x400, HDMI 1:1)
    """
    tb = ThumbBuilder(base_va)

    # -----------------------------------------------------------------------
    # Helper 0: get_og_mode() -> r0 in {0, 1, 2} (preserves r1..r12, 8B stack aligned!)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("get_og_mode")
    tb.push([1, 2, 3], lr=True)
    tb.mov32(1, G_CONFIG_FLAGS)
    tb.ldr(1, 1, 0)
    tb.tst_imm(1, 8)                    # bit3 = dual_aspect
    tb.b("mode_fixed_3x2", cond="eq")
    tb.mov32(2, EEP_ASPECT_RATIO)
    tb.ldrh(2, 2, 0)                    # [0x05fcf0d0]: 0x100=4:3, 0x200=3:2, 0x300=16:9, 0x400=1:1
    tb.cmp_w(2, 0x0100)
    tb.b("mode_ret_4x3", cond="eq")
    tb.cmp_w(2, 0x0400)
    tb.b("mode_ret_3x2", cond="ne")
    tb.tst_imm(1, 1)                    # bit0 = enable_1x1
    tb.b("mode_ret_3x2", cond="eq")
    tb.movs(0, 2)                       # 1:1 Square
    tb.pop([1, 2, 3], pc=True)
    tb.label("mode_ret_4x3")
    tb.movs(0, 0)                       # 4:3 Anamorphic
    tb.pop([1, 2, 3], pc=True)
    tb.label("mode_fixed_3x2")
    tb.label("mode_ret_3x2")
    tb.movs(0, 1)                       # 3:2 Open Gate
    tb.pop([1, 2, 3], pc=True)

    # -----------------------------------------------------------------------
    # Helper 1: get_og_width() -> r0 in {3328, 3744, 2496} (preserves r1..r12, 8B aligned!)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("get_og_width")
    tb.push([1], lr=True)
    tb.bl("get_og_mode")
    tb.cbz(0, "w_4x3")
    tb.cmp_imm(0, 2)
    tb.b("w_1x1", cond="eq")
    tb.movw(0, 3744)
    tb.pop([1], pc=True)
    tb.label("w_4x3")
    tb.movw(0, 3328)
    tb.pop([1], pc=True)
    tb.label("w_1x1")
    tb.movw(0, 2496)
    tb.pop([1], pc=True)

    # -----------------------------------------------------------------------
    # Helper 2: is_profile_10bit_a4k(r0=profile_id) -> r0=1 if 10-bit 4K/A else 0
    # Preserves r1..r12! Checks [RecQ_entry + 0x14] == 2 (10-bit).
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("is_profile_10bit_a4k")
    tb.push([1, 2, 3], lr=True)
    tb.hw(0xB082)                       # sub sp, #8 (local out_ptr at [sp, #0])
    tb.mov_reg(1, 13)                   # r1 = sp
    tb.bl(FN_LOOKUP_RECQ)               # LookupRecQualityEntry(profile_id, &out_ptr)
    tb.cbz(0, "recq_found")
    tb.movs(0, 0)
    tb.b("recq_done")
    tb.label("recq_found")
    tb.ldr(1, 13, 0)                    # r1 = RecQ_entry*
    tb.ldr(0, 1, 0x14)                  # [entry + 0x14]: bit10 (2 == 10-bit 4:2:2!)
    tb.cmp_imm(0, 2)
    tb.b("recq_is10", cond="eq")
    tb.movs(0, 0)
    tb.b("recq_done")
    tb.label("recq_is10")
    tb.movs(0, 1)
    tb.label("recq_done")
    tb.hw(0xB002)                       # add sp, #8
    tb.pop([1, 2, 3], pc=True)

    # -----------------------------------------------------------------------
    # Hook 1: ISP Normal A4K crop (0x061c3382 -> stores W to [r4], H in r0, b 0x061c33ce)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("isp_crop_normal")
    tb.bl("get_og_mode")
    tb.cbz(0, "crop_n_4x3")
    tb.cmp_imm(0, 2)
    tb.b("crop_n_1x1", cond="eq")
    tb.movw(0, 3840)                    # 3:2 full multi-aspect sensor field
    tb.str_(0, 4, 0)
    tb.movw(0, 2560)
    tb.b_w(0x061C33CE)
    tb.label("crop_n_4x3")
    tb.movw(0, 3680)                    # stock 4:3
    tb.str_(0, 4, 0)
    tb.movw(0, 2760)
    tb.b_w(0x061C33CE)
    tb.label("crop_n_1x1")
    tb.movw(0, 2752)                    # native 1:1 sensor field
    tb.str_(0, 4, 0)
    tb.movw(0, 2752)
    tb.b_w(0x061C33CE)

    # -----------------------------------------------------------------------
    # Hook 2: ISP ExTele A4K crop (0x061c329a -> stores W to [r4], H in r0, b 0x061c33ce)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("isp_crop_extele")
    tb.bl("get_og_mode")
    tb.cbz(0, "crop_e_4x3")
    tb.cmp_imm(0, 2)
    tb.b("crop_e_1x1", cond="eq")
    tb.movw(0, 3840)                    # 3:2 full multi-aspect FOV in LiveView/ExTele
    tb.str_(0, 4, 0)
    tb.movw(0, 2560)
    tb.b_w(0x061C33CE)
    tb.label("crop_e_4x3")
    tb.movw(0, 3328)                    # stock 4:3 ExTele (1:1 pixel-by-pixel!)
    tb.str_(0, 4, 0)
    tb.movw(0, 2496)
    tb.b_w(0x061C33CE)
    tb.label("crop_e_1x1")
    tb.movw(0, 2752)                    # 1:1 square
    tb.str_(0, 4, 0)
    tb.movw(0, 2752)
    tb.b_w(0x061C33CE)

    # -----------------------------------------------------------------------
    # Hook 3: ISP RAW->YC clamp W (called via `bl` at 0x062dff2a; r0 IS LIVE!)
    # Sets r4 = active_W, preserves r0..r3!
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("isp_clamp_w")
    tb.push([0], lr=True)
    tb.bl("get_og_width")
    tb.mov_reg(4, 0)
    tb.pop([0], pc=True)

    # -----------------------------------------------------------------------
    # Hook 4: ISP YC output W (jumped via `b.w` at 0x061b71d0; r0, r2, r4, r6, r12, LR ARE LIVE!)
    # CRITICAL: Stock 0x061b7142 loads `mov.w lr, #120` and 0x061b71b6 executes `strd lr, r12, [r5]`!
    # MUST enter via `b.w` (NOT `bl`!) and save/restore LR across `bl get_og_width`!
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("isp_yc_w")
    tb.push([0], lr=True)
    tb.bl("get_og_width")
    tb.mov_reg(1, 0)
    tb.pop([0], lr=True)
    tb.b_w(0x061B71D4)

    # -----------------------------------------------------------------------
    # Hook 5: Profile Aspect (0x061dc50e; r0=is_res6, r4=profile_id)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("profile_aspect")
    tb.cbz(0, "pa_not_res6")
    tb.mov_reg(0, 4)
    tb.bl("is_profile_10bit_a4k")
    tb.cbz(0, "pa_4x3")                 # 8-bit 50p/60p -> stay stock 4:3!
    tb.bl("get_og_mode")
    tb.cbz(0, "pa_4x3")
    tb.cmp_imm(0, 2)
    tb.b("pa_1x1", cond="eq")
    tb.b_w(0x061DC52C)                  # return 2 (3:2)
    tb.label("pa_4x3")
    tb.b_w(0x061DC518)                  # return 1 (4:3)
    tb.label("pa_1x1")
    tb.b_w(0x061DC54E)                  # return 4 (1:1)
    tb.label("pa_not_res6")
    tb.mov_reg(0, 4)
    tb.b_w(0x061DC512)                  # continue stock checks

    # -----------------------------------------------------------------------
    # Hook 6: Profile W (called via `bl` at 0x061dc574; r12=profile_id, returns W in r0)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("profile_w")
    tb.push([1, 2, 3, 4, 12], lr=True)  # 6 regs = 24B (strict 8-byte AAPCS align)
    tb.mov_reg(0, 12)
    tb.bl("is_profile_10bit_a4k")
    tb.cbz(0, "pw_4x3")                 # 8-bit 50p/60p -> stay stock 3328!
    tb.bl("get_og_width")
    tb.pop([1, 2, 3, 4, 12], pc=True)
    tb.label("pw_4x3")
    tb.movw(0, 3328)
    tb.pop([1, 2, 3, 4, 12], pc=True)

    # -----------------------------------------------------------------------
    # Hook 7: LiveView video aspect r6 (called via `bl` at 0x0613b7be)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("lv_aspect_r6")
    tb.push([0], lr=True)
    tb.bl("get_og_mode")
    tb.cbz(0, "lv6_4x3")
    tb.cmp_imm(0, 2)
    tb.b("lv6_1x1", cond="eq")
    tb.movw(6, 0x021B)                  # 3:2 L
    tb.pop([0], pc=True)
    tb.label("lv6_4x3")
    tb.movw(6, 0x011E)                  # 4:3 L
    tb.pop([0], pc=True)
    tb.label("lv6_1x1")
    tb.movw(6, 0x0416)                  # 1:1 L
    tb.pop([0], pc=True)

    # -----------------------------------------------------------------------
    # Hook 8: LiveView companion size r0 (called via `bl` at 0x0613ce56)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("lv_aspect_r0")
    tb.push([1], lr=True)               # 2 regs = 8B (strict 8-byte AAPCS align)
    tb.bl("get_og_mode")
    tb.cbz(0, "lv0_4x3")
    tb.cmp_imm(0, 2)
    tb.b("lv0_1x1", cond="eq")
    tb.movw(0, 0x021B)                  # 3:2 L
    tb.pop([1], pc=True)
    tb.label("lv0_4x3")
    tb.movw(0, 0x011E)                  # 4:3 L
    tb.pop([1], pc=True)
    tb.label("lv0_1x1")
    tb.movw(0, 0x0416)                  # 1:1 L
    tb.pop([1], pc=True)

    # -----------------------------------------------------------------------
    # Hook 9: OSD movie aspect r4 (called via `bl` at 0x0638a9c2; r0 IS LIVE!)
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("osd_aspect_r4")
    tb.push([0], lr=True)
    tb.bl("get_og_mode")
    tb.cbz(0, "osd_4x3")
    tb.cmp_imm(0, 2)
    tb.b("osd_1x1", cond="eq")
    tb.movw(4, 0x0200)                  # 3:2
    tb.pop([0], pc=True)
    tb.label("osd_4x3")
    tb.movw(4, 0x0100)                  # 4:3
    tb.pop([0], pc=True)
    tb.label("osd_1x1")
    tb.movw(4, 0x0400)                  # 1:1
    tb.pop([0], pc=True)

    # -----------------------------------------------------------------------
    # Hook 10: HDMI/video aspect (r2, r3) (called via `bl` at 0x0613cdbc)
    # CRITICAL: r4 == Anamorphic flag; when r4 == 0 (photo 4:3), MUST keep (4, 3)!
    # Preserves r0, r1, r4!
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("hdmi_aspect_r2r3")
    tb.push([0], lr=True)
    tb.cbz(4, "hdmi_4x3")               # NOT Anamorphic -> stock (4, 3)!
    tb.bl("get_og_mode")
    tb.cbz(0, "hdmi_4x3")
    tb.cmp_imm(0, 2)
    tb.b("hdmi_1x1", cond="eq")
    tb.movs(2, 3)                       # 3:2
    tb.movs(3, 2)
    tb.pop([0], pc=True)
    tb.label("hdmi_4x3")
    tb.movs(2, 4)                       # 4:3
    tb.movs(3, 3)
    tb.pop([0], pc=True)
    tb.label("hdmi_1x1")
    tb.movs(2, 1)                       # 1:1
    tb.movs(3, 1)
    tb.pop([0], pc=True)

    # -----------------------------------------------------------------------
    # Hook 11: Dynamic GAIA HW blob + chan_ram + RCP Info patcher!
    # Called via `bl` from 0x061b8192 (replaces `bl 0x0621ae80` GetRcpInfo(r0=sel)).
    # Patches the exact descriptor for sel in 0x200e..0x2015 right before GAIA reads it,
    # updates G_ACTIVE_A4K_W for postboot2, cleans D-cache, then tail-calls GetRcpInfo(sel).
    # -----------------------------------------------------------------------
    tb.align4()
    tb.label("sync_and_get_rcp")
    tb.push([0, 1, 2, 3, 4, 5, 6, 7, 8], lr=True)  # 10 regs = 40B (strict 8-byte AAPCS align)
    # Check if sel in 0x200e..0x2015
    tb.movw(1, 0x200E)
    tb.hw(0x1A44)                       # subs r4, r0, r1  (r4 = sel - 0x200e)
    tb.cmp_imm(4, 8)
    tb.b("sync_is_a4k10", cond="lo")
    # Not 0x200e..0x2015 (e.g. 50p/60p 8-bit, UHD, C4K, FHD) -> reset G_ACTIVE_A4K_W = 3328
    tb.mov32(1, G_ACTIVE_A4K_W)
    tb.movw(2, 3328)
    tb.str_(2, 1, 0)
    tb.pop([0, 1, 2, 3, 4, 5, 6, 7, 8], lr=True)
    tb.b_w(FN_GET_RCP_INFO)

    tb.label("sync_is_a4k10")
    # r4 = index 0..7 into desc_table (0,1 = 29.97p; 2..7 = 23.98p / 25.00p / 24.00p)
    tb.bl("get_og_mode")                # r0 = mode (0=4:3, 1=3:2, 2=1:1)
    # Safety guard: if 29.97p (r4 < 2) and mode == 1 (3744 > L5.1), require allow_2997p (bit1)
    tb.cmp_imm(4, 2)
    tb.b("sync_mode_ok", cond="hs")
    tb.cmp_imm(0, 1)
    tb.b("sync_mode_ok", cond="ne")
    tb.mov32(1, G_CONFIG_FLAGS)
    tb.ldr(1, 1, 0)
    tb.tst_imm(1, 2)                    # bit1 = allow_2997p
    tb.b("sync_mode_ok", cond="ne")
    tb.movs(0, 0)                       # clamp 29.97p 3:2 -> safe 4:3 (3328)

    tb.label("sync_mode_ok")
    # Convert mode -> r5 = W (3328 / 3744 / 2496)
    tb.cbz(0, "sync_w_4x3")
    tb.cmp_imm(0, 2)
    tb.b("sync_w_1x1", cond="eq")
    tb.movw(5, 3744)
    tb.b("sync_have_w")
    tb.label("sync_w_4x3")
    tb.movw(5, 3328)
    tb.b("sync_have_w")
    tb.label("sync_w_1x1")
    tb.movw(5, 2496)

    tb.label("sync_have_w")
    # Publish active width W to shared G_ACTIVE_A4K_W (cached; flushed via Spinlock-#24 CleanDCacheAll below)
    tb.mov32(1, G_ACTIVE_A4K_W)
    tb.str_(5, 1, 0)

    # Load descriptor entry: r6 = &desc_table[r4] (16 bytes/entry)
    # Using ADR-like mov32 patched via label? Let's compute desc_table VA explicitly after labels!
    # We'll place a fixed-size mov32 placeholder resolved cleanly via helper below.
    tb.label("sync_load_table_base")
    tb.mov32(6, 0x00000000)             # patched below to exact `desc_table` VA!
    tb.lsls_imm(0, 4, 4)                # r0 = idx * 16
    tb.hw(0x4406)                       # add r6, r0  (r6 = &desc_table[idx])

    # Load entry: r1=rcp_va, r2=chan_ram_va, r3=blob_va, r7=meta ((gop<<24)|(is_alli<<16)|sl_off)
    tb.ldr(1, 6, 0)
    tb.ldr(2, 6, 4)
    tb.ldr(3, 6, 8)
    tb.ldr(7, 6, 12)

    # r0 = w_mb = W >> 4 (208 / 234 / 156); r4 = w_mb - 1 (207 / 233 / 155)
    tb.lsrs_imm(0, 5, 4)
    tb.subs_imm3(4, 0, 1)

    # 1) RCP Info: [rcp + 0x20] = W
    tb.str_(5, 1, 0x20)
    # 2) chan_ram: [chan + 0x114] = w_mb - 1
    tb.str_(4, 2, 0x114)

    # Compute mbs = w_mb * 156 in r5
    tb.movs(1, 156)
    tb.mov_reg(5, 0)
    tb.muls(5, 1)                       # r5 = mbs (32448 / 36504 / 24336)

    # 3) GAIA blob core geometry words
    # +0xc70 = (156 << 16) | w_mb
    tb.movw(1, 0)
    tb.movt(1, 156)
    tb.orr_reg_lsl(1, 1, 0, 0)
    tb.str_(1, 3, 0x0C70)
    # +0xc74 = (mbs << 16) | mbs
    tb.orr_reg_lsl(1, 5, 5, 16)
    tb.str_(1, 3, 0x0C74)
    # +0xd0c = mbs << 16
    tb.lsls_imm(1, 5, 16)
    tb.str_(1, 3, 0x0D0C)
    # +0xd10 = (gop << 24) | (gop << 16) | mbs  (gop = r7 >> 24)
    tb.lsrs_imm(1, 7, 24)               # r1 = gop
    tb.orr_reg_lsl(2, 5, 1, 16)         # r2 = mbs | (gop << 16)
    tb.orr_reg_lsl(2, 2, 1, 24)         # r2 |= (gop << 24)
    tb.str_(2, 3, 0x0D10)
    # +0xed8 = mbs, +0xedc = mbs - 1
    tb.str_(5, 3, 0x0ED8)
    tb.subs_imm3(2, 5, 1)               # r2 = mbs - 1
    tb.str_(2, 3, 0x0EDC)
    # +0xf84 = mbs * 0x1400 = (mbs * 5) << 10
    tb.movs(1, 5)
    tb.muls(1, 5)                       # r1 = mbs * 5
    tb.lsls_imm(1, 1, 10)               # r1 = mbs * 0x1400
    tb.str_(1, 3, 0x0F84)

    # Branch on is_alli: bit 16 of r7
    tb.lsrs_imm(1, 7, 16)
    tb.tst_imm(1, 1)
    tb.b("sync_longg_fields", cond="eq")

    # ALL-I: +0xf9c = (0x11 << 24) | ((w_mb - 1) << 16)
    tb.movw(1, 0)
    tb.movt(1, 0x1100)
    tb.orr_reg_lsl(1, 1, 4, 16)
    tb.str_(1, 3, 0x0F9C)
    tb.b("sync_slices")

    # LongG: +0xf98 = mbs * 0x0c00 = (mbs * 3) << 10; +0xfbc & +0x107c = (0x11<<24)|((w_mb-1)<<16)|0x20
    tb.label("sync_longg_fields")
    tb.movs(1, 3)
    tb.muls(1, 5)
    tb.lsls_imm(1, 1, 10)
    tb.str_(1, 3, 0x0F98)
    tb.movw(1, 0x0020)
    tb.movt(1, 0x1100)
    tb.orr_reg_lsl(1, 1, 4, 16)
    tb.str_(1, 3, 0x0FBC)
    # +0x107c > 0xfff so add 0x1000 to r3 temporarily in r2
    tb.mov32(2, 0x107C)
    tb.hw(0x441A)                       # add r2, r3
    tb.str_(1, 2, 0)

    # 4) 16-slice table at blob + (r7 & 0xffff):
    #    [+0x00] = mbs - 1
    #    [+0x04 + 2*k] = slice_rows[k] * w_mb  (k=0..15)
    #    [+0x24] = (155 << 24) | ((w_mb - 1) << 16) | 0x00AA
    tb.label("sync_slices")
    tb.hw(0xB2BF)                       # uxth r7, r7  (r7 = sl_off)
    tb.hw(0x441F)                       # add  r7, r3  (r7 = blob + sl_off)
    tb.subs_imm3(2, 5, 1)               # r2 = mbs - 1
    tb.str_(2, 7, 0)                    # [+0x00] = mbs - 1

    # Load slice_rows table pointer into r6
    tb.label("sync_load_rows_base")
    tb.mov32(6, 0x00000000)             # patched below to exact `slice_rows` VA!
    tb.movs(1, 0)                       # k = 0
    tb.label("slice_loop")
    tb.ldrb_reg(2, 6, 1)                # ldrb r2, [r6, r1]  (r2 = slice_rows[k])
    tb.muls(2, 0)                       # r2 = slice_rows[k] * w_mb (r0 = w_mb!)
    tb.lsls_imm(3, 1, 1)                # r3 = 2 * k
    tb.adds_imm3(3, 3, 4)               # r3 = 4 + 2 * k
    tb.strh_reg(2, 7, 3)                # strh r2, [r7, r3]  (store slice start!)
    tb.adds_imm3(1, 1, 1)               # k++
    tb.cmp_imm(1, 16)
    tb.b("slice_loop", cond="lo")

    # End coord: [+0x24] = (155 << 24) | ((w_mb - 1) << 16) | 0x00AA
    tb.movw(1, 0x00AA)
    tb.movt(1, 0x9B00)                  # 155 = 0x9B
    tb.orr_reg_lsl(1, 1, 4, 16)
    tb.str_(1, 7, 0x24)

    # 5) Sync all 4 postboot2 SPS microcode templates' pic_width_in_mbs_minus1 = r4 (w_mb - 1)
    #    Live MMU verified 0x07300000 = 0x07315c06 (RWX WBWA Shareable S=1 -> SCU coherent with Core 1!).
    #    SMP-broadcast DCCIMVAC ensures Core 1 postboot2 (0x07032694) & DRAM see the exact width!
    tb.mov32(1, G_ACTIVE_A4K_W)
    tb.hw(0xEE07, 0x1F3E)               # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC G_ACTIVE_A4K_W)
    for sps_va in SPS_W_ADDRS:
        tb.mov32(1, sps_va)
        tb.str_(4, 1, 0)
        tb.hw(0xEE07, 0x1F3E)           # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC SPS template)
    tb.hw(0xF3BF, 0x8F4F)               # dsb sy

    # Clean L1+L2 D-cache (hardware-proven 0x06395B8B) so GAIA HW & postboot2 see all updated words!
    tb.mov32(1, FN_CLEAN_DCACHE_ALL)
    tb.hw(0x4788)                       # blx r1

    tb.pop([0, 1, 2, 3, 4, 5, 6, 7, 8], lr=True)
    tb.b_w(FN_GET_RCP_INFO)             # tail-call stock GetRcpInfo(sel)!

    # -----------------------------------------------------------------------
    # Embedded constant tables right after code (4-byte aligned)
    # -----------------------------------------------------------------------
    tb.align4()
    desc_table_va = tb.label("desc_table")
    for sel, _lbl, rcp_va, chan_ram_va, blob_va, sl_off, is_alli, gop in A4K_DESCRIPTORS:
        meta = (gop << 24) | ((1 if is_alli else 0) << 16) | sl_off
        tb.u32(rcp_va, chan_ram_va, blob_va, meta)

    slice_rows_va = tb.label("slice_rows")
    rows = bytes((k * 156) // 16 for k in range(16))
    tb.raw(rows)
    tb.align4()

    # Resolve the two mov32 table pointers cleanly
    code = bytearray(tb.finish())
    off_t = tb.labels["sync_load_table_base"] - base_va
    code[off_t : off_t + 8] = thumb_movw(6, desc_table_va & 0xFFFF) + struct.pack(
        "<HH",
        0xF2C0 | (((desc_table_va >> 27) & 1) << 10) | ((desc_table_va >> 28) & 0xF),
        (((desc_table_va >> 24) & 7) << 12) | (6 << 8) | ((desc_table_va >> 16) & 0xFF),
    )
    off_r = tb.labels["sync_load_rows_base"] - base_va
    code[off_r : off_r + 8] = thumb_movw(6, slice_rows_va & 0xFFFF) + struct.pack(
        "<HH",
        0xF2C0 | (((slice_rows_va >> 27) & 1) << 10) | ((slice_rows_va >> 28) & 0xF),
        (((slice_rows_va >> 24) & 7) << 12) | (6 << 8) | ((slice_rows_va >> 16) & 0xFF),
    )
    assert base_va + len(code) <= 0x06AA1800, f"program OG cave grew unexpectedly: {len(code)}B"
    return bytes(code), dict(tb.labels)


def build_pb2_og_cave(base_va: int = PB2_CAVE_BASE) -> tuple[bytes, dict[str, int]]:
    """
    Build postboot2 ARM32 + Thumb-2 dual-mode & 3-aspect playback cave at 0x076EE000.
    Accepts 3328x2496 (4:3), 3744x2496 (3:2), and 2496x2496 (1:1) simultaneously!
    """
    # Part 1: ARM32 handlers (0x076EE000..)
    ab = ArmBuilder(base_va)

    # 1) pb2_size_code (hooked from 0x0720c62c): pure 7-insn leaf (r0=W, r1=H) -> size_code==5
    ab.label("pb2_size_code")
    ab.cmp_imm(0, 3328)
    ab.cmp_imm(0, 3744, cond="ne")
    ab.cmp_imm(0, 2496, cond="ne")
    ab.cmp_imm(1, 2496, cond="eq")
    ab.mov_imm(0, 5, cond="eq")
    ab.bx_lr(cond="eq")
    ab.b(0x0720C63C)                    # continue stock checks (3504x2336, etc.)

    # 2) pb2_sel_to_wh (hooked from 0x072a7d4c): r0=struct_ptr, r1=&W, r2=&H, r3=sel
    #    Reads G_ACTIVE_A4K_W for 0x200e..0x2015, writes *r1=W, *r2=2496 (SPS already synced in sync_and_get_rcp!).
    ab.label("pb2_sel_to_wh")
    ab.push([4, 5])
    ab.mov32(4, 0x200E)
    ab.u32(0xE0434004)                  # sub r4, r3, r4  (r4 = sel - 0x200e)
    ab.cmp_imm(4, 8)
    ab.b("sel_fallback_3328", cond="hs")
    ab.mov32(4, G_ACTIVE_A4K_W)
    ab.ldr(3, 4, 0)                     # r3 = W from program
    ab.cmp_imm(3, 3744)
    ab.cmp_imm(3, 3328, cond="ne")
    ab.cmp_imm(3, 2496, cond="ne")
    ab.b("sel_valid_w", cond="eq")
    ab.label("sel_fallback_3328")
    ab.mov_imm(3, 3328)
    ab.label("sel_valid_w")
    ab.str_(3, 1, 0)                    # *W = r3
    ab.mov_imm(4, 2496)
    ab.str_(4, 2, 0)                    # *H = 2496
    ab.pop([4, 5])
    ab.bx_lr()

    arm_code = ab.finish()
    labels = dict(ab.labels)

    # Part 2: Thumb-2 handlers right after ARM32 part (4-byte aligned)
    thumb_base = base_va + len(arm_code)
    assert thumb_base % 4 == 0
    tb = ThumbBuilder(thumb_base)

    # 3) pb2_isp_sync (hooked from 0x06ff4c7e): r0=W -> accept {3328, 3744, 2496} -> 0x06ff4c9e (H==2496 -> 0x9c)
    tb.label("pb2_isp_sync")
    tb.cmp_w(0, 3328)
    tb.b("sync_hit", cond="eq")
    tb.cmp_w(0, 3744)
    tb.b("sync_hit", cond="eq")
    tb.cmp_w(0, 2496)
    tb.b("sync_hit", cond="eq")
    tb.b_w(0x06FF4C84)
    tb.label("sync_hit")
    tb.b_w(0x06FF4C9E)

    # 4) pb2_4k_tag (hooked from 0x07017ff2): r2=W -> accept {3328, 3744, 2496} -> 0x07018070 (H==2496 -> tag 0xc)
    tb.align4()
    tb.label("pb2_4k_tag")
    tb.cmp_w(2, 3328)
    tb.b("tag_hit", cond="eq")
    tb.cmp_w(2, 3744)
    tb.b("tag_hit", cond="eq")
    tb.cmp_w(2, 2496)
    tb.b("tag_hit", cond="eq")
    tb.b_w(0x07017FF8)
    tb.label("tag_hit")
    tb.b_w(0x07018070)

    # 5) pb2_play_box (hooked from 0x07001e76): r0=W, r1=H
    #    3328x2496 -> 1440x1080 (4:3, 0x07001ebe)
    #    3744x2496 -> 1620x1080 (3:2, 0x07001eb8)
    #    2496x2496 -> 1080x1080 (1:1, 0x07001e92)
    tb.align4()
    tb.label("pb2_play_box")
    tb.cmp_w(0, 3328)
    tb.b("play_4x3", cond="eq")
    tb.cmp_w(0, 3744)
    tb.b("play_3x2", cond="eq")
    tb.cmp_w(0, 2496)
    tb.b("play_1x1", cond="eq")
    tb.b_w(0x07001E7C)
    tb.label("play_4x3")
    tb.b_w(0x07001EBE)
    tb.label("play_3x2")
    tb.cmp_w(1, 2496)
    tb.b("play_3x2_ok", cond="eq")
    tb.b_w(0x07001ECC)
    tb.label("play_3x2_ok")
    tb.b_w(0x07001EB8)
    tb.label("play_1x1")
    tb.cmp_w(1, 2496)
    tb.b("play_1x1_ok", cond="eq")
    tb.b_w(0x07001ECC)
    tb.label("play_1x1_ok")
    tb.b_w(0x07001E92)

    # 6) pb2_mov_thumb (hooked from 0x071f1380): r1=W, r0=H
    #    3328x2496 -> 320x240 (4:3, 0x071f13ba)
    #    3744x2496 -> 352x240 (3:2, 0x071f13dc)
    #    2496x2496 -> 240x240 (1:1, 0x071f13ac)
    tb.align4()
    tb.label("pb2_mov_thumb")
    tb.cmp_w(1, 3328)
    tb.b("th_4x3", cond="eq")
    tb.cmp_w(1, 3744)
    tb.b("th_3x2", cond="eq")
    tb.cmp_w(1, 2496)
    tb.b("th_1x1", cond="eq")
    tb.b_w(0x071F1386)
    tb.label("th_4x3")
    tb.b_w(0x071F13BA)
    tb.label("th_3x2")
    tb.cmp_w(0, 2496)
    tb.b("th_3x2_ok", cond="eq")
    tb.b_w(0x071F13EE)
    tb.label("th_3x2_ok")
    tb.b_w(0x071F13DC)
    tb.label("th_1x1")
    tb.cmp_w(0, 2496)
    tb.b("th_1x1_ok", cond="eq")
    tb.b_w(0x071F13EE)
    tb.label("th_1x1_ok")
    tb.b_w(0x071F13AC)

    tb.align4()
    thumb_code = tb.finish()
    labels.update(tb.labels)
    total = arm_code + thumb_code
    assert base_va + len(total) <= 0x076EE800
    return total, labels


def _apply_startenc_resize(ps: PatchSet) -> None:
    """Layer 8: StartEnc GaiaWork MV (0x00120000) & Ref (0x00c00000) pool resize (1.72 MiB spare)."""
    u32s = [
        (0x0702B264, 0xE3A02822, 0xE3A02824, "StartEnc 2*MV (0x00240000)"),
        (0x0702B274, arm_movw(2, 0xC000), arm_movw(2, 0x0000), "StartEnc 4*Ref lo (0x03000000)"),
        (0x0702B278, arm_movt(2, 0x02D6), arm_movt(2, 0x0300), "StartEnc 4*Ref hi (0x03000000)"),
        (0x0702B300, 0xE2813811, 0xE2813812, "StartEnc MV[1] base (+0x120000)"),
        (0x0702B350, 0xE0830203, 0xE0830183, "StartEnc MV/4 mul 17->9"),
        (0x0702B354, 0xE0810700, 0xE0810780, "StartEnc MV/4 shift 14->15 (0x48000)"),
        (0x0702B368, 0xE2800911, 0xE2800912, "StartEnc MV/4 step1 (+0x48000)"),
        (0x0702B37C, 0xE2811811, 0xE2811812, "StartEnc MV loop1 (+0x120000)"),
        (0x0702B3DC, 0xE28CC911, 0xE28CC912, "StartEnc MV/4 step2 (+0x48000)"),
        (0x0702B474, 0xE0831203, 0xE0831183, "StartEnc MV/8 mul 17->9"),
        (0x0702B478, 0xE08C1681, 0xE08C1701, "StartEnc MV/8 shift 13->14 (0x24000)"),
        (0x0702B48C, 0xE2811A22, 0xE2811A24, "StartEnc MV/8 step1 (+0x24000)"),
        (0x0702B4A0, 0xE28CC811, 0xE28CC812, "StartEnc MV loop2 (+0x120000)"),
        (0x0702B4E0, 0xE2800A22, 0xE2800A24, "StartEnc MV/8 step2 (+0x24000)"),
        (0x0702B558, arm_movw(2, 0xB000), arm_movw(2, 0x0000), "StartEnc Ref lo (0x00c00000)"),
        (0x0702B55C, arm_movt(2, 0x00B5), arm_movt(2, 0x00C0), "StartEnc Ref hi (0x00c00000)"),
        (0x0702B598, arm_movw(8, 0xB000), arm_movw(8, 0x0000), "StartEnc Ref r8 lo (0x00c00000)"),
        (0x0702B59C, arm_movw(7, 0x6000), arm_movw(7, 0x0000), "StartEnc 2*Ref r7 lo (0x01800000)"),
        (0x0702B5A0, arm_movt(7, 0x016B), arm_movt(7, 0x0180), "StartEnc 2*Ref r7 hi (0x01800000)"),
        (0x0702B5A4, arm_movw(4, 0x6C00), arm_movw(4, 0x0000), "StartEnc Ref/4 r4 lo (0x00300000)"),
        (0x0702B5A8, arm_movt(4, 0x002D), arm_movt(4, 0x0030), "StartEnc Ref/4 r4 hi (0x00300000)"),
        (0x0702B5BC, arm_movt(11, 0x00B5), arm_movt(11, 0x00C0), "StartEnc Ref r11 hi (0x00c00000)"),
        (0x0702B64C, arm_movt(2, 0x00B5), arm_movt(2, 0x00C0), "StartEnc Ref r2 hi (0x00c00000)"),
        (0x0702B680, arm_movw(7, 0x6000), arm_movw(7, 0x0000), "StartEnc 2*Ref8 r7 lo (0x01800000)"),
        (0x0702B684, arm_movt(7, 0x016B), arm_movt(7, 0x0180), "StartEnc 2*Ref8 r7 hi (0x01800000)"),
        (0x0702B688, arm_movw(4, 0xB600), arm_movw(4, 0x0000), "StartEnc Ref/8 r4 lo (0x00180000)"),
        (0x0702B68C, arm_movt(4, 0x0016), arm_movt(4, 0x0018), "StartEnc Ref/8 r4 hi (0x00180000)"),
        (0x0702B6AC, 0xE28288B5, 0xE28288C0, "StartEnc Ref8 end hi (+0xc00000)"),
        (0x0702B6B0, 0xE2888A0B, 0xE2888A00, "StartEnc Ref8 end lo (+0)"),
        (0x0702B7AC, 0xE28228B5, 0xE28228C0, "StartEnc Ref8 loop hi (+0xc00000)"),
        (0x0702B7B0, 0xE2822A0B, 0xE2822A00, "StartEnc Ref8 loop lo (+0)"),
    ]
    for va, old_v, new_v, desc in u32s:
        ps.add_u32(va, old_v, new_v, desc, MODULE)

    neon_pools = [
        (0x0702B6D8, OLD_MV // 4, NEW_MV // 4, "StartEnc NEON MV/4 (0x48000)"),
        (0x0702B6E8, OLD_MV // 8, NEW_MV // 8, "StartEnc NEON MV/8 (0x24000)"),
        (0x0702B6F8, OLD_REF // 4, NEW_REF // 4, "StartEnc NEON Ref/4 (0x300000)"),
        (0x0702B708, OLD_REF * 2, NEW_REF * 2, "StartEnc NEON 2*Ref (0x1800000)"),
        (0x0702B718, OLD_REF // 8, NEW_REF // 8, "StartEnc NEON Ref/8 (0x180000)"),
    ]
    for base_va, old_v, new_v, desc in neon_pools:
        for lane in range(4):
            ps.add_u32(base_va + lane * 4, old_v, new_v, desc, MODULE)


def _apply_video_menu_aspect(ps: PatchSet) -> None:
    """
    Relocate the 34-entry Motion Picture Menu tree (`0x081831c8`) into `G_VIDEO_MENU_BASE` (`0x06AA1800`)
    with Panasonic's own multilingual `Aspect Ratio` (`0x000c`, submenu `0x08180f38` = 4:3 / 3:2 / 16:9 / 1:1)
    inserted directly on Page 1 right under Rec Quality (zero postboot1 edits!).
    """
    entries = (
        STOCK_VIDEO_MENU_ENTRIES[:2]
        + [ASPECT_MENU_ENTRY]
        + STOCK_VIDEO_MENU_ENTRIES[2:]
        + [(0xFFFF, 0x0000, 0x00000000)]
    )
    assert len(entries) == 36
    blob = b"".join(struct.pack("<HHI", mid, st, ptr) for mid, st, ptr in entries)
    ps.add_cave_bytes(
        G_VIDEO_MENU_BASE,
        blob,
        "Relocated 35-item Motion Picture menu (incl. Aspect Ratio 0x000c)",
        MODULE,
    )
    for va in VIDEO_MENU_CODE_REFS:
        ps.add_u32(va, VIDEO_MENU_ORIG_RAM, G_VIDEO_MENU_BASE, "Video menu code ptr -> relocated table", MODULE)
    for ram_va in VIDEO_MENU_DATA_REFS_RAM:
        ps.add_u32(ram_va, VIDEO_MENU_ORIG_RAM, G_VIDEO_MENU_BASE, "Video menu .data ptr -> relocated table", MODULE)


def apply_open_gate(
    ps: PatchSet,
    *,
    dual_aspect: bool = True,
    enable_1x1: bool = False,
    allow_2997p: bool = False,
    longg_bitrate: int = 200,
    alli_bitrate: int = 400,
    scale_all_4k: bool = False,
    include_playback_fix: bool = True,
    video_menu_aspect: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """
    Install the full dynamic Open Gate 3:2 / Anamorphic 4:3 (/ Square 1:1) patch set
    across 23.98p NTSC, 24.00p Cinema, and 25.00p PAL.
    """
    assert longg_bitrate in LONGG_BITRATE_CHOICES, f"Unsupported longg_bitrate: {longg_bitrate}"
    assert alli_bitrate in ALLI_BITRATE_CHOICES, f"Unsupported alli_bitrate: {alli_bitrate}"

    # 1. Shared runtime state header at 0x06AA1000..0x06AA1007
    cfg_flags = (
        (1 if enable_1x1 else 0)
        | (2 if allow_2997p else 0)
        | (4 if longg_bitrate != 150 else 0)
        | (8 if dual_aspect else 0)
        | (16 if scale_all_4k else 0)
        | ((longg_bitrate & 0xFFF) << 8)
        | ((alli_bitrate & 0xFFF) << 20)
    )
    ps.add_cave_bytes(
        G_ACTIVE_A4K_W,
        struct.pack("<II", 0, cfg_flags),
        "Shared Open Gate runtime state + config flags",
        MODULE,
    )

    if dual_aspect and video_menu_aspect:
        _apply_video_menu_aspect(ps)

    # 2. Program cave + all 11 dynamic hooks
    prog_code, prog_labels = build_program_og_cave(G_OG_CODE_BASE)
    ps.add_cave_bytes(G_OG_CODE_BASE, prog_code, "program dynamic Open Gate / Dual-Aspect cave", MODULE)

    # Hook 1 & 2: ISP Sensor Crop (Normal & ExTele)
    ps.add_bytes(0x061C3382, thumb_movw(0, 3680), thumb_b_w(0x061C3382, prog_labels["isp_crop_normal"]),
                 "ISP crop normal A4K -> dynamic", MODULE)
    ps.add_bytes(0x061C329A, thumb_movw(0, 3328), thumb_b_w(0x061C329A, prog_labels["isp_crop_extele"]),
                 "ISP crop ExTele A4K -> dynamic", MODULE)
    # Full 3:2 LiveView FOV when r5==1 (3504x2336 -> 3840x2560, does not affect 4:3 r5==0 or 1:1 r5==3!)
    ps.add_bytes(0x061C32A6, thumb_movw(0, 3504), thumb_movw(0, 3840), "ExTele r5==1 3:2 FOV W (3840)", MODULE)
    ps.add_bytes(0x061C32AC, thumb_movw(0, 2336), thumb_movw(0, 2560), "ExTele r5==1 3:2 FOV H (2560)", MODULE)

    # Hook 3 & 4: ISP RAW->YC active clamp + YC output W (0x061b71d0 MUST use b.w because LR=120 is live!)
    ps.add_bytes(0x062DFF2A, thumb_movw(4, 3328), thumb_bl(0x062DFF2A, prog_labels["isp_clamp_w"]),
                 "ISP RAW->YC clamp W -> dynamic", MODULE)
    ps.add_bytes(0x061B71D0, thumb_movw(1, 3328), thumb_b_w(0x061B71D0, prog_labels["isp_yc_w"]),
                 "ISP YC out W -> dynamic (preserves live LR=120)", MODULE)

    # Hook 5 & 6: Profile Aspect + Profile W (guarded to 10-bit 4K/A only!)
    ps.add_bytes(0x061DC50E, struct.pack("<HH", 0xB918, 0x4620),
                 thumb_b_w(0x061DC50E, prog_labels["profile_aspect"]),
                 "Profile Aspect res==6 -> dynamic", MODULE)
    ps.add_bytes(0x061DC574, thumb_movw(0, 3328), thumb_bl(0x061DC574, prog_labels["profile_w"]),
                 "Profile W res==6 -> dynamic", MODULE)

    # Hook 7..10: LiveView video, LiveView companion, OSD overlay, HDMI aspect
    ps.add_bytes(0x0613B7BE, thumb_movw(6, 0x011E), thumb_bl(0x0613B7BE, prog_labels["lv_aspect_r6"]),
                 "LiveView video aspect -> dynamic", MODULE)
    ps.add_bytes(0x0613CE56, thumb_movw(0, 0x011E), thumb_bl(0x0613CE56, prog_labels["lv_aspect_r0"]),
                 "LiveView companion size -> dynamic", MODULE)
    ps.add_bytes(0x0638A9C2, thumb_movw(4, 0x0100), thumb_bl(0x0638A9C2, prog_labels["osd_aspect_r4"]),
                 "OSD movie aspect lines/gauge -> dynamic", MODULE)
    ps.add_bytes(0x0613CDBC, struct.pack("<HH", 0x2204, 0x2303),
                 thumb_bl(0x0613CDBC, prog_labels["hdmi_aspect_r2r3"]),
                 "HDMI/video aspect -> dynamic (photo 4:3 preserved)", MODULE)

    # Hook 11: Dynamic GAIA HW blob + chan_ram + RCP sync right before GetRcpInfo(sel) @ 0x061B8192
    # (Both fn_821c and fn_84e4 already call fn_8186 -> 0x061B8192 first!)
    ps.add_bytes(
        0x061B8192,
        thumb_bl(0x061B8192, FN_GET_RCP_INFO),
        thumb_bl(0x061B8192, prog_labels["sync_and_get_rcp"]),
        "Dynamic GAIA blob + chan_ram + RCP sync hook @ 0x061b8192",
        MODULE,
    )

    # 3. Hardware GAIA RC bitrate scaling (LongGOP & ALL-I across 4K/A + optional C4K/UHD 4K)
    apply_bitrate_scaling(
        ps,
        longg_bitrate=longg_bitrate,
        alli_bitrate=alli_bitrate,
        scale_all_4k=scale_all_4k,
        include_2997p=True,
    )

    # 4. postboot2 cave + all 6 dynamic/dual-mode validators + StartEnc resize + Green Stripe Fix
    pb2_code, pb2_labels = build_pb2_og_cave(PB2_CAVE_BASE)
    ps.add_cave_bytes(PB2_CAVE_BASE, pb2_code, "postboot2 dual-mode & 3-aspect playback cave", MODULE)

    ps.add_u32(0x0720C62C, 0xE3500C0D, arm_b(0x0720C62C, pb2_labels["pb2_size_code"]),
               "pb2 size_code==5 -> accept 3328/3744/2496", MODULE)
    ps.add_u32(0x072A7D4C, 0xE3A03C0D, arm_b(0x072A7D4C, pb2_labels["pb2_sel_to_wh"]),
               "pb2 sel->(W,H) direct dynamic aspect", MODULE)
    ps.add_bytes(0x06FF4C7E, struct.pack("<HH", 0xF5B0, 0x6F50),
                 thumb_b_w(0x06FF4C7E, pb2_labels["pb2_isp_sync"]),
                 "pb2 ISP->GAIA sync -> accept 3328/3744/2496", MODULE)
    ps.add_bytes(0x07017FF2, struct.pack("<HH", 0xF5B2, 0x6F50),
                 thumb_b_w(0x07017FF2, pb2_labels["pb2_4k_tag"]),
                 "pb2 4K-class tag -> accept 3328/3744/2496", MODULE)
    ps.add_bytes(0x07001E76, struct.pack("<HH", 0xF5B0, 0x6F50),
                 thumb_b_w(0x07001E76, pb2_labels["pb2_play_box"]),
                 "pb2 Playback/HDMI box -> 4:3/3:2/1:1", MODULE)
    ps.add_bytes(0x071F1380, struct.pack("<HH", 0xF5B1, 0x6F50),
                 thumb_b_w(0x071F1380, pb2_labels["pb2_mov_thumb"]),
                 "pb2 MOV thumbnail -> 4:3/3:2/1:1", MODULE)

    _apply_startenc_resize(ps)

    if include_playback_fix:
        apply_playback_fix(ps)

    return prog_labels, pb2_labels
