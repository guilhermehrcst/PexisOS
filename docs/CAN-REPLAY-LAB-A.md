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
    [--max-signals-per-message 512] [--max-signals 50000] [--max-output-mb 4096]
```

`--max-output-mb` caps the records file and the summary separately. It is checked **before
every write**, because each decoded row repeats DBC-controlled names and units, so a
frame file within its own limits can still generate far more output than it contains.

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

**Commit protocol.** Each destination's directory is resolved once, the way the kernel
resolves it (a symlink before `..`), and that one directory is used for the temporary file,
the rename and the fsync. Both files are written to temporary files there and fsync'ed. An
existing summary is removed and **its directory fsync'ed before the records are committed**,
even if the files live in different directories. Records are then renamed into place and
their directory fsync'ed; the summary is renamed last and its directory fsync'ed. **Every
directory fsync is required** (POSIX): if one fails, the run fails with exit 1, and a summary
that the failing run had already renamed into place is removed again, so a failure never
leaves a success marker. Windows is not a supported platform for the lab: there the directory
fsync is skipped and durability is not claimed. Two-file atomicity is not claimed: **the
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
| 2 | usage | bad arguments or limits; `--out` equal to `--summary`; an output equal to an input (also via `.`, a hard link or a symlink); two names that differ only by case or Unicode normalization in the same resolved directory (rejected on every filesystem, because a case-insensitive or normalizing filesystem would map them to one entry); an output name of `.` or `..`; an existing output that is a link, directory or device |
| 3 | `decoder_unavailable` | cantools not installed |
| 4 | `invalid_dbc` | not UTF-8, over the message or signal limits, not parseable in strict mode, or a rejected construct (section 2) |
| 5 | frame contract | `invalid_header`, `csv_quoting`, `field_count`, `invalid_utf8`, `invalid_line` (stray CR/NUL), `invalid_timestamp`, `invalid_arbitration_id`, `invalid_extended_flag`, `arbitration_id_out_of_range`, `invalid_data_hex` |
| 6 | `unsupported_frame` | more than 8 data bytes |
| 7 | `input_unreadable` | missing, not a regular file, permission |
| 8 | `input_symlink` | an input is a symbolic link (never followed) |
| 9 | `input_too_large`, `line_too_long` | file bytes, line length, row count, DBC size |
| 10 | `input_changed` | an input grew past its cap, was rewritten (size, mtime or ctime differ) or replaced while read |
| 11 | `safety_guard` | replay code attempted socket, name-resolution, process, thread, native-library, device or kernel-file (`/proc`, `/sys`) access, whether or not the refusal was caught, or left a thread running; the audit hook was refused or receives no events; an open descriptor refers to a device, a kernel control file or a non-local socket |
| 12 | `output_too_large` | the records file or the summary would exceed `--max-output-mb` (checked before the write; nothing is committed) |

stderr carries `error: <code>: <message>` with an optional line number, never a path or
payload. A gate failure commits nothing.

## 5. Safety barriers

The lab is offline by construction. The runtime guard is a **second barrier for a Python
library running in this process, not a sandbox**. `sys.addaudithook` sees only events that
CPython emits, so code that avoids them (native code, saved references) is outside it. The
guarantees are therefore split into three groups.

### 5.1 Effective guarantees (enforced by construction and tested)

1. **No device code path.** The package never imports python-can (`can`) or `socket`, never
   creates a bus and never transmits. cantools is imported only inside `load_cantools_decoder`
   (AST tests). Importing the lab loads neither (checked in a fresh interpreter).
2. **Inputs.** Local regular files only: `lstat` and a same-file check before reading,
   `O_NOFOLLOW` where available. Byte caps are enforced **before** each read or readline
   allocation, at most the remaining file budget plus a one-byte growth lookahead, even when the
   line limit is much larger than the frame-file byte limit. Size, mtime, ctime and path
   identity are compared after reading. A writer that restores size, mtime and ctime while a
   file is read is not detected.
3. **Outputs.** Bounded by `--max-output-mb` before every write (exit 12). Destinations that
   alias each other or an input are rejected, including names equal under case folding or
   Unicode normalization (exit 2). The commit protocol is crash-consistent as described in
   section 3, with every directory fsync required.

### 5.2 Additional protection: the runtime guard (defense in depth)

During a replay, `no_device_access()`:

- **Proves its hook is live.** On every entry it emits a sentinel audit event. If the hook did
  not receive it (for example an embedding application's hook vetoed `sys.addaudithook`, which
  CPython then refuses silently), the replay fails closed with exit 11.
- **Refuses audited effects.** It refuses socket creation, `connect`, `bind`, `sendto`,
  `sendmsg` and name resolution (`getaddrinfo`, `gethostbyname`, `gethostbyaddr`,
  `getnameinfo`). It also refuses the audited process entry points (`subprocess.Popen`,
  `os.system`, `os.exec*`, `os.posix_spawn`, `os.spawn*`, `os.fork`, `os.forkpty`),
  `ctypes.dlopen` and `ctypes.dlsym`, and the thread-start events where CPython emits them
  (`_thread.start_new_thread` from 3.12, `_thread.start_joinable_thread` from 3.13).
- **Refuses device and kernel paths.** It refuses opening anything that is not a regular file or
  directory (character and block devices, FIFOs, sockets) and **anything under `/proc` or `/sys`**,
  even regular files (`/proc/sysrq-trigger`, `/sys/class/net/<iface>/...`). Paths are resolved
  first, so `//dev/null`, `/./dev/null`, a symlink or a `chdir("/proc")` do not bypass it.
  Opening an integer descriptor is judged by what the descriptor refers to.
