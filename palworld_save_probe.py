#!/usr/bin/env python3
"""
palworld_save_probe.py — check whether your Palworld saves are actually intact

WHAT THIS ANSWERS
-----------------
Palworld does not write saves synchronously. POST /v1/api/save returns 200 when the
server ACCEPTS the request, not when the data is safely on disk. Anything that copies
the save file during that window captures a half-written file -- a "torn" save. It
looks fine, tar exits 0, your backup log says success, and you find out at restore.

This script reads the save container header and tells you whether each file is
complete or was copied mid-write. It catches a single missing byte.

IS THIS BACKUP INTACT? -- three answers, not two
------------------------------------------------
  verified    The format is one this tool knows, and every check passed. The file
              is complete and we can say so.
  unverified  The file looks structurally plausible, but its format signature is
              not one we recognise, so the checks cannot be given meaning. NOT a
              sign of damage -- an inability to prove the file is sound. Usually it
              means Palworld changed its save format and this tool needs updating.
  failed      A check actively failed: truncation, trailing data, a null header.
              The file is damaged.

"unverified" exists because a pass/fail bool would force the wrong answer. A file
with an unfamiliar signature can satisfy the length check while that check proves
nothing -- if the layout changed, the bytes read as `compressed_len` may not be
`compressed_len` at all. Reporting that as a pass would be a quieter version of tar
exiting 0 on a torn save: a success that is not evidence of anything.

CAN THIS SAVE BE FULLY PARSED? (deep parse)
-------------------------------------------
Can the payload be decompressed to a readable GVAS blob? This is what save editors
and analysis tools need. On current Palworld it fails for everyone -- the payload
codec appears to be proprietary. That blocks save editing; it does not block knowing
whether your backup is complete.

Important limitation: "verified" proves the file is complete and coherent. It is not
a restore test. Nothing short of an actual restore proves a restore.

USAGE
-----
    python3 palworld_save_probe.py <path>
    python3 palworld_save_probe.py <path> --json
    python3 palworld_save_probe.py --selftest

<path> may be:
  * a single .sav file
  * a world save directory  (.../Pal/Saved/SaveGames/0/<WorldID>/)
  * any directory -- it will be searched recursively for .sav files

No dependencies beyond the Python standard library. Optional: `zstandard`, `lz4`
improve diagnosis if the payload turns out to use those codecs.

Exit codes:
  0  every file verified
  1  at least one file FAILED a check -- it is damaged
  3  nothing failed, but at least one file could not be verified (unfamiliar
     format). Not damage; an inability to prove the file is sound.
  2  usage or IO error
"""

import argparse
import binascii
import json
import math
import os
import sys
import zlib

# ---------------------------------------------------------------------------
# Format constants, taken from the reference implementation
# (cheahjs/palworld-save-tools, palsav.py + gvas.py)
# ---------------------------------------------------------------------------

LEGACY_MAGIC = b"PlZ"         # the older, widely documented magic value
CURRENT_MAGIC = b"PlM"        # what Palworld actually writes today
KNOWN_MAGICS = (LEGACY_MAGIC, CURRENT_MAGIC)

# The three outcomes a file check can have. A pass/fail bool cannot express the
# middle one, and the middle one is where honesty lives: a file can satisfy every
# check we know how to run and still not be something anyone can vouch for.
VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"
CHUNK_MAGIC = b"CNK"          # chunked wrapper; real header sits 12 bytes later
GVAS_MAGIC = 0x53415647       # b"GVAS" little-endian

# save_type byte meanings (offset 11, or 23 when CNK-wrapped)
SAVE_TYPES = {
    0x30: "0x30 uncompressed (declared valid, never seen in the wild)",
    0x31: "0x31 single zlib",
    0x32: "0x32 double zlib",
}

SAVE_ROLES = {
    "Level.sav": "main world data - terrain, structures, Pals, guilds, offline players",
    "LevelMeta.sav": "world metadata",
    "WorldOption.sav": "world settings snapshot (OVERRIDES PalWorldSettings.ini)",
    "LocalData.sav": "server-local data",
}


