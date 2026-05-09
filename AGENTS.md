When the operator states `fresh context`, initialize or refresh project context before substantive work:
- Infer project name, project description, primary goal, and workflow-specific AGENTS.md modifications from the operator's message and local repo state.
- Ask concise questions only when required information is missing or ambiguous enough to risk correctness, security, or fidelity.
- If the operator supplies enough context and asks not to stop, record the inferred context in `docs/agent/ACTIVE.md` plus a primary initiative log, then proceed.
- Preserve speed without trading off accuracy, fidelity, security, or durable memory.

# Agent Conductor Operator & Agent Fidelity Charter

Agent Conductor is an autonomous agent orchestration framework for launching approved CLI agents, enforcing policy-gated commands/providers/profiles, and preserving durable project memory through initiative logs so work can resume without context loss.  
Primary goal: preserve security, correctness, determinism, auditability, and persistent context across all agent work.

## 0) Security and Opsec Override (Non-Negotiable)

Security, confidentiality, least privilege, and non-exfiltration override all other goals.

Agents must not:
- Read, print, persist, or forward secrets unless the operator explicitly authorizes that access for the current task.
- Inherit broad environment variables into generated commands.
- Execute commands outside `.agents/security-policy.json`.
- Launch agent CLIs outside `.agents/agent-profiles.json`.
- Modify security policy, agent profiles, MCP config, auth config, or CI secrets without explicit operator intent.
- Persist full terminal scrollback, browser storage, cookies, auth headers, production payloads, or sensitive user data.

If correctness requires unsafe access, block by default and ask the operator for an explicit exception.

When multiple designs are possible, prefer the option that improves:
- Security, confidentiality, and least privilege
- Accuracy of persisted data and derived outputs
- Determinism and replay equivalence
- Operational correctness and explicit failure provenance
even if it costs convenience or performance.

## 1) Priority Order (Global)

If tradeoffs are required, priority is:
1. Security, confidentiality, and non-exfiltration
2. Correctness and fidelity
3. Determinism and reproducibility
4. Explicit observability and provenance
5. Reliability and safety
6. Performance
7. Convenience

## 2) Canonical Sources and Precedence

Define authoritative sources by time range and mode. Agents must not invent precedence; if a task needs a new data source, record the decision and rationale in the active initiative.

Cold window (completed turns, committed decisions, or durable artifacts):
- Authoritative: git-tracked project files, `docs/agent/initiatives/*.md`, `docs/agent/handoffs/*.md`, `docs/security/*.md`, `.agents/*.json`, and `.autopilot/spec.md` or manifests when present.

Hot window (current turn or active run):
- Authoritative: `docs/agent/ACTIVE.md`, the primary initiative log, current local files, and live command outputs generated in this turn.
- Gap fill source: summarized terminal/tool evidence recorded in the initiative `Validation` and `Turn log` sections.
- Non-authoritative unless proven flushed: browser screenshots, Playwright traces/artifacts, transient MCP state, `.autopilot/sessions/*`, and local development server output.

Rules:
- Derived views/caches are not authoritative history.
- Authoritative claims must match the project’s support matrix.
- Every result should include `source` and `degraded` metadata where feasible.

## 3) Required Checks (Non-Negotiable)

Coverage:
- For any windowed metric/output, prove full window coverage.
- If missing data exists and a higher-fidelity source exists, treat as error and repair first.

Freshness:
- Heartbeat timestamps and key pipelines must advance.
- Staleness means downstream outputs are invalid.

No future leakage:
- Computation at time `t` must not read from `t+1` or later.

Schema immutability:
- Persisted schemas are append-only.
- Breaking changes require explicit versioning and migration.

Atomic writes:
- Use temp-file-then-rename for durable artifacts.
- Never mutate persisted files in place unless explicitly designed for it.

## 4) Mode Contracts (Determinism)

Live mode:
- Define producers, consumers, and persistence boundaries explicitly.
- Ephemeral streams are not durable history unless documented otherwise.

Replay/backtest mode:
- Must be isolated from live state.
- Clear/namespace hot keys and caches before replay.
- Use event-time semantics where supported.
- Replay inputs and config must be captured for reproducibility.

## 5) Fallback and Degraded Mode

Any fallback to lower-fidelity sources must be explicit:
- Log the fallback with reason.
- Annotate output with `degraded=true` and `degraded_reason`.
- Never silently persist degraded outputs as authoritative history.

Missing values are acceptable only when:
- Support matrix says full fidelity is impossible for that case.
- Output is marked degraded and non-authoritative.

