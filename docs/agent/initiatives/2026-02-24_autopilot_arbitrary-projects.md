# Autopilot: Arbitrary New Projects (Zero-Intervention)

Status: in_progress
Last updated: 2026-04-18 07:08 UTC
Owner: agent
Scope: `README.md`, `.gitignore`, `.agents/*`, `docs/security/*`, `scripts/*`, `mcp/conductor/src/*`

## Objective
Enable a “no intervention whatsoever” flow to generate arbitrary new projects from a single spec file, producing a repo that builds/tests cleanly, with deterministic execution, explicit failure provenance, and durable run logs.

## Current context
- Mode: `live`
- Starting point: template repo with an MCP “conductor” for spawning Codex agents; no existing autopilot runner for end-to-end project generation.
- Constraint: runs must be auditable (inputs, prompts, outputs, commands, and validation results persisted).
- Current security state: generated commands and dispatched agents are now policy/profile gated; command environments are allowlisted; command logs and screen reads are redacted; intake model providers are profile/data-class gated; repo secret scan and dependency audit are part of validation.

## Constraints
- Prefer correctness and auditability over convenience.
- No silent fallbacks: if validation fails, log and iterate or fail clearly.
- Keep tooling self-contained and reproducible; avoid adding heavy dependencies unless necessary.

## Decisions
- 2026-02-24 09:52 UTC - Implement a spec-driven “autopilot runner” CLI with structured manifests and run logs. Rationale: enables unattended runs while preserving provenance and replayability.
- 2026-02-24 11:01 UTC - Default the first implementation to `codex exec` as the worker engine. Rationale: smallest working path to zero-intervention execution; local-LLM engines can be added behind the same manifest contract.
- 2026-02-24 11:25 UTC - Integrate LM Studio as an alternate local engine that returns a structured JSON changeset applied by the runner. Rationale: enables fully-local autopilot without depending on Codex CLI for generation/fixes.
- 2026-02-24 13:53 UTC - Make LM Studio the intake/orchestration front-end (questionnaire → spec), and require a one-command visual try path (`bash scripts/dev`) plus CI (`bash scripts/ci`). Rationale: produces “ready-to-try-by-eyes” outputs and a consistent CI contract.
- 2026-04-18 05:51 UTC - Add `.agents/security-policy.json` and `.agents/agent-profiles.json` as machine-readable authorities. Rationale: security and agent permissions must be enforceable, not advisory.
- 2026-04-18 05:51 UTC - Route autopilot, `scripts/ci`, and `scripts/dev` through policy validation and sanitized command environments. Rationale: generated manifests are untrusted until checked.
- 2026-04-18 05:51 UTC - Gate conductor dispatch through approved agent profiles and deny full-auto by default. Rationale: runtime operator choice should select a permission envelope, not arbitrary binaries or flags.
- 2026-04-18 06:27 UTC - Replace LM Studio-specific intake with provider-neutral intake profiles. Rationale: local/cloud model choice should be operator-selected through policy, with `omlx-local` as the local default and OpenAI/Claude cloud providers requiring explicit opt-in and data-class checks.

## Validation
- Command: `cd mcp/conductor && npm run build`
- Expected: TypeScript compiles without errors
- Outcome: success
- Command: `cd mcp/conductor && node dist/security-cli.js scan --repo-root ../..`
- Expected: no secret findings
- Outcome: success
- Command: `cd mcp/conductor && npm audit --audit-level=high`
- Expected: no high severity vulnerabilities
- Outcome: success after `npm audit fix` updated vulnerable transitive packages in `package-lock.json`
- Command: temp manifest with `node --version` via `node mcp/conductor/dist/security-cli.js run-manifest --phase test`
- Expected: allowed command runs and security gates pass
- Outcome: success
- Command: temp manifest with `curl https://example.com` via `node mcp/conductor/dist/security-cli.js check-manifest --phase test`
- Expected: denied command fails policy validation
- Outcome: success (failed as expected)
- Command: `bash -n scripts/ci`, `bash -n scripts/dev`, `bash -n scripts/autopilot`
- Expected: shell syntax valid
- Outcome: success
- Command: temp `.autopilot/sessions` tree via `node mcp/conductor/dist/security-cli.js prune-logs --repo-root <tmpdir>`
- Expected: old log directory pruned according to retention policy
- Outcome: success
- Command: `cd mcp/conductor && npm run build`
- Expected: TypeScript compiles after provider-neutral intake changes
- Outcome: success
- Command: `cd mcp/conductor && node dist/security-cli.js scan --repo-root ../..`
- Expected: no secret findings after adding provider profiles and redaction logic
- Outcome: success
- Command: `cd mcp/conductor && npm audit --audit-level=high`
- Expected: no high severity vulnerabilities
- Outcome: success
- Command: `bash -n scripts/autopilot`, `bash -n scripts/ci`, `bash -n scripts/dev`
- Expected: shell syntax valid
- Outcome: success
- Command: provider-resolution smoke test via `node --input-type=module`
- Expected: `omlx-local` resolves, OpenAI cloud is blocked without opt-in, restricted data is blocked, and OpenAI cloud resolves with explicit opt-in plus API-key env
- Outcome: success
- Command: repo review of proposed hardening directive against current sources (`mcp/conductor/src/state.ts`, `mcp/conductor/src/index.ts`, `mcp/conductor/src/security-cli.ts`, `mcp/conductor/src/modelProviders.ts`, `scripts/autopilot`, `scripts/ci`, `README.md`, `mcp/conductor/ORCHESTRATION_RUNBOOK.md`)
- Expected: confirm whether the directive and critique match the current implementation and identify missing constraints
- Outcome: found additional gaps: add/remove/update persistence contract is self-contradictory ("before returning" vs fire-and-forget), recovered sessions currently have no path from `unknown` back to live status, persisted session sidecar would include prompt/screen cache unless explicitly minimized or redacted, `.conductor-state.json` needs gitignore coverage, and README placeholder examples conflict with generic angle-bracket scanning

