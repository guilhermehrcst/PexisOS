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
    [--max-frames-mb 256] [--max-line-bytes 256] [--max-rows 5000000] [--max-dbc-kb 4096] [--max-messages 10000] \
    [--max-signals-per-message 512] [--max-signals 50000]
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
| `timestamp` | RFC 3339: `YYYY-MM-DDTHH:MM:SS`, optional fraction of 1-6 digits, `Z` or `±HH:MM` (`-00:00` accepted as UTC); years 1970-2199; real calendar dates; leap seconds (`:60`) are rejected |
| `arbitration_id` | `0x` + 1-8 hex digits |
| `is_extended_id` | `0`: 11-bit, id ≤ `0x7FF`; `1`: 29-bit, id ≤ `0x1FFFFFFF` |
| `data_hex` | 0-16 hex digits, even length (0-8 bytes). More than 8 bytes (CAN FD) is `unsupported_frame`. No RTR or error frames |

`--max-line-bytes` counts content bytes, excluding a BOM and the line terminator. Input order is kept. Equal and backwards timestamps are recorded per row (`order`) and
counted; nothing is sorted or removed.

**DBC**: a local UTF-8 file (a leading BOM is accepted) parsed by cantools in strict mode. A
cheap line count runs first and rejects more than `--max-messages` messages, more than
`--max-signals` signals or more than `--max-signals-per-message` in one message: cantools'
strict parse is quadratic in the signals of one message (the review measured 119 s for 32,000
signals). The DBC is rejected (`invalid_dbc`) if it cannot be parsed, defines no message, CAN
FD or longer-than-8-byte messages, container messages, an id outside its format's range, two
messages with the same id and format, or two messages with the same name. cantools itself
accepts the last two and only logs a warning naming the messages; the lab drops cantools log
records and warnings during the parse, so no DBC name reaches stderr.

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
  `value_out_of_range` (an integer beyond the IEEE double range, which JSON readers would turn
  into Infinity), `unsupported_value_type`, `unexpected_signal`, `unsupported_message_type`,
  `decode_failed` (decoder exception, e.g. a multiplexer value not in the DBC; its text is never
  written).
- Signal names, units and scaling come only from the DBC (`unit: null` when the DBC has none).
  Values are finite JSON numbers: integers when the DBC scale and offset are integral.
  Integers beyond 2^53 are exact in the file but lose precision in IEEE-double JSON readers.
- **Raw payload bytes are not written** anywhere.

**Summary** (`--summary`, schema `pexislink.can-replay.v1`): tool version, package-source
SHA-256, Python version, decoder name and version, limits, the command with paths as
placeholders, SHA-256 and byte length of both inputs, the DBC's message schema (ids, names,
lengths, signal names and units), status counts, timestamp-order counts, the number of
distinct unknown ids (counted up to 100,000; `distinct_unknown_ids_capped` says whether the cap was hit), and the SHA-256, count and length of the records. No paths, no payloads,
no run time. Outputs are byte-identical across runs; across Python 3.11-3.13 only
`tool.python` differs (checked on 1,000,000 frames).

**Commit protocol.** Both files are written to temporary files next to their destinations
and fsync'ed; then an existing summary is removed, the records are renamed into place, the
directory fsync'ed, and the summary renamed last. Two-file atomicity is not claimed: **the
summary is the commit marker**. A records file without a summary whose `records.sha256`
matches is an interrupted run. A run that fails before the commit leaves previous outputs
untouched. A failure or Ctrl-C during the commit can leave the new records without a summary
(the interrupted state above); the old summary is already gone, so it never pairs with them.
No temporary file is left in either case.

## 4. Failure semantics

| Exit | Code | Cause |
|---|---|---|
| 0 | | success |
| 1 | `output_write_error` | destination directory missing or unwritable, disk full |
| 2 | usage | bad arguments or limits; `--out` equal to `--summary`; an output equal to an input (also via `.`, a hard link or a symlink); an existing output that is a link, directory or device |
| 3 | `decoder_unavailable` | cantools not installed |
| 4 | `invalid_dbc` | not UTF-8, over the message or signal limits, not parseable in strict mode, or a rejected construct (section 2) |
| 5 | frame contract | `invalid_header`, `csv_quoting`, `field_count`, `invalid_utf8`, `invalid_line` (stray CR/NUL), `invalid_timestamp`, `invalid_arbitration_id`, `invalid_extended_flag`, `arbitration_id_out_of_range`, `invalid_data_hex` |
| 6 | `unsupported_frame` | more than 8 data bytes |
| 7 | `input_unreadable` | missing, not a regular file, permission |
| 8 | `input_symlink` | an input is a symbolic link (never followed) |
| 9 | `input_too_large`, `line_too_long` | file bytes, line length, row count, DBC size |
| 10 | `input_changed` | an input grew past its cap, was rewritten (size, mtime or ctime differ) or replaced while read |
| 11 | `safety_guard` | replay code attempted socket, name-resolution, process, native-library or device access, whether or not the refusal was caught, or left a thread running |

