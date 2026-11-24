"""
Pure-Python ARM32 and Thumb-2 instruction encoders + mini label-resolving assemblers.
Zero external dependencies at runtime; verified via llvm-mc roundtrip in tests/test_offline.py.
"""
from __future__ import annotations

import struct


# ---------------------------------------------------------------------------
# Thumb-2 scalar encoders
# ---------------------------------------------------------------------------

def thumb_movw(rd: int, val: int) -> bytes:
    """Thumb-2 `movw rD, #imm16` (T3 encoding)."""
    assert 0 <= rd <= 12 and 0 <= val <= 0xFFFF
    imm4 = (val >> 12) & 0xF
    i = (val >> 11) & 1
    imm3 = (val >> 8) & 0x7
    imm8 = val & 0xFF
    hw1 = 0xF240 | (i << 10) | imm4
    hw2 = (imm3 << 12) | (rd << 8) | imm8
    return struct.pack("<HH", hw1, hw2)


def thumb_movt(rd: int, val: int) -> bytes:
    """Thumb-2 `movt rD, #imm16` (T1 encoding)."""
    assert 0 <= rd <= 12 and 0 <= val <= 0xFFFF
    imm4 = (val >> 12) & 0xF
    i = (val >> 11) & 1
    imm3 = (val >> 8) & 0x7
    imm8 = val & 0xFF
    hw1 = 0xF2C0 | (i << 10) | imm4
    hw2 = (imm3 << 12) | (rd << 8) | imm8
    return struct.pack("<HH", hw1, hw2)


def thumb_mov32(rd: int, val: int) -> bytes:
    """Thumb-2 `movw rD, #lo16; movt rD, #hi16` (8 bytes, position-independent)."""
    return thumb_movw(rd, val & 0xFFFF) + thumb_movt(rd, (val >> 16) & 0xFFFF)


def thumb_cmp_w(rn: int, val: int) -> bytes:
    """Thumb-2 `cmp.w rN, #modified_imm` (T2 encoding)."""
    assert 0 <= rn <= 12
    for code in range(8, 32):
        for lo7 in range(128):
            x = 0x80 | lo7
            v = ((x >> code) | (x << (32 - code))) & 0xFFFFFFFF
            if v == val:
                i = (code >> 4) & 1
                imm3 = (code >> 1) & 7
                a = code & 1
                imm8 = (a << 7) | lo7
                hw1 = 0xF1B0 | (i << 10) | rn
                hw2 = (imm3 << 12) | 0x0F00 | imm8
                return struct.pack("<HH", hw1, hw2)
    raise ValueError(f"Value {val:#x} is not a valid Thumb-2 modified immediate")


