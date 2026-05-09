# Agent Conductor Template

## Autopilot (no-intervention project generation)

This template includes an unattended “autopilot” flow:
1) a policy-approved intake model asks a comprehensive set of questions
2) Codex iterates until the project is ready to try by eyes with one command: `bash scripts/dev`

Run:
- Start a local oMLX OpenAI-compatible server (default: `http://127.0.0.1:8080/v1`)
- `bash scripts/autopilot --intake-provider=omlx-local --intake-model=<local-model>`

Cloud intake is opt-in and redacts prompt/response logs by default:
- OpenAI: set `OPENAI_API_KEY`, then run `bash scripts/autopilot --intake-provider=openai-cloud --intake-model=<model> --allow-cloud-intake=true`
- Claude: set `ANTHROPIC_API_KEY`, then run `bash scripts/autopilot --intake-provider=claude-cloud --intake-model=<model> --allow-cloud-intake=true`

Useful flags:
- `--intake-provider=omlx-local|openai-cloud|claude-cloud`
- `--intake-model=<model>`
- `--intake-base-url=<url>`
- `--intake-data-class=public|internal|sensitive|restricted`
- `--scaffold=auto|always|never`
- `--max-fix-attempts=3`
- `--model=<codex-model>`
- `--sandbox=workspace-write`

Outputs:
- Try it: `bash scripts/dev`
- CI: `bash scripts/ci` (GitHub Actions runs this via `.github/workflows/ci.yml`)
- Logs: `.autopilot/sessions/<sessionId>/`

Security defaults:
- Command execution is checked against `.agents/security-policy.json`
- Agent dispatch is checked against `.agents/agent-profiles.json`
- Intake providers are checked against `.agents/model-providers.json` and `.agents/security-policy.json`
- `scripts/ci` and `scripts/dev` run through the policy-aware security CLI
- Autopilot treats security-gate failures as validation failures and iterates instead of declaring success

---

An MCP server that lets an operator/orchestrator launch approved CLI agent profiles in parallel via iTerm2 tabs,
each working in isolated git worktrees on dedicated branches.

## Architecture

```
Operator/orchestrator (Claude, Codex, or another MCP-capable agent)
    |  MCP stdio
    v
agent-conductor (Node.js, this server)
    |-- iTerm2 bridge (AppleScript via osascript)
    |-- Git bridge (worktrees + branches)
    |-- In-memory agent state
    |
    v
iTerm2 Window
  Tab "[Codex] task-a"  -->  .worktrees/task-a/ (branch: conductor/task-a)
  Tab "[Agent] task-b"  -->  .worktrees/task-b/ (branch: conductor/task-b)
  Tab "[Codex] task-c"  -->  .worktrees/task-c/ (branch: conductor/task-c)
```

## Requirements

