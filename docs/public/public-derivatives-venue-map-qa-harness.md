# Public Derivatives Venue-Map QA Harness

This artifact provides a public-only metadata fixture and checker for crypto nonlinear-derivatives venue semantics. It is intentionally separate from record-level market-data freshness validation: the goal here is to prevent ingestion systems from silently merging unlike surfaces such as native crypto options, options on futures, listed ETF-share options, and perpetual references.

## Files

- `fixtures/public_derivatives_venue_map.json`
- `scripts/check_public_derivatives_venue_map.py`

## Run

```bash
python3 scripts/check_public_derivatives_venue_map.py fixtures/public_derivatives_venue_map.json --pretty
python3 scripts/check_public_derivatives_venue_map.py --self-test --pretty
```

The checker uses only the Python standard library. It validates required public metadata fields, HTTPS source URL presence, explicit unknown handling, degradation/freshness consistency, and blocks fixture fields that look like private account data, wallet/auth material, MNPI, rankings, signals, thresholds, execution instructions, or strategy logic.

## Scope

Allowed fixture content is public metadata only:

- Venue and product family.
- Underlying exposure.
- Contract type.
- Quote, collateral, and settlement asset semantics.
- Session and timestamp caveats.
- Public source URL fields.
- Freshness/degradation flags.
- Explicit unknowns when a field should not be asserted.

The fixture is not a venue ranking, trading model, live market-data scrape, execution guide, or account-state export.
