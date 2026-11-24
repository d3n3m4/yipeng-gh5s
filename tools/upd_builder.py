"""
Official GH5S_V25.bin -> Modded GH5S_V26.bin + Stock Rollback GH5S_V27.bin UPD Package Builder.

Legal & Safety Hygiene:
  - Never embeds Panasonic firmware binaries or proprietary AES-5 keys.
  - Operates on a user-supplied official `GH5S_V25.bin`, external `--key-file`, and `--aes5-lib`.
  - Leaves 45/47 partitions bit-for-bit identical to official GH5S_V25.bin.
  - Performs a full AES-5 decrypt roundtrip + SHA-256 + CRC32 + exact byte-diff audit before saving.
  - Generates an accompanying stock rollback `GH5S_V27.bin` (47/47 untouched V2.5 partitions)
    so the user can revert to 100% factory firmware via USB Service Mode flag + SD Playback.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import struct
import zlib
from pathlib import Path

from patches.common import PB2_SIZE, PROG_SIZE, PatchSet


class PanaAes5:
    def __init__(self, lib_path: Path, key_path: Path) -> None:
        self.key = key_path.read_bytes()
        assert len(self.key) == 16, f"AES-5 key must be 16 bytes, got {len(self.key)}"
        self.lib = C.CDLL(str(lib_path))
        assert self.lib.pana_init() == 1, "libpana_aes5.so pana_init() failed"
        p_u8 = C.POINTER(C.c_ubyte)
        self.lib.pana_encrypt_cbc.argtypes = [p_u8, C.c_size_t, p_u8, p_u8, p_u8, C.c_uint]
        self.lib.pana_decrypt_cbc.argtypes = [p_u8, C.c_size_t, p_u8, p_u8, p_u8, C.c_uint]

    def cbc(self, data: bytes, iv: bytes, encrypt: bool) -> bytes:
        sz = len(data)
        src = (C.c_ubyte * sz).from_buffer_copy(data)
        dst = (C.c_ubyte * sz)()
        k = (C.c_ubyte * 16).from_buffer_copy(self.key)
        v = (C.c_ubyte * 16).from_buffer_copy(iv)
        fn = self.lib.pana_encrypt_cbc if encrypt else self.lib.pana_decrypt_cbc
        assert fn(src, sz, dst, k, v, 5) == 1
        return bytes(dst)


def extract_stock_partitions(orig_fw: bytes, aes5: PanaAes5) -> dict[str, bytes]:
    """Decrypt and SHA-256-verify `program`, `postboot2`, and `postboot5` directly from GH5S_V25.bin."""
    assert orig_fw[0x00C:0x02C] == orig_fw[0x2AC:0x2CC], "Not a valid Panasonic UPD header"
    assert struct.unpack_from("<I", orig_fw, 0x040)[0] == (zlib.crc32(orig_fw[0x200:]) & 0xFFFFFFFF), "Bad input CRC32"
    ver = struct.unpack_from("<H", orig_fw, 0x01C)[0]
    assert ver == 0x0252, f"Expected official GH5S V2.5 (0x0252), got 0x{ver:04x}"

    wanted = {"program", "postboot2", "postboot5"}
    out: dict[str, bytes] = {}
    cur = 0x200 + struct.unpack_from("<I", orig_fw, 0x2E0)[0]
    for idx in range(47):
        rec = 0x2EC + idx * 92
        name = orig_fw[rec : rec + 16].split(b"\x00", 1)[0].decode()
        sz, _, flags = struct.unpack_from("<III", orig_fw, rec + 16)
        exp_sha = orig_fw[rec + 28 : rec + 60]
        iv = orig_fw[rec + 60 : rec + 76]
        if name in wanted:
            assert flags == 3 and sz > 0
            pt = aes5.cbc(orig_fw[cur : cur + sz], iv, encrypt=False)
            assert hashlib.sha256(pt).digest() == exp_sha, f"SHA-256 mismatch decrypting {name}"
            out[name] = pt
        cur += sz
    assert cur == len(orig_fw)
    assert len(out["program"]) == PROG_SIZE and len(out["postboot2"]) == PB2_SIZE
    return out


def _repack_upd(orig_fw: bytes, aes5: PanaAes5, version_u16: int, replacements: dict[str, bytes]) -> bytes:
    out = bytearray(orig_fw)
    for off in (0x1C, 0x1E, 0x2BC, 0x2BE):
        struct.pack_into("<H", out, off, version_u16)

    cur = 0x200 + struct.unpack_from("<I", out, 0x2E0)[0]
    for idx in range(47):
        rec = 0x2EC + idx * 92
        name = out[rec : rec + 16].split(b"\x00", 1)[0].decode()
        sz, _, flags = struct.unpack_from("<III", out, rec + 16)
        iv = bytes(out[rec + 60 : rec + 76])
        if name in replacements:
            pt = replacements[name]
            assert len(pt) == sz and flags == 3
            enc = aes5.cbc(pt, iv, encrypt=True)
            assert len(enc) == sz
            out[cur : cur + sz] = enc
            out[rec + 28 : rec + 60] = hashlib.sha256(pt).digest()
        cur += sz
    assert cur == len(orig_fw)
    struct.pack_into("<I", out, 0x040, zlib.crc32(out[0x200:]) & 0xFFFFFFFF)
    return bytes(out)


def _verify_roundtrip(
    pkg: bytes,
    orig_fw: bytes,
    aes5: PanaAes5,
    expected_ver: int,
    stock_prog: bytes,
    stock_pb2: bytes,
    exp_prog_diffs: dict[int, int],
    exp_pb2_diffs: dict[int, int],
) -> None:
    assert len(pkg) == len(orig_fw)
    assert pkg[0x00C:0x02C] == pkg[0x2AC:0x2CC]
    assert struct.unpack_from("<H", pkg, 0x01C)[0] == expected_ver
    assert struct.unpack_from("<I", pkg, 0x040)[0] == (zlib.crc32(pkg[0x200:]) & 0xFFFFFFFF)

    untouched = 0
    cur = 0x200 + struct.unpack_from("<I", pkg, 0x2E0)[0]
    for idx in range(47):
        rec = 0x2EC + idx * 92
        name = pkg[rec : rec + 16].split(b"\x00", 1)[0].decode()
        sz, _, flags = struct.unpack_from("<III", pkg, rec + 16)
        exp_sha = pkg[rec + 28 : rec + 60]
        iv = pkg[rec + 60 : rec + 76]
        if sz > 0 and flags == 3:
            enc = pkg[cur : cur + sz]
            dec = aes5.cbc(enc, iv, encrypt=False)
            assert hashlib.sha256(dec).digest() == exp_sha, f"Roundtrip SHA-256 mismatch on {name}"
            if name == "program" and exp_prog_diffs:
                actual = {i: dec[i] for i in range(sz) if dec[i] != stock_prog[i]}
                assert actual == exp_prog_diffs, f"program diff mismatch: {len(actual)} != {len(exp_prog_diffs)}"
            elif name == "postboot2" and exp_pb2_diffs:
                actual = {i: dec[i] for i in range(sz) if dec[i] != stock_pb2[i]}
                assert actual == exp_pb2_diffs, f"postboot2 diff mismatch: {len(actual)} != {len(exp_pb2_diffs)}"
            else:
                assert enc == orig_fw[cur : cur + sz], f"Untouched partition {name} modified!"
                untouched += 1
        else:
            untouched += 1
        cur += sz
    assert cur == len(orig_fw)
    expected_untouched = 47 - (1 if exp_prog_diffs else 0) - (1 if exp_pb2_diffs else 0)
    assert untouched == expected_untouched, f"Expected {expected_untouched} untouched partitions, got {untouched}"


def build_and_verify_upd(
    orig_fw_path: Path,
    key_path: Path,
    lib_path: Path,
    ps: PatchSet,
    out_v26_path: Path,
    out_v27_rollback_path: Path | None = None,
    version_u16: int = 0x0260,
    unlock_sd_rollback: bool = False,
) -> None:
    orig_fw = orig_fw_path.read_bytes()
    aes5 = PanaAes5(lib_path, key_path)
    parts = extract_stock_partitions(orig_fw, aes5)
    stock_prog, stock_pb2 = parts["program"], parts["postboot2"]

    mod_prog, mod_pb2, prog_diffs, pb2_diffs = ps.apply_to_images(
        stock_prog, stock_pb2, unlock_sd_rollback=unlock_sd_rollback
    )
    repl: dict[str, bytes] = {}
    if prog_diffs:
        repl["program"] = mod_prog
    if pb2_diffs:
        repl["postboot2"] = mod_pb2

    pkg = _repack_upd(orig_fw, aes5, version_u16, repl)
    _verify_roundtrip(pkg, orig_fw, aes5, version_u16, stock_prog, stock_pb2, prog_diffs, pb2_diffs)
    out_v26_path.parent.mkdir(parents=True, exist_ok=True)
    out_v26_path.write_bytes(pkg)

    print(f"[UPD VERIFIED] {out_v26_path}")
    print(f"  Version    : 0x{version_u16:04x} ({len(pkg):,} bytes)")
    print(f"  Diffs      : program={len(prog_diffs)} B, postboot2={len(pb2_diffs)} B, untouched={47 - len(repl)}/47")
    print(f"  CRC32      : 0x{struct.unpack_from('<I', pkg, 0x040)[0]:08x}")
    print(f"  SHA-256    : {hashlib.sha256(pkg).hexdigest()}")

    if out_v27_rollback_path is not None:
        rb_ver = version_u16 + 0x0010
        rb_pkg = _repack_upd(orig_fw, aes5, rb_ver, {})
        _verify_roundtrip(rb_pkg, orig_fw, aes5, rb_ver, stock_prog, stock_pb2, {}, {})
        out_v27_rollback_path.parent.mkdir(parents=True, exist_ok=True)
        out_v27_rollback_path.write_bytes(rb_pkg)
        print(f"[ROLLBACK OK ] {out_v27_rollback_path} (47/47 stock V2.5 partitions, ver=0x{rb_ver:04x})")
