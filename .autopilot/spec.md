# Project Spec (Authoritative)

This file is the canonical source of truth for autopilot project generation.
Autopilot must not ask questions at runtime; specify constraints explicitly here.

## Goal
Build a practical BTC/ETH crypto options volatility-dislocation scanner for an operator researching volatility-market signals. The scanner must use live public market data, print a timestamped CLI table, and expose a browser dashboard with visualizations that make ATM implied-vol term structure, ETH/BTC vol spread, and top dislocation flags easy to inspect.

## Must-haves
- Use public Deribit market-data endpoints for active BTC and ETH option instruments plus relevant implied-volatility fields.
- Approximate ATM IV for at least three expiries per asset by selecting the listed strike nearest the expiry-specific underlying price and averaging call/put `mark_iv` when both sides are available.
- Calculate term-structure slopes between near and farther expiries.
- Calculate ETH-minus-BTC ATM IV spread for matched expiries where possible.
- Rank and print the top three dislocations by absolute term-structure steepness or ETH/BTC vol spread.
- Provide a single runnable code artifact with no package dependencies.
- Provide an end-user dashboard with visualizations for term structures, spreads, slope steepness, and top flags.

## Non-goals
- Execute trades, produce investment advice, persist historical market data, or require API credentials.

## Constraints
- Determinism: prefer lockfiles/pinned versions where feasible.
- Safety: no destructive commands; no writes outside the repo.
- Security: commands must comply with `.agents/security-policy.json`; no secrets or production data are required for local development.
- Platform: any platform with Python 3 and outbound access to Deribit public APIs.
- License: repository default.

## Acceptance checks
- `python3 -m py_compile crypto_vol_dislocation_scanner.py` passes.
- `python3 crypto_vol_dislocation_scanner.py --max-expiries 6 --top 3` prints timestamped BTC/ETH ATM IV, term-slope, spread, and top-flag tables.
- `bash scripts/ci` passes through the repo manifest/security workflow.
- Visual test is one command: `bash scripts/dev`.
- Playwright can load the local dashboard and observe non-empty charts and tables.
- Security gates pass without secrets or policy weakening.
