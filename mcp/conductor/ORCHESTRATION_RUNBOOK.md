# Agent Orchestration Runbook

Canonical reference for any operator/orchestrator using the conductor MCP to dispatch, monitor, and
coordinate approved CLI agent profiles. Read this before dispatching your first agent.

---

## 1. What This System Does

You (the orchestrator, via the conductor MCP) open iTerm2 tabs, each running an approved CLI agent
instance on its own git worktree and branch. The user can watch every agent's work in real time.
You act as the orchestrator: deciding what to dispatch, choosing models, writing prompts,
monitoring progress, handling failures, reviewing output, and creating PRs.

```
Operator/orchestrator
    |  MCP stdio
    v
conductor (Node.js)
    |-- iTerm2 bridge (AppleScript via osascript)
    |-- Git bridge (worktrees + branches)
    |-- In-memory state (Map<id, AgentSession>)
    |
    v
iTerm2 Window
  Tab "[Codex] task-a"  -->  conductor/task-a branch
  Tab "[Codex] task-b"  -->  conductor/task-b branch
```

Each agent gets:
- A git worktree at `.worktrees/{name}/` (isolated filesystem)
- A branch named `conductor/{name}` (forked from a base, usually `main`)
- A visible iTerm2 tab named `[Codex] {name}`
- A permission envelope selected from `.agents/agent-profiles.json`

---

## 2. Available Tools (Quick Reference)

| Tool | Purpose |
|------|---------|
| `dispatch_agent` | Create worktree + tab + launch Codex with a task |
| `batch_dispatch` | Dispatch multiple agents sequentially (1s delay between) |
| `list_agents` | Compact summary of all tracked agents (shows idle status) |
| `agent_status` | Detailed status with optional git/screen refresh |
| `read_screen` | Read visible screen or full scrollback from a tab |
| `signal_agent` | Send text or control keys (ctrl-c, enter, etc.) to a tab |
| `terminate_agent` | Stop agent, optionally clean up worktree/branch/tab |
| `reconnect_agent` | Re-attach to orphaned tabs after MCP restart |
| `branch_diff` | Show diff between agent branch and base |
| `create_pr` | Push branch + create GitHub PR via `gh` CLI |
| `finalize_agent` | Review diff + push + create PR + terminate in one step |
| `merge_to_base` | Merge agent's branch into its base (for wave transitions) |

### Automatic Idle Detection

The conductor polls running agents every 30 seconds and checks if Codex has returned to its
input prompt. When detected, the agent's status changes from `"running"` to `"idle"`. This
means `list_agents` will immediately show which agents have finished their task. Idle agents
include an `idleSince` timestamp.

---

## 3. Model Selection

Choose the model based on perceived task difficulty.

| Model | Strengths | When to Use |
|-------|-----------|-------------|
| **gpt-5.2 xhigh** | Strongest reasoning, highest coherence over long tasks | Complex architectural work, multi-file refactors, tasks where a wrong first attempt wastes significant time |
| **gpt-5.3-codex xhigh** | Latest coding model, very fast, excellent code gen | Standard feature implementation, straightforward multi-file tasks |
| **gpt-5.3-codex-spark** | Smaller/faster variant | Quick fixes, single-file changes, clear specifications |

### Selection Heuristic

```
Is the task ambiguous, architectural, or touches invariants?
  YES --> gpt-5.2 xhigh
  NO  --> Is it a substantial coding task (>3 files, non-trivial logic)?
            YES --> gpt-5.3-codex xhigh
            NO  --> gpt-5.3-codex-spark
```

When in doubt, use the stronger model. A slower correct result beats a fast wrong one.

### Intake Provider Selection

Autopilot intake uses `.agents/model-providers.json`, with `.agents/security-policy.json`
as the allowlist and data-class gate.

Initial approved profiles:

| Profile | Locality | Notes |
|---------|----------|-------|
| `omlx-local` | Local | Default intake provider; OpenAI-compatible endpoint at `http://127.0.0.1:8080/v1` unless overridden |
| `openai-cloud` | Cloud | Requires `OPENAI_API_KEY`, `--allow-cloud-intake=true`, and an allowed `--intake-data-class` |
| `claude-cloud` | Cloud | Requires `ANTHROPIC_API_KEY`, `--allow-cloud-intake=true`, and an allowed `--intake-data-class` |

