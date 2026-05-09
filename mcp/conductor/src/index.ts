import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { ConductorState, type AgentSession } from "./state.js";
import * as iterm from "./iterm.js";
import * as git from "./git.js";
import { buildAgentCommand, getProfile, loadAgentProfiles } from "./agentProfiles.js";
import { loadSecurityPolicy, redactText } from "./security.js";

// ─── Configuration ───────────────────────────────────────────────────────────
// Set via environment variables in .mcp.json

const REPO_ROOT = process.env.CONDUCTOR_REPO_ROOT || process.cwd();
const SERVER_NAME = process.env.CONDUCTOR_SERVER_NAME || "conductor";

const state = new ConductorState();

const server = new McpServer({
  name: SERVER_NAME,
  version: "0.1.0",
});

// ─── Helpers ────────────────────────────────────────────────────────────────

function formatRuntime(launchedAt: string): string {
  const ms = Date.now() - new Date(launchedAt).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function validateName(name: string): string | null {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9-]{1,38}[a-zA-Z0-9]$/.test(name)) {
    return "Name must be 3-40 chars, alphanumeric and hyphens only, cannot start/end with hyphen";
  }
  if (state.has(name)) {
    return `Agent "${name}" already exists`;
  }
  return null;
}

function tailLines(text: string, n: number): string {
  const lines = text.split("\n");
  return lines.slice(-n).join("\n");
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── Tool: list_agents ──────────────────────────────────────────────────────

server.tool(
  "list_agents",
  "List all managed Codex agent sessions with their status",
  {},
  async () => {
    const agents = state.list();
    if (agents.length === 0) {
      return { content: [{ type: "text", text: "No agents active." }] };
    }
    const summary = agents.map((a) => ({
      id: a.id,
      status: a.status,
    branch: a.branch,
    mode: a.mode,
    agentProfile: a.agentProfile,
    runtime: formatRuntime(a.launchedAt),
      commitCount: a.commitCount,
      ...(a.idleDetectedAt ? { idleSince: a.idleDetectedAt } : {}),
    }));
    return {
      content: [{ type: "text", text: JSON.stringify(summary, null, 2) }],
    };
  }
);

// ─── Tool: dispatch_agent ───────────────────────────────────────────────────

server.tool(
  "dispatch_agent",
  "Create a git worktree, open an iTerm2 tab, and launch a Codex agent with a task on a dedicated branch",
  {
    name: z.string().describe("Agent identifier (alphanumeric + hyphens, 3-40 chars). Used as tab name and branch suffix."),
    prompt: z.string().describe("Task instructions for the Codex agent"),
    base_branch: z.string().default("main").describe("Branch to create the agent's branch from"),
    mode: z.enum(["interactive", "exec"]).default("interactive").describe("Interactive shows TUI (watchable), exec runs headless"),
    agent_profile: z.string().optional().describe("Agent profile from .agents/agent-profiles.json"),
    model: z.string().optional().describe("Model override (default: uses Codex config)"),
    full_auto: z.boolean().optional().describe("Auto-approve actions only if selected profile allows it"),
    sandbox: z.string().optional().describe("Sandbox mode (e.g. 'workspace-write')"),
    extra_flags: z.array(z.string()).optional().describe("Additional CLI flags for Codex"),
  },
  async ({ name, prompt, base_branch, mode, agent_profile, model, full_auto, sandbox, extra_flags }) => {
    const nameError = validateName(name);
    if (nameError) {
      return { content: [{ type: "text", text: `Error: ${nameError}` }], isError: true };
    }

    try {
      const registry = await loadAgentProfiles(REPO_ROOT);
      const { id: profileId, profile } = getProfile(registry, agent_profile);

      // 1. Create worktree + branch
      const { worktreePath, branch } = await git.createWorktree(
        REPO_ROOT,
        name,
        base_branch
      );

      // 2. Create iTerm2 tab
      const tabName = `${profile.tabPrefix} ${name}`;
      const tty = await iterm.createTab(tabName);

      // 3. Build and send agent command
      const command = buildAgentCommand(profile, {
        prompt,
        worktreePath,
        mode,
        model,
        fullAuto: full_auto,
        sandbox,
        extraFlags: extra_flags,
      });

      await sleep(500);
      await iterm.writeText(tty, command);

      // 4. Store session
      const session: AgentSession = {
        id: name,
        name,
        tabName,
        tty,
        branch,
        baseBranch: base_branch,
        worktreePath,
        repoRoot: REPO_ROOT,
        mode,
        prompt,
        model: model || "default",
        agentProfile: profileId,
        idlePattern: profile.idlePattern || "",
        launchedAt: new Date().toISOString(),
        status: "running",
        exitCode: null,
        lastScreenContent: "",
        lastScreenAt: "",
        commitCount: 0,
        idleDetectedAt: "",
      };
      state.add(session);

      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            id: name,
            branch,
            worktree: worktreePath,
            tabName,
            tty,
            status: "running",
            agentProfile: profileId,
          }, null, 2),
        }],
      };
    } catch (err) {
      try { await git.removeWorktree(REPO_ROOT, name, true); } catch {}
      const msg = err instanceof Error ? err.message : String(err);
      return { content: [{ type: "text", text: `Dispatch failed: ${msg}` }], isError: true };
    }
  }
);

