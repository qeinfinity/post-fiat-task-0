export interface AgentSession {
  id: string;
  name: string;

  // iTerm2 binding
  tabName: string;
  tty: string;

  // Git context
  branch: string;
  baseBranch: string;
  worktreePath: string;
  repoRoot: string;

  // Execution
  mode: "interactive" | "exec";
  prompt: string;
  model: string;
  agentProfile: string;
  idlePattern: string;
  launchedAt: string;

  // Status
  status: "running" | "idle" | "completed" | "failed" | "terminated" | "unknown";
  exitCode: number | null;

  // Cached state
  lastScreenContent: string;
  lastScreenAt: string;
  commitCount: number;
  idleDetectedAt: string;
}

export class ConductorState {
  private agents = new Map<string, AgentSession>();

  add(session: AgentSession): void {
    this.agents.set(session.id, session);
  }

  get(id: string): AgentSession | undefined {
    return this.agents.get(id);
  }

  remove(id: string): boolean {
    return this.agents.delete(id);
  }

  list(): AgentSession[] {
    return Array.from(this.agents.values());
  }

  listByStatus(status: AgentSession["status"]): AgentSession[] {
    return this.list().filter((a) => a.status === status);
  }

  findByTty(tty: string): AgentSession | undefined {
    return this.list().find((a) => a.tty === tty);
  }

  findByBranch(branch: string): AgentSession | undefined {
    return this.list().find((a) => a.branch === branch);
  }

  findByTabName(tabName: string): AgentSession | undefined {
    return this.list().find((a) => a.tabName === tabName);
  }

  activeCount(): number {
    return this.listByStatus("running").length;
  }

  has(id: string): boolean {
    return this.agents.has(id);
  }

  update(id: string, updates: Partial<AgentSession>): void {
    const session = this.agents.get(id);
    if (session) {
      Object.assign(session, updates);
    }
  }
}
