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

| Exit code | Meaning |
|---|---|
| `0` | Every file **verified** — complete, and we can say so |
| `1` | At least one file **failed** — it is damaged |
| `3` | Nothing failed, but at least one file is **unverified** — see below |
| `2` | Usage or IO error |

> **Changed 2026-08-15:** exit code `3` is new. Previously a file with an
> unrecognised format signature exited `0` alongside genuinely verified files. If you
> script against this, treat `3` as "look at this" rather than "all good". Codes `0`,
> `1` and `2` are unchanged.

---

## The problem

Palworld does not write saves synchronously.

`POST /v1/api/save` returns **200 on acceptance, not on durable write** — the server
says "got it" and finishes writing afterwards. Separately, the game rewrites the save
on its own timer whether or not anything asked it to.

Copy the file during one of those writes and you get a **torn save**: a file shorter
than its own header says it should be. It looks fine. `tar` exits 0. Your backup log
says success. You find out when you restore.

The failure is silent by construction, which is why it survives in so many setups:
nothing in the normal path ever disagrees with you.

## What the probe checks

### Is the backup intact? — three answers, not two

| Result | Meaning |
|---|---|
| **verified** | The format is one this tool knows and every check passed. Complete, and we can say so. |
| **unverified** | Structurally plausible, but its format signature isn't one we recognise, so the checks can't be given meaning. **Not a sign of damage** — an inability to prove the file is sound. |
| **failed** | A check actively failed: truncation, trailing data, a null header. The file is damaged. |

The middle one exists because a pass/fail answer would force a wrong verdict. A file
with an unfamiliar signature can satisfy the length check while that check proves
nothing — if the layout changed, the bytes read as `compressed_len` may not be
`compressed_len` at all.

Calling that a pass would be a quieter version of `tar` exiting 0 on a torn save: a
success that isn't evidence of anything. So the tool says plainly that it cannot tell,
which is a less satisfying answer and a more useful one.

If you get **unverified**, it most likely means Palworld changed its save format and
this tool needs updating. Please open an issue.

### How it checks

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

### Separately: can the save be fully parsed?

Decompress the payload and read the GVAS header. This is what save editors and
analysis tools need. It is reported on its own line and does not affect whether your
backup is intact.

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
the declared length and the file size. Files come back **verified** while deep parsing
fails, and for backup purposes that is a perfectly good outcome.

If you have information about the current payload codec, please open an issue — that
would be useful to everyone working on Palworld tooling.

## Why waiting a fixed number of seconds isn't enough

The obvious fix is to sleep for a few seconds after asking the server to save. That
helps, and it beats not waiting. It is not a guarantee, for two separate reasons.

**The save request returns before the write finishes.** That part is measurable: on a
test server `POST /v1/api/save` returned in about 60 ms while the file was still being
written for some tens of milliseconds after that. The gap is small on a small world. How
it behaves on a large, busy one is not something we have measured, so a delay chosen
against a small world is a delay chosen without evidence.

**The game rewrites the save on its own schedule regardless.** `AutoSaveSpan` defaults
to 30 seconds, so `Level.sav` is rewritten every 30 seconds whether or not anything
asked for it. A backup that copies at an arbitrary moment can land inside one of those
writes, and waiting after *your* save request does nothing about *the game's*.

So the reliable approach has two parts: watch the file until it stops changing before
copying it, then check what you copied. The second part is the one that holds whatever
the world size, the disk speed, or the timing luck — which is what this tool is for.

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

- **"verified" proves the container is complete and coherent. It is not a restore
  test.** A file can pass every check here and still be logically broken in ways only
  the game can detect. Nothing short of an actual restore proves a restore.
- Deep parsing fails on current Palworld for everyone, not just you. See above.
- Unfamiliar signatures and unknown `save_type` bytes come back **unverified** rather
  than being guessed at in either direction. You get an honest "cannot tell" instead
  of a confident wrong answer — in either direction, since calling an undamaged file
  broken is its own kind of wrong.
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
fails. The probe is free and always will be — if it tells you your
backups are fine, that is a complete and useful answer and you need nothing else.*
