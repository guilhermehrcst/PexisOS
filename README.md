# PexisLink

**Open-source research in vehicle telemetry, anomaly detection, and predictive health.**

PexisLink starts with a **small, read-only, reproducible experiment**: compare a vehicle's later coolant-temperature readings against an earlier reference period under similar engine-speed and vehicle-speed conditions. Its long-term research direction includes commercial vehicles, robotics, aerospace telemetry, and prognostics.

> [!IMPORTANT]
> **This is research software, not a diagnostic or safety system.** An anomaly is not evidence of a mechanical defect, an estimate of failure probability, or a remaining-useful-life prediction. Do not use the output to decide whether a vehicle is safe to operate or to control vehicle systems.

## Quick start

Requires Python 3.11+ and no runtime dependencies.

~~~bash
python -m pip install -e .
python -m pexislink --baseline examples/baseline.csv --observations examples/observations.csv
python -m unittest discover -s tests -v
~~~

The CLI emits **JSON Lines**, one assessment per observation. With the included *synthetic* data, the statuses are normal, suspected_anomaly, and anomaly. The default threshold (8 degrees C), operating buckets and persistence settings are **demonstration parameters, not validated automotive thresholds**.

### Required CSV columns

timestamp, vehicle_id, engine_rpm, speed_kmh, coolant_temp_c

- Timestamps require an ISO 8601 UTC offset; samples must be ordered per vehicle.
- Baseline samples must precede observations for the same vehicle.
- Reference readings are isolated by vehicle and coarse engine-speed / vehicle-speed ranges.
- Without enough comparable reference readings, the output is insufficient_baseline, **not** normal.
- No OBD adapter, private data, cloud service, or vehicle actuation is required.

## Design

**Known v0.1 limitation:** sample-based persistence resets at operating-bucket boundaries and cannot distinguish a transient from sustained abnormal behaviour. The historical v0.1 rule is deliberately preserved as a benchmark comparator while the research-grade replacement in [PR #3](https://github.com/guilhermehrcst/Pexislink/pull/3) uses time-based evidence states. Neither version establishes that a vehicle is safe or predicts component failure.

See [architecture and safety boundaries](docs/ARCHITECTURE.md). The initial milestone prioritizes trustworthy ingestion, clear semantics, and deterministic tests over complex models.

**Next milestones (not yet implemented):** quality flags for missing and unreliable sensors; offline benchmarking with labeled real-world datasets; calibrated model comparison; supported read-only vehicle adapters; and eventually specialized aerospace research, always subject to independent validation.

## Open source

Apache-2.0. Contributions and bug reports are welcome once contribution and security processes are documented. Avoid committing real vehicle identifiers, location traces, credentials, or proprietary datasets.
