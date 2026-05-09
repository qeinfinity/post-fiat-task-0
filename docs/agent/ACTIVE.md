# Agent Active Workboard

Last updated: 2026-05-06 15:03 UTC

## Primary initiative
- `docs/agent/initiatives/2026-02-24_autopilot_arbitrary-projects.md`

## Other active initiatives
- `<none>`

## Current objective
- Add a spec-driven, no-intervention “autopilot” flow to generate arbitrary new projects (scaffold → build → test → iterate) with reproducible logs.

## Current mode
- `live`

## Current constraints
- Prefer determinism/auditability over convenience.
- No interactive approvals during the autopilot run; failures must be explicit and logged.
- Security policy is now a first-class run contract for generated commands, agent profiles, env filtering, log redaction, and completion gates.
- Intake model providers are now profile/policy gated; default local intake is `omlx-local`, while OpenAI and Claude require explicit cloud opt-in.
- The policy layer is not an OS-level network sandbox; future hardening should add platform-enforced network/process isolation where feasible.
- Do not modify `.agents/security-policy.json`, `.agents/agent-profiles.json`, `.agents/model-providers.json`, `.mcp.json`, or unrelated existing changes without explicit operator intent.

## Next operator decision needed
- `<none>`

## Recent review note
- Reviewed a proposed hardening directive for state persistence, intake-provider detection, and template placeholder validation. Main gaps: persist semantics are internally inconsistent, recovered sessions need a way back out of `unknown`, sidecar persistence should minimize/redact sensitive fields and be gitignored, and template placeholder scanning needs a marker convention that does not trip over README usage placeholders.
- Added dispatch-ready amendments document at `docs/agent/handoffs/2026-04-18_hardening-directive-amendments.md` so the directive can be patched without reconstructing truncated prompt text.

## Recent completed work
- 2026-05-06 02:40 UTC - Configured Playwright MCP browser control using a pinned local `@playwright/mcp@0.0.73` dependency, isolated browser profile mode, and `.playwright-mcp/` artifact output. See `docs/agent/initiatives/2026-05-06_playwright-mcp.md`.
- 2026-05-06 07:29 UTC - Registered the same Playwright server in Codex global MCP config via `codex mcp add`; `codex mcp list` now shows it enabled.
- 2026-05-06 15:02 UTC - Added a single-artifact live Deribit BTC/ETH options volatility-dislocation scanner with CLI tables, dashboard visualizations, manifest wiring, and Playwright desktop/mobile validation. See `docs/agent/initiatives/2026-05-06_crypto-vol-dislocation-scanner.md`.

## Resume protocol (mandatory)
1. Open this file first.
2. Open the primary initiative log.
3. Resume from that log's `Next actions` section.
4. Update both files before ending the turn.

## Initiative index
- `<YYYY-MM-DD_slug>.md` - `<status>` - `<one-line summary>`
- `2026-02-24_autopilot_arbitrary-projects.md` - `in_progress` - Spec-driven autopilot runner for zero-intervention project generation.
- `2026-05-06_playwright-mcp.md` - `done` - Local Playwright MCP browser-control server configuration.
- `2026-05-06_crypto-vol-dislocation-scanner.md` - `done` - Live Deribit BTC/ETH options IV dislocation scanner with CLI output and dashboard verification.