// ─── Tool: batch_dispatch ───────────────────────────────────────────────────

server.tool(
  "batch_dispatch",
  "Dispatch multiple Codex agents in sequence (1s delay between each to avoid AppleScript race conditions)",
  {
    agents: z.array(z.object({
      name: z.string(),
      prompt: z.string(),
      base_branch: z.string().default("main"),
      mode: z.enum(["interactive", "exec"]).default("interactive"),
      agent_profile: z.string().optional(),
      model: z.string().optional(),
      full_auto: z.boolean().optional(),
      sandbox: z.string().optional(),
      extra_flags: z.array(z.string()).optional(),
    })).describe("Array of agent specifications"),
  },
  async ({ agents }) => {
    const results: Array<{ name: string; status: string; error?: string }> = [];
    const registry = await loadAgentProfiles(REPO_ROOT);

    for (const spec of agents) {
      const nameError = validateName(spec.name);
      if (nameError) {
        results.push({ name: spec.name, status: "failed", error: nameError });
        continue;
      }

      try {
        const { id: profileId, profile } = getProfile(registry, spec.agent_profile);
        const { worktreePath, branch } = await git.createWorktree(
          REPO_ROOT,
          spec.name,
          spec.base_branch
        );

        const tabName = `${profile.tabPrefix} ${spec.name}`;
        const tty = await iterm.createTab(tabName);

        const command = buildAgentCommand(profile, {
          prompt: spec.prompt,
          worktreePath,
          mode: spec.mode,
          model: spec.model,
          fullAuto: spec.full_auto,
          sandbox: spec.sandbox,
          extraFlags: spec.extra_flags,
        });

        await sleep(500);
        await iterm.writeText(tty, command);

        const session: AgentSession = {
          id: spec.name,
          name: spec.name,
          tabName,
          tty,
          branch,
          baseBranch: spec.base_branch,
          worktreePath,
          repoRoot: REPO_ROOT,
          mode: spec.mode,
          prompt: spec.prompt,
          model: spec.model || "default",
          agentProfile: profileId,
          idlePattern: profile.idlePattern || "",
          launchedAt: new Date().toISOString(),
          status: "running",
          exitCode: null,
          lastScreenContent: "",
          lastScreenAt: "",
          commitCount: 0,
          idleDetectedAt: "",
        };
        state.add(session);

        results.push({ name: spec.name, status: `dispatched (${profileId})` });
      } catch (err) {
        try { await git.removeWorktree(REPO_ROOT, spec.name, true); } catch {}
        const msg = err instanceof Error ? err.message : String(err);
        results.push({ name: spec.name, status: "failed", error: msg });
      }

      // Delay between dispatches
      if (agents.indexOf(spec) < agents.length - 1) {
        await sleep(1000);
      }
    }

    return {
      content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
    };
  }
);

// ─── Tool: read_screen ──────────────────────────────────────────────────────

