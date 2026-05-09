# Crypto Volatility Dislocation Scanner

A single-file Python scanner for BTC and ETH options implied-volatility dislocations.

The scanner pulls live public Deribit option data, approximates at-the-money implied volatility across expiries, calculates term-structure slopes, compares ETH ATM IV against BTC ATM IV for matched expiries, and ranks the largest dislocation signals.

It can run as a timestamped CLI report, JSON snapshot producer, or local browser dashboard.

## Requirements

- Python 3.10 or newer
- Outbound HTTPS access to Deribit public API endpoints
- No package install, API key, account, or credentials required

## Quick Start

Run the default CLI report:

```bash
python3 crypto_vol_dislocation_scanner.py
```

Run with the project acceptance settings:

```bash
python3 crypto_vol_dislocation_scanner.py --max-expiries 6 --top 3
```

Serve the dashboard:

```bash
python3 crypto_vol_dislocation_scanner.py --serve --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765/`.

## Outputs

The CLI report prints:

- BTC and ETH ATM IV snapshots by expiry
- Term-structure slope rows between adjacent and near-to-far expiries
- ETH minus BTC ATM IV spreads for matched expiries
- Top ranked dislocation flags by absolute signal size
- Warnings for missing, degraded, or thin data

JSON mode emits the same snapshot as structured data:

```bash
python3 crypto_vol_dislocation_scanner.py --json --max-expiries 6 --top 3
```

When running in dashboard mode, these endpoints are available:

- `/` - HTML dashboard with SVG charts and tables
- `/api/snapshot` - JSON snapshot
- `/api/snapshot?pretty=1` - formatted JSON snapshot
- `/healthz` - simple health check

## Methodology

The scanner uses Deribit public `get_instruments` and `get_book_summary_by_currency` data for active BTC and ETH options.

For each asset and expiry, it:

1. Filters out expired or very near-expiry options using `--min-dte-hours`.
2. Finds the expiry-specific median underlying price from available book summaries.
3. Selects the listed strike nearest that underlying price.
4. Uses `mark_iv` from the call and put at that strike when both are available.
5. Falls back to a single-sided ATM approximation when only one side is usable and marks the row as degraded.

Term-structure signals are expressed as annualized implied-volatility point changes normalized to 30 days. ETH/BTC spread signals are shown as ETH ATM IV minus BTC ATM IV in volatility points for matched expiries.

## Options

```text
--serve                 Serve the browser dashboard instead of printing once
--host HOST             Dashboard bind host (default: 127.0.0.1)
--port PORT             Dashboard bind port (default: 8765)
--max-expiries N        Expiries per asset to include (default: 6, minimum: 3)
--min-dte-hours HOURS   Skip options expiring sooner than this (default: 6)
--top N                 Number of dislocation flags to print (default: 3)
--timeout SECONDS       Per-request Deribit timeout (default: 20)
--json                  Print raw snapshot JSON
```

## Validation

Compile-check the script:

```bash
python3 -m py_compile crypto_vol_dislocation_scanner.py
```

Run a live snapshot:

```bash
python3 crypto_vol_dislocation_scanner.py --max-expiries 6 --top 3
```

Check that JSON output is valid:

```bash
python3 crypto_vol_dislocation_scanner.py --json --max-expiries 3 --top 3 | python3 -m json.tool >/dev/null
```

## Notes

- This tool is for market research and inspection, not trade execution or investment advice.
- Output depends on live Deribit API availability and current option-market liquidity.
- Missing or lower-fidelity rows are marked with `degraded=true` and a reason in JSON output, and with a quality note in tables.
