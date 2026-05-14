# Public Derivatives Replay-Parity QA Harness

This artifact provides a public-only replay/parity checker for synthetic crypto derivatives ingestion records. It is separate from the existing integrity validator, venue-map harness, and source-coverage probe: this checker focuses on event-time ordering, no-future-leakage replay cutoffs, deterministic replay metadata, and degraded/failure provenance.

## Files

- `fixtures/public_derivatives_replay_parity_cases.json`
- `scripts/check_public_derivatives_replay_parity.py`

## Run

Validate the committed fixture:

```bash
python3 scripts/check_public_derivatives_replay_parity.py fixtures/public_derivatives_replay_parity_cases.json --pretty
```

Run deterministic self-tests:

```bash
python3 scripts/check_public_derivatives_replay_parity.py --self-test --pretty
```

The checker uses only the Python standard library. The fixture is synthetic and contains no live market data, account state, wallet material, MNPI, trading instructions, venue rankings, model thresholds, or proprietary strategy logic.

## Cases

- `valid_ordered_public_synthetic`: ordered replay rows that should pass.
- `out_of_order_event_time_detected`: descending event time should be detected.
- `future_leakage_window_detected`: records after the replay cutoff should be detected.
- `replay_metadata_mismatch_detected`: sequence and deterministic identifier mismatch should be detected.
- `degraded_failure_provenance_recorded`: degraded data must carry an explicit safe failure reason.

The CLI returns overall `pass` only when every synthetic case produces its expected result and the public-safety scanner finds no disallowed fields or values.