stderr carries `error: <code>: <message>` with an optional line number, never a path or
payload. A gate failure commits nothing.

## 5. Safety barriers

1. **No device code path.** The package never imports python-can (`can`) and never creates a
   bus; cantools is imported only inside `load_cantools_decoder` (AST tests). Importing the
   lab loads neither (checked in a fresh interpreter).
2. **Runtime guard** (a second barrier, not a sandbox). `no_device_access()` installs a
   process audit hook that, during a replay, refuses socket creation, `connect`, `bind`,
   `sendto`, `sendmsg`, name resolution (`getaddrinfo`, `gethostbyname`, `gethostbyaddr`,
   `getnameinfo`), the audited process entry points (`subprocess.Popen`, `os.system`,
   `os.exec*`, `os.posix_spawn`, `os.spawn*`, `os.fork`, `os.forkpty`), `ctypes.dlopen` and
   opening anything that is not a regular file or directory (character and block devices,
   FIFOs, sockets). Paths are resolved against the current directory before the check, so
   `//dev/null`, `/./dev/null` or a `chdir("/dev")` do not bypass it. A refusal fails the run
   with exit 11 whether library code catches it or not, and so does a thread that was started
   during the replay and is still alive afterwards. Tests try each of these from inside the
   decoder. In a fresh process the cantools import itself runs under the guard without a
   refusal.

   Not covered (documented gaps): a direct call to the private `_posixsubprocess.fork_exec`
   (no audit event); a path opened relative to a `dir_fd` (the hook does not see the
   directory); system calls made by native code; activity of other threads after the replay
   (only threads still alive are detected). While active, the guard also refuses these events
   in unrelated threads of the same process. In the pinned tree, `_posixsubprocess` and
   `multiprocessing` appear nowhere; threads and name resolution appear only in python-can
   bus and interface modules (`can/bus.py`, `can/notifier.py`, `can/interfaces/...`), which run
   only when a bus is created, which the lab never does.
3. **Inputs.** `lstat` and a same-file check before reading, `O_NOFOLLOW` where available,
   byte caps enforced while streaming (the DBC is read with a bounded `read`), and
   size/mtime/ctime/path identity compared after reading. A writer that restores size, mtime
   and ctime while a file is read is not detected.

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
| 3.11.17 | 29.9 s | 29.7 MB |
| 3.12.3 | 33.3 s | 30.6 MB |
| 3.13.16 | 33.2 s | 31.3 MB |

Memory does not grow with the number of frames (tracemalloc test). Records were
byte-identical across Python versions.

## 8. Not done or not verified

- Real vehicle data, real or OEM DBCs, any physical meaning of a signal.
- DBCs that are not UTF-8 (many real ones are Windows-1252) are rejected.
- Multiplexed messages decode through cantools (checked in the review) but have no dedicated
  test; container messages and CAN FD are rejected.
- Native-code system calls are outside the audit hook (section 5).
- Windows and macOS were not run.
- Lab B (tsfresh) is not started.

## 9. Independent adversarial review (of `aaac4dd`)

All findings were reproduced; each fix has a test that fails on `aaac4dd`
(`ReviewRegressionTests` and the `test_review_*` real-decoder tests).

| ID | Severity | Finding | Fix |
|---|---|---|---|
| F1 | Medium | An uncaught guard refusal escaped as a traceback (exit 1); regular files under `/dev/shm` were refused | refusals always become exit 11; the device rule is by file type, not path prefix |
| F2 | Medium | Name resolution, re-spelled device paths and a lingering thread bypassed the guard; the docs overclaimed | DNS events blocked, paths resolved, live new threads fail the run; remaining gaps documented |
| F3 | Medium | A DBC under the 4 MiB cap could stall the parse for minutes (quadratic in signals per message) | pre-parse line count with per-message and total signal limits |
| F4 | Low | Ctrl-C during the commit left a temporary file; the docs overstated "untouched" | cleanup on any exception; commit-time behaviour documented |
| F5 | Low | `distinct_unknown_ids` was silently capped | fixed cap of 100,000 with a `capped` flag |
| F6 | Low | The line limit depended on the terminator and BOM | counts content bytes only |
| F7 | Low | cantools warnings named DBC messages on stderr; duplicate names accepted | log records and warnings dropped during the parse; duplicate names and empty DBCs rejected |
| F8 | Low | Integers beyond the double range were written as decoded | `value_out_of_range` |
| F9 | Low | Unbounded DBC read, long output names, permissions reset, DBC BOM rejected, undocumented leap-second and multiplexer behaviour | bounded read, short temp names, existing permissions kept, DBC BOM accepted, documented |