server.tool(
  "read_screen",
  "Read the visible screen or full scrollback from an agent's iTerm2 tab",
  {
    agent_id: z.string().describe("Agent identifier"),
    full_scrollback: z.boolean().default(false).describe("Read entire scrollback instead of just visible screen"),
    tail_lines: z.number().default(50).describe("Return only the last N lines"),
    reason: z.string().optional().describe("Required when reading full scrollback if policy demands a reason"),
  },
  async ({ agent_id, full_scrollback, tail_lines, reason }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    try {
      const policy = await loadSecurityPolicy(REPO_ROOT);
      if (full_scrollback && policy.logs.fullScrollbackRequiresReason && !reason?.trim()) {
        return {
          content: [{ type: "text", text: "Full scrollback requires a reason under the active security policy." }],
          isError: true,
        };
      }
      const content = full_scrollback
        ? await iterm.readFullText(agent.tty)
        : await iterm.readContents(agent.tty);

      const trimmed = redactText(tailLines(content, tail_lines), policy);

      state.update(agent_id, {
        lastScreenContent: trimmed,
        lastScreenAt: new Date().toISOString(),
      });

      return {
        content: [{
          type: "text",
          text: `[${agent.tabName}] (${agent.status})\n${"─".repeat(40)}\n${trimmed}`,
        }],
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      state.update(agent_id, { status: "unknown" });
      return { content: [{ type: "text", text: `Screen read failed: ${msg}` }], isError: true };
    }
  }
);

// ─── Tool: agent_status ─────────────────────────────────────────────────────

server.tool(
  "agent_status",
  "Get detailed status of one or all agents, optionally refreshing git and screen state",
  {
    agent_id: z.string().optional().describe("Agent ID (omit for all agents)"),
    refresh_git: z.boolean().default(false).describe("Refresh git commit/diff info"),
    refresh_screen: z.boolean().default(false).describe("Read last 10 lines of screen"),
  },
  async ({ agent_id, refresh_git, refresh_screen }) => {
    const agents = agent_id ? [state.get(agent_id)].filter(Boolean) as AgentSession[] : state.list();
    const policy = await loadSecurityPolicy(REPO_ROOT);

    if (agents.length === 0) {
      const msg = agent_id ? `Agent "${agent_id}" not found` : "No agents active";
      return { content: [{ type: "text", text: msg }] };
    }

    const statuses = [];
    for (const agent of agents) {
      const info: Record<string, unknown> = {
        id: agent.id,
        status: agent.status,
        branch: agent.branch,
        mode: agent.mode,
        agentProfile: agent.agentProfile,
        runtime: formatRuntime(agent.launchedAt),
        worktree: agent.worktreePath,
      };

      const alive = await iterm.isSessionAlive(agent.tty);
      info.processAlive = alive;
      if (!alive && agent.status === "running") {
        state.update(agent.id, { status: "unknown" });
        info.status = "unknown";
      }

      if (refresh_git) {
        const { count, log } = await git.getBranchLog(agent.worktreePath, agent.baseBranch);
        const diffStat = await git.getBranchDiffStat(agent.worktreePath, agent.baseBranch);
        const gitStatus = await git.getGitStatus(agent.worktreePath);
        state.update(agent.id, { commitCount: count });
        info.commits = count;
        info.commitLog = log;
        info.diffStat = diffStat;
        info.uncommitted = gitStatus;
      }

      if (refresh_screen) {
        try {
          const screen = await iterm.readContents(agent.tty);
          info.screenTail = redactText(tailLines(screen, 10), policy);
        } catch {
          info.screenTail = "(unable to read screen)";
        }
      }

      statuses.push(info);
    }

    return {
      content: [{ type: "text", text: JSON.stringify(statuses, null, 2) }],
    };
  }
);

// ─── Tool: signal_agent ─────────────────────────────────────────────────────

server.tool(
  "signal_agent",
  "Send text or a control key to an agent's iTerm2 tab",
  {
    agent_id: z.string().describe("Agent identifier"),
    text: z.string().optional().describe("Text to type into the session (mutually exclusive with key)"),
    key: z.enum(["ctrl-c", "ctrl-d", "ctrl-z", "enter"]).optional().describe("Control key to send"),
  },
  async ({ agent_id, text, key }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    if (!text && !key) {
      return { content: [{ type: "text", text: "Provide either 'text' or 'key'" }], isError: true };
    }

    try {
      if (key === "enter") {
        await iterm.writeText(agent.tty, "");
      } else if (key) {
        await iterm.sendControl(agent.tty, key);
      } else if (text) {
        await iterm.writeText(agent.tty, text);
      }

      return {
        content: [{ type: "text", text: `Sent ${key || "text"} to ${agent.tabName}` }],
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { content: [{ type: "text", text: `Signal failed: ${msg}` }], isError: true };
    }
  }
);

// ─── Tool: terminate_agent ──────────────────────────────────────────────────

server.tool(
  "terminate_agent",
  "Stop a running Codex agent, optionally cleaning up its worktree, branch, and tab",
  {
    agent_id: z.string().describe("Agent identifier"),
    cleanup_worktree: z.boolean().default(false).describe("Remove the git worktree"),
    cleanup_branch: z.boolean().default(false).describe("Delete the git branch"),
    close_tab: z.boolean().default(false).describe("Close the iTerm2 tab"),
  },
  async ({ agent_id, cleanup_worktree, cleanup_branch, close_tab }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    const actions: string[] = [];

    try {
      await iterm.sendControl(agent.tty, "ctrl-c");
      actions.push("Sent Ctrl+C");
      await sleep(2000);
    } catch {
      actions.push("Could not send Ctrl+C (tab may be closed)");
    }

    if (cleanup_worktree) {
      try {
        await git.removeWorktree(REPO_ROOT, agent.name, cleanup_branch);
        actions.push(`Removed worktree${cleanup_branch ? " and branch" : ""}`);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        actions.push(`Worktree cleanup failed: ${msg}`);
      }
    }

    if (close_tab) {
      try {
        await iterm.closeTab(agent.tty);
        actions.push("Closed tab");
      } catch {
        actions.push("Tab already closed");
      }
    }

    state.update(agent_id, { status: "terminated" });
    if (close_tab && cleanup_worktree) {
      state.remove(agent_id);
    }

    return {
      content: [{
        type: "text",
        text: `Terminated ${agent_id}:\n${actions.map((a) => `  - ${a}`).join("\n")}`,
      }],
    };
  }
);

// ─── Tool: reconnect_agent ──────────────────────────────────────────────────

server.tool(
  "reconnect_agent",
  "Scan iTerm2 tabs for conductor sessions and re-attach to them after MCP restart",
  {
    scan: z.boolean().default(true).describe("Scan all tabs and return conductor-related candidates"),
    tab_name: z.string().optional().describe("Specific tab name to reconnect to (required if scan=false)"),
    agent_id: z.string().optional().describe("Agent ID to assign (required if scan=false)"),
    branch: z.string().optional().describe("Git branch name (required if scan=false)"),
    worktree_path: z.string().optional().describe("Worktree path (required if scan=false)"),
    base_branch: z.string().default("main").describe("Base branch for the agent"),
  },
  async ({ scan, tab_name, agent_id, branch, worktree_path, base_branch }) => {
    if (scan) {
      const tabs = await iterm.listTabs();
      const registry = await loadAgentProfiles(REPO_ROOT);
      const prefixes = new Set(Object.values(registry.profiles).map((profile) => profile.tabPrefix));
      const conductorTabs = tabs.filter((t) => Array.from(prefixes).some((prefix) => t.name.startsWith(prefix)));
      const worktrees = await git.listWorktrees(REPO_ROOT);

      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            conductorTabs,
            conductorWorktrees: worktrees,
            trackedAgents: state.list().map((a) => ({ id: a.id, tabName: a.tabName, status: a.status })),
          }, null, 2),
        }],
      };
    }

    if (!tab_name || !agent_id || !branch) {
      return {
        content: [{ type: "text", text: "When scan=false, tab_name, agent_id, and branch are required" }],
        isError: true,
      };
    }

    const tabs = await iterm.listTabs();
    const tab = tabs.find((t) => t.name === tab_name);
    if (!tab) {
      return { content: [{ type: "text", text: `Tab "${tab_name}" not found` }], isError: true };
    }

    const session: AgentSession = {
      id: agent_id,
      name: agent_id,
      tabName: tab_name,
      tty: tab.tty,
      branch,
      baseBranch: base_branch,
      worktreePath: worktree_path || `${REPO_ROOT}/.worktrees/${agent_id}`,
      repoRoot: REPO_ROOT,
      mode: "interactive",
      prompt: "(reconnected)",
      model: "default",
      agentProfile: "reconnected",
      idlePattern: "",
      launchedAt: new Date().toISOString(),
      status: "running",
      exitCode: null,
      lastScreenContent: "",
      lastScreenAt: "",
      commitCount: 0,
      idleDetectedAt: "",
    };
    state.add(session);

    return {
      content: [{ type: "text", text: `Reconnected to "${tab_name}" as agent "${agent_id}"` }],
    };
  }
);

