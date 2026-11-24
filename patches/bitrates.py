"""
Hardware H.264 Rate-Controller (GAIA RC) Bitrate Scaler & SD Card Profiles for GH5S V2.5.

How It Works:
  Panasonic's `postboot2:0x0720c6a0` validates discrete RCP Info bitrate enums (100/150/200/400M),
  so RCP Info stays untouched (`150M` LongGOP / `400M` ALL-I) while we cleanly scale the actual
  GAIA hardware rate-controller struct (`blob_va + 0x0C10`) inside `program`.

  - LongGOP 10-bit (stock 150M -> 100 / 120 / 150 / 200 / 250 / 300 Mbps):
      Keeps stock 45.0 Mbit CPB/HRD timing untouched and scales the 13 proven RC fields
      (+0x08, +0x74, +0x7C..+0x88, +0xA0..+0xAC, +0xB4..+0xBC), capping single-frame max
      bits (+0xB4..+0xBC) at <= 36.0 Mbit (80% of 45M CPB) so CPB can never underflow.
      (200M LongGOP live-verified for 68.5s & 31s continuous clips on GH5S hardware!)

  - ALL-Intra 10-bit (stock 400M -> 200 / 250 / 300 / 400 / 500 / 600 Mbps):
      Uses Panasonic's exact reverse-engineered ALL-I RC formula (reproduces Panasonic's
      official 200M ALL-I RC struct bit-for-bit at 24.00p/25.00p/29.97p and within 1 bit
      at 23.98p!): scales peak bitrate (+0x08), CPB size (+0x0C, capped at stock 50.0 Mbit),
      CPB delay (+0x10, +0x28, +0x3C f32, +0x48 f64), initial VBV fullness (+0x78),
      target/frame bit budgets (+0x74, +0x7C..+0x88, +0xA0..+0xAC), RC reaction (+0x94,
      capped at stock 0x08000000), and max frame cap (+0xB4..+0xBC = 1.25 * bpf <= 62.5% CPB).

SD Card Profiles (`--sd-profile`):
  - `v30-eco` : 100M LongGOP (12.5 MB/s) + 200M ALL-I (25.0 MB/s) — ultra-safe V30 / small files
  - `v30`     : 150M LongGOP (18.8 MB/s) + 200M ALL-I (25.0 MB/s) — enables 10-bit ALL-I on V30!
  - `v60`     : 200M LongGOP (25.0 MB/s) + 400M ALL-I (50.0 MB/s) — default (live-verified!)
  - `v90`     : 250M LongGOP (31.3 MB/s) + 500M ALL-I (62.5 MB/s) — high-bitrate V90 (test in RAM first)
"""
from __future__ import annotations

import struct

from .common import PatchSet

MODULE = "bitrates"

# Named SD card speed profiles -> (longg_mbps, alli_mbps)
SD_PROFILES: dict[str, tuple[int, int]] = {
    "v30-eco": (100, 200),
    "v30":     (150, 200),
    "v60":     (200, 400),
    "v90":     (250, 500),
}

LONGG_BITRATE_CHOICES: tuple[int, ...] = (100, 120, 150, 200, 250, 300)
ALLI_BITRATE_CHOICES: tuple[int, ...] = (200, 250, 300, 400, 500, 600)

# Framerate rational descriptors: fps_key -> (fps_nom, fps_num, fps_den)
FPS_SPECS: dict[str, tuple[int, int, int]] = {
    "23.98p": (24, 24000, 1001),
    "24.00p": (24, 24, 1),
    "25.00p": (25, 25, 1),
    "29.97p": (30, 30000, 1001),
}

