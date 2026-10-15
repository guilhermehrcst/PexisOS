# v0.4 Lab A: offline CAN / DBC replay

Plan: [`docs/plans/0003-v04-offline-can-replay.md`](plans/0003-v04-offline-can-replay.md).
Tracking issue: [#7](https://github.com/guilhermehrcst/Pexislink/issues/7).
Code: `src/pexislink/canreplay.py` (core, standard library), `src/pexislink/lab_cli.py` (CLI).
Tests: `tests/test_canreplay.py` (stub decoder), `tests/test_canreplay_cantools.py` (real cantools).

> [!IMPORTANT]
> This lab reads two **local files** and writes two local files. It never opens a CAN
> interface, never creates a bus, never transmits, never contacts an ECU or the network.
> Every fixture is **synthetic**. Decoding a frame is not evidence that a reading is real,
> that a vehicle is faulty, or that anything will fail.

## 1. Use

```bash
pip install --require-hashes -r requirements/lab-can.txt   # optional decoder, hash-locked
pexislink lab can-replay --dbc synthetic.dbc --frames frames.csv --out records.jsonl --summary summary.json \
    [--max-frames-mb 256] [--max-line-bytes 256] [--max-rows 5000000] [--max-dbc-kb 4096] [--max-messages 10000]
```

`pip install 'pexislink[can]'` also works but pins only cantools; the requirements file pins
and hash-checks the whole tested tree. Without cantools the default installation is unchanged
(`dependencies = []`) and the command exits 3 with the install hint.

## 2. Input contract (closed)

**Frame CSV**: exactly the header `timestamp,arbitration_id,is_extended_id,data_hex`, UTF-8
(a leading BOM is accepted and recorded), `\n` or `\r\n` line endings, no quoting, no blank
lines, no extra columns.

| Field | Rule |
|---|---|
| `timestamp` | RFC 3339: `YYYY-MM-DDTHH:MM:SS`, optional fraction of 1-6 digits, `Z` or `±HH:MM`; years 1970-2199; real calendar dates |
| `arbitration_id` | `0x` + 1-8 hex digits |
| `is_extended_id` | `0`: 11-bit, id ≤ `0x7FF`; `1`: 29-bit, id ≤ `0x1FFFFFFF` |
| `data_hex` | 0-16 hex digits, even length (0-8 bytes). More than 8 bytes (CAN FD) is `unsupported_frame`. No RTR or error frames |

Input order is kept. Equal and backwards timestamps are recorded per row (`order`) and
counted; nothing is sorted or removed.

**DBC**: a local UTF-8 file parsed by cantools in strict mode. It is rejected (`invalid_dbc`)
if it cannot be parsed, defines CAN FD or longer-than-8-byte messages, container messages, an
id outside its format's range, or two messages with the same id and format.

**Same numeric id, different format.** cantools 44.1.0's own `get_message_by_frame_id(0x123)`
returns only one of an 11-bit and a 29-bit message with id `0x123` (verified by
`test_cantools_own_lookup_aliases_but_the_lab_does_not`). The lab keys messages by
`(id, is_extended_id)` and never uses that lookup.

## 3. Output

**Records** (`--out`, JSON Lines, ASCII, sorted keys): exactly one per frame, in input order.

```json
{"arbitration_id":"0x123","dlc":8,"epoch_us":1767225600000000,"index":0,"is_extended_id":false,"line":2,"message":"SYNTH_STD","order":"first","signals":[{"name":"std_s12","unit":"V","value":-102.0},{"name":"std_s8","unit":null,"value":-2},{"name":"std_u16","unit":"degC","value":960.0}],"status":"decoded","timestamp":"2026-01-01T00:00:00Z"}
{"arbitration_id":"0x456","dlc":1,"epoch_us":1767225600030000,"index":3,"is_extended_id":false,"line":5,"order":"increasing","status":"unknown_message","timestamp":"2026-01-01T00:00:00.030Z"}
{"arbitration_id":"0x123","dlc":2,"epoch_us":1767225600040000,"error":"length_mismatch","expected_dlc":8,"index":4,"is_extended_id":false,"line":6,"message":"SYNTH_STD","order":"increasing","status":"decode_error","timestamp":"2026-01-01T00:00:00.040Z"}
```

- `status`: `decoded`, `unknown_message` (id and format not in the DBC: no name, no signals)
  or `decode_error` with a stable `error`: `length_mismatch`, `nonfinite_value`,
  `unsupported_value_type`, `unexpected_signal`, `unsupported_message_type`, `decode_failed`
  (decoder exception; its text is never written).
- Signal names, units and scaling come only from the DBC (`unit: null` when the DBC has none).
  Values are finite JSON numbers: integers when the DBC scale and offset are integral.
- **Raw payload bytes are not written** anywhere.

**Summary** (`--summary`, schema `pexislink.can-replay.v1`): tool version, package-source
SHA-256, Python version, decoder name and version, limits, the command with paths as
placeholders, SHA-256 and byte length of both inputs, the DBC's message schema (ids, names,
lengths, signal names and units), status counts, timestamp-order counts, the number of
distinct unknown ids, and the SHA-256, count and length of the records. No paths, no payloads,
no run time. Outputs are byte-identical across runs; across Python 3.11-3.13 only
`tool.python` differs (checked on 1,000,000 frames).

**Commit protocol.** Both files are written to temporary files next to their destinations
and fsync'ed; then an existing summary is removed, the records are renamed into place, the
directory fsync'ed, and the summary renamed last. Two-file atomicity is not claimed: **the
summary is the commit marker**. A records file without a summary whose `records.sha256`
matches is an interrupted run. A failed run leaves previous outputs untouched and no
temporary files (also on Ctrl-C).

