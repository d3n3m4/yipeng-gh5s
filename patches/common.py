"""
GH5S V2.5 memory map, verified RWX code caves, and strict PatchSet container.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# Partition virtual load addresses (1:1 flat RWX DRAM mapping verified in loader3 DACR=0xFFFFFFFF)
PROG_BASE = 0x05FA0000
PROG_SIZE = 0x00BC0000  # 0x05FA0000..0x06B60000
PROG_DATA_ROM = 0x06A6BE14
PROG_DATA_RAM = 0x08180000
PROG_DATA_DELTA = PROG_DATA_RAM - PROG_DATA_ROM

PB1_BASE = 0x06B60000
PB2_BASE = 0x06FE0000
PB2_SIZE = 0x00740000  # 0x06FE0000..0x07720000
PB5_BASE = 0x07760000

# Verified 100%-0xFF trailing flash padding caves (zero references in stock V2.5; within single b.w/bl range)
PROG_CAVE_BASE = 0x06AA1000
PROG_CAVE_END = 0x06AA4000  # 12 KiB reserved inside 764 KiB trailing padding
PB2_CAVE_BASE = 0x076EE000
PB2_CAVE_END = 0x076F1000   # 12 KiB reserved inside 200 KiB trailing padding

# Shared runtime state inside PROG_CAVE (readable from both `program` and `postboot2`!)
G_ACTIVE_A4K_W = 0x06AA1000   # u32: active 4K/A width (0=default, 3328, 3744, 2496)
G_CONFIG_FLAGS = 0x06AA1004   # u32: bit0=1x1, bit1=2997p, bit2=longg_mod, bit3=dual_aspect, bit4=all4k, [8:19]=longg_M, [20:31]=alli_M
G_LPCM_CODE_BASE = 0x06AA1010
G_OG_CODE_BASE = 0x06AA1140
G_VIDEO_MENU_BASE = 0x06AA1800  # 288B: relocated 35-item Motion Picture menu (incl. Aspect Ratio 0x000c)

# Permanent firmware rollback unlock (only written in UPD flash builds, NEVER in RAM tests)
SD_ROLLBACK_FLAG_VA = 0x0603801A

# Expected official Panasonic GH5S_V25.bin SHA-256 (verified on stock download)
STOCK_V25_PARTITIONS_SHA256 = {
    "program": None,    # populated / verified dynamically against local decrypted V25
    "postboot2": None,
}


@dataclass(frozen=True)
class HalfwordPatch:
    addr: int          # Cached VA (program, program RAM .data 0x0818xxxx, or postboot2)
    old_u16: int       # Expected stock V2.5 u16
    new_u16: int       # New u16
    desc: str
    module: str


@dataclass
class PatchSet:
    """Ordered, collision-checked collection of halfword patches."""
    patches: list[HalfwordPatch] = field(default_factory=list)
    _seen: set[int] = field(default_factory=set, repr=False)

    def add_u16(self, addr: int, old_u16: int, new_u16: int, desc: str, module: str) -> None:
        assert addr % 2 == 0, f"Unaligned VA 0x{addr:08x} ({desc})"
        assert addr != SD_ROLLBACK_FLAG_VA, "SD rollback flag must be added explicitly by UPD builder only"
        if old_u16 == new_u16:
            return
        assert addr not in self._seen, f"Patch collision @ 0x{addr:08x} ({desc})"
        self._seen.add(addr)
        self.patches.append(HalfwordPatch(addr, old_u16 & 0xFFFF, new_u16 & 0xFFFF, desc, module))

    def add_bytes(self, addr: int, old_b: bytes, new_b: bytes, desc: str, module: str) -> None:
        assert len(old_b) == len(new_b) and len(old_b) % 2 == 0, f"Bad length for {desc}"
        for off in range(0, len(old_b), 2):
            o = struct.unpack_from("<H", old_b, off)[0]
            n = struct.unpack_from("<H", new_b, off)[0]
            self.add_u16(addr + off, o, n, desc, module)

    def add_u32(self, addr: int, old_v: int, new_v: int, desc: str, module: str) -> None:
        self.add_bytes(addr, struct.pack("<I", old_v & 0xFFFFFFFF), struct.pack("<I", new_v & 0xFFFFFFFF), desc, module)

    def add_cave_bytes(self, addr: int, payload: bytes, desc: str, module: str) -> None:
        """Write payload into verified 0xFF cave padding."""
        assert len(payload) % 2 == 0, f"Cave payload must be halfword-aligned ({desc})"
        in_prog = PROG_CAVE_BASE <= addr and addr + len(payload) <= PROG_CAVE_END
        in_pb2 = PB2_CAVE_BASE <= addr and addr + len(payload) <= PB2_CAVE_END
        assert in_prog or in_pb2, f"Cave write 0x{addr:08x}..0x{addr+len(payload):08x} outside reserved cave!"
        self.add_bytes(addr, b"\xFF" * len(payload), payload, desc, module)

    def verify_against_stock(self, stock_prog: bytes, stock_pb2: bytes) -> None:
        """Verify EVERY patch matches the exact expected stock V2.5 bytes before touching RAM or flash."""
        for p in self.patches:
            if PROG_BASE <= p.addr < PROG_BASE + len(stock_prog):
                actual = struct.unpack_from("<H", stock_prog, p.addr - PROG_BASE)[0]
            elif PROG_DATA_RAM <= p.addr < PROG_DATA_RAM + (len(stock_prog) - (PROG_DATA_ROM - PROG_BASE)):
                rom_va = p.addr - PROG_DATA_DELTA
                actual = struct.unpack_from("<H", stock_prog, rom_va - PROG_BASE)[0]
            elif PB2_BASE <= p.addr < PB2_BASE + len(stock_pb2):
                actual = struct.unpack_from("<H", stock_pb2, p.addr - PB2_BASE)[0]
            else:
                raise AssertionError(f"Address 0x{p.addr:08x} ({p.desc}) outside program/postboot2")
            assert actual == p.old_u16, (
                f"Stock V2.5 mismatch @ 0x{p.addr:08x} [{p.module}: {p.desc}]: "
                f"stock=0x{actual:04x} != expected=0x{p.old_u16:04x}"
            )

    def apply_to_images(
        self,
        stock_prog: bytes,
        stock_pb2: bytes,
        unlock_sd_rollback: bool = False,
    ) -> tuple[bytes, bytes, dict[int, int], dict[int, int]]:
        """Apply all patches to stock plaintext `program` and `postboot2` images."""
        self.verify_against_stock(stock_prog, stock_pb2)
        prog = bytearray(stock_prog)
        pb2 = bytearray(stock_pb2)
        prog_diffs: dict[int, int] = {}
        pb2_diffs: dict[int, int] = {}

        def _put(buf: bytearray, diffs: dict[int, int], off: int, old_u16: int, new_u16: int) -> None:
            ob = struct.pack("<H", old_u16)
            nb = struct.pack("<H", new_u16)
            for i in range(2):
                if ob[i] != nb[i]:
                    assert (off + i) not in diffs, f"Duplicate byte write @ off 0x{off+i:x}"
                    buf[off + i] = nb[i]
                    diffs[off + i] = nb[i]

        for p in self.patches:
            if PROG_BASE <= p.addr < PROG_BASE + len(prog):
                _put(prog, prog_diffs, p.addr - PROG_BASE, p.old_u16, p.new_u16)
            elif PROG_DATA_RAM <= p.addr < PROG_DATA_RAM + (len(prog) - (PROG_DATA_ROM - PROG_BASE)):
                rom_va = p.addr - PROG_DATA_DELTA
                _put(prog, prog_diffs, rom_va - PROG_BASE, p.old_u16, p.new_u16)
            elif PB2_BASE <= p.addr < PB2_BASE + len(pb2):
                _put(pb2, pb2_diffs, p.addr - PB2_BASE, p.old_u16, p.new_u16)
            else:
                raise AssertionError(f"Address 0x{p.addr:08x} outside program/postboot2")

        if unlock_sd_rollback:
            off = SD_ROLLBACK_FLAG_VA - PROG_BASE
            cur = struct.unpack_from("<H", prog, off)[0]
            assert cur == 0x0000, f"Unexpected rollback flag 0x{cur:04x}"
            _put(prog, prog_diffs, off, 0x0000, 0x0001)

        return bytes(prog), bytes(pb2), prog_diffs, pb2_diffs

    def summary_by_module(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.patches:
            counts[p.module] = counts.get(p.module, 0) + 1
        return counts


def load_decrypted_v25(fw_dir: Path) -> tuple[bytes, bytes, bytes]:
    prog = (fw_dir / "program.plaintext.bin").read_bytes()
    pb2 = (fw_dir / "postboot2.plaintext.bin").read_bytes()
    pb5 = (fw_dir / "postboot5.plaintext.bin").read_bytes()
    assert len(prog) == PROG_SIZE and len(pb2) == PB2_SIZE
    return prog, pb2, pb5
