# palworld-save-probe

**Check whether your Palworld backups are actually intact.**

One file, no dependencies, no install. It reads the save container header and tells
you whether the file is complete or was copied mid-write.

```bash
curl -O https://raw.githubusercontent.com/Blackthornes/palworld-save-probe/main/palworld_save_probe.py
python3 palworld_save_probe.py /path/to/Pal/Saved/SaveGames/0/<WorldID>
```

No server, no network, nothing installed:

```bash
python3 palworld_save_probe.py --selftest
```

Exit code `0` means every file passed. `1` means at least one did not.

---

## The problem

Palworld does not write saves synchronously.

`POST /v1/api/save` returns **200 on acceptance, not on durable write**. The server
says "got it" and then keeps writing for anywhere from a few seconds to well over a
minute, depending on world size, disk speed, and load.

Copy the file inside that window and you get a **torn save**: a file that is a few
hundred bytes short of complete. It looks fine. `tar` exits 0. Your backup log says
success. You find out when you restore.

The failure is silent by construction, which is why it survives in so many setups:
nothing in the normal path ever disagrees with you.

## What the probe checks

It reports **two independent verdicts**, because they mean different things.

### Tier 1 — is the backup intact?

Reads the container header, which declares exactly how many payload bytes should
follow it, and compares that to how many bytes are actually there.

```
bytes 0-4    uncompressed_len   (u32 little-endian)
bytes 4-8    compressed_len     (u32 LE)
bytes 8-11   magic              b"PlM" (current) / b"PlZ" (legacy)
byte  11     save_type          0x30 / 0x31 / 0x32
bytes 12+    payload
```

For `save_type` `0x31`, `compressed_len` must equal `len(file) - 12`. A mismatch is a
torn write. This catches a **single missing byte**.

It requires no understanding of the game's data at all — which is why it still works
even though the next part doesn't.

### Tier 2 — can the save be fully parsed?

Decompress the payload and read the GVAS header. This is what save editors and
analysis tools need.

**On current Palworld this fails, and that's worth knowing.**

## What we found about the current format

Measured against a live server on **2026-08-15**, Palworld **v1.0.3.101283** (UE 5.1.1):

**Magic bytes are `PlM`, not `PlZ`.** Every file, every time. `PlZ` is the older,
widely documented value, so anything written against the published format will want
updating for this.

**`save_type` `0x31` is labelled "single zlib" and is not zlib.** No standard codec
decompressed any file — not zlib, raw deflate, gzip, zstd, or lz4. The byte
pattern is consistent with **Oodle** (Kraken/Mermaid), which is proprietary and needs
a licensed SDK. The `save_type` byte describes what the format *was*, not what the
payload *is*. Don't trust it to pick a decompressor.

**The practical consequence:** deep save parsing is currently gated behind a
proprietary codec. Integrity checking is not, because the header check reads only
the declared length and the file size. Tier 1 passes while Tier 2 fails, and for
backup purposes that is a perfectly good outcome.

If you have information about the current payload codec, please open an issue — that
would be useful to everyone working on Palworld tooling.

## Why a fixed delay doesn't solve it

The intuitive fix is to wait a few seconds after requesting a save before copying.
That helps, but it can't be relied on, because there is no correct constant to pick.

Measured on our dev server: a fresh 15 KB world settled **8 seconds** after the API
returned 200. A populated 50 MB world takes considerably longer. So any fixed delay
is simultaneously too long for small worlds and too short for large ones — and which
way it fails depends on the world you didn't test against. Disk speed and server load
move the number too.

The reliable approach is to watch the file until it stops changing, then copy — and
then check the result anyway. "Probably finished" isn't the standard you want for the
only copy of a two-hundred-hour world.

## What a good backup process does

If you're writing or evaluating one, these are the steps that matter:

1. **Ask the server to save**, if the game gives you a way to.
2. **Wait for the write to actually finish** — poll the file's size and modification
   time until they hold steady, rather than sleeping a fixed amount.
3. **Copy**, excluding the game's own rolling `backup` folder so you don't nest
   backups inside backups.
4. **Verify what you copied**, and treat a failed check as a failed backup.
5. **Never let a failed backup delete a good one.** Prune old snapshots only after
   the new one passes.

Step 4 is what turns a silent failure into a loud one, and it's the step this probe
exists to make easy. `tar` exiting 0 is not evidence of a good backup — it exits 0 on
a torn save too.

## Limitations — read this

- **Tier 1 proves the container is complete and coherent. It is not a restore test.**
  A file can pass every check here and still be logically broken in ways only the
  game can detect. Nothing short of an actual restore proves a restore.
- Tier 2 fails on current Palworld for everyone, not just you. See above.
- Unknown `save_type` bytes are reported as failures rather than guessed at. If
  Palworld changes format again, you'll get a clear "unrecognised" instead of a
  confident wrong answer.
- It reads files. It never writes to, modifies, or uploads anything.

## FAQ

**Do I need to stop my server?** No. Probe a copy, not the live file — a live file
may legitimately be mid-write.

**It says my backup is torn. Now what?** That specific archive is not restorable.
Older backups may be fine — probe them. Then fix the process that produced it: wait
for the file to stop changing before copying, and check the result.

**Does this work on Xbox/Game Pass saves?** Untested. It reads the same container
format, so it may; reports welcome.

**Why is `--selftest` there?** It builds synthetic saves — healthy, truncated by one
byte, truncated by 5%, null-header, wrong magic — and confirms the probe reaches the
right verdict on each. You shouldn't trust a checker that can't demonstrate it
catches anything.

## License

MIT. Do what you like with it.

---

*Built while developing a paid tool that does the automated version of this: waits for
each write to settle, backs up off-box, verifies every snapshot, and tells you when one
fails. <!-- TODO: link the product landing page here once it exists. Do NOT link the
private strategy repo. --> The probe is free and always will be — if it tells you your
backups are fine, that is a complete and useful answer and you need nothing else.*
