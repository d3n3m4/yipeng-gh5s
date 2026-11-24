#!/usr/bin/env python3
"""
GH5S Pro Mod — Modular Firmware Patcher & Volatile RAM Tester for Panasonic Lumix DC-GH5S (V2.5).

Features:
  --open-gate         Dynamic Open Gate 3:2 (3744x2496) <-> Anamorphic 4:3 (3328x2496) across
                      23.98p (NTSC), 24.00p (Cinema), and 25.00p (PAL) + Video Menu Aspect Ratio
                      + Dual Playback + Green Stripe Fix.
  --fixed-3x2         Lock 10-bit 4K/A to 3:2 (3744x2496) instead of switching via Aspect Ratio menu.
  --enable-1x1        Also enable Square 1:1 (2496x2496) Open Gate when Aspect Ratio == 1:1.
  --allow-2997p       [Experimental] Allow 3744x2496 at 29.97p (1.09M MB/s > H.264 L5.1).
  --sd-profile P      SD card speed preset: v30-eco (100M/200M), v30 (150M/200M),
                      v60 (200M/400M, default), v90 (250M/500M, experimental).
  --longg-bitrate N   Override 10-bit LongGOP hardware bitrate (100/120/150/200/250/300 Mbps).
  --alli-bitrate N    Override 10-bit ALL-I hardware bitrate (200/250/300/400/500/600 Mbps).
  --scale-all-4k      Also apply LongGOP/ALL-I bitrate scaling to C4K (4096x2160) & UHD 4K (3840x2160).
  --lpcm24-menu       Unlock native 16-bit / 24-bit LPCM menu toggle (XLR Mic Adaptor Setting)
                      for internal & 3.5mm mics without DMW-XLR1 (preserves real XLR1 & 16-bit Wind Cut).
  --gh7-extras        Unlock Video Guide Line / Aspect Marker inside 3:2 Open Gate & 4:3 Anamorphic.
  --playback-fix-only Apply ONLY the in-camera green vertical stripe playback fix (42 B in postboot2).

Execution Modes:
  --verify-offline    Default: assemble & verify every patch against stock decrypted V2.5 images.
  --ram-verify        Read-only live RAM inspection via USB Service Mode (04da:2370).
  --ram-status        Read-only dump of live camera menu/aspect/audio state via USB Service Mode.
  --ram-apply         Apply patches to VOLATILE RAM only (requires --confirm-ram-only; zero flash writes).
  --ram-restore       Restore patched addresses in VOLATILE RAM back to stock V2.5 (requires --confirm-ram-only).
  --build-upd OUT     Build roundtrip-verified GH5S_V26.bin (+ optional --rollback-out GH5S_V27.bin).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from patches.bitrates import (
    ALLI_BITRATE_CHOICES,
    LONGG_BITRATE_CHOICES,
    SD_PROFILES,
    apply_bitrate_scaling,
    resolve_bitrates,
)
from patches.common import PatchSet, load_decrypted_v25
from patches.gh7_extras import apply_gh7_extras
from patches.lpcm24_menu import apply_lpcm24_menu
from patches.open_gate import apply_open_gate
from patches.playback_fix import apply_playback_fix


def _default_path(env_key: str, *candidates: Path) -> Path:
    if env_key in os.environ and os.environ[env_key]:
        return Path(os.environ[env_key])
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def build_patchset(args: argparse.Namespace) -> tuple[PatchSet, tuple[int, int] | None]:
    ps = PatchSet()
    if args.playback_fix_only:
        apply_playback_fix(ps)
        return ps, None

    bitrate_requested = (
        args.sd_profile is not None
        or args.longg_bitrate is not None
        or args.alli_bitrate is not None
        or args.scale_all_4k
    )
    resolved_br: tuple[int, int] | None = None
    if args.open_gate or bitrate_requested:
        resolved_br = resolve_bitrates(
            sd_profile=args.sd_profile or "v60",
            longg_bitrate=args.longg_bitrate,
            alli_bitrate=args.alli_bitrate,
        )

    if args.lpcm24_menu:
        apply_lpcm24_menu(ps)
    if args.open_gate:
        assert resolved_br is not None
        longg_mbps, alli_mbps = resolved_br
        apply_open_gate(
            ps,
            dual_aspect=not args.fixed_3x2,
            enable_1x1=args.enable_1x1,
            allow_2997p=args.allow_2997p,
            longg_bitrate=longg_mbps,
            alli_bitrate=alli_mbps,
            scale_all_4k=args.scale_all_4k,
            include_playback_fix=True,
            video_menu_aspect=not args.no_video_menu_aspect,
        )
    elif bitrate_requested:
        assert resolved_br is not None
        longg_mbps, alli_mbps = resolved_br
        apply_bitrate_scaling(
            ps,
            longg_bitrate=longg_mbps,
            alli_bitrate=alli_mbps,
            scale_all_4k=args.scale_all_4k,
            include_2997p=True,
        )

    if args.gh7_extras:
        apply_gh7_extras(ps)

    if not ps.patches:
        raise SystemExit(
            "No patches generated. Pass --all, or combine --open-gate / --lpcm24-menu / --gh7-extras / --sd-profile."
        )
    return ps, resolved_br


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="GH5S V2.5 Pro Mod — Patcher & Volatile RAM Tester")

    feat = ap.add_argument_group("Features")
    feat.add_argument("--all", action="store_true", help="Enable --open-gate --lpcm24-menu --gh7-extras --enable-1x1")
    feat.add_argument("--open-gate", action="store_true", help="Dynamic 4:3 (3328x2496) <-> 3:2 (3744x2496) Open Gate + Video Menu Aspect Ratio + Dual Playback + Green Stripe Fix")
    feat.add_argument("--fixed-3x2", action="store_true", help="Use fixed 3:2 (3744x2496) instead of Aspect Ratio menu switching")
    feat.add_argument("--no-video-menu-aspect", action="store_true", help="Do not add Aspect Ratio directly into the Motion Picture menu tab")
    feat.add_argument("--enable-1x1", action="store_true", help="Enable 1:1 Square (2496x2496) when Aspect Ratio == 1:1")
    feat.add_argument("--allow-2997p", action="store_true", help="[Experimental] Enable 3744x2496 on 29.97p (exceeds H.264 L5.1)")
    feat.add_argument("--lpcm24-menu", action="store_true", help="Native 16-bit / 24-bit LPCM toggle via unlocked XLR Mic Adaptor menu")
    feat.add_argument("--gh7-extras", action="store_true", help="Unlock Video Guide Line / Aspect Marker in Open Gate & Anamorphic")
    feat.add_argument("--playback-fix-only", action="store_true", help="Only apply the 42B in-camera playback green stripe fix")

    br = ap.add_argument_group("Bitrate & SD Card Profiles")
    br.add_argument(
        "--sd-profile",
        choices=tuple(SD_PROFILES.keys()),
        default=None,
        help="SD card preset: v30-eco (100M/200M), v30 (150M/200M), v60 (200M/400M, default with --open-gate), v90 (250M/500M)",
    )
    br.add_argument(
        "--longg-bitrate",
        type=int,
        choices=LONGG_BITRATE_CHOICES,
        default=None,
        help="Override 10-bit LongGOP bitrate in Mbps (stock=150; 200 live-verified)",
    )
    br.add_argument(
        "--alli-bitrate",
        type=int,
        choices=ALLI_BITRATE_CHOICES,
        default=None,
        help="Override 10-bit ALL-I bitrate in Mbps (stock=400; 200 fits V30 SD cards!)",
    )
    br.add_argument(
        "--scale-all-4k",
        action="store_true",
        help="Also apply LongGOP/ALL-I bitrate scaling to standard 10-bit C4K (4096x2160) and UHD 4K (3840x2160)",
    )

    mode = ap.add_argument_group("Execution Mode")
    mx = mode.add_mutually_exclusive_group()
    mx.add_argument("--verify-offline", action="store_true", help="Verify patches offline against decrypted V2.5 partitions (default)")
    mx.add_argument("--ram-verify", action="store_true", help="Read-only live RAM check via USB Service Mode (04da:2370)")
    mx.add_argument("--ram-status", action="store_true", help="Read-only live camera status via USB Service Mode")
    mx.add_argument("--ram-apply", action="store_true", help="Apply patches to VOLATILE RAM only (requires --confirm-ram-only)")
    mx.add_argument("--ram-restore", action="store_true", help="Restore patched addresses in VOLATILE RAM to stock (requires --confirm-ram-only)")
    mx.add_argument("--build-upd", type=Path, help="Output path for permanent GH5S_V26.bin UPD firmware")

    ap.add_argument("--confirm-ram-only", action="store_true", help="Explicit confirmation for volatile RAM writes")
    ap.add_argument("--fw-dir", type=Path,
                    default=_default_path("GH5S_FW_DIR", ROOT / "firmware" / "GH5S_V25_decrypted", ROOT.parent / "firmware" / "GH5S_V25_decrypted"),
                    help="Directory containing decrypted stock V2.5 partitions (env: GH5S_FW_DIR)")
    ap.add_argument("--orig-fw", type=Path,
                    default=_default_path("GH5S_ORIG_FW", ROOT / "GH5S_V25.bin", ROOT.parent / "firmware" / "GH5S_V25.bin"),
                    help="Official Panasonic GH5S_V25.bin (required for --build-upd; env: GH5S_ORIG_FW)")
    ap.add_argument("--key-file", type=Path,
                    default=_default_path("GH5S_KEY_FILE", ROOT / "gh5s_aes5_key.bin"),
                    help="External 16-byte AES-5 key file (required for --build-upd; never committed to Git; env: GH5S_KEY_FILE)")
    ap.add_argument("--aes5-lib", type=Path,
                    default=_default_path("GH5S_AES5_LIB", ROOT / "libpana_aes5.so"),
                    help="Path to libpana_aes5.so (required for --build-upd; env: GH5S_AES5_LIB)")
    ap.add_argument("--probe-dir", type=Path, default=None,
                    help="Optional directory containing external USB probe helper (built-in PyUSB transport used by default)")
    ap.add_argument("--rollback-out", type=Path, default=None,
                    help="Optional output path for stock rollback GH5S_V27.bin (recommended!)")
    ap.add_argument("--version-hex", default="0x0260", help="UPD version u16 hex (default: 0x0260 = V2.6)")

    args = ap.parse_args(argv)
    if args.all:
        args.open_gate = True
        args.lpcm24_menu = True
        args.gh7_extras = True
        args.enable_1x1 = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ps, resolved_br = build_patchset(args)

    print("GH5S Pro Mod — Selected Patch Modules:")
    for mod, cnt in ps.summary_by_module().items():
        print(f"  - {mod:14s}: {cnt:4d} halfwords")
    print(f"  TOTAL           : {len(ps.patches):4d} halfwords")
    if resolved_br is not None:
        lg, ai = resolved_br
        scope = "4K/A + C4K + UHD 4K" if args.scale_all_4k else "4K/A (Open Gate / Anamorphic)"
        prof = args.sd_profile or "v60"
        print(f"  BITRATES ({prof:7s}): LongGOP={lg}M ({lg/8:.1f} MB/s), ALL-I={ai}M ({ai/8:.1f} MB/s) [{scope}]")

    if args.build_upd is not None:
        from tools.upd_builder import build_and_verify_upd
        ver = int(args.version_hex, 16)
        build_and_verify_upd(
            orig_fw_path=args.orig_fw,
            key_path=args.key_file,
            lib_path=args.aes5_lib,
            ps=ps,
            out_v26_path=args.build_upd,
            out_v27_rollback_path=args.rollback_out,
            version_u16=ver,
            unlock_sd_rollback=False,
        )
        return 0

    stock_prog, stock_pb2, stock_pb5 = load_decrypted_v25(args.fw_dir)
    ps.verify_against_stock(stock_prog, stock_pb2)
    print("[OFFLINE VERIFIED] 100% of stock V2.5 halfwords matched; zero address collisions.")

    if args.ram_verify or args.ram_status or args.ram_apply or args.ram_restore:
        from tools.ram_deployer import run_ram_action
        if (args.ram_apply or args.ram_restore) and not args.confirm_ram_only:
            raise SystemExit("Refusing live RAM write without --confirm-ram-only.")
        action = (
            "verify" if args.ram_verify else
            "status" if args.ram_status else
            "apply" if args.ram_apply else
            "restore"
        )
        return run_ram_action(ps, stock_pb5, action=action, probe_module_dir=args.probe_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