Do not put API keys in command-line flags. Set them in the shell environment and let the selected
profile read the named environment variable.

---

## 4. Security Policy and Agent Profiles

Security is a dispatch-time and completion-time gate, not a cleanup task.

Authoritative files:
- `.agents/security-policy.json` controls generated command execution, allowed environment variables, denied tools, log redaction, and completion gates.
- `.agents/agent-profiles.json` controls which agent CLIs may be launched and whether full-auto is allowed.
- `.agents/model-providers.json` controls which intake model providers exist and whether raw prompt/response logging is allowed.
- `docs/security/*` explains the human-readable rules, data classification, validation gates, and incident response.

### Profile Selection

Dispatch with the least-privileged profile that can finish the work:

- `codex-standard`: default; workspace sandbox; no full-auto escalation.
- `codex-full-auto`: only for explicitly authorized autonomous runs.
- `generic-local`: template profile for another local CLI; customize before use.

Runtime operator choice selects a profile. It does not permit arbitrary binaries, flags, or sandbox escalation.

### Security Stop Conditions

Stop and ask the operator before proceeding if an agent needs:

- Secrets, tokens, cookies, private keys, SSH agent, OS keychain, or production credentials
- Production data, customer data, PII, or sensitive payloads
- External network access outside policy
- Destructive commands or writes outside the repository
- Edits to `.agents/security-policy.json`, `.agents/agent-profiles.json`, `.mcp.json`, auth config, or CI secrets
- Weakened auth, encryption, validation, provenance, or audit behavior

---

## 5. Dispatching Agents

### Prompt Engineering

The prompt determines everything. Dispatched agents have no memory of prior sessions unless the selected CLI provides it.
Every prompt must be **self-contained and specific**.

A good dispatch prompt includes:
1. **Objective** -- one sentence, unambiguous
2. **Context** -- what files to read first, what patterns to follow
3. **Constraints** -- what NOT to do, invariants to respect
4. **Verification** -- how the agent should validate its own work
5. **Commit convention** -- message format

Template:
```
You are working in the <PROJECT> codebase. Read AGENTS.md first for project conventions.

TASK: <clear objective>

CONTEXT:
- Read <file1> for the existing pattern to follow
- Read <file2> for the schema/types you'll need

CONSTRAINTS:
- Do NOT modify <hot files that other agents touch>
- Follow existing naming conventions in <reference file>
- Follow `.agents/security-policy.json` and `.agents/agent-profiles.json`
- Do not read, log, or request secrets

VERIFICATION:
- Run `<validation command>` to verify no errors
- Run applicable security checks or explain why a gate is not applicable

COMMIT:
- Use conventional commit format: <type>(<scope>): <description>
- Commit when done, then stop
```

### Hot File Awareness

Some files are edited by many tasks. **Never dispatch two agents that will modify the same
file concurrently.** Serialize changes to hot files, then parallelize independent work.

### Wave Pattern

For large initiatives, structure work in waves:

1. **Wave 1 (serial)**: Foundation changes to hot files (schemas, shared types)
2. **Wave 2 (parallel 2-3)**: Independent implementations that build on Wave 1
3. **Wave 3 (parallel 2-3)**: Derived work that depends on Wave 2 outputs

Merge each wave to `main` before dispatching the next.

### Concurrency Limits

Practical limit: **3-4 simultaneous agents**. Beyond that:
- AppleScript tab operations get sluggish
- Review burden exceeds orchestrator capacity
- Git worktree disk usage grows (each is a full checkout)

---

## 6. Monitoring Agents

### Routine Check Pattern

```
1. agent_status (refresh_git=true, refresh_screen=true)
   - commits > 0? Progress is being made
   - uncommitted changes? Work in progress
   - screenTail shows Codex prompt? Agent is idle/done/waiting
   - processAlive=false? Agent crashed or exited

2. If agent appears stuck (no progress for >5 minutes):
   - read_screen (full_scrollback=true, tail_lines=100, reason="<why raw scrollback is needed>")
   - Look for error messages, approval prompts, or loops

3. If agent is waiting for approval:
   - signal_agent (key="enter") to approve
   - Or signal_agent (text="y") depending on the prompt
```

### Recognizing Completion

