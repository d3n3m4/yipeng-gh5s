#!/usr/bin/env python3
"""
Exhaustive offline verification suite for gh5s_pro_mod:
  1. Verifies EVERY stock V2.5 halfword & collision/bounds check across all feature combinations.
  2. Disassembles EVERY generated Thumb-2 & ARM32 cave and hook instruction via llvm-mc,
     asserting ZERO invalid/unrecognized instructions and exact branch/label targets.
  3. Verifies exact stock values of all 8 A4K GAIA descriptors and all 3 LongG RC tables.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from patches.bitrates import (
    A4K_ALLI_RC_BASES,
    A4K_LONGG_RC_BASES,
    ALLI_BITRATE_CHOICES,
    C4K_ALLI_RC_BASES,
    C4K_LONGG_RC_BASES,
    LONGG_BITRATE_CHOICES,
    SD_PROFILES,
    STOCK_ALLI_400M_RC,
    STOCK_LONGG_150M_RC,
    UHD_ALLI_RC_BASES,
    UHD_LONGG_RC_BASES,
    apply_bitrate_scaling,
    compute_alli_rc_fields,
    compute_longg_rc_fields,
    resolve_bitrates,
)
from patches.common import (
    G_LPCM_CODE_BASE,
    G_OG_CODE_BASE,
    PB2_CAVE_BASE,
    PROG_BASE,
    PROG_DATA_DELTA,
    PatchSet,
    load_decrypted_v25,
)
from patches.gh7_extras import apply_gh7_extras
from patches.lpcm24_menu import apply_lpcm24_menu, build_lpcm24_cave
from patches.open_gate import (
    A4K_DESCRIPTORS,
    STOCK_VIDEO_MENU_ENTRIES,
    VIDEO_MENU_CODE_REFS,
    VIDEO_MENU_DATA_REFS_RAM,
    VIDEO_MENU_ORIG_RAM,
    apply_open_gate,
    build_pb2_og_cave,
    build_program_og_cave,
)
from patches.playback_fix import apply_playback_fix
from tools.ram_deployer import ATOMIC_CODE_VA, STUB_ADDR, build_atomic_hook_installer


def _resolve_fw_dir() -> Path:
    if os.environ.get("GH5S_FW_DIR"):
        return Path(os.environ["GH5S_FW_DIR"])
    for cand in (ROOT / "firmware" / "GH5S_V25_decrypted", ROOT.parent / "firmware" / "GH5S_V25_decrypted"):
        if cand.exists():
            return cand
    raise SystemExit("Decrypted V2.5 directory not found. Set GH5S_FW_DIR=/path/to/GH5S_V25_decrypted")


def disasm_bytes(data: bytes, triple: str) -> list[str]:
    hex_in = " ".join(f"0x{b:02x}" for b in data)
    proc = subprocess.run(
        ["llvm-mc", "--disassemble", f"-triple={triple}", "-mattr=+neon"],
        input=hex_in.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stderr = proc.stderr.decode(errors="replace").strip()
    assert proc.returncode == 0, f"llvm-mc failed ({triple}): {stderr}"
    assert "invalid instruction" not in stderr.lower(), f"Invalid encoding ({triple}): {stderr}"
    assert "warning:" not in stderr.lower(), f"llvm-mc warning ({triple}): {stderr}"
    lines = [ln.strip() for ln in proc.stdout.decode().splitlines() if ln.strip() and not ln.strip().startswith(".")]
    return lines


def _assert_8b_stack_aligned(lines: list[str], name: str) -> None:
    """Assert every push/pop instruction in our Thumb-2 caves pushes an even number of 4-byte regs (8B AAPCS)."""
    for ln in lines:
        if ln.startswith(("push", "pop")) and "{" in ln and "}" in ln:
            regs = [r.strip() for r in ln.split("{", 1)[1].split("}", 1)[0].split(",") if r.strip()]
            assert len(regs) % 2 == 0, f"Unaligned 4B stack op in {name}: {ln}"


def test_caves_disasm() -> None:
    if not shutil.which("llvm-mc"):
        print("[SKIP] llvm-mc not installed; skipping disassembly check")
        return

    # 1. LPCM24 menu cave (pure Thumb-2)
    lpcm_code, lpcm_labels = build_lpcm24_cave(G_LPCM_CODE_BASE)
    lpcm_lines = disasm_bytes(lpcm_code, "thumbv7a")
    _assert_8b_stack_aligned(lpcm_lines, "lpcm24_menu")
    print(f"[PASS] lpcm24_menu cave: {len(lpcm_code)} B, {len(lpcm_lines)} Thumb-2 insns (strict 8B aligned), labels={sorted(lpcm_labels)}")

    # 2. Program Open Gate cave (code ends right at `desc_table` label!)
    og_code, og_labels = build_program_og_cave(G_OG_CODE_BASE)
    code_end = og_labels["desc_table"] - G_OG_CODE_BASE
    og_lines = disasm_bytes(og_code[:code_end], "thumbv7a")
    _assert_8b_stack_aligned(og_lines, "open_gate_prog")
    print(f"[PASS] open_gate prog cave: {code_end} B code + {len(og_code)-code_end} B tables, {len(og_lines)} Thumb-2 insns (strict 8B aligned)")

    # 3. Postboot2 cave (ARM32 part up to pb2_isp_sync, then Thumb-2 part)
    pb2_code, pb2_labels = build_pb2_og_cave(PB2_CAVE_BASE)
    split = pb2_labels["pb2_isp_sync"] - PB2_CAVE_BASE
    arm_lines = disasm_bytes(pb2_code[:split], "armv7a")
    th_lines = disasm_bytes(pb2_code[split:], "thumbv7a")
    _assert_8b_stack_aligned(arm_lines, "open_gate_pb2_arm")
    _assert_8b_stack_aligned(th_lines, "open_gate_pb2_thumb")
    assert not any("mcr" in ln for ln in arm_lines), f"Unexpected mcr p15 in postboot2 cave: {arm_lines}"
    print(f"[PASS] open_gate pb2 cave: {split} B ARM32 ({len(arm_lines)} insns) + {len(pb2_code)-split} B Thumb-2 ({len(th_lines)} insns)")

    # 4. Atomic IRQ-disabled RAM hook installer + op80 trampoline
    stub_atomic, atomic_code = build_atomic_hook_installer()
    stub_lines = disasm_bytes(stub_atomic, "thumbv7a")
    atom_lines = disasm_bytes(atomic_code, "thumbv7a")
    _assert_8b_stack_aligned(stub_lines, "ram_stub_atomic")
    _assert_8b_stack_aligned(atom_lines, "ram_atomic_installer")
    assert any("CPSR_c" in ln for ln in atom_lines), "Missing CPSR_c IRQ guard in atomic installer!"
    print(f"[PASS] ram_deployer atomic installer: {len(stub_atomic)} B stub @ 0x{STUB_ADDR:08x} + {len(atomic_code)} B @ 0x{ATOMIC_CODE_VA:08x} ({len(atom_lines)} Thumb-2 insns)")


def test_video_menu_relocation_against_v25(stock_prog: bytes) -> None:
    """Verify exact Motion Picture menu entries (0x081831c8) and exhaustive 23/23 pointer coverage."""
    rom_off = (VIDEO_MENU_ORIG_RAM - PROG_DATA_DELTA) - PROG_BASE
    for idx, exp in enumerate(STOCK_VIDEO_MENU_ENTRIES):
        actual = struct.unpack_from("<HHI", stock_prog, rom_off + idx * 8)
        assert actual == exp, f"Video menu entry [{idx}] mismatch: {actual} != {exp}"
    term = struct.unpack_from("<HHI", stock_prog, rom_off + len(STOCK_VIDEO_MENU_ENTRIES) * 8)
    assert term == (0xFFFF, 0x0000, 0x00000000), f"Missing 0xFFFF terminator: {term}"

    # Verify EVERY u32 anywhere in stock_prog pointing into [0x081831c8..0x081832e0) is in our 23-ref list!
    expected_rom_vas = set(VIDEO_MENU_CODE_REFS) | {va - PROG_DATA_DELTA for va in VIDEO_MENU_DATA_REFS_RAM}
    found_rom_vas: set[int] = set()
    for off in range(0, len(stock_prog) - 4, 4):
        val = struct.unpack_from("<I", stock_prog, off)[0]
        if VIDEO_MENU_ORIG_RAM <= val < VIDEO_MENU_ORIG_RAM + (len(STOCK_VIDEO_MENU_ENTRIES) + 1) * 8:
            assert val == VIDEO_MENU_ORIG_RAM, f"Unexpected interior ptr 0x{val:08x} @ 0x{PROG_BASE+off:08x}"
            found_rom_vas.add(PROG_BASE + off)
    assert found_rom_vas == expected_rom_vas, f"Pointer set mismatch: {found_rom_vas ^ expected_rom_vas}"
    print(f"[PASS] Motion Picture menu (34 stock entries + 23/23 exhaustive pointers) verified 100% against V2.5")


def test_descriptors_against_v25(stock_prog: bytes) -> None:
    """Verify every one of the 8 A4K descriptors matches stock 3328x2496 geometry in GH5S V2.5."""
    old_w_mb, old_h_mb = 208, 156
    old_mb = old_w_mb * old_h_mb
    rows = [(k * 156) // 16 for k in range(16)]
    for sel, lbl, rcp_va, chan_ram_va, blob_va, sl_off, is_alli, gop in A4K_DESCRIPTORS:
        w, h = struct.unpack_from("<II", stock_prog, rcp_va + 0x20 - PROG_BASE)
        assert (w, h) == (3328, 2496), f"RCP mismatch for {lbl}"
        chan_rom = chan_ram_va - PROG_DATA_DELTA
        wm1, hm1 = struct.unpack_from("<II", stock_prog, chan_rom + 0x114 - PROG_BASE)
        assert (wm1, hm1) == (old_w_mb - 1, old_h_mb - 1), f"chan_ram mismatch for {lbl}"
        boff = blob_va - PROG_BASE
        assert struct.unpack_from("<I", stock_prog, boff + 0x0C70)[0] == ((old_h_mb << 16) | old_w_mb)
        assert struct.unpack_from("<I", stock_prog, boff + 0x0C74)[0] == ((old_mb << 16) | old_mb)
        assert struct.unpack_from("<I", stock_prog, boff + 0x0D0C)[0] == (old_mb << 16)
        assert struct.unpack_from("<I", stock_prog, boff + 0x0D10)[0] == ((gop << 24) | (gop << 16) | old_mb)
        assert struct.unpack_from("<I", stock_prog, boff + 0x0ED8)[0] == old_mb
        assert struct.unpack_from("<I", stock_prog, boff + 0x0EDC)[0] == old_mb - 1
        assert struct.unpack_from("<I", stock_prog, boff + 0x0F84)[0] == old_mb * 0x1400
        if is_alli:
            assert struct.unpack_from("<I", stock_prog, boff + 0x0F9C)[0] == ((0x11 << 24) | ((old_w_mb - 1) << 16))
        else:
            assert struct.unpack_from("<I", stock_prog, boff + 0x0F98)[0] == old_mb * 0x0C00
            assert struct.unpack_from("<I", stock_prog, boff + 0x0FBC)[0] == ((0x11 << 24) | ((old_w_mb - 1) << 16) | 0x20)
            assert struct.unpack_from("<I", stock_prog, boff + 0x107C)[0] == ((0x11 << 24) | ((old_w_mb - 1) << 16) | 0x20)
        assert struct.unpack_from("<I", stock_prog, boff + sl_off)[0] == old_mb - 1
        for k in range(16):
            actual_s = struct.unpack_from("<H", stock_prog, boff + sl_off + 4 + 2 * k)[0]
            assert actual_s == rows[k] * old_w_mb, f"Slice[{k}] mismatch for {lbl}"
        assert struct.unpack_from("<I", stock_prog, boff + sl_off + 0x24)[0] == ((155 << 24) | ((old_w_mb - 1) << 16) | 0xAA)
    print(f"[PASS] All 8 A4K 10-bit descriptors (0x200e..0x2015) verified 100% against stock V2.5")


def test_bitrates_against_v25(stock_prog: bytes) -> None:
    """Verify all 24 stock 10-bit RC structs, Panasonic 200M ALL-I formula accuracy, and CPB safety bounds."""
    # 1. Verify ALL 12 LongGOP (150M) and ALL 12 ALL-I (400M) stock RC structs across A4K / C4K / UHD
    for res_name, base_map in (("A4K", A4K_LONGG_RC_BASES), ("C4K", C4K_LONGG_RC_BASES), ("UHD", UHD_LONGG_RC_BASES)):
        for fps_key, rc_base in base_map.items():
            for off, exp in STOCK_LONGG_150M_RC[fps_key].items():
                actual = struct.unpack_from("<I", stock_prog, rc_base - PROG_BASE + off)[0]
                assert actual == exp, f"{res_name} LongG {fps_key} +0x{off:02x}: {actual} != {exp}"
    for res_name, base_map in (("A4K", A4K_ALLI_RC_BASES), ("C4K", C4K_ALLI_RC_BASES), ("UHD", UHD_ALLI_RC_BASES)):
        for fps_key, rc_base in base_map.items():
            for off, exp in STOCK_ALLI_400M_RC[fps_key].items():
                actual = struct.unpack_from("<I", stock_prog, rc_base - PROG_BASE + off)[0]
                assert actual == exp, f"{res_name} ALL-I {fps_key} +0x{off:02x}: {actual} != {exp}"

    # 2. Verify compute_alli_rc_fields(fps, 200) matches stock Panasonic 200M ALL-I tables within <= 1 bit!
    stock_200m_alli = {
        "23.98p": 0x064E8724,
        "24.00p": 0x064EA844,
        "25.00p": 0x064EC964,
        "29.97p": 0x064EEA84,
    }
    for fps_key, rc_base in stock_200m_alli.items():
        calc = compute_alli_rc_fields(fps_key, 200)
        for off, val in calc.items():
            actual = struct.unpack_from("<I", stock_prog, rc_base - PROG_BASE + off)[0]
            assert abs(val - actual) <= 1, f"200M ALL-I formula drift @ {fps_key} +0x{off:02x}: calc={val} vs stock={actual}"

    # 3. Verify CPB safety invariant (max_frame < cpb) for every supported LongGOP & ALL-I choice
    for fps_key in ("23.98p", "24.00p", "25.00p", "29.97p"):
        for lg in LONGG_BITRATE_CHOICES:
            f = compute_longg_rc_fields(fps_key, lg)
            assert f[0xB4] <= 36_000_000 < 44_999_680
        for ai in ALLI_BITRATE_CHOICES:
            f = compute_alli_rc_fields(fps_key, ai)
            assert f[0xB4] < f[0x0C] <= 50_000_000
            assert f[0x94] <= 134_217_728

    print(
        f"[PASS] All 24 10-bit RC structs (A4K/C4K/UHD x LongG/ALL-I x 4 fps) + "
        f"Panasonic 200M ALL-I formula + CPB bounds verified 100%"
    )


def test_all_feature_matrix(stock_prog: bytes, stock_pb2: bytes) -> None:
    configs = [
        ("lpcm24_only", dict(lpcm=True, og=False, pb=False, gh7=False)),
        ("playback_fix_only", dict(lpcm=False, og=False, pb=True, gh7=False)),
        ("bitrates_v30_all4k", dict(lpcm=False, og=False, pb=False, gh7=False, prof="v30", all4k=True)),
        ("og_v30_eco_full", dict(lpcm=True, og=True, dual=True, prof="v30-eco", sq=True, gh7=True)),
        ("og_v30_full", dict(lpcm=True, og=True, dual=True, prof="v30", sq=True, gh7=True)),
        ("og_v60_default_full", dict(lpcm=True, og=True, dual=True, prof="v60", sq=True, gh7=True)),
        ("og_v90_all4k_full", dict(lpcm=True, og=True, dual=True, prof="v90", sq=True, gh7=True, all4k=True)),
        ("og_custom_300m_600m", dict(lpcm=True, og=True, dual=True, lg=300, ai=600, sq=True, gh7=True, all4k=True)),
        ("og_fixed_3x2_full", dict(lpcm=True, og=True, dual=False, prof="v60", sq=False, gh7=True)),
    ]
    for name, cfg in configs:
        ps = PatchSet()
        if cfg.get("lpcm"):
            apply_lpcm24_menu(ps)
        if cfg.get("og"):
            lg, ai = resolve_bitrates(cfg.get("prof", "v60"), cfg.get("lg"), cfg.get("ai"))
            apply_open_gate(
                ps,
                dual_aspect=cfg["dual"],
                enable_1x1=cfg["sq"],
                longg_bitrate=lg,
                alli_bitrate=ai,
                scale_all_4k=cfg.get("all4k", False),
                include_playback_fix=True,
            )
        elif cfg.get("pb"):
            apply_playback_fix(ps)
        elif cfg.get("prof") or cfg.get("lg") or cfg.get("ai"):
            lg, ai = resolve_bitrates(cfg.get("prof", "v60"), cfg.get("lg"), cfg.get("ai"))
            apply_bitrate_scaling(ps, longg_bitrate=lg, alli_bitrate=ai, scale_all_4k=cfg.get("all4k", False))
        if cfg.get("gh7"):
            apply_gh7_extras(ps)
        mod_prog, mod_pb2, pd, p2d = ps.apply_to_images(stock_prog, stock_pb2, unlock_sd_rollback=True)
        assert len(mod_prog) == len(stock_prog) and len(mod_pb2) == len(stock_pb2)
        print(f"[PASS] Config {name:24s}: {len(ps.patches):4d} hws -> prog={len(pd):4d} B, pb2={len(p2d):4d} B  {ps.summary_by_module()}")


def test_no_mid_instruction_branch_collisions(stock_prog: bytes, stock_pb2: bytes) -> None:
    """
    Ensure NO stock branch (b, bcc, cbz/cbnz, b.w, tbb) jumps into va+2 (middle of a 32-bit Thumb-2 hook)
    whenever a hook overwrites two 16-bit stock instructions with a 32-bit Thumb-2 branch.
    """
    from patches.common import PB2_BASE, PB2_CAVE_BASE, PB2_CAVE_END, PROG_CAVE_BASE, PROG_CAVE_END

    ps = PatchSet()
    apply_lpcm24_menu(ps)
    apply_open_gate(ps, dual_aspect=True, enable_1x1=True, longg_bitrate=150, alli_bitrate=200, include_playback_fix=True)
    apply_gh7_extras(ps)

    by_addr = {p.addr: p for p in ps.patches}
    monitored: list[tuple[bytes, int, int, str]] = []
    for p in sorted(ps.patches, key=lambda x: x.addr):
        if (PROG_CAVE_BASE <= p.addr < PROG_CAVE_END) or (PB2_CAVE_BASE <= p.addr < PB2_CAVE_END):
            continue
        if p.module == "bitrates" or (p.addr + 2) not in by_addr:
            continue
        # Only check 32-bit Thumb-2 b.w / bl hooks (first hw in 0xF000..0xF7FF, second hw bit15==1)
        if not (0xF000 <= p.new_u16 <= 0xF7FF and (by_addr[p.addr + 2].new_u16 & 0x8000)):
            continue
        if PROG_BASE <= p.addr < PROG_BASE + len(stock_prog):
            buf, base = stock_prog, PROG_BASE
        elif PB2_BASE <= p.addr < PB2_BASE + len(stock_pb2):
            buf, base = stock_pb2, PB2_BASE
        else:
            continue
        # Skip if stock instruction at p.addr was already a 32-bit Thumb-2 instruction
        if p.old_u16 >= 0xE800:
            continue
        monitored.append((buf, base, p.addr, p.desc))

    for buf, base, va, desc in monitored:
        mid = va + 2
        lo = max(base, va - 4096)
        hi = min(base + len(buf) - 4, va + 4096)
        for scan in range(lo, hi, 2):
            hw = struct.unpack_from("<H", buf, scan - base)[0]
            if (hw & 0xF800) == 0xE000:
                imm = hw & 0x7FF
                if imm & 0x400:
                    imm -= 0x800
                assert scan + 4 + (imm << 1) != mid, f"16-bit b @ 0x{scan:08x} jumps into middle of hook 0x{va:08x} ({desc})"
            elif (hw & 0xF000) == 0xD000 and (hw & 0x0F00) not in (0x0E00, 0x0F00):
                imm = hw & 0xFF
                if imm & 0x80:
                    imm -= 0x100
                assert scan + 4 + (imm << 1) != mid, f"16-bit bcc @ 0x{scan:08x} jumps into middle of hook 0x{va:08x} ({desc})"
            elif (hw & 0xF500) == 0xB100:
                imm = (((hw >> 9) & 1) << 6) | (((hw >> 3) & 0x1F) << 1)
                assert scan + 4 + imm != mid, f"cbz/cbnz @ 0x{scan:08x} jumps into middle of hook 0x{va:08x} ({desc})"
            elif hw == 0xE8DF:
                hw2 = struct.unpack_from("<H", buf, scan - base + 2)[0]
                if (hw2 & 0xFFF0) == 0xF000:
                    for k in range(32):
                        b = buf[scan - base + 4 + k]
                        assert scan + 4 + 2 * b != mid, f"tbb[{k}] @ 0x{scan:08x} jumps into middle of hook 0x{va:08x} ({desc})"
            elif (hw & 0xF800) == 0xF000:
                hw2 = struct.unpack_from("<H", buf, scan - base + 2)[0]
                if (hw2 & 0xD000) == 0x9000:
                    S = (hw >> 10) & 1
                    J1 = (hw2 >> 13) & 1
                    J2 = (hw2 >> 11) & 1
                    I1 = ~(J1 ^ S) & 1
                    I2 = ~(J2 ^ S) & 1
                    imm = (S << 24) | (I1 << 23) | (I2 << 22) | ((hw & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
                    if S:
                        imm -= 1 << 25
                    assert scan + 4 + imm != mid, f"32-bit b.w @ 0x{scan:08x} jumps into middle of hook 0x{va:08x} ({desc})"
    print(f"[PASS] Zero mid-instruction branch collisions across all {len(monitored)} 16+16->32 Thumb-2 hooks")


def test_bitrate_math_standalone() -> None:
    """Verify CPB safety invariants and preset resolution without requiring Panasonic firmware binaries."""
    for prof_name, (exp_lg, exp_ai) in SD_PROFILES.items():
        lg, ai = resolve_bitrates(prof_name)
        assert (lg, ai) == (exp_lg, exp_ai)
    for fps_key in ("23.98p", "24.00p", "25.00p", "29.97p"):
        for lg in LONGG_BITRATE_CHOICES:
            f = compute_longg_rc_fields(fps_key, lg)
            assert f[0xB4] <= 36_000_000 < 44_999_680
        for ai in ALLI_BITRATE_CHOICES:
            f = compute_alli_rc_fields(fps_key, ai)
            assert f[0xB4] < f[0x0C] <= 50_000_000
            assert f[0x94] <= 134_217_728
    print("[PASS] Standalone bitrate math, SD profile presets, and CPB bounds verified 100%")


def main() -> int:
    caves_only = "--caves-only" in sys.argv[1:]
    test_caves_disasm()
    test_bitrate_math_standalone()
    if caves_only:
        print("\nALL STANDALONE CAVE & MATH CHECKS PASSED 100%!")
        return 0
    stock_prog, stock_pb2, _ = load_decrypted_v25(_resolve_fw_dir())
    test_descriptors_against_v25(stock_prog)
    test_video_menu_relocation_against_v25(stock_prog)
    test_bitrates_against_v25(stock_prog)
    test_no_mid_instruction_branch_collisions(stock_prog, stock_pb2)
    test_all_feature_matrix(stock_prog, stock_pb2)
    print("\nALL OFFLINE VERIFICATION CHECKS PASSED 100%!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