def shannon_entropy(data: bytes) -> float:
    """Bits per byte. ~8.0 = compressed/encrypted. <6.0 = probably not compressed."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    ent = 0.0
    for c in counts:
        if c:
            p = c / n
            ent -= p * math.log2(p)
    return ent


def try_codecs(payload: bytes):
    """
    Attempt every plausible decompression. Returns (name, data) on first success,
    else (None, None). Ordered by likelihood given what we know about the format.
    """
    attempts = []

    # zlib, single and double (the documented cases)
    try:
        d = zlib.decompress(payload)
        attempts.append(("zlib", d))
    except Exception:
        pass

    # raw deflate (no zlib header)
    try:
        d = zlib.decompressobj(-15).decompress(payload)
        if d:
            attempts.append(("raw-deflate", d))
    except Exception:
        pass

    # gzip
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(payload)
        if d:
            attempts.append(("gzip", d))
    except Exception:
        pass

    # zstd, if available -- a plausible modern replacement for zlib
    try:
        import zstandard  # type: ignore
        try:
            d = zstandard.ZstdDecompressor().decompress(payload)
            attempts.append(("zstd", d))
        except Exception:
            pass
    except ImportError:
        pass

    # lz4, if available
    try:
        import lz4.frame  # type: ignore
        try:
            d = lz4.frame.decompress(payload)
            attempts.append(("lz4-frame", d))
        except Exception:
            pass
    except ImportError:
        pass

    if attempts:
        return attempts[0]
    return (None, None)


def sniff_codec(payload: bytes) -> str:
    """Best-effort identification of an unknown payload's codec from its first bytes."""
    if len(payload) < 4:
        return "payload too short to identify"
    b0, b1 = payload[0], payload[1]
    head4 = payload[:4]

    if b0 == 0x78 and b1 in (0x01, 0x5E, 0x9C, 0xDA):
        return "zlib stream (standard header present)"
    if head4[:2] == b"\x1f\x8b":
        return "gzip"
    if head4 == b"\x28\xb5\x2f\xfd":
        return "zstd (magic 28 B5 2F FD)"
    if head4 == b"\x04\x22\x4d\x18":
        return "lz4 frame (magic 04 22 4D 18)"
    if head4[:3] == b"\x8c\x0c\x00" or b0 in (0x8C, 0xCC):
        return "possibly Oodle (Kraken/Mermaid) - proprietary, needs the Oodle SDK"
    ent = shannon_entropy(payload[:65536])
    if ent > 7.9:
        return f"unrecognized, but entropy {ent:.2f} bits/byte = definitely compressed or encrypted"
    if ent < 6.0:
        return f"unrecognized, entropy {ent:.2f} bits/byte = likely NOT compressed (plain or corrupt)"
    return f"unrecognized codec, entropy {ent:.2f} bits/byte"


def read_gvas_header(data: bytes) -> dict:
    """Read as much of the GVAS header as we safely can. Never raises."""
    out = {"gvas_magic_ok": False}
    if len(data) < 4:
        out["error"] = "decompressed payload shorter than 4 bytes"
        return out

    magic = int.from_bytes(data[0:4], "little")
    out["gvas_magic_raw"] = f"0x{magic:08X}"
    out["gvas_magic_ascii"] = data[0:4].decode("latin-1", errors="replace")
    if magic != GVAS_MAGIC:
        out["error"] = (
            f"expected GVAS magic 0x{GVAS_MAGIC:08X} ('GVAS'), got 0x{magic:08X}"
        )
        return out

    out["gvas_magic_ok"] = True
    try:
        out["save_game_version"] = int.from_bytes(data[4:8], "little")
        out["package_file_version_ue4"] = int.from_bytes(data[8:12], "little")
        out["package_file_version_ue5"] = int.from_bytes(data[12:16], "little")
        out["engine_version_major"] = int.from_bytes(data[16:18], "little")
        out["engine_version_minor"] = int.from_bytes(data[18:20], "little")
        out["engine_version_patch"] = int.from_bytes(data[20:22], "little")
        out["engine_version_changelist"] = int.from_bytes(data[22:26], "little")
        out["engine_version"] = "{}.{}.{}".format(
            out["engine_version_major"],
            out["engine_version_minor"],
            out["engine_version_patch"],
        )
        # engine_version_branch is an FString: i32 length then bytes
        blen = int.from_bytes(data[26:30], "little", signed=True)
        if 0 < blen < 512:
            out["engine_version_branch"] = (
                data[30:30 + blen].rstrip(b"\x00").decode("utf-8", errors="replace")
            )
    except Exception as e:  # header shape changed; not fatal for Tier 1
        out["header_walk_error"] = f"{type(e).__name__}: {e}"
    return out