# Verified stock V2.5 GAIA RC bases (blob_va + 0x0C10) for ALL 24 10-bit 4K/A, C4K, and UHD 4K modes.
# Verified bit-for-bit identical stock RC values across A4K, C4K, and UHD 4K per (codec, fps).
A4K_LONGG_RC_BASES: dict[str, int] = {
    "23.98p": 0x064DC444,  # 0x2010
    "24.00p": 0x064DF4FC,  # 0x2014
    "25.00p": 0x064E25B4,  # 0x2012
    "29.97p": 0x064E566C,  # 0x200E
}
A4K_ALLI_RC_BASES: dict[str, int] = {
    "23.98p": 0x064D3FC4,  # 0x2011
    "24.00p": 0x064D60E4,  # 0x2015
    "25.00p": 0x064D8204,  # 0x2013
    "29.97p": 0x064DA324,  # 0x200F
}
C4K_LONGG_RC_BASES: dict[str, int] = {
    "23.98p": 0x064BFA54,
    "24.00p": 0x064C2B0C,
    "25.00p": 0x064F7E9C,
    "29.97p": 0x064F4DE4,
}
C4K_ALLI_RC_BASES: dict[str, int] = {
    "23.98p": 0x064FAF54,
    "24.00p": 0x064D1EA4,
    "25.00p": 0x064FF194,
    "29.97p": 0x064FD074,
}
UHD_LONGG_RC_BASES: dict[str, int] = {
    "23.98p": 0x064C5BC4,
    "24.00p": 0x064C8C7C,
    "25.00p": 0x064CBD34,
    "29.97p": 0x064CEDEC,
}
UHD_ALLI_RC_BASES: dict[str, int] = {
    "23.98p": 0x064B75D4,
    "24.00p": 0x064B96F4,
    "25.00p": 0x064BB814,
    "29.97p": 0x064BD934,
}

# Verified stock V2.5 LongGOP 150M RC u32 values (shared identically across A4K, C4K, UHD)
STOCK_LONGG_150M_RC: dict[str, dict[int, int]] = {
    "23.98p": {
        0x08: 179_999_744, 0x74: 6_256_250, 0x7C: 150_000_000,
        0x80: 6_256_249,   0x84: 6_256_249, 0x88: 7_507_489,
        0xA0: 3_128_124,   0xA4: 6_256_249, 0xA8: 12_512_498, 0xAC: 6_256_249,
        0xB4: 25_000_000,  0xB8: 25_000_000, 0xBC: 25_000_000,
    },
    "24.00p": {
        0x08: 179_999_744, 0x74: 6_250_000, 0x7C: 150_000_000,
        0x80: 6_250_000,   0x84: 6_250_000, 0x88: 7_499_989,
        0xA0: 3_125_000,   0xA4: 6_250_000, 0xA8: 12_500_000, 0xAC: 6_250_000,
        0xB4: 25_000_000,  0xB8: 25_000_000, 0xBC: 25_000_000,
    },
    "25.00p": {
        0x08: 179_999_744, 0x74: 6_000_000, 0x7C: 150_000_000,
        0x80: 6_000_000,   0x84: 6_000_000, 0x88: 7_199_990,
        0xA0: 3_000_000,   0xA4: 6_000_000, 0xA8: 12_000_000, 0xAC: 6_000_000,
        0xB4: 25_000_000,  0xB8: 25_000_000, 0xBC: 25_000_000,
    },
    "29.97p": {
        0x08: 179_999_744, 0x74: 5_005_000, 0x7C: 150_000_000,
        0x80: 5_005_000,   0x84: 5_005_000, 0x88: 6_005_991,
        0xA0: 2_502_500,   0xA4: 5_005_000, 0xA8: 10_010_000, 0xAC: 5_005_000,
        0xB4: 25_000_000,  0xB8: 25_000_000, 0xBC: 25_000_000,
    },
}

