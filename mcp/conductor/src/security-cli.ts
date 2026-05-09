import { spawn } from "node:child_process";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { AutopilotManifestSchema, type AutopilotManifest, type CommandSpec } from "./autopilot/schema.js";
import {
  buildPolicyEnv,
  collectSecretEnvValues,
  formatSecurityIssues,
  loadSecurityPolicy,
  redactText,
  resolveCommandCwd,
  runSecurityCompletionGates,
  validateCommandSecurity,
  validateManifestSecurity,
  type SecurityIssue,
  type SecurityPolicy,
} from "./security.js";

function readArgValue(argv: string[], i: number): { value: string | null; nextIndex: number } {
  const a = argv[i];
  const eq = a.indexOf("=");
  if (eq !== -1) return { value: a.slice(eq + 1), nextIndex: i };
  const next = argv[i + 1];
  if (!next || next.startsWith("-")) return { value: null, nextIndex: i };
  return { value: next, nextIndex: i + 1 };
}

function parseArgs(argv: string[]): { command: string; args: Record<string, string | boolean> } {
  const command = argv[0] && !argv[0].startsWith("--") ? argv[0] : "check-manifest";
  const rest = command === argv[0] ? argv.slice(1) : argv;
  const args: Record<string, string | boolean> = {};
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (!a.startsWith("--")) continue;
    const key = a.replace(/^--/, "").split("=")[0];
    const { value, nextIndex } = readArgValue(rest, i);
    if (value === null) args[key] = true;
    else {
      args[key] = value;
      i = nextIndex;
    }
  }
  return { command, args };
}

function getString(args: Record<string, string | boolean>, key: string, fallback?: string): string | undefined {
  const v = args[key];
  if (typeof v === "string") return v;
  return fallback;
}

async function loadManifest(manifestPath: string): Promise<AutopilotManifest> {
  const raw = await fs.readFile(manifestPath, "utf8");
  return AutopilotManifestSchema.parse(JSON.parse(raw));
}

function selectedPhases(value: string | undefined): Array<keyof AutopilotManifest["commands"]> {
  if (!value || value === "ci") return ["bootstrap", "format", "lint", "test", "build"];
  if (value === "all") return ["bootstrap", "format", "lint", "test", "build", "start"];
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean) as Array<keyof AutopilotManifest["commands"]>;
}

function failIssues(issues: SecurityIssue[]): never {
  console.error("security: failed");
  console.error(formatSecurityIssues(issues));
  process.exit(1);
}

function runCommand(params: {
  repoRoot: string;
  policy: SecurityPolicy;
  phase: string;
  command: CommandSpec;
}): Promise<void> {
  const issues = validateCommandSecurity(params);
  if (issues.length > 0) failIssues(issues);

  const cwd = resolveCommandCwd(params.repoRoot, params.command);
  const args = params.command.args ?? [];
  const env = buildPolicyEnv(params.policy, process.env, params.command.env);
  const secretValues = collectSecretEnvValues(process.env);

  return new Promise((resolve, reject) => {
    const child = spawn(params.command.cmd, args, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    child.stdout?.on("data", (chunk: Buffer) => {
      process.stdout.write(redactText(chunk.toString("utf8"), params.policy, secretValues));
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      process.stderr.write(redactText(chunk.toString("utf8"), params.policy, secretValues));
    });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0 && !signal) resolve();
      else reject(new Error(`Command failed (${params.command.cmd}) exit=${code ?? "null"} signal=${signal ?? "null"}`));
    });
  });
}

async function pruneAutopilotLogs(repoRoot: string, policy: SecurityPolicy): Promise<number> {
  const cutoff = Date.now() - policy.logs.retainDays * 24 * 60 * 60 * 1000;
  const roots = [".autopilot/runs", ".autopilot/sessions"];
  const repoAbs = path.resolve(repoRoot);
  const repoWithSep = repoAbs.endsWith(path.sep) ? repoAbs : repoAbs + path.sep;
  let removed = 0;

  for (const relRoot of roots) {
    const absRoot = path.resolve(repoAbs, relRoot);
    if (absRoot !== repoAbs && !absRoot.startsWith(repoWithSep)) continue;
    const entries = await fs.readdir(absRoot, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      const abs = path.join(absRoot, entry.name);
      const stat = await fs.stat(abs).catch(() => null);
      if (!stat || stat.mtimeMs >= cutoff) continue;
      await fs.rm(abs, { recursive: true, force: true });
      removed += 1;
    }
  }

  return removed;
}

async function main(): Promise<void> {
  const parsed = parseArgs(process.argv.slice(2));
  const repoRoot = path.resolve(getString(parsed.args, "repo-root", process.cwd())!);
  const manifestPath = path.resolve(repoRoot, getString(parsed.args, "manifest", ".autopilot/manifest.json")!);
  const policy = await loadSecurityPolicy(repoRoot, getString(parsed.args, "policy"));

  if (parsed.command === "scan") {
    const issues = await runSecurityCompletionGates({ repoRoot, policy });
    if (issues.length > 0) failIssues(issues);
    console.log("security: scan ok");
    return;
  }

  if (parsed.command === "prune-logs") {
    const removed = await pruneAutopilotLogs(repoRoot, policy);
    console.log(`security: pruned ${removed} log entr${removed === 1 ? "y" : "ies"}`);
    return;
  }

  const manifest = await loadManifest(manifestPath);

  if (parsed.command === "check-manifest") {
    const phases = selectedPhases(getString(parsed.args, "phases", getString(parsed.args, "phase", "all")));
    const issues = validateManifestSecurity({ repoRoot, policy, manifest, phases });
    if (issues.length > 0) failIssues(issues);
    console.log("security: manifest ok");
    return;
  }

  if (parsed.command === "run-manifest") {
    const phases = selectedPhases(getString(parsed.args, "phases", getString(parsed.args, "phase", "ci")));
    const issues = validateManifestSecurity({ repoRoot, policy, manifest, phases });
    if (issues.length > 0) failIssues(issues);

    for (const phase of phases) {
      const commands = manifest.commands[phase] ?? [];
      for (const command of commands) {
        console.log(`\n==> ${[command.cmd, ...(command.args ?? [])].join(" ")}`);
        await runCommand({ repoRoot, policy, phase, command });
      }
    }

    const gateIssues = await runSecurityCompletionGates({ repoRoot, policy, manifest });
    if (gateIssues.length > 0) failIssues(gateIssues);
    console.log("\nsecurity: gates ok");
    return;
  }

  if (parsed.command === "run-start") {
    const start = manifest.commands.start;
    if (!start || start.length === 0) throw new Error("Manifest missing commands.start");
    await runCommand({ repoRoot, policy, phase: "start", command: start[0] });
    return;
  }

  throw new Error(`Unknown security command: ${parsed.command}`);
}

main().catch((err) => {
  console.error(`security: failed: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