def thumb_b_w(pc_va: int, target_va: int) -> bytes:
    """Thumb-2 unconditional 32-bit `b.w target` (T4 encoding, range ±16 MiB)."""
    delta = target_va - (pc_va + 4)
    assert delta % 2 == 0 and -(1 << 24) <= delta < (1 << 24), f"b.w out of range: 0x{pc_va:x} -> 0x{target_va:x}"
    u = delta & 0x1FFFFFF
    S = (u >> 24) & 1
    I1 = (u >> 23) & 1
    I2 = (u >> 22) & 1
    J1 = (~(I1 ^ S)) & 1
    J2 = (~(I2 ^ S)) & 1
    imm10 = (u >> 12) & 0x3FF
    imm11 = (u >> 1) & 0x7FF
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = 0x9000 | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def thumb_bl(pc_va: int, target_va: int) -> bytes:
    """Thumb-2 32-bit `bl target` (T1 encoding, range ±16 MiB)."""
    delta = target_va - (pc_va + 4)
    assert delta % 2 == 0 and -(1 << 24) <= delta < (1 << 24), f"bl out of range: 0x{pc_va:x} -> 0x{target_va:x}"
    u = delta & 0x1FFFFFF
    S = (u >> 24) & 1
    I1 = (u >> 23) & 1
    I2 = (u >> 22) & 1
    J1 = (~(I1 ^ S)) & 1
    J2 = (~(I2 ^ S)) & 1
    imm10 = (u >> 12) & 0x3FF
    imm11 = (u >> 1) & 0x7FF
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = 0xD000 | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def thumb_b_cond_w(pc_va: int, target_va: int, cond: int) -> bytes:
    """Thumb-2 conditional 32-bit `b<cond>.w target` (T3 encoding, range ±1 MiB)."""
    assert 0 <= cond <= 13
    delta = target_va - (pc_va + 4)
    assert delta % 2 == 0 and -(1 << 20) <= delta < (1 << 20)
    u = delta & 0x1FFFFF
    S = (u >> 20) & 1
    J2 = (u >> 19) & 1
    J1 = (u >> 18) & 1
    imm6 = (u >> 12) & 0x3F
    imm11 = (u >> 1) & 0x7FF
    hw1 = 0xF000 | (S << 10) | (cond << 6) | imm6
    hw2 = 0x8000 | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def thumb_b_short(pc_va: int, target_va: int, cond: int | None = None) -> bytes:
    """Thumb-16 short `b` or `b<cond>`."""
    delta = target_va - (pc_va + 4)
    assert delta % 2 == 0
    if cond is None:
        assert -2048 <= delta <= 2046, f"short b out of range: {delta}"
        return struct.pack("<H", 0xE000 | ((delta >> 1) & 0x7FF))
    assert 0 <= cond <= 13 and -256 <= delta <= 254, f"short b<cond> out of range: {delta}"
    return struct.pack("<H", 0xD000 | (cond << 8) | ((delta >> 1) & 0xFF))


def thumb_cbz(pc_va: int, target_va: int, rn: int, nonzero: bool = False) -> bytes:
    """Thumb-16 `cbz` / `cbnz rN, target` (forward only, 0..126 bytes)."""
    assert 0 <= rn <= 7
    delta = target_va - (pc_va + 4)
    assert delta % 2 == 0 and 0 <= delta <= 126, f"cbz/cbnz out of range: {delta}"
    i = (delta >> 6) & 1
    imm5 = (delta >> 1) & 0x1F
    op = 1 if nonzero else 0
    return struct.pack("<H", 0xB100 | (op << 11) | (i << 9) | (imm5 << 3) | rn)


# ARM condition codes
COND = {
    "eq": 0x0, "ne": 0x1, "cs": 0x2, "hs": 0x2, "cc": 0x3, "lo": 0x3,
    "mi": 0x4, "pl": 0x5, "vs": 0x6, "vc": 0x7, "hi": 0x8, "ls": 0x9,
    "ge": 0xA, "lt": 0xB, "gt": 0xC, "le": 0xD, "al": 0xE,
}


# ---------------------------------------------------------------------------
# ARM32 scalar encoders
# ---------------------------------------------------------------------------

def arm_rot_imm12(val: int) -> int:
    """Encode ARM32 12-bit rotated immediate (rot4:imm8)."""
    for r in range(16):
        sh = (2 * r) & 31
        x = ((val << sh) | (val >> (32 - sh))) & 0xFFFFFFFF if sh else val
        if x <= 0xFF:
            return (r << 8) | x
    raise ValueError(f"Value {val:#x} is not a valid ARM32 rotated immediate")


def arm_movw(rd: int, imm16: int, cond: int = 0xE) -> int:
    assert 0 <= rd <= 14 and 0 <= imm16 <= 0xFFFF
    return (cond << 28) | 0x03000000 | (((imm16 >> 12) & 0xF) << 16) | (rd << 12) | (imm16 & 0xFFF)


def arm_movt(rd: int, imm16: int, cond: int = 0xE) -> int:
    assert 0 <= rd <= 14 and 0 <= imm16 <= 0xFFFF
    return (cond << 28) | 0x03400000 | (((imm16 >> 12) & 0xF) << 16) | (rd << 12) | (imm16 & 0xFFF)