# Verified stock V2.5 ALL-I 400M RC u32 values (shared identically across A4K, C4K, UHD)
STOCK_ALLI_400M_RC: dict[str, dict[int, int]] = {
    "23.98p": {
        0x08: 480_000_000, 0x0C: 50_000_000, 0x10: 49_996_875, 0x28: 9375,
        0x3C: 0x46127C00,  0x48: 0xAAAAAAAB, 0x4C: 0x3FBAAAAA,
        0x74: 16_683_333,  0x78: 41_658_334, 0x7C: 400_000_000,
        0x80: 16_683_332,  0x84: 16_683_332, 0x88: 20_020_000, 0x94: 134_217_728,
        0xA0: 8_341_666,   0xA4: 16_683_332, 0xA8: 33_366_664, 0xAC: 16_683_332,
        0xB4: 20_854_166,  0xB8: 20_854_166, 0xBC: 20_854_166,
    },
    "24.00p": {
        0x08: 480_000_000, 0x0C: 50_000_000, 0x10: 49_996_875, 0x28: 9375,
        0x3C: 0x46127C00,  0x48: 0xAAAAAAAB, 0x4C: 0x3FBAAAAA,
        0x74: 16_666_667,  0x78: 41_666_667, 0x7C: 400_000_000,
        0x80: 16_666_666,  0x84: 16_666_666, 0x88: 20_000_000, 0x94: 134_217_728,
        0xA0: 8_333_333,   0xA4: 16_666_666, 0xA8: 33_333_332, 0xAC: 16_666_666,
        0xB4: 20_833_333,  0xB8: 20_833_333, 0xBC: 20_833_333,
    },
    "25.00p": {
        0x08: 480_000_000, 0x0C: 50_000_000, 0x10: 49_996_875, 0x28: 9375,
        0x3C: 0x46127C00,  0x48: 0xAAAAAAAB, 0x4C: 0x3FBAAAAA,
        0x74: 16_000_000,  0x78: 42_000_000, 0x7C: 400_000_000,
        0x80: 16_000_000,  0x84: 16_000_000, 0x88: 19_200_000, 0x94: 134_217_728,
        0xA0: 8_000_000,   0xA4: 16_000_000, 0xA8: 32_000_000, 0xAC: 16_000_000,
        0xB4: 20_000_000,  0xB8: 20_000_000, 0xBC: 20_000_000,
    },
    "29.97p": {
        0x08: 480_000_000, 0x0C: 50_000_000, 0x10: 49_996_875, 0x28: 9375,
        0x3C: 0x46127C00,  0x48: 0xAAAAAAAB, 0x4C: 0x3FBAAAAA,
        0x74: 13_346_667,  0x78: 43_326_667, 0x7C: 400_000_000,
        0x80: 13_346_666,  0x84: 13_346_666, 0x88: 16_016_000, 0x94: 134_217_728,
        0xA0: 6_673_333,   0xA4: 13_346_666, 0xA8: 26_693_332, 0xAC: 13_346_666,
        0xB4: 16_683_333,  0xB8: 16_683_333, 0xBC: 16_683_333,
    },
}

# Safety ceiling for LongGOP single-frame max cap (+0xB4..+0xBC): 80% of stock 45.0 Mbit CPB.
LONGG_MAX_FRAME_BITS_CAP = 36_000_000
# Stock 400M ALL-I CPB & RC reaction ceilings (never exceeded even for 500M/600M V90 modes).
ALLI_MAX_CPB_BITS = 50_000_000
ALLI_MAX_REACTION = 134_217_728


def compute_longg_rc_fields(fps_key: str, target_mbps: int) -> dict[int, int]:
    """Compute target u32 RC fields for a 10-bit LongGOP mode scaled from stock 150M."""
    assert target_mbps in LONGG_BITRATE_CHOICES, f"Unsupported LongGOP bitrate: {target_mbps}"
    stock = STOCK_LONGG_150M_RC[fps_key]
    if target_mbps == 150:
        return dict(stock)

    out: dict[int, int] = {}
    for off, old_v in stock.items():
        if off == 0x08:
            out[off] = (target_mbps * 1_200_000) & ~0xFF
        elif off == 0x7C:
            out[off] = target_mbps * 1_000_000
        elif off in (0xB4, 0xB8, 0xBC):
            scaled = (old_v * target_mbps + 75) // 150
            out[off] = min(scaled, LONGG_MAX_FRAME_BITS_CAP)
        else:
            out[off] = (old_v * target_mbps + 75) // 150
    return out