def probe_file(path: str) -> dict:
    """Probe a single .sav file. Returns a result dict; never raises on bad data."""
    name = os.path.basename(path)
    r = {
        "file": path,
        "name": name,
        "role": SAVE_ROLES.get(name, "player save" if name.endswith(".sav") else "unknown"),
        "state": FAILED,
        "tier1_pass": False,
        "tier2_pass": False,
        "notes": [],
    }

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        r["error"] = f"cannot read: {e}"
        return r

    r["size_bytes"] = len(data)
    r["size_human"] = human(len(data))

    if len(data) < 24:
        r["error"] = "file shorter than 24 bytes - truncated or empty"
        return r

    r["sha_prefix"] = binascii.hexlify(data[:16]).decode()

    # --- container header -------------------------------------------------
    uncompressed_len = int.from_bytes(data[0:4], "little")
    compressed_len = int.from_bytes(data[4:8], "little")
    magic = data[8:11]
    save_type = data[11]
    offset = 12
    chunked = False

    if magic == CHUNK_MAGIC:
        chunked = True
        uncompressed_len = int.from_bytes(data[12:16], "little")
        compressed_len = int.from_bytes(data[16:20], "little")
        magic = data[20:23]
        save_type = data[23]
        offset = 24
        r["notes"].append("CNK-wrapped container; real header at offset 12")

    r["chunked"] = chunked
    r["magic_bytes"] = magic.decode("latin-1", errors="replace")
    r["magic_hex"] = binascii.hexlify(magic).decode()
    r["magic_is_known"] = (magic in KNOWN_MAGICS)
    r["save_type_byte"] = f"0x{save_type:02X}"
    r["save_type_meaning"] = SAVE_TYPES.get(save_type, "UNKNOWN save type")
    r["declared_uncompressed_len"] = uncompressed_len
    r["declared_compressed_len"] = compressed_len
    r["payload_offset"] = offset
    r["actual_payload_len"] = len(data) - offset

    # The all-null corruption signature the reference tool checks for
    if magic == b"\x00\x00\x00" and uncompressed_len == 0 and compressed_len == 0:
        r["error"] = "all-null header - file is corrupt or was written empty"
        r["notes"].append("This is the classic torn/aborted-write signature.")
        return r

    if not r["magic_is_known"]:
        r["notes"].append(
            f"Magic is {r['magic_bytes']!r}, which is neither of the signatures this "
            f"tool knows ({LEGACY_MAGIC.decode()!r}, {CURRENT_MAGIC.decode()!r}). The "
            "checks below only mean something if the layout is unchanged, so this "
            "file cannot be vouched for either way."
        )

    # --- Tier 1: structural integrity ------------------------------------
    # For backup verification we care about: is the header coherent, and is the
    # payload the length it claims to be? That catches torn writes, which is the
    # actual failure mode we are selling against.
    payload = data[offset:]
    structural_ok = True

    if save_type == 0x31:
        if compressed_len != len(payload):
            structural_ok = False
            r["notes"].append(
                f"TRUNCATION DETECTED: header declares {compressed_len} payload bytes, "
                f"file contains {len(payload)}. Difference {len(payload) - compressed_len}."
            )
        else:
            r["notes"].append("Payload length matches header declaration.")
    else:
        r["notes"].append(
            "save_type is not 0x31, so the single-pass length check does not apply; "
            "relying on decompression to confirm integrity."
        )

    if uncompressed_len == 0:
        structural_ok = False
        r["notes"].append("Header declares zero uncompressed length - suspicious.")

    # --- Tier 1 continued + Tier 2: decompression ------------------------
    codec, blob = try_codecs(payload)
    if codec:
        r["codec"] = codec
        # handle the documented double-zlib case
        if save_type == 0x32:
            c2, blob2 = try_codecs(blob)
            if c2:
                r["codec"] = f"{codec} x2 ({c2})"
                blob = blob2
            else:
                r["notes"].append(
                    "save_type 0x32 implies double compression but the second pass failed."
                )
        r["decompressed_len"] = len(blob)
        r["length_matches_header"] = (len(blob) == uncompressed_len)
        if not r["length_matches_header"]:
            structural_ok = False
            r["notes"].append(
                f"LENGTH MISMATCH: decompressed to {len(blob)} bytes, "
                f"header declared {uncompressed_len}. Payload is damaged."
            )
        else:
            r["notes"].append("Decompressed size matches header exactly.")

        gv = read_gvas_header(blob)
        r["gvas"] = gv
        r["tier2_pass"] = bool(gv.get("gvas_magic_ok"))
        if r["tier2_pass"]:
            r["notes"].append("GVAS header valid - deep parsing is viable.")
        else:
            r["notes"].append(
                "Decompressed successfully but payload is not GVAS - "
                "inner format changed. Tier 1 still OK."
            )
    else:
        r["codec"] = None
        r["codec_guess"] = sniff_codec(payload)
        r["notes"].append(
            f"Could not decompress with any known codec. Diagnosis: {r['codec_guess']}"
        )
        # If we cannot decompress, Tier 1 can still pass on header coherence alone,
        # but only if the declared lengths are self-consistent.
        if save_type not in SAVE_TYPES:
            r["notes"].append(
                "Unknown save_type byte - the layout is unknown, so the checks below "
                "cannot be given meaning. Not a detected fault."
            )

    # A detected fault outranks an unfamiliar format: when in doubt, distrust.
    if not structural_ok:
        r["state"] = FAILED
    elif not r["magic_is_known"] or save_type not in SAVE_TYPES:
        # Every check passed, but the checks only mean something if the layout is
        # what we assume. An unfamiliar signature is evidence that it might not be.
        r["state"] = UNVERIFIED
    else:
        r["state"] = VERIFIED

    # Retained so anything scripting against the old JSON keeps working. Only a
    # fully verified file counts as a pass.
    r["tier1_pass"] = (r["state"] == VERIFIED)
    return r


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