def arm_b(pc_va: int, target_va: int, link: bool = False, cond: int = 0xE) -> int:
    """ARM32 `b` / `bl` (range ±32 MiB)."""
    delta = target_va - (pc_va + 8)
    assert delta % 4 == 0 and -(1 << 25) <= delta < (1 << 25), f"ARM b out of range: 0x{pc_va:x} -> 0x{target_va:x}"
    imm24 = (delta >> 2) & 0xFFFFFF
    op = 0x0B000000 if link else 0x0A000000
    return (cond << 28) | op | imm24


# ---------------------------------------------------------------------------
# Mini two-pass Thumb-2 / ARM32 cave assemblers with symbolic labels
# ---------------------------------------------------------------------------

class ThumbBuilder:
    """Tiny deterministic two-pass Thumb-2 code builder anchored at an exact VA."""

    def __init__(self, base_va: int) -> None:
        assert base_va % 2 == 0
        self.base_va = base_va
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str, str, object]] = []

    @property
    def pc(self) -> int:
        return self.base_va + len(self.buf)

    def label(self, name: str) -> int:
        assert name not in self.labels, f"Duplicate label {name}"
        self.labels[name] = self.pc
        return self.pc

    def align4(self) -> None:
        if len(self.buf) % 4 == 2:
            self.hw(0xBF00)  # nop

    def raw(self, data: bytes) -> None:
        self.buf.extend(data)

    def hw(self, *vals: int) -> None:
        for v in vals:
            self.buf.extend(struct.pack("<H", v & 0xFFFF))

    def u32(self, *vals: int) -> None:
        for v in vals:
            self.buf.extend(struct.pack("<I", v & 0xFFFFFFFF))

    def movs(self, rd: int, imm8: int) -> None:
        assert 0 <= rd <= 7 and 0 <= imm8 <= 0xFF
        self.hw(0x2000 | (rd << 8) | imm8)

    def movw(self, rd: int, imm16: int) -> None:
        self.raw(thumb_movw(rd, imm16))

    def movt(self, rd: int, imm16: int) -> None:
        self.raw(thumb_movt(rd, imm16))

    def mov32(self, rd: int, val: int) -> None:
        self.raw(thumb_mov32(rd, val))

    def mov_reg(self, rd: int, rm: int) -> None:
        assert 0 <= rd <= 15 and 0 <= rm <= 15
        self.hw(0x4600 | ((rd & 8) << 4) | (rm << 3) | (rd & 7))

    def cmp_imm(self, rn: int, imm8: int) -> None:
        assert 0 <= rn <= 7 and 0 <= imm8 <= 0xFF
        self.hw(0x2800 | (rn << 8) | imm8)

    def cmp_w(self, rn: int, val: int) -> None:
        self.raw(thumb_cmp_w(rn, val))

    def cmp_reg(self, rn: int, rm: int) -> None:
        assert 0 <= rn <= 7 and 0 <= rm <= 7
        self.hw(0x4280 | (rm << 3) | rn)

    def subs_imm8(self, rd: int, imm8: int) -> None:
        assert 0 <= rd <= 7 and 0 <= imm8 <= 0xFF
        self.hw(0x3800 | (rd << 8) | imm8)

    def adds_imm8(self, rd: int, imm8: int) -> None:
        assert 0 <= rd <= 7 and 0 <= imm8 <= 0xFF
        self.hw(0x3000 | (rd << 8) | imm8)

    def subs_imm3(self, rd: int, rn: int, imm3: int) -> None:
        assert 0 <= rd <= 7 and 0 <= rn <= 7 and 0 <= imm3 <= 7
        self.hw(0x1E00 | (imm3 << 6) | (rn << 3) | rd)

    def adds_imm3(self, rd: int, rn: int, imm3: int) -> None:
        assert 0 <= rd <= 7 and 0 <= rn <= 7 and 0 <= imm3 <= 7
        self.hw(0x1C00 | (imm3 << 6) | (rn << 3) | rd)

    def lsls_imm(self, rd: int, rm: int, imm5: int) -> None:
        assert 0 <= rd <= 7 and 0 <= rm <= 7 and 0 <= imm5 <= 31
        self.hw(0x0000 | (imm5 << 6) | (rm << 3) | rd)

    def lsrs_imm(self, rd: int, rm: int, imm5: int) -> None:
        assert 0 <= rd <= 7 and 0 <= rm <= 7 and 1 <= imm5 <= 32
        self.hw(0x0800 | ((imm5 & 31) << 6) | (rm << 3) | rd)

    def muls(self, rdm: int, rn: int) -> None:
        assert 0 <= rdm <= 7 and 0 <= rn <= 7
        self.hw(0x4340 | (rn << 3) | rdm)

    def orr_reg_lsl(self, rd: int, rn: int, rm: int, shift: int = 0) -> None:
        """Thumb-2 `orr.w rD, rN, rM, lsl #shift`."""
        assert 0 <= rd <= 12 and 0 <= rn <= 12 and 0 <= rm <= 12 and 0 <= shift <= 31
        imm3 = (shift >> 2) & 7
        imm2 = shift & 3
        self.hw(0xEA40 | rn, (imm3 << 12) | (rd << 8) | (imm2 << 6) | rm)

    def bic_imm(self, rd: int, rn: int, imm8: int) -> None:
        """Thumb-2 `bic.w rD, rN, #imm8` (small 8-bit mask)."""
        assert 0 <= rd <= 12 and 0 <= rn <= 12 and 0 <= imm8 <= 0xFF
        self.hw(0xF020 | rn, (rd << 8) | imm8)

    def tst_imm(self, rn: int, imm8: int) -> None:
        """Thumb-2 `tst.w rN, #imm8`."""
        assert 0 <= rn <= 12 and 0 <= imm8 <= 0xFF
        self.hw(0xF010 | rn, 0x0F00 | imm8)

    def ldr(self, rt: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rn <= 13 and imm % 4 == 0 and 0 <= imm <= 4092
        if rt <= 7 and rn <= 7 and imm <= 124:
            self.hw(0x6800 | ((imm >> 2) << 6) | (rn << 3) | rt)
        elif rt <= 7 and rn == 13 and imm <= 1020:
            self.hw(0x9800 | (rt << 8) | (imm >> 2))
        else:
            self.hw(0xF8D0 | rn, (rt << 12) | imm)

    def str_(self, rt: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rn <= 13 and imm % 4 == 0 and 0 <= imm <= 4092
        if rt <= 7 and rn <= 7 and imm <= 124:
            self.hw(0x6000 | ((imm >> 2) << 6) | (rn << 3) | rt)
        elif rt <= 7 and rn == 13 and imm <= 1020:
            self.hw(0x9000 | (rt << 8) | (imm >> 2))
        else:
            self.hw(0xF8C0 | rn, (rt << 12) | imm)

    def ldrh(self, rt: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rn <= 13 and imm % 2 == 0 and 0 <= imm <= 4094
        if rt <= 7 and rn <= 7 and imm <= 62:
            self.hw(0x8800 | ((imm >> 1) << 6) | (rn << 3) | rt)
        else:
            self.hw(0xF8B0 | rn, (rt << 12) | imm)

    def strh(self, rt: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rn <= 13 and imm % 2 == 0 and 0 <= imm <= 4094
        if rt <= 7 and rn <= 7 and imm <= 62:
            self.hw(0x8000 | ((imm >> 1) << 6) | (rn << 3) | rt)
        else:
            self.hw(0xF8A0 | rn, (rt << 12) | imm)

    def ldrb(self, rt: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rn <= 13 and 0 <= imm <= 4095
        if rt <= 7 and rn <= 7 and imm <= 31:
            self.hw(0x7800 | (imm << 6) | (rn << 3) | rt)
        else:
            self.hw(0xF890 | rn, (rt << 12) | imm)

    def ldrb_reg(self, rt: int, rn: int, rm: int) -> None:
        """Thumb-16 `ldrb rT, [rN, rM]` (T1 encoding)."""
        assert 0 <= rt <= 7 and 0 <= rn <= 7 and 0 <= rm <= 7
        self.hw(0x5C00 | (rm << 6) | (rn << 3) | rt)

    def strh_reg(self, rt: int, rn: int, rm: int) -> None:
        """Thumb-16 `strh rT, [rN, rM]` (T1 encoding)."""
        assert 0 <= rt <= 7 and 0 <= rn <= 7 and 0 <= rm <= 7
        self.hw(0x5200 | (rm << 6) | (rn << 3) | rt)

    def strd(self, rt: int, rt2: int, rn: int, imm: int = 0) -> None:
        assert 0 <= rt <= 12 and 0 <= rt2 <= 12 and 0 <= rn <= 13 and imm % 4 == 0 and 0 <= imm <= 1020
        self.hw(0xE9C0 | rn, (rt << 12) | (rt2 << 8) | (imm >> 2))

    def push(self, regs: list[int], lr: bool = False) -> None:
        if all(r <= 7 for r in regs):
            mask = sum(1 << r for r in regs)
            self.hw(0xB400 | (0x100 if lr else 0) | mask)
        else:
            count = len(regs) + (1 if lr else 0)
            assert count >= 2, "Thumb-2 STMDB requires >= 2 registers"
            mask = sum(1 << r for r in regs) | (0x4000 if lr else 0)
            self.hw(0xE92D, mask)

    def pop(self, regs: list[int], pc: bool = False, lr: bool = False) -> None:
        assert not (pc and lr)
        if not lr and all(r <= 7 for r in regs):
            mask = sum(1 << r for r in regs)
            self.hw(0xBC00 | (0x100 if pc else 0) | mask)
        elif lr and not regs:
            self.hw(0xF85D, 0xEB04)  # architectural `ldr.w lr, [sp], #4`
        else:
            count = len(regs) + (1 if (pc or lr) else 0)
            assert count >= 2, "Thumb-2 LDMIA requires >= 2 registers"
            mask = sum(1 << r for r in regs) | (0x8000 if pc else 0) | (0x4000 if lr else 0)
            self.hw(0xE8BD, mask)

    def bx_lr(self) -> None:
        self.hw(0x4770)

    def nop(self) -> None:
        self.hw(0xBF00)

    def b_w(self, target: int | str) -> None:
        if isinstance(target, int):
            self.raw(thumb_b_w(self.pc, target))
        else:
            self.fixups.append((len(self.buf), "b_w", target, None))
            self.hw(0, 0)

    def bl(self, target: int | str) -> None:
        if isinstance(target, int):
            self.raw(thumb_bl(self.pc, target))
        else:
            self.fixups.append((len(self.buf), "bl", target, None))
            self.hw(0, 0)

    def b(self, target: str, cond: str | None = None) -> None:
        self.fixups.append((len(self.buf), "b_short", target, COND[cond] if cond else None))
        self.hw(0)

    def cbz(self, rn: int, target: str, nonzero: bool = False) -> None:
        self.fixups.append((len(self.buf), "cbz", target, (rn, nonzero)))
        self.hw(0)

    def finish(self) -> bytes:
        for off, kind, target, extra in self.fixups:
            assert target in self.labels, f"Undefined label {target!r}"
            pc_va = self.base_va + off
            t_va = self.labels[target]
            if kind == "b_w":
                enc = thumb_b_w(pc_va, t_va)
            elif kind == "bl":
                enc = thumb_bl(pc_va, t_va)
            elif kind == "b_short":
                enc = thumb_b_short(pc_va, t_va, cond=extra)
            elif kind == "cbz":
                rn, nz = extra
                enc = thumb_cbz(pc_va, t_va, rn, nonzero=nz)
            else:
                raise AssertionError(kind)
            self.buf[off : off + len(enc)] = enc
        return bytes(self.buf)


class ArmBuilder:
    """Tiny deterministic two-pass ARM32 code builder anchored at an exact VA."""

    def __init__(self, base_va: int) -> None:
        assert base_va % 4 == 0
        self.base_va = base_va
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str, str, object]] = []

    @property
    def pc(self) -> int:
        return self.base_va + len(self.buf)

    def label(self, name: str) -> int:
        assert name not in self.labels, f"Duplicate label {name}"
        self.labels[name] = self.pc
        return self.pc

    def u32(self, *vals: int) -> None:
        for v in vals:
            self.buf.extend(struct.pack("<I", v & 0xFFFFFFFF))

    def movw(self, rd: int, imm16: int, cond: str = "al") -> None:
        self.u32(arm_movw(rd, imm16, COND[cond]))

    def movt(self, rd: int, imm16: int, cond: str = "al") -> None:
        self.u32(arm_movt(rd, imm16, COND[cond]))

    def mov32(self, rd: int, val: int, cond: str = "al") -> None:
        self.movw(rd, val & 0xFFFF, cond)
        self.movt(rd, (val >> 16) & 0xFFFF, cond)

    def mov_imm(self, rd: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x03A00000 | (rd << 12) | arm_rot_imm12(val))

    def cmp_imm(self, rn: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x03500000 | (rn << 16) | arm_rot_imm12(val))

    def ldr(self, rd: int, rn: int, imm: int = 0, cond: str = "al") -> None:
        assert 0 <= imm <= 4095
        self.u32((COND[cond] << 28) | 0x05900000 | (rn << 16) | (rd << 12) | imm)

    def ldrh(self, rd: int, rn: int, imm: int = 0, cond: str = "al") -> None:
        assert 0 <= imm <= 255
        self.u32(
            (COND[cond] << 28)
            | 0x01D000B0
            | (rn << 16)
            | (rd << 12)
            | (((imm >> 4) & 0xF) << 8)
            | (imm & 0xF)
        )

    def str_(self, rd: int, rn: int, imm: int = 0, cond: str = "al") -> None:
        assert 0 <= imm <= 4095
        self.u32((COND[cond] << 28) | 0x05800000 | (rn << 16) | (rd << 12) | imm)

    def add_imm(self, rd: int, rn: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x02800000 | (rn << 16) | (rd << 12) | arm_rot_imm12(val))

    def orr_imm(self, rd: int, rn: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x03800000 | (rn << 16) | (rd << 12) | arm_rot_imm12(val))

    def tst_imm(self, rn: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x03100000 | (rn << 16) | arm_rot_imm12(val))

    def and_reg(self, rd: int, rn: int, rm: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x00000000 | (rn << 16) | (rd << 12) | rm)

    def cmp_reg(self, rn: int, rm: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x01500000 | (rn << 16) | rm)

    def lsr_imm(self, rd: int, rm: int, shift: int, cond: str = "al") -> None:
        assert 1 <= shift <= 31
        self.u32((COND[cond] << 28) | 0x01A00020 | (rd << 12) | (shift << 7) | rm)

    def sub_imm(self, rd: int, rn: int, val: int, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x02400000 | (rn << 16) | (rd << 12) | arm_rot_imm12(val))

    def dccmvac(self, rt: int) -> None:
        """ARM32 `mcr p15, 0, rT, c7, c10, 1` (Clean D-cache line by MVA to PoC)."""
        self.u32(0xEE070F3A | (rt << 12))

    def dsb_sy(self) -> None:
        self.u32(0xF57FF04F)

    def push(self, regs: list[int]) -> None:
        mask = sum(1 << r for r in regs)
        self.u32(0xE92D0000 | mask)

    def pop(self, regs: list[int]) -> None:
        mask = sum(1 << r for r in regs)
        self.u32(0xE8BD0000 | mask)

    def bx_lr(self, cond: str = "al") -> None:
        self.u32((COND[cond] << 28) | 0x012FFF1E)

    def b(self, target: int | str, cond: str = "al", link: bool = False) -> None:
        if isinstance(target, int):
            self.u32(arm_b(self.pc, target, link=link, cond=COND[cond]))
        else:
            self.fixups.append((len(self.buf), "b", target, (COND[cond], link)))
            self.u32(0)

    def finish(self) -> bytes:
        for off, kind, target, extra in self.fixups:
            assert target in self.labels, f"Undefined label {target!r}"
            cond, link = extra
            w = arm_b(self.base_va + off, self.labels[target], link=link, cond=cond)
            self.buf[off : off + 4] = struct.pack("<I", w)
        return bytes(self.buf)