## 6) Support Matrix Is Canonical

`docs/security/VALIDATION_GATES.md`, `.agents/security-policy.json`, `.agents/agent-profiles.json`, and `.agents/model-providers.json` define which commands, agent profiles, model providers, data classes, and validation gates are eligible for:
- Persistence
- Backfill
- Authoritative history claims

Agents must not bypass this with convenience writes.

## 7) Persistent Memory Contract (Mandatory)

Purpose: ensure every agent knows exact current context and how the project got there.

Complexity triggers (any one means memory logging is required):
- Work expected to exceed 30 minutes
- Work spans more than 2 assistant turns
- More than 3 steps or more than 3 files/modules touched
- Cross-subsystem diagnosis or unknown prerequisites
- Changes affecting correctness/determinism/routing/persistence

When triggered, agent MUST:
- Create `docs/agent/initiatives/YYYY-MM-DD_<slug>.md` from template
- Update `docs/agent/ACTIVE.md` with active initiative(s) and one primary
- Start each turn by reading `docs/agent/ACTIVE.md` and primary initiative
- Resume from `Next actions` unless user redirects
- Update initiative log at end of every turn and after key events

Key events requiring immediate log update:
- Decision made (with rationale)
- Constraint discovered
- Patch applied
- Validation run
- Blocked/unblocked status changes

## 8) Memory File Layout

Required files:
- `docs/agent/ACTIVE.md`
- `docs/agent/initiatives/YYYY-MM-DD_<slug>.md`
- `docs/agent/initiatives/TEMPLATE.md`

Recommended files:
- `docs/agent/decisions/YYYY-MM-DD_<slug>.md` (ADR-style)
- `docs/agent/handoffs/YYYY-MM-DD_<slug>.md`

## 9) Required Log Content

`docs/agent/ACTIVE.md` must include:
- Primary initiative
- Other active initiatives
- Current objective
- Current constraints
- Current mode (`live`, `replay`, `migration`, etc.)
- Next operator decision needed (if any)

Each initiative log must keep these near top:
- `Status`
- `Last updated`
- `Owner`
- `Scope`
- `Current context`
- `Decisions` (chronological, with rationale)
- `Validation` (commands + outcomes)
- `Next actions`
- `Related` links

## 10) Turn Lifecycle (Exact)

For every turn:
1. Read `docs/agent/ACTIVE.md`
2. Read primary initiative log
3. Confirm constraints and mode
4. Execute next action
5. Record decisions and evidence
6. Update `Next actions`
7. Update `Last updated`

If memory files are missing and trigger criteria apply:
- Create them before making significant code changes.

## 11) Hygiene and Security

Authoritative security files:
- `.agents/security-policy.json` defines command, environment, network, filesystem, logging, and completion-gate policy.
- `.agents/agent-profiles.json` defines which agent CLIs may be launched and with which permissions.
- `.agents/model-providers.json` defines which intake model providers may be used and how prompts/responses are logged.
- `docs/security/SECURITY.md` defines the human-readable security charter.
- `docs/security/DATA_CLASSIFICATION.md` defines what can be logged, committed, or sent to model providers.
- `docs/security/VALIDATION_GATES.md` defines security checks required before completion.

Never log:
- Secrets, tokens, passwords, private keys
- PII or sensitive production payloads
- Browser cookies, local storage, session storage, auth headers, or full request/response payloads
- Raw terminal scrollback unless a security policy exception requires it

Always prefer:
- Summaries over raw dumps
- File paths and exact commands when reproducibility matters
- Policy-enforced command execution over ad hoc shell commands
- Local model providers for sensitive intake; cloud providers only with explicit opt-in and compatible data classification

Stop immediately and escalate if a task requires:
- Secrets, tokens, private keys, cookies, password material, or OS keychain access
- Production data, customer data, PII, or private payloads
- Disabling auth, authorization, validation, encryption, audit trails, provenance, or security checks
- Network access outside policy
- Destructive commands or writes outside the repository
- Permission expansion, sandbox bypass, or profile/policy weakening

## 12) Completion Rules

When initiative is complete:
- Set `Status: done`
- Clear `Next actions` or link follow-up initiative
- Ensure `ACTIVE.md` reflects closure
- Keep initiative file for historical continuity
- Confirm required security gates passed or record an explicit operator-approved exception

## 13) Ownership and Escalation

If a change could reduce correctness, fidelity, or determinism:
- Block by default or mark clearly as risk
- Require explicit review and acceptance before shipping
