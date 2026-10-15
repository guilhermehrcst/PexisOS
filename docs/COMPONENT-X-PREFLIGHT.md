# Scania Component X v3: source evidence and local preflight (v0.3 Stage A)

Plan: [`docs/plans/0002-component-x-v03-preflight.md`](plans/0002-component-x-v03-preflight.md).
Tracking issue: [#4](https://github.com/guilhermehrcst/Pexislink/issues/4).
Code: `src/pexislink/componentx.py`. Tests: `tests/test_componentx.py` (synthetic fixtures only).

> [!IMPORTANT]
> **No real Scania file has been downloaded, inspected or hashed in this work.** Every
> test runs on synthetic fixtures written by this project. Nothing here is a result
> about the dataset, about vehicles or about predictive value.

## 1. What the preflight is, and is not

It is a read-only, offline, streaming check of files **you** have obtained from the
publisher. It answers: are these the files the publisher describes, are they
structurally sound, and are they fit for vehicle-isolated research later?

It is not a loader for modelling. It derives no feature, trains nothing, predicts
nothing, does not interpret anonymous columns, and does not read outcome values.

## 2. Gate 0: source evidence (2026-10-15)

### 2.1 Access attempts from this environment

Every publisher host was unreachable:

| Request | Result |
|---|---|
| `curl https://researchdata.se/en/catalogue/dataset/2024-34/3` | `curl: (56) CONNECT tunnel failed, response 403` |
| `curl https://doi.org/10.5878/bnh5-ka77` | same |
| `curl https://api.researchdata.se/dataset/2024-34/3` | same |
| `curl https://pmc.ncbi.nlm.nih.gov/articles/PMC11933314/`, `https://www.nature.com/articles/s41597-025-04802-6`, `https://creativecommons.org/licenses/by/4.0/legalcode` | same (HTTP code 000) |
| Fetch tool on `researchdata.se`, `api.researchdata.se`, `pmc.ncbi.nlm.nih.gov`, `arxiv.org` | `getaddrinfo ENOTFOUND` |

The environment's network policy denies these hosts. To enable them, edit the cloud
environment's Network access (session title bar → environment menu → Edit) and add
`researchdata.se`, `api.researchdata.se` and `doi.org` to Allowed domains
([steps](https://code.claude.com/docs/en/cloud-environments#network-access)).

### 2.2 What is known, and how well

Only web-search index excerpts of the publisher's pages were reachable. They are
**secondary**: the pages themselves were not read, so nothing below is
`verified_local`. The same table is in code as `componentx.SOURCE`, `LICENSE` and
`EXPECTED`, and is copied into every manifest.

| Item | Claim | Evidence |
|---|---|---|
| Title, version, DOI | SCANIA Component X Dataset, version 3, `10.5878/bnh5-ka77` | search excerpt of the publisher's DDI export for 2024-34/3 |
| Previous version | version 2, `10.5878/jvb5-d390` (the DOI the descriptor cites) | search excerpt of the publisher's download page; issue #4 |
| Size | 11 files, about 1.54 GiB | search excerpt of the catalogue page |
| Licence | CC BY 4.0 | **unverified**: issue #4 only; the version 3 licence field could not be read |
| `train_tte.csv` | 337.32 KiB; 23,550 rows; columns `length_of_study_time_step`, `in_study_repair` | catalogue excerpt; descriptor (via search) |
| `train_specifications.csv` | 1.03 MiB; 23,550 rows; 8 categorical features | catalogue excerpt; descriptor (via search) |
| `validation_labels.csv` | 37.83 KiB; 5,046 rows; `class_label` | catalogue excerpt; descriptor (via search) |
| `test_labels.csv` | 37.78 KiB (38,682 bytes); 5,045 rows | catalogue excerpt |
| `train_operational_readouts.csv` | 1,122,452 rows, 23,550 vehicles, 107 columns (about 1.14 GB) | descriptor (via search); file name and size from issue #4 |
| `validation_` / `test_operational_readouts.csv` | 196,227 / 198,140 rows, 5,046 / 5,045 vehicles | issue #4 citing the descriptor; file names unverified |
| `validation_` / `test_specifications.csv` | exist | **name inferred by analogy, unverified** |
| Validation class counts | conflicting figures in two secondary sources | not resolved; not needed in Stage A |

**Not established:** exact file names of all 11 files, the two documentation files,
compression, encoding, column headers, the boundary convention of the label
classes, public download endpoints, and the licence of version 3.

## 3. Gate 1: what the preflight checks

```bash
pexislink dataset preflight --source component-x-v3 --input-dir DIR --out MANIFEST.json \
    [--max-file-mb 2048] [--max-line-bytes 65536] [--max-rows 5000000] \
    [--max-vehicles 100000] [--max-columns 1024]
```

Files are recognised by exact name, top level of `DIR` only. Symbolic links, devices,
directories and FIFOs are not followed or read. Every file is read once, in a
stream; its SHA-256 covers every byte even when parsing stops early.

| Role | Files | Checks |
|---|---|---|
| operational | `*_operational_readouts.csv` | strict UTF-8 (BOM noted), no NUL, bounded line length, strict CSV quoting, header (safe names, unique, `vehicle_id` and `time_step` present), field count, vehicle-id syntax, strict numbers (no `nan`/`inf`/overflow/`1_000`/non-ASCII digits/blanks; nothing coerced to 0), per vehicle in file order: backwards `time_step`, duplicate `(vehicle_id, time_step)`, non-contiguous vehicle rows, positive gap histogram (powers of two, no unit assumed); per column: missing, non-numeric, non-finite, over-long, integers beyond 2^53, min, max, and *candidate* decreases within a vehicle's contiguous rows |
| specifications | `*_specifications.csv` | as above for structure; duplicate `vehicle_id`; per column missing count and number of distinct values (capped) |
| outcome | `train_tte.csv`, `validation_labels.csv`, `test_labels.csv` | **metadata only**: header line, byte length, SHA-256, physical line count. Values are never decoded (a test feeds invalid UTF-8 after the header to prove it) |

Across files: vehicle-ID overlap between the train, validation and test operational
files, and operational-versus-specification coverage per split, as counts. Label
coverage is deliberately not computed (outcome files are not parsed).

**Duplicate-key completeness.** Without sorting or storing every key, duplicates are
found when adjacent within a vehicle. If a vehicle's `time_step` never goes
backwards, this is exhaustive (any duplicate would be a non-increase); otherwise the
count is reported as a `lower_bound`.

**Candidate decreases** are counted, not interpreted: the tool does not assume any
column is a counter, and decreases across a vehicle's non-contiguous rows are not
compared.

### Failure semantics

| Situation | Effect |
|---|---|
| A file fails structurally (encoding, CSV syntax, header, limits, compression, changed while read) | that file `check: fail` with `fatal: {code, line}`; it contributes no vehicle set; manifest still written; exit 3 |
| A row is rejected (field count, vehicle id, `time_step`) | counted with the first 5 line numbers; the file fails; exit 3 |
| A feature cell is missing or invalid | counted per column; the row is kept; not blocking |
| Expected files absent, nothing blocking | `overall: incomplete`, exit 4 |
| All nine expected files present and clean | `overall: pass`, exit 0 (a structural statement only) |
| Bad arguments, missing input directory, manifest inside the input directory | exit 2, no manifest |
| Manifest cannot be written | exit 1 |

Compressed files (gzip, zip, xz, bzip2, zstd) are detected by magic bytes and not
decompressed (no decompression bombs); decompress them yourself and re-run.

## 4. Gate 2: the manifest

Deterministic JSON (sorted keys; same inputs and runtime give identical bytes; only
`tool.python` differs between Python versions, checked on 3.11, 3.12 and 3.13). It
records: dataset identity, licence and evidence with explicit statuses
(`publisher_claim`, `verified_local`, `not_available`, `not_checked`), per file the
byte length, SHA-256, content type, header, checks and a comparison with each
publisher claim, split checks, limits, the command (paths replaced by placeholders),
the PexisLink version, a SHA-256 of the package sources, the git HEAD read from
files, and the Python version.

It contains **no row values, no vehicle identifiers and no local paths**. Time and
peak memory go to stderr only, so they do not break determinism.

## 5. Resources (synthetic data, this container: 4 CPUs, 16 GB RAM)

`scripts/componentx_synthetic_scale.py` writes a synthetic file of the described
shape (107 columns). With 200,000 rows and 4,000 vehicles (124 MiB):

| Python | Time | Peak RSS |
|---|---|---|
| 3.11.17 | 13.0 s | 19.5 MB |
| 3.12.3 | 11.6 s | 21.7 MB |
| 3.13.16 | 15.8 s | 21.7 MB |

Memory is bounded by vehicles and columns, not rows (tested with `tracemalloc`).
Linear extrapolation to the described training file (1.12 M rows, about 1.14 GB):
roughly 65-90 s and a peak RSS in the tens of MB, single core. This is an
extrapolation from synthetic data, not a measurement on the real file.

**Download feasibility.** About 1.54 GiB of disk and no meaningful RAM are needed.
The plan forbids downloading the full bundle without explicit authorisation, so the
recommended order is: read the licence on the catalog page; download the small files
first (`test_labels.csv` is *not* needed and should not be opened: the tool would only
hash it); run the preflight; only then, with authorisation, the operational files.

## 6. How to run with legally obtained files

1. Read the version 3 licence and terms on the [catalog page](https://researchdata.se/en/catalogue/dataset/2024-34/3) and record them.
2. Put the files in a directory outside the repository, or in `data/` (git-ignored). Do not commit them.
3. `pexislink dataset preflight --source component-x-v3 --input-dir DIR --out manifest.json`.
4. Compare `files.*.sha256` with any checksum the publisher provides (none was found yet).
5. Review `overall`, every `fatal`, and every `publisher_claims.*.match: false` before using the data.

## 7. Limitations and what is not validated

- **The real files have never been seen by this tool.** File names, headers, encoding,
  delimiter, quoting and numeric format are assumptions from secondary sources. A
  real header that differs will fail visibly (`missing_key_column`,
  `unsupported_column_name`), not silently.
- Column names must match `[A-Za-z0-9_.-]{1,64}`; others fail the file. Vehicle IDs
  must match the same pattern.
- Files are matched by exact name; a renamed file is reported as unrecognised.
- Rejecting any row fails the file. That is deliberately strict; a real file with a
  few malformed rows will need a documented decision, not a silent filter.
- The git HEAD does not reveal uncommitted changes; `package_sources_sha256` covers
  the code actually run.
- Cross-split overlap needs every split's vehicle IDs in memory (bounded by
  `--max-vehicles`; about 34,000 described in total).
- No checksum from the publisher was found to compare against.
- Synthetic fixtures follow the *described* layout; they cannot reveal problems the
  description does not mention.