- macOS (iTerm2 + AppleScript)
- Node.js >= 18
- Git
- [Codex CLI](https://github.com/openai/codex) installed globally, or another local CLI declared in `.agents/agent-profiles.json`
- [gh CLI](https://cli.github.com/) (for PR creation)
- iTerm2

## Setup

### 1. Install dependencies

```bash
cd mcp/conductor
npm install
```

### 2. Build

```bash
npm run build
```

### 3. Configure `.mcp.json`

Copy `.mcp.json` to the root of your target project and update:

```json
{
  "mcpServers": {
    "conductor": {
      "command": "node",
      "args": ["/absolute/path/to/agent-template/mcp/conductor/dist/index.js"],
      "env": {
        "CONDUCTOR_REPO_ROOT": "/absolute/path/to/your/project"
      }
    },
    "playwright": {
      "command": "node",
      "args": [
        "/absolute/path/to/agent-template/mcp/conductor/node_modules/@playwright/mcp/cli.js",
        "--browser",
        "chrome",
        "--isolated",
        "--block-service-workers",
        "--output-dir",
        ".playwright-mcp"
      ]
    }
  }
}
```

- `CONDUCTOR_REPO_ROOT`: The git repository the conductor will create worktrees in
- `CONDUCTOR_SERVER_NAME`: (optional) Custom MCP server name, defaults to "conductor"
- `playwright`: Browser automation MCP. It uses a pinned local `@playwright/mcp` package, an isolated browser profile, and `.playwright-mcp/` for generated artifacts.

### 4. Add `.worktrees/` to your `.gitignore`

```
.worktrees/
```

## Tools (12 total)

| Tool | Purpose |
|------|---------|
| `dispatch_agent` | Create worktree + iTerm2 tab + launch Codex with a task |
| `batch_dispatch` | Dispatch multiple agents sequentially (1s delay between) |
| `list_agents` | Compact summary of all tracked agents |
| `agent_status` | Detailed status with optional git/screen refresh |
| `read_screen` | Read visible screen or scrollback from an agent's tab |
| `signal_agent` | Send text or control keys to an agent's tab |
| `terminate_agent` | Stop agent, optionally clean up worktree/branch/tab |
| `reconnect_agent` | Re-attach to orphaned tabs after MCP restart |
| `branch_diff` | Show git diff between agent's branch and base |
| `create_pr` | Push branch + create GitHub PR |
| `finalize_agent` | All-in-one: review, push, PR, terminate |
| `merge_to_base` | Merge agent's branch into base branch |

## How It Works

1. **Dispatch**: Creates a git worktree (`.worktrees/{name}/`) with a branch (`conductor/{name}`),
   opens a new iTerm2 tab, selects an approved profile from `.agents/agent-profiles.json`, and runs the agent with your prompt.

2. **Monitor**: Automatic idle detection polls every 30s. When Codex returns to its input prompt,
   the agent status changes to `"idle"`. You can also manually read screens and check git status.

3. **Finalize**: Review the diff, push the branch, create a PR, and close the tab in one call.

4. **Merge**: After PR review, merge the branch back to main before dispatching the next wave.

## Key Design Decisions

- **TTY-based iTerm2 lookup**: Tab name assignment via AppleScript has timing issues.
  All operations use the tty path (returned at tab creation) for reliable session identification.

- **In-memory state**: Agent state is volatile -- lost on MCP restart. Use `reconnect_agent`
  to re-attach to orphaned tabs.

- **Git worktrees for isolation**: Each agent gets a full filesystem checkout. This enables
  true parallel work without merge conflicts during execution.

- **Profile-gated agent execution**: Agent commands and flags come from `.agents/agent-profiles.json`.
  Runtime dispatch chooses a profile; it does not permit arbitrary binaries, flags, or full-auto escalation.

- **Policy-gated project commands**: Generated manifests are checked against `.agents/security-policy.json`
  before `bootstrap`, `test`, `build`, `start`, or readiness commands execute.

## Security Policy

Primary files:
- `.agents/security-policy.json` - command, environment, network, filesystem, logging, and completion-gate policy
- `.agents/agent-profiles.json` - approved agent profiles and permission envelopes
- `.agents/model-providers.json` - approved intake provider profiles
- `docs/security/SECURITY.md` - security charter
- `docs/security/VALIDATION_GATES.md` - completion gates
- `docs/security/DATA_CLASSIFICATION.md` - logging and model-exposure rules

Useful checks:

```bash
cd mcp/conductor
npm run build
node dist/security-cli.js check-manifest --repo-root ../.. --manifest ../../.autopilot/manifest.json --phase all
node dist/security-cli.js scan --repo-root ../..
node dist/security-cli.js prune-logs --repo-root ../..
```

## Customization

### Using with a different CLI agent

Use `.agents/agent-profiles.json` to add a new operator-approved profile.

For Codex-compatible CLIs, use `kind: "codex"`. For other local CLIs, use `kind: "generic"`
with an explicit `command`, `baseArgs`, `allowedFlags`, and optional `idlePattern`.
Do not bypass profiles by editing dispatch code for one-off commands.

### Changing the branch prefix

Search for `conductor/` in `src/git.ts` and replace with your preferred prefix.

### Changing the tab name prefix

Search for `[Codex]` in `src/index.ts` and replace with your preferred prefix.

## Development

```bash
cd mcp/conductor
npm run dev    # tsc --watch
```

## Troubleshooting

### `gh` not found
The MCP process may not inherit your shell PATH. The conductor checks common locations
at startup. Set `GH_PATH` in `.mcp.json` env if needed.

### Orphaned worktrees after crashes
```bash
git worktree prune
git worktree remove .worktrees/{name} --force
git branch -D conductor/{name}
```

### Agent stuck at approval prompt
Use `signal_agent` with `key="enter"` or `text="y"`.

## Full Reference

See `mcp/conductor/ORCHESTRATION_RUNBOOK.md` for the complete operational guide.