def collect(path: str):
    if os.path.isfile(path):
        return [path]
    found = []
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            if fn.lower().endswith(".sav"):
                found.append(os.path.join(root, fn))
    return found


# ---------------------------------------------------------------------------
# Self-test: builds synthetic saves so the probe's own logic can be validated
# without a real server. Proves the probe detects a torn write.
# ---------------------------------------------------------------------------

def build_fake_sav(gvas_ok=True, truncate=0, save_type=0x31, magic=CURRENT_MAGIC,
                   null_header=False):
    if null_header:
        return b"\x00" * 64
    body = bytearray()
    body += (GVAS_MAGIC if gvas_ok else 0x11111111).to_bytes(4, "little")
    body += (3).to_bytes(4, "little")            # save_game_version
    body += (522).to_bytes(4, "little")          # ue4
    body += (1004).to_bytes(4, "little")         # ue5
    body += (5).to_bytes(2, "little")            # major
    body += (1).to_bytes(2, "little")            # minor
    body += (1).to_bytes(2, "little")            # patch
    body += (0).to_bytes(4, "little")            # changelist
    branch = b"++UE5+Release-5.1\x00"
    body += len(branch).to_bytes(4, "little") + branch
    # Semi-incompressible deterministic filler, so the compressed payload is a
    # realistic size. With all-zero filler the payload would be ~76 bytes and a
    # truncation test would chop the header instead of the payload, which would
    # not exercise the length-mismatch check at all.
    rng = 0x12345678
    filler = bytearray()
    for _ in range(200000):
        rng = (1103515245 * rng + 12345) & 0xFFFFFFFF
        filler.append((rng >> 16) & 0xFF)
    body += filler
    raw = bytes(body)

    comp = zlib.compress(raw)
    out = bytearray()
    out += len(raw).to_bytes(4, "little")
    out += len(comp).to_bytes(4, "little")
    out += magic
    out += bytes([save_type])
    out += comp
    blob = bytes(out)
    if truncate:
        blob = blob[:-truncate]
    return blob


