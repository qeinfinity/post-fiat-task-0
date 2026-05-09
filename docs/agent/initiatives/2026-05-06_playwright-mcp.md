# Playwright MCP Browser Control

Status: done
Last updated: 2026-05-06 07:29 UTC
Owner: agent
Scope: `.mcp.json`, `mcp/conductor/package.json`, `mcp/conductor/package-lock.json`, `.gitignore`, `README.md`

## Objective
Configure a Playwright MCP server so agents can use browser automation functionality through the project's MCP configuration.

## Current context
- Mode: `live`
- The project already had a local `conductor` MCP server in `.mcp.json`.
- Playwright validation is enabled in `.agents/security-policy.json`.
- Security policy files and MCP config are protected, but the operator explicitly requested MCP browser configuration for this task.

## Constraints
- Prefer pinned local tooling over runtime network fetches.
- Do not persist browser storage, cookies, auth headers, or session data.
- Keep generated Playwright artifacts out of git.

## Decisions
- 2026-05-06 02:40 UTC - Install `@playwright/mcp@0.0.73` as a dev dependency under `mcp/conductor`. Rationale: the repo has no root package, and a local pinned dependency avoids `npx` downloading code when the MCP client starts.
- 2026-05-06 02:40 UTC - Configure the MCP server to use `--browser chrome`, `--isolated`, `--block-service-workers`, and `.playwright-mcp` output. Rationale: this enables browser control while avoiding persistent browser profile state and keeping artifacts contained.

## Validation
- Command: `node -e "JSON.parse(require('fs').readFileSync('.mcp.json','utf8')); JSON.parse(require('fs').readFileSync('.agents/security-policy.json','utf8')); console.log('json ok')"`
- Expected: JSON parses successfully.
- Outcome: success.
- Command: `npm run build --prefix mcp/conductor`
- Expected: TypeScript compiles without errors.
- Outcome: success.
- Command: `npm audit --prefix mcp/conductor --audit-level=high`
- Expected: no high severity vulnerabilities.
- Outcome: success; npm reports existing moderate transitive advisory through `@modelcontextprotocol/sdk`.
- Command: `node -e "<load .mcp.json playwright entry, spawn server, keep stdio open briefly, terminate>"`
- Expected: server process remains alive while stdio is open.
- Outcome: success.
- Command: `codex mcp list`
- Expected: `playwright` appears as an enabled global MCP server.
- Outcome: success.

## Risks and blockers
- The config uses the system Chrome channel. If Chrome is not installed on a machine, switch to an installed browser channel or install Playwright-managed browsers explicitly.
- `npm audit` reports moderate transitive advisories in the MCP SDK dependency chain; no high severity advisory blocks this change.

## Next actions
- [x] Configure Playwright MCP in `.mcp.json`.
- [x] Pin local Playwright MCP dependency.
- [x] Validate config and startup.
- [x] Register Playwright in Codex global MCP config.

## Related
- `docs/agent/ACTIVE.md`
- `.mcp.json`
- `README.md`

## Turn log
- 2026-05-06 02:40 UTC - Added local Playwright MCP dependency, wired `.mcp.json`, ignored `.playwright-mcp/`, documented the server, and validated JSON, build, audit level, Chrome availability, and configured process startup.
- 2026-05-06 07:29 UTC - Confirmed Codex had no global MCP servers configured, then registered `playwright` with `codex mcp add` using the pinned local CLI path. `codex mcp list` now shows `playwright` enabled.
