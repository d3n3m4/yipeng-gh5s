"""
100% Volatile USB Service Mode (04da:2370) RAM Deployer / Verifier / Restorer.
ZERO flash/NAND/EEPROM writes: power-cycling (battery out 5s) always restores factory stock.

Atomic Live-RAM Architecture:
  Writing 32-bit Thumb-2/ARM32 instructions one 16-bit halfword at a time over USB while LiveView
  is running can expose a half-written ("torn") instruction to L2/I-cache. To eliminate that race
  completely, `run_ram_action` NEVER writes live hook addresses directly over USB:
    1. All passive regions (code caves + bitrate RC tables) are staged over USB.
    2. A tiny IRQ-disabled Thumb-2 atomic installer (`ATOMIC_CODE_VA = 0x06AA2000`) and a packed
       hook table (`ATOMIC_TBL_VA = 0x06AA2100`) are written into dead 0xFF cave padding (both
       cached & uncached mirrors).
    3. A single `postboot5` opcode 0x80 call jumps to `0x06AA2001`, disables IRQs, flushes all
       passive caves to DRAM (SMP-broadcast `DCCIMVAC`, 32B stride), atomically writes every
       32-bit/16-bit hook in < 20 us, cleans L1+L2 D-cache (`FN_CLEAN_DCACHE_ALL`), invalidates
       I-cache & branch predictor (`ICIALLUIS`/`ICIALLU`/`BPIALLIS`), and re-enables IRQs.
    4. `finally:` restores the original 20 bytes of `postboot5:0x077BBBB8`, and post-apply
       readback verifies `cached == uncached == target_val` on 100% of hooks and every 32B line!
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

from patches.asm import ThumbBuilder
from patches.common import (
    G_ACTIVE_A4K_W,
    G_CONFIG_FLAGS,
    PB2_CAVE_BASE,
    PB2_CAVE_END,
    PB5_BASE,
    PROG_CAVE_BASE,
    PROG_CAVE_END,
    HalfwordPatch,
    PatchSet,
    SD_ROLLBACK_FLAG_VA,
)

# Temporary postboot5 op80 trampoline (20 B, restored in finally:) -> jumps to ATOMIC_CODE_VA
STUB_ADDR = 0x077BBBB8
OP80_ORIG_TAIL = 0x077BAEAA
ATOMIC_CODE_VA = 0x06AA2000
ATOMIC_TBL_VA = 0x06AA2100
FN_CLEAN_DCACHE_ALL = 0x06395B8B


def build_atomic_hook_installer() -> tuple[bytes, bytes]:
    """Build (stub_20B @ 0x077BBBB8, SMP-safe atomic_code @ 0x06AA2000), strictly 8B stack aligned."""
    sb = ThumbBuilder(STUB_ADDR)
    sb.push([0], lr=True)
    sb.mov32(1, ATOMIC_CODE_VA | 1)
    sb.hw(0x4788)                           # blx r1
    sb.pop([0], lr=True)
    sb.b_w(OP80_ORIG_TAIL)
    stub = sb.finish()
    assert len(stub) == 20

    tb = ThumbBuilder(ATOMIC_CODE_VA)
    tb.push([0, 1, 2, 3, 4], lr=True)       # 6 regs = 24B (strict 8B AAPCS alignment)
    tb.hw(0xF3EF, 0x8400)                   # mrs r4, apsr (save CPSR/IRQ state)
    tb.hw(0xF044, 0x0080)                   # orr.w r0, r4, #128 (disable IRQ bit I)
    tb.hw(0xF380, 0x8100)                   # msr CPSR_c, r0

    # Step A: SMP-broadcast DCCIMVAC across ALL staged passive caves & RC tables BEFORE applying hooks!
    # (Unlike DCCSW, DCCIMVAC broadcasts by MVA across all 4 Cortex-A9 SMP cores via SCU!)
    for idx, (r_start, r_end) in enumerate((
        (0x06AA1000, 0x06AA2000),           # prog caves + relocated Video menu
        (0x076EE000, 0x076EE800),           # postboot2 cave
        (0x064D0000, 0x064F2000),           # all 24 GAIA bitrate RC structs
    )):
        tb.mov32(0, r_start)
        tb.mov32(1, r_end)
        lbl = f"flush_a_{idx}"
        tb.label(lbl)
        tb.hw(0xEE07, 0x0F3E)               # mcr p15, #0, r0, c7, c14, #1 (DCCIMVAC)
        tb.adds_imm8(0, 32)                 # 32B stride covers both 32B L1 and 64B L2 lines!
        tb.cmp_reg(0, 1)
        tb.b(lbl, cond="lo")
    tb.hw(0xF3BF, 0x8F4F)                   # dsb sy

    # Step B: apply all packed hooks atomically with IRQs OFF + SMP-broadcast DCCIMVAC per hook!
    tb.mov32(0, ATOMIC_TBL_VA)
    tb.label("loop")
    tb.hw(0xF850, 0x1B04)                   # ldr r1, [r0], #4  (addr; 0 = end)
    tb.cbz(1, "done")
    tb.hw(0xF850, 0x2B04)                   # ldr r2, [r0], #4  (val)
    tb.tst_imm(1, 1)                        # bit0 == 1 -> single 16-bit halfword write
    tb.b("write16", cond="ne")
    tb.tst_imm(1, 2)                        # bit1 == 1 -> 2B-aligned 32-bit write
    tb.b("write32_half", cond="ne")
    tb.str_(2, 1, 0)                        # single-cycle atomic 32-bit str
    tb.hw(0xEE07, 0x1F3E)                   # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC)
    tb.b("loop")
    tb.label("write32_half")
    tb.strh(2, 1, 0)                        # 2-cycle back-to-back strh with IRQs OFF
    tb.lsrs_imm(2, 2, 16)
    tb.strh(2, 1, 2)
    tb.hw(0xEE07, 0x1F3E)                   # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC line 0)
    tb.adds_imm3(1, 1, 2)
    tb.hw(0xEE07, 0x1F3E)                   # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC line +2 if straddling 64B)
    tb.b("loop")
    tb.label("write16")
    tb.hw(0xF021, 0x0101)                   # bic.w r1, r1, #1
    tb.strh(2, 1, 0)
    tb.hw(0xEE07, 0x1F3E)                   # mcr p15, #0, r1, c7, c14, #1 (DCCIMVAC)
    tb.b("loop")

    # Step C: flush local L1/L2 + SMP-broadcast I-cache & branch predictor invalidate, then restore IRQs!
    tb.label("done")
    tb.hw(0xF3BF, 0x8F4F)                   # dsb sy
    tb.mov32(1, FN_CLEAN_DCACHE_ALL)
    tb.hw(0x4788)                           # blx r1
    tb.movs(1, 0)
    tb.hw(0xEE07, 0x1F11)                   # mcr p15, #0, r1, c7, c1, #0 (ICIALLUIS)
    tb.hw(0xEE07, 0x1F15)                   # mcr p15, #0, r1, c7, c5, #0 (ICIALLU)
    tb.hw(0xEE07, 0x1FD1)                   # mcr p15, #0, r1, c7, c1, #6 (BPIALLIS)
    tb.hw(0xF3BF, 0x8F4F)                   # dsb sy
    tb.hw(0xF3BF, 0x8F6F)                   # isb sy
    tb.hw(0xF384, 0x8100)                   # msr CPSR_c, r4 (restore original IRQ state!)
    tb.pop([0, 1, 2, 3, 4], pc=True)
    tb.align4()
    code = tb.finish()
    assert len(code) <= ATOMIC_TBL_VA - ATOMIC_CODE_VA, f"atomic_code overflow: {len(code)} B"
    return stub, code


def _pack_hook_table(hooks: list[HalfwordPatch], apply_mode: bool) -> bytes:
    """Coalesce adjacent (addr, addr+2) hook halfwords into atomic 32-bit table entries."""
    items = sorted(
        ((p.addr, p.new_u16 if apply_mode else p.old_u16) for p in hooks),
        key=lambda x: x[0],
    )
    out = bytearray()
    i = 0
    while i < len(items):
        addr, v0 = items[i]
        assert addr != SD_ROLLBACK_FLAG_VA and (addr & 1) == 0
        if i + 1 < len(items) and items[i + 1][0] == addr + 2:
            v1 = items[i + 1][1]
            out.extend(struct.pack("<II", addr, v0 | (v1 << 16)))
            i += 2
        else:
            out.extend(struct.pack("<II", addr | 1, v0))
            i += 1
    out.extend(struct.pack("<I", 0))  # terminator
    assert ATOMIC_TBL_VA + len(out) <= PROG_CAVE_END, f"hook table too big: {len(out)} B"
    return bytes(out)


def _build_16_req(op: int, addr: int = 0, val16: int = 0) -> bytes:
    pkt = bytearray(16)
    pkt[0:4] = bytes((0x08, 0x00, 0xFF, op))
    pkt[4:8] = addr.to_bytes(4, "little")
    pkt[8:10] = val16.to_bytes(2, "little")
    pkt[14] = sum(pkt[1:14]) & 0xFF
    return bytes(pkt)


def _send_16(r, pkt: bytes) -> bytes:
    r.prepare(0)
    assert r.dev.write(0x01, pkt, timeout=1500) == 16
    time.sleep(0.01)
    r.prepare(1)
    kind = bytes(r.dev.read(0x82, 1, timeout=1500))
    assert kind == b"\x08", f"Unexpected kind {kind.hex()}"
    r.prepare(1)
    rep = bytes(r.dev.read(0x82, 16, timeout=1500))
    assert len(rep) == 16 and rep[15] == 0 and rep[14] == (sum(rep[1:14]) & 0xFF), f"Bad reply {rep.hex()}"
    return rep


def read_u16(r, addr: int) -> int:
    rep = _send_16(r, _build_16_req(0x9B, addr))
    assert rep[:4] == b"\x08\x00\xff\x36", f"Bad read reply @ 0x{addr:08x}: {rep.hex()}"
    return struct.unpack_from("<H", rep, 4)[0]


def read_u32(r, addr: int) -> int:
    return read_u16(r, addr) | (read_u16(r, addr + 2) << 16)


def _write_u16_cached(r, addr: int, val16: int, allowed: set[int] | None) -> None:
    c_addr = addr & ~0x80000000
    assert c_addr != SD_ROLLBACK_FLAG_VA, "SAFETY BLOCK: never touch flash rollback flag in RAM test"
    if allowed is not None:
        assert c_addr in allowed, f"SAFETY BLOCK: 0x{c_addr:08x} not in active PatchSet whitelist"
    ack = _send_16(r, _build_16_req(0x9C, c_addr, val16))
    assert ack[:5] == b"\x08\x00\xff\x01\x00", f"Cached write failed @ 0x{c_addr:08x}: {ack.hex()}"


def _write_u16_both(r, addr: int, val16: int, allowed: set[int] | None) -> None:
    c_addr = addr & ~0x80000000
    u_addr = addr | 0x80000000
    assert c_addr != SD_ROLLBACK_FLAG_VA, "SAFETY BLOCK: never touch flash rollback flag in RAM test"
    if allowed is not None:
        assert c_addr in allowed, f"SAFETY BLOCK: 0x{c_addr:08x} not in active PatchSet whitelist"
    for target in (c_addr, u_addr):
        ack = _send_16(r, _build_16_req(0x9C, target, val16))
        assert ack[:5] == b"\x08\x00\xff\x01\x00", f"Write failed @ 0x{target:08x}: {ack.hex()}"
    gc = read_u16(r, c_addr)
    gu = read_u16(r, u_addr)
    if gc != val16 or gu != val16:
        raise RuntimeError(
            f"Readback mismatch @ 0x{c_addr:08x}: cached=0x{gc:04x}, uncached=0x{gu:04x}, exp=0x{val16:04x}"
        )


class _UsbServiceDevice:
    """Minimal self-contained PyUSB transport for Panasonic DC-GH5S Service Mode (04da:2370)."""

    VID = 0x04DA
    PID = 0x2370

    def __init__(self) -> None:
        try:
            import usb.core  # type: ignore
            import usb.util  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PyUSB is required for USB Service Mode (`pip install pyusb`).") from exc

        self._usb_util = usb.util
        self.dev = usb.core.find(idVendor=self.VID, idProduct=self.PID)
        if self.dev is None:
            raise RuntimeError("DC-GH5S in USB Service Mode (04da:2370) not found.")
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) != (0xFF, 0xA0, 0xB0):
            raise RuntimeError(
                f"Unexpected USB interface {intf.bInterfaceClass:02x}/"
                f"{intf.bInterfaceSubClass:02x}/{intf.bInterfaceProtocol:02x}"
            )
        usb.util.claim_interface(self.dev, 0)
        self.claimed = True

    def prepare(self, index: int) -> None:
        assert index in (0, 1)
        ret = self.dev.ctrl_transfer(0x40, 0x00, 0x0000, index, None, timeout=1500)
        if ret != 0:
            raise RuntimeError(f"USB prepare({index}) returned {ret}")

    def close(self) -> None:
        if getattr(self, "claimed", False) and self.dev is not None:
            try:
                self._usb_util.release_interface(self.dev, 0)
            except Exception:
                pass
            self._usb_util.dispose_resources(self.dev)
            self.claimed = False


def _open_service_mode_device(probe_module_dir: Path | None = None):
    if probe_module_dir is not None and (probe_module_dir / "gh5s_service_probe.py").exists():
        if str(probe_module_dir) not in sys.path:
            sys.path.insert(0, str(probe_module_dir))
        from gh5s_service_probe import ServiceReader  # type: ignore

        return ServiceReader()
    return _UsbServiceDevice()


def dump_live_status(r) -> None:
    aspect = read_u16(r, 0x05FCF0D0)
    xlr_eep = read_u16(r, 0x05FCF25C) & 0xFF
    xlr_eff = read_u16(r, 0x0837369C) & 0xFF
    container = read_u16(r, 0x08373512) & 0xFF
    active_w = read_u32(r, G_ACTIVE_A4K_W)
    cfg_flags = read_u32(r, G_CONFIG_FLAGS)
    aspect_names = {0x0100: "4:3", 0x0200: "3:2", 0x0300: "16:9", 0x0400: "1:1"}
    print(f"  [LIVE] Aspect Ratio [0x05fcf0d0] : 0x{aspect:04x} ({aspect_names.get(aspect, '?')})")
    print(f"  [LIVE] XLR setting  [0x05fcf25c] : {xlr_eep} (0=OFF, 1=48k/16b, 2=48k/24b, 3=96k/24b)")
    print(f"  [LIVE] XLR hw eff   [0x0837369c] : {xlr_eff} (0 without physical DMW-XLR1)")
    print(f"  [LIVE] Container    [0x08373512] : {container} (6=MOV)")
    print(f"  [LIVE] G_ACTIVE_A4K_W            : {active_w} (0x{active_w:08x})")
    rcp_w = read_u32(r, 0x06538D80)
    chan_wmb1 = read_u32(r, 0x0819488C)
    blob_c70 = read_u32(r, 0x064DF55C)
    sps0_w = read_u32(r, 0x07375714)
    print(f"  [LIVE] 24p LongG RCP/chan/blob   : W={rcp_w}, chan_wmb-1={chan_wmb1}, blob+c70=0x{blob_c70:08x}, SPS0=0x{sps0_w:x}")
    mmu_06a = read_u32(r, 0x85F90000 + (0x06A * 4))
    mmu_073 = read_u32(r, 0x85F90000 + (0x073 * 4))
    mmu_076 = read_u32(r, 0x85F90000 + (0x076 * 4))
    print(f"  [LIVE] MMU L1 (06A/073/076)      : 0x{mmu_06a:08x} / 0x{mmu_073:08x} / 0x{mmu_076:08x}")
    if cfg_flags != 0xFFFFFFFF:
        longg_m = (cfg_flags >> 8) & 0xFFF
        alli_m = (cfg_flags >> 20) & 0xFFF
        flags_str = (
            f"dual={int(bool(cfg_flags & 8))} 1x1={int(bool(cfg_flags & 1))} "
            f"2997p={int(bool(cfg_flags & 2))} all4k={int(bool(cfg_flags & 16))}"
        )
        if longg_m or alli_m:
            flags_str += f" LongG={longg_m}M ALL-I={alli_m}M"
        print(f"  [LIVE] G_CONFIG_FLAGS            : 0x{cfg_flags:08x} ({flags_str})")
    else:
        print(f"  [LIVE] G_CONFIG_FLAGS            : 0x{cfg_flags:08x} (stock/unpatched)")


def run_ram_action(
    ps: PatchSet,
    stock_pb5: bytes,
    action: str,
    probe_module_dir: Path | None = None,
) -> int:
    """
    action in {"verify", "status", "apply", "restore"}:
      - verify  : read-only check comparing live RAM to stock & patched values
      - status  : read-only dump of live camera menu/aspect/audio state
      - apply   : atomic IRQ-disabled volatile RAM apply + L1/L2 D-cache clean & I-cache flush
      - restore : atomic IRQ-disabled volatile RAM restore to stock V2.5 + cache flush
    """
    stub_atomic, atomic_code = build_atomic_hook_installer()
    orig_stub = stock_pb5[STUB_ADDR - PB5_BASE : STUB_ADDR - PB5_BASE + len(stub_atomic)]
    assert len(orig_stub) == len(stub_atomic)

    r = _open_service_mode_device(probe_module_dir)
    try:
        print(f"Connected to GH5S Service Mode (04da:2370). Patch count: {len(ps.patches)} halfwords.", flush=True)

        if action == "status":
            dump_live_status(r)
            return 0

        stock_cnt = patched_cnt = other_cnt = 0
        others = []
        live_cached: dict[int, int] = {}
        for p in ps.patches:
            cur = read_u16(r, p.addr)
            live_cached[p.addr] = cur
            if cur == p.old_u16:
                stock_cnt += 1
            elif cur == p.new_u16:
                patched_cnt += 1
            else:
                other_cnt += 1
                others.append((p, cur))

        print(f"  Live RAM state: {stock_cnt} stock, {patched_cnt} patched, {other_cnt} other", flush=True)
        for p, cur in others[:12]:
            print(f"    OTHER @ 0x{p.addr:08x} ({p.module}: {p.desc}): live=0x{cur:04x}, stock=0x{p.old_u16:04x}, new=0x{p.new_u16:04x}")
        dump_live_status(r)

        if action == "verify":
            return 0

        assert action in ("apply", "restore")
        apply_mode = action == "apply"
        if apply_mode and other_cnt > 0:
            raise RuntimeError(
                f"SAFETY ABORT: {other_cnt} live RAM halfwords match neither stock V2.5 nor target patch. "
                "If an older custom firmware is flashed to NAND, restore stock V2.5 first."
            )
        allowed = {p.addr for p in ps.patches}

        def _is_passive(p: HalfwordPatch) -> bool:
            return (
                (PROG_CAVE_BASE <= p.addr < PROG_CAVE_END)
                or (PB2_CAVE_BASE <= p.addr < PB2_CAVE_END)
                or p.module == "bitrates"
            )

        passive = [p for p in ps.patches if _is_passive(p)]
        hooks = [p for p in ps.patches if not _is_passive(p)]

        # 1. Stage all passive caves/data in cached memory (fast, zero live-code risk; flushed in op80 Step A!)
        w_passive = 0
        for idx, p in enumerate(passive, 1):
            target_val = p.new_u16 if apply_mode else p.old_u16
            if live_cached[p.addr] != target_val:
                _write_u16_cached(r, p.addr, target_val, allowed)
                w_passive += 1
            if idx % 350 == 0 or idx == len(passive):
                print(f"  [{action.upper()} CAVES/DATA] {idx}/{len(passive)} ({w_passive} staged)", flush=True)

        # 2. Write IRQ-disabled atomic hook installer + packed hook table into dead 0xFF cave (cached + uncached)
        hook_tbl = _pack_hook_table(hooks, apply_mode=apply_mode)
        for off in range(0, len(atomic_code), 2):
            _write_u16_both(r, ATOMIC_CODE_VA + off, struct.unpack_from("<H", atomic_code, off)[0], allowed=None)
        for off in range(0, len(hook_tbl), 2):
            _write_u16_both(r, ATOMIC_TBL_VA + off, struct.unpack_from("<H", hook_tbl, off)[0], allowed=None)
        print(
            f"  [{action.upper()} ATOMIC] Loaded {len(atomic_code)} B installer + "
            f"{len(hook_tbl)} B hook table ({len(hooks)} halfwords).",
            flush=True,
        )

        # 3. Execute ONE atomic op80: flush caves -> IRQs-OFF hook install -> flush L1/L2 + I-cache invalidate!
        try:
            for off in range(0, len(stub_atomic), 2):
                _write_u16_both(r, STUB_ADDR + off, struct.unpack_from("<H", stub_atomic, off)[0], allowed=None)
            rep = _send_16(r, _build_16_req(0x80, 0, 0))
            print(f"  [ATOMIC-OP80] IRQ-disabled hook install + L1/L2/I-cache flush OK (reply={rep[:6].hex()})", flush=True)
        finally:
            for off in range(0, len(orig_stub), 2):
                _write_u16_both(r, STUB_ADDR + off, struct.unpack_from("<H", orig_stub, off)[0], allowed=None)
            print("  [CLEANUP] Original postboot5 op80 stub restored.", flush=True)

        # 4. Verify 100% of hooks + passive caves landed identically in BOTH cached & uncached DRAM!
        for p in hooks:
            target_val = p.new_u16 if apply_mode else p.old_u16
            gc = read_u16(r, p.addr)
            gu = read_u16(r, p.addr | 0x80000000)
            if gc != target_val or gu != target_val:
                raise RuntimeError(
                    f"Post-atomic hook mismatch @ 0x{p.addr:08x} ({p.desc}): "
                    f"cached=0x{gc:04x}, uncached=0x{gu:04x}, exp=0x{target_val:04x}"
                )
        # Verify EVERY 32-byte half-cacheline across ALL passive caves/data landed in uncached DRAM!
        checked_lines = 0
        seen_lines: set[int] = set()
        for p in passive:
            line = p.addr & ~31
            if line in seen_lines:
                continue
            seen_lines.add(line)
            target_val = p.new_u16 if apply_mode else p.old_u16
            gu = read_u16(r, p.addr | 0x80000000)
            if gu != target_val:
                raise RuntimeError(
                    f"Post-atomic passive DRAM mismatch @ 0x{p.addr:08x} ({p.desc}): "
                    f"uncached=0x{gu:04x} != exp=0x{target_val:04x}"
                )
            checked_lines += 1

        print(
            f"  [{action.upper()}] Verified 100% ({len(hooks)}/{len(hooks)} hooks cached == uncached, "
            f"{checked_lines}/{checked_lines} passive 32B lines verified in DRAM).",
            flush=True,
        )
        return 0
    finally:
        r.close()