def selftest():
    import tempfile
    print("Running self-test - validating the probe's own logic on synthetic saves.\n")
    probe_size = len(build_fake_sav())
    cases = [
        ("healthy save (current 'PlM' signature)", dict(), VERIFIED, True),
        ("healthy save (legacy 'PlZ' signature)",
         dict(magic=LEGACY_MAGIC), VERIFIED, True),
        ("TORN WRITE - last 1 byte lost", dict(truncate=1), FAILED, False),
        ("TORN WRITE - last 5% of payload lost",
         dict(truncate=max(2, probe_size // 20)), FAILED, False),
        ("all-null header (aborted write)", dict(null_header=True), FAILED, False),
        ("valid container, non-GVAS payload", dict(gvas_ok=False), VERIFIED, False),
        # Neither of these is damaged, and neither can be vouched for.
        ("unfamiliar signature 'PlQ'", dict(magic=b"PlQ"), UNVERIFIED, True),
        ("unknown save_type byte", dict(save_type=0x77), UNVERIFIED, True),
        # A real defect outranks an unfamiliar signature.
        ("unfamiliar signature AND truncated",
         dict(magic=b"PlQ", truncate=1), FAILED, False),
    ]
    ok = True
    with tempfile.TemporaryDirectory() as td:
        for label, kwargs, want_state, want_t2 in cases:
            p = os.path.join(td, "Level.sav")
            with open(p, "wb") as f:
                f.write(build_fake_sav(**kwargs))
            r = probe_file(p)
            state, t2 = r["state"], r["tier2_pass"]
            good = (state == want_state and t2 == want_t2)
            ok = ok and good
            print(f"  [{'PASS' if good else 'FAIL'}] {label}")
            print(f"         magic={r.get('magic_bytes')!r} "
                  f"state={state} (want {want_state})  tier2={t2} (want {want_t2})")
            for n in r["notes"]:
                print(f"         - {n}")
            print()
    print("Self-test", "PASSED - the probe correctly detects torn writes." if ok else "FAILED.")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="Probe Palworld .sav container format and integrity.")
    ap.add_argument("path", nargs="?", help="a .sav file or a save directory")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--selftest", action="store_true",
                    help="validate the probe itself using synthetic saves")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.path:
        ap.print_help()
        return 2

    if not os.path.exists(args.path):
        print(f"error: no such path: {args.path}", file=sys.stderr)
        return 2

    files = collect(args.path)
    if not files:
        print(f"error: no .sav files found under {args.path}", file=sys.stderr)
        return 2

    results = [probe_file(p) for p in files]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        report(results)

    # Exit codes are a contract people script against, so the three states get three
    # distinct codes rather than being squashed into pass/fail. A detected fault
    # outranks an unverifiable file.
    if any(r["state"] == FAILED for r in results):
        return 1
    if any(r["state"] == UNVERIFIED for r in results):
        return 3
    return 0


def report(results):
    W = 78
    print("=" * W)
    print("PALWORLD SAVE PROBE")
    print("=" * W)
    print(f"Files examined: {len(results)}\n")

    for r in results:
        print("-" * W)
        print(f"{r['name']}   ({r.get('size_human','?')})")
        print(f"  role            : {r['role']}")
        if "error" in r:
            print(f"  ERROR           : {r['error']}")
            for n in r.get("notes", []):
                print(f"  note            : {n}")
            print(f"  TIER 1          : FAIL")
            continue
        print(f"  magic bytes     : {r['magic_bytes']!r}  (hex {r['magic_hex']})"
              f"{'  <-- KNOWN' if r['magic_is_known'] else '  <-- UNKNOWN/NEW'}")
        print(f"  save type       : {r['save_type_byte']}  {r['save_type_meaning']}")
        print(f"  declared sizes  : compressed={r['declared_compressed_len']:,}  "
              f"uncompressed={r['declared_uncompressed_len']:,}")
        print(f"  actual payload  : {r['actual_payload_len']:,} bytes")
        if r.get("codec"):
            print(f"  codec           : {r['codec']}")
            print(f"  decompressed    : {r.get('decompressed_len', 0):,} bytes"
                  f"  (matches header: {r.get('length_matches_header')})")
        else:
            print(f"  codec           : NONE MATCHED")
            print(f"  diagnosis       : {r.get('codec_guess')}")
        gv = r.get("gvas") or {}
        if gv.get("gvas_magic_ok"):
            print(f"  GVAS            : valid"
                  f"  engine {gv.get('engine_version','?')}"
                  f"  branch {gv.get('engine_version_branch','?')}")
        elif gv:
            print(f"  GVAS            : {gv.get('error','not present')}")
        for n in r["notes"]:
            print(f"  note            : {n}")
        print(f"  BACKUP INTACT   : {r['state'].upper()}")
        print(f"  DEEP PARSE      : {'PASS' if r['tier2_pass'] else 'FAIL'}")

    verified = sum(1 for r in results if r.get("state") == VERIFIED)
    unverified = sum(1 for r in results if r.get("state") == UNVERIFIED)
    broken = sum(1 for r in results if r.get("state", FAILED) == FAILED)
    t1 = verified
    t2 = sum(1 for r in results if r["tier2_pass"])
    n = len(results)

    print("\n" + "=" * W)
    print("VERDICT")
    print("=" * W)
    print(f"  Tier 1 -- backup intact          : {t1}/{n} files")
    print(f"  Tier 2 -- save fully parseable   : {t2}/{n} files\n")

    if t1 == n and t2 == n:
        print("  YOUR BACKUP LOOKS GOOD.")
        print("  Every file is complete, and the save data is fully readable.")
        print("  Note that this proves the files are whole, not that the world will")
        print("  load. Only an actual restore proves a restore.")
    elif t1 == n:
        print("  YOUR BACKUP LOOKS GOOD.")
        print("  Every file is complete and nothing was lost to a torn write.")
        print()
        print("  Tier 2 failed, which is expected on current Palworld and is not a")
        print("  problem with your backup. The save payload uses a codec no open")
        print("  tool can currently decompress, so save editors and analysers cannot")
        print("  read it either. That does not affect whether your backup is intact.")
        print()
        print("  Note that this proves the files are whole, not that the world will")
        print("  load. Only an actual restore proves a restore.")
    elif t1 > 0:
        print("  SOME FILES ARE DAMAGED.")
        print("  Read the per-file results above -- the ones marked TIER 1 FAIL are")
        print("  not restorable. This usually means the backup was taken while the")
        print("  server was still writing.")
        print()
        print("  What to do: check your older backups with this same command; they")
        print("  may be fine. Then fix the process that produced this one.")
    else:
        print("  NO FILE PASSED.")
        print("  If these are real save files, none of them are restorable.")
        print()
        print("  Before concluding that: confirm the path pointed at actual .sav")
        print("  files, and that you probed a COPY rather than a file the server is")
        print("  writing to right now -- a live file may legitimately be mid-write.")
    print()


if __name__ == "__main__":
    sys.exit(main())