The idle detection loop catches when Codex returns to its input prompt. Once idle:
1. Review the diff with `branch_diff`
2. If satisfied, use `finalize_agent` to push, create PR, and terminate
3. When ready for the next wave, use `merge_to_base`

### Recognizing Failure

Signs an agent is failing:
- Repeated identical errors in screen output
- Agent modifying files outside its scope
- Context usage dropping rapidly (>50% consumed = compaction imminent)
- Reasoning trace shows confusion, circular logic, or contradictions

---

## 7. Handling Disagreements with Agent Output

1. **Discuss first.** Use `signal_agent` to send a message explaining your concern.
2. **Argue until the better point surfaces.** Don't pull rank.
3. **Log the interaction** in `mcp/conductor/docs/observation-log.md` if notable.
4. **Know when to cut losses.** Sometimes an instance is "fried" -- circular logic,
   contradicting itself, repeating failed approaches. Terminate, log, dispatch fresh.

---

## 8. Persistence Protocol

### For Dispatched Agents

Include in dispatch prompts for complex tasks (>30 min expected):

```
PERSISTENCE PROTOCOL:
This task may exceed your context window. To ensure continuity after compaction:
- Create docs/agent/initiatives/YYYY-MM-DD_<slug>.md
- Update your initiative log after each major step
- If you lose context, read docs/agent/ACTIVE.md first to resume
```

---

## 9. Observation Log Protocol

Every orchestration session SHOULD update `mcp/conductor/docs/observation-log.md`.

### What to Log

- Model performance differences
- Prompting lessons
- Failure patterns
- Orchestration insights (optimal concurrency, wave sizing, timing)

### Format

```
### YYYY-MM-DD HH:MM -- <brief title>

**Model**: <which model>
**Task**: <what was dispatched>
**Outcome**: success | partial | failure
**Observation**: <what happened, what was learned>
**Action**: <what to do differently next time>
```

---

## 10. Operational Checklist

### Before Dispatching

- [ ] Base branch is up to date with origin
- [ ] No uncommitted changes on main
- [ ] Hot files identified -- no two agents will edit the same file
- [ ] Security policy and agent profile files reviewed
- [ ] Least-privileged `agent_profile` selected
- [ ] Full-auto is disabled unless explicitly authorized by profile and operator intent
- [ ] Model selected based on task difficulty
- [ ] Prompt is self-contained
- [ ] Prompt includes security constraints and validation gates

### During Execution

- [ ] Check agent status every 3-5 minutes (short tasks) or 10-15 (long tasks)
- [ ] Watch for approval prompts
- [ ] Monitor context consumption
- [ ] Watch for permission escalation, unexpected network use, and out-of-scope files
- [ ] Do not use full scrollback without a reason when policy requires one

### After Completion

- [ ] `list_agents` shows agent as `idle`
- [ ] Review diff with `branch_diff`
- [ ] Verify agent stayed in scope
- [ ] Verify no secrets, logs, screenshots, traces, browser state, or local-only artifacts are in the diff
- [ ] Run command policy and secret-scan gates
- [ ] `finalize_agent` to push + create PR + terminate
- [ ] Update observation log if notable
- [ ] If last agent in a wave, `merge_to_base` then dispatch next wave

---

## 11. Known Issues and Workarounds

### `gh` CLI PATH resolution
The conductor resolves `gh` at startup by checking common locations. If `gh` is installed
in a non-standard location, set `GH_PATH` in the `.mcp.json` env block.

### Tab name lookup is unreliable
iTerm2 AppleScript name assignment has timing issues. All operations use tty-based lookup.
Never rewrite to use tab names.

### Full-auto is denied
The default `codex-standard` profile denies `full_auto`. Use `agent_profile="codex-full-auto"` only when the operator explicitly wants an autonomous run with that permission envelope.

### Sandbox conflicts with full-auto
Some CLI versions treat sandbox flags and full-auto flags differently. Keep the behavior in `.agents/agent-profiles.json` instead of adding ad hoc flags at dispatch time.

### Agent stuck at approval prompt
Send `signal_agent` with `key="enter"` or `text="y"` depending on the prompt format.

### Worktree cleanup after crashes
```bash
cd /path/to/repo
git worktree prune
git worktree remove .worktrees/{name} --force
git branch -D conductor/{name}
```