## 4. Failure semantics

| Exit | Code | Cause |
|---|---|---|
| 0 | | success |
| 1 | `output_write_error` | destination directory missing or unwritable, disk full |
| 2 | usage | bad arguments or limits; `--out` equal to `--summary`; an output equal to an input (also via `.`, a hard link or a symlink); an existing output that is a link, directory or device |
| 3 | `decoder_unavailable` | cantools not installed |
| 4 | `invalid_dbc` | not UTF-8, not parseable in strict mode, or a rejected construct (section 2) |
| 5 | frame contract | `invalid_header`, `csv_quoting`, `field_count`, `invalid_utf8`, `invalid_line` (stray CR/NUL), `invalid_timestamp`, `invalid_arbitration_id`, `invalid_extended_flag`, `arbitration_id_out_of_range`, `invalid_data_hex` |
| 6 | `unsupported_frame` | more than 8 data bytes |
| 7 | `input_unreadable` | missing, not a regular file, permission |
| 8 | `input_symlink` | an input is a symbolic link (never followed) |
| 9 | `input_too_large`, `line_too_long` | file bytes, line length, row count, DBC size |
| 10 | `input_changed` | an input grew past its cap, was rewritten (size, mtime or ctime differ) or replaced while read |
| 11 | `safety_guard` | replay code attempted socket, process, native-library or device access |

stderr carries `error: <code>: <message>` with an optional line number, never a path or
payload. A gate failure commits nothing.

## 5. Safety barriers

1. **No device code path.** The package never imports python-can (`can`) and never creates a
   bus; cantools is imported only inside `load_cantools_decoder` (AST tests). Importing the
   lab loads neither (checked in a fresh interpreter).
2. **Runtime guard.** `no_device_access()` installs a process audit hook that, during a
   replay, refuses `socket.__new__`, `connect`, `bind`, `sendto`, `sendmsg`, process creation
   (`subprocess.Popen`, `os.system`, `os.exec*`, `os.posix_spawn`, `os.spawn*`, `os.fork`),
   `ctypes.dlopen` and opening paths under `/dev/`, `/sys/class/net` or `\\.\`. A refusal
   that library code swallows is still counted and fails the run (exit 11). Tests try each
   of these from inside the decoder. In a fresh process the cantools import itself runs under
   the guard without a refusal.
3. **Inputs.** `lstat` and a same-file check before reading, `O_NOFOLLOW` where available,
   byte caps enforced while streaming, and size/mtime/ctime/path identity compared after
   reading.

Limits of the guard: audit hooks see Python-level events only. Native code that issues
system calls directly (a C extension) is not intercepted; the pinned tree's native parts
are bitstruct and wrapt, neither of which does I/O. The hook stays installed for the life
of the process but acts only inside a replay. A writer that restores size, mtime and ctime
while a file is read is not detected.

## 6. Dependencies and licences

PexisLink stays Apache-2.0 with no runtime dependency. The optional decoder tree, as
installed and tested here (Python 3.11.17, 3.12.3, 3.13.16), licences from package metadata:

| Package | Version | Licence | Pulled in by |
|---|---|---|---|
| cantools | 44.1.0 (published 2026-09-16) | MIT | the `can` extra |
| python-can | 4.6.1 | **LGPL-3.0-only** | cantools |
| bitstruct | 8.23.0 | MIT | cantools |
| textparser | 0.26.2 | MIT | cantools |
| argparse-addons | 0.12.0 | MIT | cantools |
| crccheck | 1.3.1 | MIT | cantools |
| wrapt | 1.17.3 | BSD-2-Clause | python-can |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | python-can |
| typing-extensions | 4.16.0 | PSF-2.0 | python-can |

- **No third-party code is vendored or redistributed** by this repository: the user installs
  these packages from PyPI. python-can is used unmodified as a separately installed library
  and is imported only by cantools.
- If PexisLink were ever distributed **bundled** with this tree (a wheelhouse, a frozen binary,
  a container image), the LGPL-3.0 obligations for python-can (notice, licence text, the
  ability to replace the library) and the MIT/BSD notices would apply. That is not done here
  and needs its own review.
- Why 44.1.0 and not 45.0.0: the two newest releases were under two weeks old on 2026-10-15;
  44.1.0 was the newest release at least four weeks old.
- The user-provided `cantools-master.zip` and `tsfresh-main.zip` were not used, inspected for
  code or committed. tsfresh is Lab B and is not used here.

## 7. Resources (measured, synthetic data, 4 CPUs, 16 GB)

1,000,000 frames (48.5 MiB, 45 % SYNTH_STD, 45 % SYNTH_EXT, 10 % random ids), real cantools:

| Python | Time | Peak RSS |
|---|---|---|
| 3.11.17 | 31.1 s | 29.7 MB |
| 3.12.3 | 33.1 s | 30.3 MB |
| 3.13.16 | 35.4 s | 31.2 MB |

Memory does not grow with the number of frames (tracemalloc test). Records were
byte-identical across Python versions.

## 8. Not done or not verified

- Real vehicle data, real or OEM DBCs, any physical meaning of a signal.
- DBCs that are not UTF-8 (many real ones are Windows-1252) are rejected.
- Multiplexed messages decode through cantools but are not covered by a dedicated test;
  container messages and CAN FD are rejected.
- Native-code system calls are outside the audit hook (section 5).
- Windows and macOS were not run.
- Lab B (tsfresh) is not started.