- **Covers thread starts on 3.11/3.12.** CPython 3.11 emits no thread-start audit event at all,
  so during the replay the guard also wraps `_thread.start_new_thread`, `threading.Thread.start`
  and threading's raw start alias. Activation, audit callbacks and finalization share one lock.
  At exit the guard is deactivated *before* the native entry points are restored, both under
  that lock, so a native entry point is never reachable while the guard is active.
- **Checks open descriptors (Linux).** Before activation, it fails closed if any open descriptor
  refers to a character device (other than the null device or a terminal), a block device, a file
  under `/proc` or `/sys`, a socket that is not AF_UNIX (CAN, packet, netlink, IP), or an
  unidentifiable object. Pipes, anonymous kernel objects (epoll, eventfd), AF_UNIX sockets and
  regular files are allowed. Descriptors opened during the replay go through the audited `open`
  checks above.
- **Fails the run on any violation.** A refusal fails the run with exit 11 whether or not library
  code catches it, and so does a thread started during the replay that is still alive afterwards.

Tests try each path from synthetic decoders. In a fresh process the cantools import itself runs
under the guard without a refusal.

### 5.3 Outside the guarantee (residual risks)

- **Native code.** System calls made by native code, including `os.write` on a descriptor that
  native code obtained, and native thread APIs.
- **Unaudited Python paths.** A direct call to the private `_posixsubprocess.fork_exec` (no audit
  event); a path opened relative to a `dir_fd` (the hook does not see the directory); references to
  raw thread-start functions saved before the replay.
- **Descriptor checks off Linux.** The descriptor check needs `/proc/self/fd` and
  `/proc/self/net/unix`, so it is not performed elsewhere. Socket families other than AF_UNIX are
  refused by default, and an inherited AF_UNIX socket is trusted as local IPC.
- **Activity outside the replay.** The guard does not control anything after deactivation or
  outside the replay window. While active, it also refuses these events in unrelated threads of
  the same process.
- **No hardware isolation is claimed.** Isolation from real CAN hardware rests on 5.1 (no bus
  code, no socket import) plus the operator not running the lab next to live interfaces. In the
  pinned tree, `_posixsubprocess` and `multiprocessing` appear nowhere. Threads and name resolution
  appear only in python-can bus and interface modules (`can/bus.py`, `can/notifier.py`,
  `can/interfaces/...`), which run only when a bus is created, and the lab never creates one.

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

## 10. Automated review on PR #10 (of `3a0d6fb`)