// ─── Tool: branch_diff ──────────────────────────────────────────────────────

server.tool(
  "branch_diff",
  "Show the git diff between an agent's branch and its base branch",
  {
    agent_id: z.string().describe("Agent identifier"),
    stat_only: z.boolean().default(false).describe("Return --stat summary only"),
    file_filter: z.string().optional().describe("Glob pattern to filter files in the diff"),
  },
  async ({ agent_id, stat_only, file_filter }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    const diff = stat_only
      ? await git.getBranchDiffStat(agent.worktreePath, agent.baseBranch)
      : await git.getBranchDiff(agent.worktreePath, agent.baseBranch, file_filter);

    if (!diff) {
      return { content: [{ type: "text", text: `No changes on ${agent.branch} vs ${agent.baseBranch}` }] };
    }

    return {
      content: [{ type: "text", text: `Diff for ${agent.branch} vs ${agent.baseBranch}:\n\n${diff}` }],
    };
  }
);

// ─── Tool: create_pr ────────────────────────────────────────────────────────

server.tool(
  "create_pr",
  "Push an agent's branch to origin and create a GitHub pull request",
  {
    agent_id: z.string().describe("Agent identifier"),
    title: z.string().describe("PR title"),
    body: z.string().default("").describe("PR description/body"),
    draft: z.boolean().default(false).describe("Create as draft PR"),
    base: z.string().optional().describe("Target branch (default: agent's base branch)"),
  },
  async ({ agent_id, title, body, draft, base }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    const targetBase = base || agent.baseBranch;

    try {
      await git.pushBranch(agent.worktreePath, agent.branch);
      const prUrl = await git.createPR(
        agent.worktreePath,
        agent.branch,
        targetBase,
        title,
        body,
        draft
      );

      state.update(agent_id, { status: "completed" });

      return {
        content: [{
          type: "text",
          text: `PR created: ${prUrl}\nBranch: ${agent.branch} -> ${targetBase}`,
        }],
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { content: [{ type: "text", text: `PR creation failed: ${msg}` }], isError: true };
    }
  }
);

// ─── Tool: finalize_agent ──────────────────────────────────────────────────

server.tool(
  "finalize_agent",
  "Review diff, push branch, create PR, and terminate an agent in one step",
  {
    agent_id: z.string().describe("Agent identifier"),
    pr_title: z.string().describe("PR title"),
    pr_body: z.string().default("").describe("PR description/body"),
    draft: z.boolean().default(false).describe("Create as draft PR"),
    base: z.string().optional().describe("Target branch (default: agent's base branch)"),
    cleanup_worktree: z.boolean().default(false).describe("Remove the git worktree after PR creation"),
    close_tab: z.boolean().default(true).describe("Close the iTerm2 tab after PR creation"),
  },
  async ({ agent_id, pr_title, pr_body, draft, base, cleanup_worktree, close_tab }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    const targetBase = base || agent.baseBranch;
    const steps: string[] = [];

    try {
      // 1. Get diff for review context
      const diffStat = await git.getBranchDiffStat(agent.worktreePath, agent.baseBranch);
      const { count, log } = await git.getBranchLog(agent.worktreePath, agent.baseBranch);
      steps.push(`Commits: ${count}\n${log}`);
      steps.push(`Diff:\n${diffStat}`);

      // 2. Push branch
      await git.pushBranch(agent.worktreePath, agent.branch);
      steps.push(`Pushed ${agent.branch} to origin`);

      // 3. Create PR
      const prUrl = await git.createPR(
        agent.worktreePath,
        agent.branch,
        targetBase,
        pr_title,
        pr_body,
        draft
      );
      steps.push(`PR created: ${prUrl}`);

      // 4. Terminate
      try {
        await iterm.sendControl(agent.tty, "ctrl-c");
        await sleep(1000);
      } catch {}

      if (cleanup_worktree) {
        try {
          await git.removeWorktree(REPO_ROOT, agent.name, false);
          steps.push("Worktree removed");
        } catch (err) {
          steps.push(`Worktree cleanup failed: ${err instanceof Error ? err.message : String(err)}`);
        }
      }

      if (close_tab) {
        try {
          await iterm.closeTab(agent.tty);
          steps.push("Tab closed");
        } catch {}
      }

      state.update(agent_id, { status: "completed" });
      if (close_tab && cleanup_worktree) {
        state.remove(agent_id);
      }

      return {
        content: [{
          type: "text",
          text: steps.join("\n\n"),
        }],
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        content: [{ type: "text", text: `Finalize failed at: ${steps.join(" | ")}\n\nError: ${msg}` }],
        isError: true,
      };
    }
  }
);

// ─── Tool: merge_to_base ──────────────────────────────────────────────────

server.tool(
  "merge_to_base",
  "Merge an agent's branch into its base branch (e.g. into main)",
  {
    agent_id: z.string().describe("Agent identifier"),
    ff_only: z.boolean().default(true).describe("Require fast-forward merge (safer, fails if diverged)"),
  },
  async ({ agent_id, ff_only }) => {
    const agent = state.get(agent_id);
    if (!agent) {
      return { content: [{ type: "text", text: `Agent "${agent_id}" not found` }], isError: true };
    }

    try {
      const result = await git.mergeBranch(
        REPO_ROOT,
        agent.branch,
        agent.baseBranch,
        ff_only
      );
      return {
        content: [{
          type: "text",
          text: `Merged ${agent.branch} into ${agent.baseBranch}\n${result}`,
        }],
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        content: [{ type: "text", text: `Merge failed: ${msg}` }],
        isError: true,
      };
    }
  }
);

// ─── Idle Detection Loop ──────────────────────────────────────────────────

const IDLE_POLL_INTERVAL = 30_000; // 30 seconds
const CODEX_IDLE_PATTERN = /\?\s+for shortcuts\s+\d+%\s+context left/;

function idlePatternFor(agent: AgentSession): RegExp {
  if (!agent.idlePattern) return CODEX_IDLE_PATTERN;
  try {
    return new RegExp(agent.idlePattern);
  } catch {
    return CODEX_IDLE_PATTERN;
  }
}

async function pollIdleStatus(): Promise<void> {
  for (const agent of state.listByStatus("running")) {
    try {
      const screen = await iterm.readContents(agent.tty);
      if (idlePatternFor(agent).test(screen)) {
        state.update(agent.id, {
          status: "idle",
          idleDetectedAt: new Date().toISOString(),
          lastScreenContent: tailLines(screen, 10),
          lastScreenAt: new Date().toISOString(),
        });
      }
    } catch {
      const alive = await iterm.isSessionAlive(agent.tty);
      if (!alive) {
        state.update(agent.id, { status: "unknown" });
      }
    }
  }
}

let idleTimer: ReturnType<typeof setInterval> | null = null;

function startIdleDetection(): void {
  if (idleTimer) return;
  idleTimer = setInterval(() => {
    pollIdleStatus().catch(() => {});
  }, IDLE_POLL_INTERVAL);
}

// ─── Start ──────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  startIdleDetection();
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
