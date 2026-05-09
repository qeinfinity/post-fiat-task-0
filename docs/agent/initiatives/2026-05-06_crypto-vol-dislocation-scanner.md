# Crypto Volatility Dislocation Scanner

Status: done
Last updated: 2026-05-06 15:03 UTC
Owner: agent
Scope: `AGENTS.md`, `crypto_vol_dislocation_scanner.py`, `.autopilot/manifest.json`, `docs/agent/*`

## Objective
Create one runnable code artifact that pulls live public Deribit BTC and ETH options data, approximates ATM IV across at least three expiries per asset, calculates term-structure slopes and matched ETH-minus-BTC vol spreads, ranks the top three dislocations, and exposes both timestamped CLI tables and browser visualizations.

## Current context
- Mode: `live`
- Operator supplied complete project-init context after saying `fresh context`; no intake questions are needed because project name, description, goal, validation expectations, and agent-file update intent are inferable from the request.
- Existing repo is an Agent Conductor/autopilot framework with persistent initiative logging and policy-gated commands.
- Worktree had pre-existing uncommitted changes before this initiative, including `.gitignore`, `.mcp.json`, `README.md`, Playwright MCP docs/artifacts, and conductor package files.

## Constraints
- Keep the scanner self-contained and runnable from a single command.
- Use unauthenticated public market data only; do not read or log secrets.
- Do not weaken security policy, agent profiles, model-provider config, MCP auth/config, or CI secrets.
- Preserve existing user changes and avoid unrelated refactors.
- Verify in the way an end user would verify: run the CLI scanner and load the browser dashboard through Playwright.

## Decisions
- 2026-05-06 07:36 UTC - Treat the complete user request as sufficient `fresh context` intake and proceed without questions. Rationale: the operator explicitly supplied the application objective, verification criteria, and permission to make remaining decisions.
- 2026-05-06 07:36 UTC - Implement as a Python standard-library script with optional local dashboard mode. Rationale: this satisfies the single-artifact and single-command requirements without adding package dependencies.
- 2026-05-06 07:36 UTC - Use Deribit public market-data endpoints for live instruments and option book summaries. Rationale: Deribit exposes active option instruments plus `mark_iv` through unauthenticated public API methods documented by Deribit.
- 2026-05-06 07:43 UTC - Wire the scanner through `.autopilot/manifest.json` for `scripts/ci` and `scripts/dev`. Rationale: the repo already defines these as the end-user validation and visual try paths.

## Validation
- Command: `python3 -m py_compile crypto_vol_dislocation_scanner.py`
- Expected: scanner artifact compiles
- Outcome: success
- Command: `python3 crypto_vol_dislocation_scanner.py --max-expiries 6 --top 3`
- Expected: timestamped BTC/ETH ATM IV, term-slope, ETH/BTC spread, and top-three flag tables from live public Deribit data
- Outcome: success; printed 6 BTC expiries, 6 ETH expiries, 12 term-slope rows, 6 matched ETH/BTC spread rows, and 3 top dislocation flags
- Command: `bash scripts/ci`
- Expected: manifest-driven live scanner validation plus compile check and security gates pass
- Outcome: success; command policy, secret scan, dependency audit, and scanner commands completed with `security: gates ok` and `ci: ok`
- Command: `bash scripts/dev`
- Expected: policy-aware start command serves the dashboard at `http://127.0.0.1:8765/`
- Outcome: success; dashboard served and was stopped after Playwright validation
- Command: Playwright desktop validation at `1440x1000`
- Expected: dashboard loads with non-empty charts/tables and no console errors or warnings
- Outcome: success; title correct, 3 SVG charts, 3 tables, 12 ATM rows, 6 spread rows, 3 top flags, no console warnings/errors, no horizontal page overflow; transient screenshot captured during validation and removed from repo root after cleanup
- Command: Playwright mobile validation at `390x844`
- Expected: dashboard loads on mobile without page overflow and with the same data sections available
- Outcome: success; no console warnings/errors, no page overflow, dashboard sections and tables present; transient screenshot captured during validation and removed from repo root after cleanup
- Command: Playwright `/api/snapshot` fetch
- Expected: JSON API returns BTC/ETH rows, slopes, spreads, flags, and timestamp
- Outcome: success; BTC rows 6, ETH rows 6, slope rows 12, spread rows 6, flag rows 3
- Command: `python3 crypto_vol_dislocation_scanner.py --json --max-expiries 3 --top 3 | python3 -m json.tool >/dev/null`
- Expected: JSON mode emits valid parseable snapshot data
- Outcome: success

## Risks and blockers
- Deribit public API availability and market hours/liquidity can change live output; script must show degraded/missing rows explicitly rather than fabricating values.
- Repo security policy network allowlist does not enumerate Deribit, but the operator explicitly requested live Deribit-style public market data. Keep the access read-only and unauthenticated, and do not modify the policy without explicit intent.

## Next actions
- [x] Update `AGENTS.md` to remove placeholders and reflect this Agent Conductor persistence framework.
- [x] Implement the scanner artifact with CLI tables and browser dashboard.
- [x] Add/update the manifest or scripts so end-user commands are reproducible under the repo workflow.
- [x] Run CLI validation against live data.
- [x] Run Playwright browser validation against the dashboard.
- [x] Update initiative log and `ACTIVE.md` with final evidence.

## Related
- `docs/agent/ACTIVE.md`
- `AGENTS.md`
- `crypto_vol_dislocation_scanner.py`
- `.autopilot/manifest.json`

## Turn log
- 2026-05-06 07:36 UTC - Created initiative and made it primary after reading the previous active workboard and initiative log.
- 2026-05-06 07:43 UTC - Updated `AGENTS.md`, added `crypto_vol_dislocation_scanner.py`, and added `.autopilot/manifest.json` plus scanner-specific `.autopilot/spec.md` acceptance checks.
- 2026-05-06 15:02 UTC - Completed live CLI, manifest CI, desktop Playwright, mobile Playwright, and dashboard JSON API validation. Stopped the local dashboard server after verification and removed generated root screenshots plus `__pycache__`.
- 2026-05-06 15:03 UTC - Verified JSON output is parseable and confirmed AGENTS/spec/memory docs contain no remaining project placeholder tokens.
