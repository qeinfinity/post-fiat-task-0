import { execFile, execFileSync } from "node:child_process";
import { promisify } from "node:util";
import * as path from "node:path";
import * as fs from "node:fs";

const execFileAsync = promisify(execFile);

const GIT_TIMEOUT = 30_000;

/**
 * Resolve the full path to the `gh` CLI binary.
 * The MCP process may not inherit the user's full shell PATH,
 * so we check common locations.
 */
function resolveGhPath(): string {
  try {
    const result = execFileSync("which", ["gh"], { timeout: 5000 });
    const p = result.toString().trim();
    if (p) return p;
  } catch {}
  const candidates = [
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
    path.join(process.env.HOME || "", ".local/bin/gh"),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return "gh";
}

const GH_PATH = resolveGhPath();

async function git(
  args: string[],
  cwd: string
): Promise<string> {
  const { stdout } = await execFileAsync("git", args, {
    cwd,
    timeout: GIT_TIMEOUT,
  });
  return stdout.trim();
}

/**
 * Create a git worktree with a new branch.
 * Worktrees are stored under `.worktrees/{name}/`.
 * Branches are named `conductor/{name}`.
 */
export async function createWorktree(
  repoRoot: string,
  name: string,
  baseBranch: string
): Promise<{ worktreePath: string; branch: string }> {
  const worktreePath = path.join(repoRoot, ".worktrees", name);
  const branch = `conductor/${name}`;

  await git(["worktree", "prune"], repoRoot);
  await git(
    ["worktree", "add", worktreePath, "-b", branch, baseBranch],
    repoRoot
  );

  return { worktreePath, branch };
}

/**
 * Remove a git worktree and optionally its branch.
 */
export async function removeWorktree(
  repoRoot: string,
  name: string,
  deleteBranch = false
): Promise<void> {
  const worktreePath = path.join(repoRoot, ".worktrees", name);
  const branch = `conductor/${name}`;

  try {
    await git(["worktree", "remove", worktreePath, "--force"], repoRoot);
  } catch {}

  if (deleteBranch) {
    try {
      await git(["branch", "-D", branch], repoRoot);
    } catch {}
  }
}

/**
 * Get commit log for a branch relative to its base.
 */
export async function getBranchLog(
  worktreePath: string,
  baseBranch: string
): Promise<{ count: number; log: string }> {
  try {
    const log = await git(
      ["log", "--oneline", `${baseBranch}..HEAD`],
      worktreePath
    );
    const lines = log ? log.split("\n").filter((l) => l.trim()) : [];
    return { count: lines.length, log };
  } catch {
    return { count: 0, log: "" };
  }
}

/**
 * Get diff stat between agent's branch and base.
 */
export async function getBranchDiffStat(
  worktreePath: string,
  baseBranch: string
): Promise<string> {
  try {
    return await git(["diff", "--stat", `${baseBranch}...HEAD`], worktreePath);
  } catch {
    return "";
  }
}

/**
 * Get full diff between agent's branch and base.
 */
export async function getBranchDiff(
  worktreePath: string,
  baseBranch: string,
  fileFilter?: string
): Promise<string> {
  const args = ["diff", `${baseBranch}...HEAD`];
  if (fileFilter) {
    args.push("--", fileFilter);
  }
  try {
    return await git(args, worktreePath);
  } catch {
    return "";
  }
}

/**
 * Get git status for a worktree.
 */
export async function getGitStatus(worktreePath: string): Promise<string> {
  try {
    return await git(["status", "--short"], worktreePath);
  } catch {
    return "";
  }
}

/**
 * Push a branch to origin.
 */
export async function pushBranch(
  worktreePath: string,
  branch: string
): Promise<string> {
  return git(["push", "-u", "origin", branch], worktreePath);
}

/**
 * Check if a branch exists locally.
 */
export async function branchExists(
  repoRoot: string,
  branch: string
): Promise<boolean> {
  try {
    await git(["rev-parse", "--verify", branch], repoRoot);
    return true;
  } catch {
    return false;
  }
}

/**
 * List existing conductor worktrees.
 */
export async function listWorktrees(
  repoRoot: string
): Promise<string[]> {
  try {
    const output = await git(["worktree", "list", "--porcelain"], repoRoot);
    const worktrees: string[] = [];
    for (const line of output.split("\n")) {
      if (line.startsWith("worktree ") && line.includes(".worktrees/")) {
        worktrees.push(line.replace("worktree ", ""));
      }
    }
    return worktrees;
  } catch {
    return [];
  }
}

/**
 * Merge a branch into a target branch.
 * Operates on the main repo, not the worktree.
 */
export async function mergeBranch(
  repoRoot: string,
  sourceBranch: string,
  targetBranch: string,
  ffOnly = true
): Promise<string> {
  await git(["checkout", targetBranch], repoRoot);
  try {
    await git(["fetch", "origin", targetBranch], repoRoot);
  } catch {}
  const mergeArgs = ["merge", sourceBranch];
  if (ffOnly) {
    mergeArgs.push("--ff-only");
  }
  return git(mergeArgs, repoRoot);
}

/**
 * Create a PR via gh CLI.
 */
export async function createPR(
  worktreePath: string,
  branch: string,
  baseBranch: string,
  title: string,
  body: string,
  draft = false
): Promise<string> {
  const args = [
    "pr",
    "create",
    "--head", branch,
    "--base", baseBranch,
    "--title", title,
    "--body", body,
  ];
  if (draft) {
    args.push("--draft");
  }
  const { stdout } = await execFileAsync(GH_PATH, args, {
    cwd: worktreePath,
    timeout: GIT_TIMEOUT,
  });
  return stdout.trim();
}
