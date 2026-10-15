# PexisLink v0.3 · Stage A: Scania Component X source and ingestion preflight

**Status:** Implementation assignment only. No verified local dataset has been downloaded or evaluated. No predictive models are authorized in this milestone.

**Repository:** https://github.com/guilhermehrcst/Pexislink  
**Tracking issue:** https://github.com/guilhermehrcst/Pexislink/issues/4  
**Working branch:** `feat/component-x-preflight-v0-3` (created from main at `6329baf562aec002984c006c5d849eb44266b4d1`)  
**Source catalog:** https://researchdata.se/en/catalogue/dataset/2024-34/3  
**Publisher DOI:** https://doi.org/10.5878/bnh5-ka77  
**Dataset descriptor:** https://doi.org/10.1038/s41597-025-04802-6

## Objective

Prove that a genuine, legally accessible copy of the *Scania Component X* **version 3** data can be identified, locally inspected, and prepared for causal, vehicle-isolated research, using a reproducible CPU-only workflow.

**Do not** infer the physical meaning of anonymous columns. These are mainly cumulative counters and histogram bins, **not** coolant/RPM instantaneous sensor streams. Treat `time_step` as anonymized relative time, never minutes or hours. A repair event is not a proven component failure. The v0.2 synthetic coolant detector stays unchanged.

## Gate 0: Actual state and publisher evidence (must happen first)

1. Record Git status, actual base SHA, installed Python version, and v0.2 test baseline.
2. Read the official v3 catalog **and license/terms from the primary source**. Publisher metadata currently reports CC BY 4.0 and about 1.54 GiB across 11 files, but the final manifest must distinguish *published metadata* from *verified downloaded bytes*.
3. Record canonical exact file names, extension, compression, byte lengths, expected column headings, label availability, and public download endpoints **only when directly evidenced**.
4. If external network requests are blocked, document URLs/status/errors and continue only with reusable local-file tooling and **explicitly synthetic test fixtures**. Never manufacture a real-data result.
5. The largest official files require an explicit feasibility and capacity check before downloading. Do not automatically download the entire dataset. No cloud spend or paid APIs.

## Gate 1: Read-only local preflight

Deliver a narrowly scoped local CLI, for example:

```text
pexislink dataset preflight --source component-x-v3 --input-dir PATH --out PATH
```

Choose a maintainable actual interface after inspecting existing CLI architecture. Requirements:

- All input paths are local by default; **no network activity inside the preflight CLI**.
- Validate the **real file headers first**. Never hardcode unverified claims from a paper as if inspected.
- Handle UTF-8, quoted CSV, malformed rows, missing fields, duplicates, nonfinite numbers, huge fields, truncated files, integer overflow, and explicit error codes.
- Stream rows instead of materializing datasets in RAM. Bound row size, count, file size, number of vehicles and report size using documented configurable limits.
- Preserve the separation of raw anonymous feature IDs, `vehicle_id`, and `time_step`; do not write identifiable raw row examples into logs or output.
- Check monotonicity, duplicate `(vehicle_id, time_step)` keys and discontinuities per vehicle **within each file**; if order is not guaranteed, surface an explicit report and do not silently sort a huge dataset in RAM.
- If train/validation/test are all present, check vehicle-ID intersections without assuming the publisher split is correct. Do not read test labels except to verify presence/schema in a strictly isolated metadata-only path.
- Detect *candidate* counter resets or implausible backward changes, but do **not** automatically reinterpret anonymous histogram/counter semantics without evidence.
- Track corrupted/omitted records transparently; no silent coercion to 0, forward-fill or `healthy`.

## Gate 2: Provenance manifest

Write an inspectable JSON output with:

- dataset name, version and canonical catalog/DOI URLs;
- verified license and attribution requirements (or `unverified`);
- each **locally present** filename, byte length, SHA-256 (computed while streaming), content type and parsed schema;
- schema issues and split-overlap results, including `not_checked` where inputs are missing;
- exact tooling version, PexisLink source SHA, Python/runtime version, and executed commands;
- strict distinction between `publisher_claim`, `verified_local`, `not_available`, and `not_checked`.

Do not commit third-party raw CSVs, derived row-level personal/vehicle identifiers, private logs or dataset bundles. Put data directories in `.gitignore` appropriately, without hiding test fixtures or code.

## Gate 3: Quality engineering

- Python 3.11+; ideally standard library only or an optional, justified dependency.
- Unit tests with clearly labeled **synthetic** micro-fixtures, positive/negative/malformed/corrupt cases, edge boundaries, and bounded-memory/streaming tests.
- Separate **dataset adapter/preflight** from future evaluation models; do not change the v0.1 historical benchmark comparator or v0.2 detector.
- Respect existing packaging, CI permissions and timeouts. All CI checks must run **offline** without downloading real data.
- Provide a real-data dry run on a *small, legally retrieved* official file or document, **only if actually obtained**. Identify when a test uses synthetic fixtures.
- Report what could **not** be verified or executed; no assertions of performance based on the dataset paper.

## Strict non-goals for this PR

- No training, inference, machine-learning accuracy metrics, RUL, prediction claims or neural networks.
- No modifying ECU or vehicles, no new web UI, no cloud/database infrastructure.
- No downloading the ~1.54 GiB dataset without an explicit preflight justification and authorized execution conditions.
- No reading or exploiting future labels for feature preparation.
- No direct changes to `main`, destructive operations, or merge.

## Acceptance criteria

1. Source/license/schema evidence **or** an explicit, reproducible access blocker.
2. Functional offline, bounded streaming preflight with synthetic fixtures.
3. Deterministic and privacy-conscious provenance/quality summary.
4. CLI failure semantics documented and tested.
5. Existing v0.2 CI and benchmark unaffected.
6. Focused documentation explaining source limitations, what is **not yet** validated and how to run with legally obtained local files.
7. Open PR targeting `main`, with exact test commands, CI status and evidence; **leave unmerged**.

Only after independent review of this stage should v0.3 proceed to preregistering hypotheses, freezing vehicle splits and building a real-data benchmark.
