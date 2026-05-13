# Public Derivatives Source-Coverage Provenance Probe

This artifact provides a public-only source manifest and checker for crypto derivatives ingestion preflight checks. It is separate from the prior venue-map QA harness and record-integrity validator: this checker asks whether a declared public source is reachable or safely degraded, not whether instruments are normalized or market-data records are fresh.

## Files

- `fixtures/public_derivatives_source_coverage_manifest.json`
- `scripts/check_public_derivatives_source_coverage.py`

## Run

Offline metadata validation:

```bash
python3 scripts/check_public_derivatives_source_coverage.py fixtures/public_derivatives_source_coverage_manifest.json --pretty
```

Bounded public URL probe:

```bash
python3 scripts/check_public_derivatives_source_coverage.py fixtures/public_derivatives_source_coverage_manifest.json --probe --pretty
```

Deterministic self-tests:

```bash
python3 scripts/check_public_derivatives_source_coverage.py --self-test --pretty
```

The checker uses only the Python standard library. Probe mode makes bounded public `GET` requests with a short timeout and reads only a small response prefix. It does not use credentials, cookies, browser storage, wallet material, private endpoints, or account-specific data.

## Scope

Allowed manifest content is public source provenance only:

- Source URL.
- Venue label.
- Product family.
- Expected content type.
- Auth requirement stated as public/no-credential access.
- Timestamp or session caveat.
- Freshness or degradation status.
- Explicit unsupported cases.
- Safe failure reasons.

The manifest is not a live scrape, source ranking, trading model, execution guide, private dataset, or account-state export.