## Risks and blockers
- Codex CLI behaviors/flags may differ across versions; runner must log full command lines and fail with actionable errors.
- Some stacks require network/toolchain installs; runner must make these explicit rather than “fixing” silently.
- Policy enforcement blocks known unsafe commands/env/logging paths, but it is not an OS-level sandbox. Package manager scripts can still execute arbitrary project code once allowlisted tooling runs.

## Next actions
- [x] Add provider-neutral intake model profiles with `omlx-local`, `openai-cloud`, and `claude-cloud`.
- [ ] Generalize or retire the older LM Studio patch engine so scaffold/fix generation uses the same provider profile contract.
- [ ] Add an optional provider-backed planner/patch engine behind the same manifest contract.
- [ ] Add git integration (optional init, commit hashes in run logs, optional branch/worktree isolation).
- [x] Add a stricter “allowed commands” policy layer for high-safety runs.
- [x] Add a first-class security policy contract covering agent profiles, command allowlists, env/network permissions, log redaction, and completion-gate security validation.
- [ ] Add optional OS-level network/process sandboxing for high-safety runs.
- [ ] If this hardening work is implemented, require explicit acceptance criteria for persisted-state redaction/minimization, `.conductor-state.json` ignore rules, and a test harness (`node:test`) before landing.
- [x] Capture the directive amendments in a dispatch-ready artifact under `docs/agent/handoffs/`.

## Related
- `docs/agent/ACTIVE.md`
- `mcp/conductor/ORCHESTRATION_RUNBOOK.md`

## Turn log
- 2026-02-24 09:52 UTC - Created initiative log and set as primary in `docs/agent/ACTIVE.md`.
- 2026-02-24 11:01 UTC - Added `.autopilot` spec template + no-intervention runner (`mcp/conductor/src/autopilot/*`), root `scripts/autopilot`, and ignored `.autopilot/runs/`. Built successfully.
- 2026-02-24 11:02 UTC - Enforced `commands.test` presence in manifest validation and rebuilt successfully.
- 2026-02-24 11:25 UTC - Added LM Studio engine (`mcp/conductor/src/autopilot/lmstudio.ts`) + JSON changeset protocol; autopilot now supports `--engine=lmstudio` and applies changesets deterministically with full request/response logs.
- 2026-02-24 11:26 UTC - Redacted LM Studio API key in persisted request logs and rebuilt successfully.
- 2026-02-24 11:26 UTC - Redacted LM Studio API key in `inputs.json` run metadata and rebuilt successfully.
- 2026-02-24 11:28 UTC - Prevented LM Studio changesets from modifying autopilot infrastructure files and rebuilt successfully.
- 2026-02-24 11:29 UTC - Defaulted LM Studio temperature to 0 for more deterministic generation and rebuilt successfully.
- 2026-02-24 13:53 UTC - Added LM Studio-driven intake (`mcp/conductor/src/autopilot/intake.ts`, `mcp/conductor/src/autopilot-flow-cli.ts`), standardized CI/dev entrypoints (`scripts/ci`, `scripts/dev`, `.github/workflows/ci.yml`), and readiness checks so autopilot iterates until `bash scripts/dev` is usable.
- 2026-04-18 05:27 UTC - Reviewed template security/opsec posture. Main gaps: policy enforcement for commands/agents/env/network, log redaction and retention, model/provider trust boundaries, and security-specific completion gates.
- 2026-04-18 05:56 UTC - Implemented policy/profile contracts, command/env enforcement, redaction, security CLI gates, profile-gated conductor dispatch, protected generated writes, automatic npm high-severity audit where package-lock exists, log retention pruning, security docs, and dependency audit remediation. Build, secret scan, audit, shell syntax checks, log pruning, and policy allow/deny smoke tests passed.
- 2026-04-18 06:27 UTC - Added provider-neutral intake (`.agents/model-providers.json`, `mcp/conductor/src/modelProviders.ts`) with `omlx-local` default and OpenAI/Claude cloud profiles. Intake now redacts cloud prompts/responses, rejects deprecated LM Studio intake flags, enforces cloud opt-in and prompt data-class gates, and documents the provider workflow. Build, secret scan, audit, shell syntax checks, and provider policy smoke tests passed.
- 2026-04-18 07:00 UTC - Reviewed a proposed hardening directive and critique against the current template sources. Confirmed the main concerns and identified a few missing requirements: resolve the persistence contract contradiction, define how recovered sessions leave `unknown`, minimize/redact sidecar contents, add `.conductor-state.json` to ignore rules, and avoid generic angle-bracket scanning because current README examples intentionally contain `<local-model>` and `<model>`.
- 2026-04-18 07:08 UTC - Added `docs/agent/handoffs/2026-04-18_hardening-directive-amendments.md` with concrete amendments for the proposed hardening directive: async/serialized persistence, recovered-session reconciliation, sidecar minimization, provider-selection precedence, template-root skip semantics, and required tests/validation.