| Finding | Fix |
|---|---|
| P1: when a different error escaped the replay (e.g. a parser that caught the refusal and raised its own error), the guard's violation and live-thread checks were skipped, so the run reported the wrong code and a new thread could outlive the guard | the checks always run before any error propagates; a violation takes precedence (exit 11) |
| P2: an absurd `--max-dbc-kb` overflowed the read size and gave a traceback | every limit is bounded to 1..2^40; anything else is a usage error (exit 2) |

Both have tests in `CodexReviewTests` that fail on `3a0d6fb`.

## 11. Safety gate after the ADR merge

The latest Codex review of Lab A identified four risks, addressed in this branch:

| Severity | Fix and test |
|---|---|
| P1 | Device-path audit in flight while the guard closes: synchronization around callback classification and finalization, with a deterministic held-callback regression |
| P1 | A growing unterminated CSV row exceeding the file budget: read and readline allocations bounded by the remaining bytes plus one lookahead byte |
| P1 | Low-level threads absent from threading.enumerate: audit thread-start events where emitted, plus a temporarily installed Python-level wrapper for CPython 3.11/3.12, with regression tests and documented saved-reference/native bypasses |
| P2 | Old summary surviving a crash after new records are committed: fsync the summary directory after unlink, fail closed if unsupported, then commit records |

The Fabric MCP job now explicitly sets a Bash shell with pipefail so unittest failures cannot be hidden by tee. These are defense-in-depth measures for an offline lab, not an OS sandbox. Independent re-review and CI on the final HEAD are still required before merge.

## 12. Codex review of `7242970` (5 P1, 3 P2)

Every fix has a regression in `CodexReview7242970Tests` (`tests/test_canreplay.py`) that fails
on `7242970`, apart from the two control tests named below.

| Severity | Finding | Fix | Regression test |
|---|---|---|---|
| P1 | The raw-thread wrapper was restored before the guard deactivated, so a native thread start was reachable while the guard was active | deactivate first, then restore, both under the shared lock | `test_native_thread_entry_points_are_never_restored_while_the_guard_is_active` |
| P1 | A vetoed `sys.addaudithook` was silently ignored, and the replay ran unguarded | sentinel audit event on every entry; fail closed (exit 11) | `test_refused_audit_hook_fails_closed` (fresh process with a vetoing hook) |
| P1 | Kernel control files under `/proc` and `/sys` are `S_ISREG` and were allowed | refused by resolved path before `stat`, even if absent | `test_kernel_control_and_information_files_are_refused` |
| P1 | No cap on generated bytes | `--max-output-mb` checked before every write; exit 12 | `test_output_budget_is_enforced_before_each_write`, `test_output_budget_exit_code_and_no_commit` |
| P1 | Destinations equal on a case-insensitive filesystem were treated as distinct | names equal under casefold and NFC in the same resolved directory are rejected | `test_case_or_normalization_aliases_are_rejected` |
| P2 | Directory fsync failures on the commit were ignored | every directory fsync is required; a marker renamed by a failing run is withdrawn | `test_failed_fsync_after_the_summary_rename_withdraws_the_marker`, `test_failed_fsync_after_the_records_rename_prevents_the_summary` |
| P2 | `link/../file` was collapsed lexically, so the temporary file and the fsync used the wrong directory | the destination directory is resolved once (`realpath`) and used for temp file, rename and fsync | `test_destination_parent_is_resolved_like_the_kernel` |
| P2 | Inherited device or CAN socket descriptors were not checked | fail-closed descriptor check before activation (Linux); integer-descriptor opens judged during the replay; residual limits in 5.3 | `test_inherited_unsafe_descriptors_refuse_the_replay`, `test_opening_an_unsafe_descriptor_during_the_replay_is_refused` |

Controls, not regressions: `test_local_pipes_and_unix_sockets_are_allowed` proves the descriptor
check does not refuse local IPC. `test_output_names_must_name_a_file` is hardening, because `7242970`
already refused `.` and `..` as existing directories.

The summary's `limits` object gains `max_output_bytes`. The addition is additive, and the schema
name `pexislink.can-replay.v1` is unchanged.
