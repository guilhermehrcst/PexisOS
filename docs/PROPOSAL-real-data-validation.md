# Proposal: validating PexisLink on independent real-world data

Status: **proposal only, nothing implemented or downloaded.** This environment
cannot reach the dataset hosts (HTTP 403 from the network policy, see
BENCHMARK.md §5), so every dataset fact below comes from the publishers'
documentation as known before this work. **Each fact must be re-checked against
the source page, and the licence read in full, before any download.**

## The question that matters

PexisLink's synthetic benchmark shows internal consistency with assumptions we
wrote. The open question is external: *on real vehicles, do the evidence states
behave as documented, and do confirmed anomalies precede independently recorded
failures more often than chance?* Answering it needs real per-vehicle time series
with independently labelled failure or repair events. Separating four claims is
essential:

| Claim | Needs | Not the same as |
|---|---|---|
| Anomaly detection | real series, labelled abnormal periods | predicting failure |
| Sensor-integrity assessment | real sensor faults (frozen, dropout, drift) labelled | detecting engine faults |
| Fault classification | snapshots labelled by fault type | detecting *before* the fault |
| Failure prediction / RUL | time series with event times, censoring, lead-time evaluation | classifying past failures |

## Candidate datasets

| | Scania APS (UCI) | Scania Component X (SND) | NASA C-MAPSS |
|---|---|---|---|
| What it is | Snapshot records from heavy trucks; positive class = failure attributed to the air pressure system | Operational readouts of a fleet of heavy trucks over time, with a repair event for one anonymised component | Simulated turbofan engines run to failure |
| Engineering question it can answer | Fault classification of records: "is this record from an APS-failure truck?" | Time to event: "does the signal of a vehicle change before its repair, compared with vehicles that were not repaired?" | RUL estimation on simulated degradation |
| Temporal trajectories | **No** (independent rows, no vehicle id or time) | **Yes**, per vehicle, irregular readout times | **Yes**, per engine, by cycle |
| Failure labels and times | Class label only, no time | Repair flag and time of the last readout (time-to-event, right-censored); test labels as windows before the event | Run to failure in training; true RUL for truncated test series |
| Supports | Fault classification only | Time-to-event / proximity-to-failure classification; anomaly detection per vehicle | RUL, anomaly detection on simulated data |
| Variables | ~170 anonymised counters and histogram bins | Anonymised counters and histograms, plus vehicle specifications | 3 operating settings, 21 sensors |
| Coolant / rpm / speed series | No | Not identifiable (anonymised) | No (turbofan) |
| Licence and provenance (verify) | Released by Scania for the IDA 2016 challenge; stated as GNU GPL v3 in its readme | Published by Scania and academic partners through the Swedish National Data Service, described in a 2024 data paper; stated as CC BY 4.0 | NASA Prognostics Center of Excellence; US government work |
| Size (verify) | tens of MB | hundreds of MB or more | about 12 MB compressed |
| CPU-only fit | Yes | Probably, with streaming; check size first | Yes |

### Leakage and generalisation risks

- **APS.** No vehicle id: records from the same truck can sit in both splits, so
  high scores can come from memorising trucks. A heavy class imbalance and the
  challenge's cost matrix (false negatives far more costly) make accuracy
  misleading. It cannot show anything happens *before* a failure.
- **Component X.** Splits must be by vehicle and by time. The repair time is
  known only for repaired vehicles; unrepaired vehicles are censored, not
  healthy, so treating them as negatives biases results. Anonymised features
  prevent any physical interpretation, and fleet effects (model, market, usage)
  can confound "near repair". The readout cadence differs between vehicles,
  which is exactly the sampling-rate weakness found in v0.2 (finding 3).
- **C-MAPSS.** Simulated, so it shares the circularity of our own generator in a
  different domain. Common pitfalls: normalising with statistics from test
  engines, capping RUL differently from the published protocol, tuning on the
  test subset. A result there says nothing about vehicles.

## Recommendation: one experiment

**Scania Component X, as a time-to-event test of the evidence-state method, not
of the coolant rule.** It is the only candidate with real vehicles, per-vehicle
trajectories and independently recorded events, so it is the only one that can
falsify the claim "deviation from a vehicle's own reference carries information
about later repairs". APS cannot (no time); C-MAPSS cannot (not real, not
vehicles).

Protocol, to be pre-registered before any result is seen:

1. Licence read and recorded; archive pinned by SHA-256; download on demand in a
   separate module, never in CI; no data committed.
2. Splits by vehicle, frozen before evaluation; the published test labels used
   once, at the end.
3. Per vehicle, an early reference window; per selected counter, the robust
   median and scale as in `reference.py`; the same confirmation and abstention
   rules, with the reference minimum and evidence gap expressed in time (fixing
   v0.2 finding 3 first).
4. Primary outcome: share of repaired vehicles with a confirmed deviation within
   a pre-declared horizon before repair, against a **placebo**: the same
   horizon on censored vehicles at matched calendar and mileage times. Excess
   rate with a vehicle-bootstrap interval; lead-time distribution; coverage.
5. Report what cannot be claimed: anonymised features mean no mechanism, and a
   repair is not necessarily a failure.

Expected cost: one CPU, a few GB of disk at most, hours of compute. Main risk:
the dataset size and format must be checked before committing to the plan.

## Not proposed

No learned model is proposed until the simple method has been evaluated on real
data with this protocol. No result from any of these datasets would validate
the coolant-specific rules, because none contains coolant temperature; that
still requires licensed fleet telemetry with workshop records (BENCHMARK.md §4).