def compute_alli_rc_fields(fps_key: str, target_mbps: int) -> dict[int, int]:
    """Compute target u32 RC fields for a 10-bit ALL-I mode using Panasonic's exact formula."""
    assert target_mbps in ALLI_BITRATE_CHOICES, f"Unsupported ALL-I bitrate: {target_mbps}"
    if target_mbps == 400:
        return dict(STOCK_ALLI_400M_RC[fps_key])

    fps_nom, fps_num, fps_den = FPS_SPECS[fps_key]
    target_bps = target_mbps * 1_000_000
    max_bps = (target_mbps * 1_200_000) & ~0xFF
    cpb = min(int(round(3.0 * max_bps / fps_nom)), ALLI_MAX_CPB_BITS)
    cpb2 = cpb - (cpb // 16000 if cpb >= ALLI_MAX_CPB_BITS else cpb // 4000)
    dly = int(round(cpb * 90000 / max_bps))
    f3c = struct.unpack("<I", struct.pack("<f", float(dly)))[0]
    d48_lo, d48_hi = struct.unpack("<II", struct.pack("<d", dly / 90000.0))

    bpf_round = (target_bps * fps_den + fps_num // 2) // fps_num
    bpf_floor = (target_bps * fps_den) // fps_num
    v78 = cpb - (bpf_floor // 2)
    max_bpf = (max_bps * fps_den + fps_num // 2) // fps_num
    v94 = min(int(round(target_mbps / 400.0 * ALLI_MAX_REACTION)), ALLI_MAX_REACTION)
    max_frame = (bpf_floor * 5) // 4
    assert max_frame < cpb, f"ALL-I max_frame {max_frame} >= CPB {cpb}!"

    return {
        0x08: max_bps,
        0x0C: cpb,
        0x10: cpb2,
        0x28: dly,
        0x3C: f3c,
        0x48: d48_lo,
        0x4C: d48_hi,
        0x74: bpf_round,
        0x78: v78,
        0x7C: target_bps,
        0x80: bpf_floor,
        0x84: bpf_floor,
        0x88: max_bpf,
        0x94: v94,
        0xA0: bpf_floor // 2,
        0xA4: bpf_floor,
        0xA8: bpf_floor * 2,
        0xAC: bpf_floor,
        0xB4: max_frame,
        0xB8: max_frame,
        0xBC: max_frame,
    }


def resolve_bitrates(
    sd_profile: str = "v60",
    longg_bitrate: int | None = None,
    alli_bitrate: int | None = None,
) -> tuple[int, int]:
    """Resolve effective (longg_mbps, alli_mbps) from `--sd-profile` plus explicit overrides."""
    assert sd_profile in SD_PROFILES, f"Unknown --sd-profile {sd_profile!r} (valid: {list(SD_PROFILES)})"
    prof_longg, prof_alli = SD_PROFILES[sd_profile]
    eff_longg = prof_longg if longg_bitrate is None else longg_bitrate
    eff_alli = prof_alli if alli_bitrate is None else alli_bitrate
    assert eff_longg in LONGG_BITRATE_CHOICES, f"Invalid LongGOP bitrate {eff_longg}"
    assert eff_alli in ALLI_BITRATE_CHOICES, f"Invalid ALL-I bitrate {eff_alli}"
    return eff_longg, eff_alli


def apply_bitrate_scaling(
    ps: PatchSet,
    *,
    longg_bitrate: int = 200,
    alli_bitrate: int = 400,
    scale_all_4k: bool = False,
    include_2997p: bool = True,
) -> None:
    """
    Apply GAIA hardware RC bitrate scaling to 10-bit 4K/A (Open Gate / Anamorphic),
    and optionally to standard 10-bit C4K (4096x2160) & UHD 4K (3840x2160) when `scale_all_4k=True`.
    """
    assert longg_bitrate in LONGG_BITRATE_CHOICES
    assert alli_bitrate in ALLI_BITRATE_CHOICES
    fps_keys = ("23.98p", "24.00p", "25.00p", "29.97p") if include_2997p else ("23.98p", "24.00p", "25.00p")

    longg_groups = [("A4K", A4K_LONGG_RC_BASES)]
    alli_groups = [("A4K", A4K_ALLI_RC_BASES)]
    if scale_all_4k:
        longg_groups.extend([("C4K", C4K_LONGG_RC_BASES), ("UHD", UHD_LONGG_RC_BASES)])
        alli_groups.extend([("C4K", C4K_ALLI_RC_BASES), ("UHD", UHD_ALLI_RC_BASES)])

    if longg_bitrate != 150:
        for res_label, base_map in longg_groups:
            for fps_key in fps_keys:
                rc_base = base_map[fps_key]
                stock = STOCK_LONGG_150M_RC[fps_key]
                target = compute_longg_rc_fields(fps_key, longg_bitrate)
                for off, old_v in stock.items():
                    ps.add_u32(
                        rc_base + off,
                        old_v,
                        target[off],
                        f"{res_label} LongG {longg_bitrate}M RC {fps_key} +0x{off:02x}",
                        MODULE,
                    )

    if alli_bitrate != 400:
        for res_label, base_map in alli_groups:
            for fps_key in fps_keys:
                rc_base = base_map[fps_key]
                stock = STOCK_ALLI_400M_RC[fps_key]
                target = compute_alli_rc_fields(fps_key, alli_bitrate)
                for off, old_v in stock.items():
                    ps.add_u32(
                        rc_base + off,
                        old_v,
                        target[off],
                        f"{res_label} ALL-I {alli_bitrate}M RC {fps_key} +0x{off:02x}",
                        MODULE,
                    )
