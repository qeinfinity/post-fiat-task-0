import * as fs from "node:fs/promises";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { z } from "zod";

export interface CommandLike {
  cmd: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
  timeoutMs?: number;
}

export interface SecurityIssue {
  code: string;
  message: string;
  path?: string;
  phase?: string;
  command?: string;
}

const CommandRuleSchema = z.object({
  cmd: z.string().min(1),
  argsExact: z.array(z.string()).optional(),
  argsPrefix: z.array(z.string()).optional(),
  phases: z.array(z.string()).optional(),
});

const SecurityPolicySchema = z.object({
  policyVersion: z.literal(1),
  mode: z.enum(["sealed", "standard", "permissive"]).default("sealed"),
  network: z
    .object({
      default: z.enum(["deny", "allow"]).default("deny"),
      allowDomains: z.array(z.string()).default([]),
    })
    .default({ default: "deny", allowDomains: [] }),
  env: z
    .object({
      inherit: z.boolean().default(false),
      allow: z.array(z.string()).default([]),
      allowPrefixes: z.array(z.string()).default([]),
      deny: z.array(z.string()).default([]),
    })
    .default({ inherit: false, allow: [], allowPrefixes: [], deny: [] }),
  commands: z
    .object({
      allow: z.array(CommandRuleSchema).default([]),
      deny: z.array(z.string()).default([]),
      denyArgsContaining: z.array(z.string()).default([]),
      maxTimeoutMs: z.number().int().positive().optional(),
    })
    .default({ allow: [], deny: [], denyArgsContaining: [] }),
  filesystem: z
    .object({
      writeRoots: z.array(z.string()).default(["."]),
      denyWrites: z.array(z.string()).default([]),
      protectedPaths: z.array(z.string()).default([]),
    })
    .default({ writeRoots: ["."], denyWrites: [], protectedPaths: [] }),
  logs: z
    .object({
      redact: z.boolean().default(true),
      retainDays: z.number().int().positive().default(7),
      fullScrollbackRequiresReason: z.boolean().default(true),
    })
    .default({ redact: true, retainDays: 7, fullScrollbackRequiresReason: true }),
  completionGates: z
    .object({
      secretScan: z.boolean().default(true),
      commandPolicy: z.boolean().default(true),
      dependencyAudit: z.boolean().default(true),
      manualBrowserValidation: z.boolean().default(true),
      playwrightValidation: z.boolean().default(true),
    })
    .default({
      secretScan: true,
      commandPolicy: true,
      dependencyAudit: true,
      manualBrowserValidation: true,
      playwrightValidation: true,
    }),
  modelProviders: z
    .object({
      remoteDefault: z.enum(["deny", "allow"]).default("deny"),
      allowedProfiles: z.array(z.string()).default([]),
      remoteRequiresExplicitOptIn: z.boolean().default(true),
      maxPromptDataClass: z.enum(["public", "internal", "sensitive", "restricted"]).default("sensitive"),
      forbidRestrictedData: z.boolean().default(true),
    })
    .optional(),
});

export type SecurityPolicy = z.infer<typeof SecurityPolicySchema>;

const DEFAULT_POLICY: SecurityPolicy = SecurityPolicySchema.parse({
  policyVersion: 1,
  mode: "sealed",
  env: {
    inherit: false,
    allow: ["PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL", "CI", "NODE_ENV"],
    allowPrefixes: ["npm_config_"],
    deny: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK"],
  },
  commands: {
    allow: [
      { cmd: "bash", argsExact: ["scripts/ci"], phases: ["ci"] },
      { cmd: "bash", argsExact: ["scripts/dev"], phases: ["start"] },
      { cmd: "npm", argsPrefix: ["ci"], phases: ["bootstrap"] },
      { cmd: "npm", argsPrefix: ["install"], phases: ["bootstrap"] },
      { cmd: "npm", argsPrefix: ["audit"], phases: ["dependency-audit"] },
      { cmd: "npm", argsPrefix: ["run"] },
      { cmd: "npm", argsPrefix: ["test"] },
      { cmd: "npx", argsPrefix: ["--no-install"] },
      { cmd: "pnpm", argsPrefix: ["install"], phases: ["bootstrap"] },
      { cmd: "pnpm", argsPrefix: ["run"] },
      { cmd: "pnpm", argsPrefix: ["test"] },
      { cmd: "yarn", argsPrefix: ["install"], phases: ["bootstrap"] },
      { cmd: "yarn", argsPrefix: ["run"] },
      { cmd: "yarn", argsPrefix: ["test"] },
      { cmd: "bun", argsPrefix: ["install"], phases: ["bootstrap"] },
      { cmd: "bun", argsPrefix: ["run"] },
      { cmd: "bun", argsPrefix: ["test"] },
      { cmd: "node" },
      { cmd: "python" },
      { cmd: "python3" },
      { cmd: "pytest" },
      { cmd: "uv" },
      { cmd: "cargo" },
      { cmd: "go" },
      { cmd: "make" },
      { cmd: "dotnet" },
    ],
    deny: [
      "rm",
      "curl",
      "wget",
      "ssh",
      "scp",
      "sftp",
      "nc",
      "netcat",
      "ncat",
      "telnet",
      "ftp",
      "gh",
      "git",
      "sudo",
      "su",
      "chmod",
      "chown",
      "security",
      "pbpaste",
      "pbcopy",
      "osascript",
    ],
    denyArgsContaining: [
      "rm -rf",
      "curl ",
      "wget ",
      "| sh",
      "| bash",
      ">/dev/tcp",
      "base64 -d",
      "chmod 777",
      "sudo ",
      "gh auth",
      "git push",
      "git remote",
      "ssh ",
      "scp ",
    ],
    maxTimeoutMs: 600000,
  },
  filesystem: {
    writeRoots: ["."],
    denyWrites: [
      ".git",
      ".env",
      ".env.local",
      ".env.production",
      ".mcp.json",
      ".agents/security-policy.json",
      ".agents/agent-profiles.json",
      ".agents/model-providers.json",
    ],
    protectedPaths: ["docs/security", "mcp/conductor", "scripts/autopilot"],
  },
  modelProviders: {
    remoteDefault: "deny",
    allowedProfiles: ["omlx-local", "openai-cloud", "claude-cloud"],
    remoteRequiresExplicitOptIn: true,
    maxPromptDataClass: "sensitive",
    forbidRestrictedData: true,
  },
});

const SECRET_KEY_RE = /(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|credential)/i;
const SECRET_PATTERNS: Array<{ code: string; re: RegExp }> = [
  { code: "private-key", re: /-----BEGIN [A-Z ]*PRIVATE KEY-----/ },
  { code: "aws-access-key", re: /\bAKIA[0-9A-Z]{16}\b/ },
  { code: "github-token", re: /\bgh[pousr]_[A-Za-z0-9_]{30,}\b/ },
  { code: "openai-token", re: /\bsk-[A-Za-z0-9_-]{20,}\b/ },
  {
    code: "secret-assignment",
    re: /\b(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|credential)\b\s*[:=]\s*["']?[^"'\s,}]{8,}/i,
  },
];

const SKIP_DIRS = new Set([
  ".git",
  ".worktrees",
  "node_modules",
  "dist",
  "coverage",
  ".next",
  "build",
  "target",
]);

const SKIP_PATH_PREFIXES = [
  ".autopilot/runs/",
  ".autopilot/sessions/",
  "mcp/conductor/package-lock.json",
];

function isFileNotFound(err: unknown): boolean {
  return err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT";
}

function isInsidePath(root: string, candidate: string): boolean {
  const rootResolved = path.resolve(root);
  const resolved = path.resolve(candidate);
  const rootWithSep = rootResolved.endsWith(path.sep) ? rootResolved : rootResolved + path.sep;
  return resolved === rootResolved || resolved.startsWith(rootWithSep);
}

function commandName(cmd: string): string {
  return path.basename(cmd.replace(/\\/g, "/")).trim();
}

function commandLine(spec: CommandLike): string {
  return [spec.cmd, ...(spec.args ?? [])].join(" ").trim();
}

function argsMatch(rule: z.infer<typeof CommandRuleSchema>, args: string[]): boolean {
  if (rule.argsExact) {
    return args.length === rule.argsExact.length && rule.argsExact.every((v, i) => args[i] === v);
  }
  if (rule.argsPrefix) {
    return args.length >= rule.argsPrefix.length && rule.argsPrefix.every((v, i) => args[i] === v);
  }
  return true;
}

function rulePhaseMatches(rule: z.infer<typeof CommandRuleSchema>, phase: string): boolean {
  return !rule.phases || rule.phases.length === 0 || rule.phases.includes(phase);
}

function isAllowedCommand(policy: SecurityPolicy, spec: CommandLike, phase: string): boolean {
  const name = commandName(spec.cmd);
  const args = spec.args ?? [];
  return policy.commands.allow.some((rule) => rule.cmd === name && rulePhaseMatches(rule, phase) && argsMatch(rule, args));
}

function isShellEval(spec: CommandLike): boolean {
  const name = commandName(spec.cmd);
  const args = spec.args ?? [];
  if (["bash", "sh", "zsh", "fish"].includes(name)) return args.includes("-c");
  if (name === "node") return args.includes("-e") || args.includes("--eval");
  if (["python", "python3"].includes(name)) return args.includes("-c");
  if (["ruby", "perl"].includes(name)) return args.includes("-e");
  return false;
}

function isAllowedEnvKey(policy: SecurityPolicy, key: string): boolean {
  if (policy.env.deny.includes(key) || SECRET_KEY_RE.test(key)) return false;
  if (policy.env.inherit) return true;
  if (policy.env.allow.includes(key)) return true;
  return policy.env.allowPrefixes.some((prefix) => key.startsWith(prefix));
}

export async function loadSecurityPolicy(repoRoot: string, policyPath?: string): Promise<SecurityPolicy> {
  const resolved = path.resolve(repoRoot, policyPath ?? ".agents/security-policy.json");
  try {
    const raw = await fs.readFile(resolved, "utf8");
    return SecurityPolicySchema.parse(JSON.parse(raw));
  } catch (err) {
    if (isFileNotFound(err)) return DEFAULT_POLICY;
    throw err;
  }
}

export function resolveCommandCwd(repoRoot: string, spec: CommandLike): string {
  const root = path.resolve(repoRoot);
  const cwd = spec.cwd ? (path.isAbsolute(spec.cwd) ? spec.cwd : path.join(root, spec.cwd)) : root;
  const resolved = path.resolve(cwd);
  if (!isInsidePath(root, resolved)) {
    throw new Error(`Command cwd is outside repo root: ${spec.cwd}`);
  }
  return resolved;
}

export function validateCommandSecurity(params: {
  repoRoot: string;
  policy: SecurityPolicy;
  phase: string;
  command: CommandLike;
}): SecurityIssue[] {
  const issues: SecurityIssue[] = [];
  const { repoRoot, policy, phase, command } = params;
  const name = commandName(command.cmd);
  const line = commandLine(command);

  if (!name) {
    issues.push({ code: "command-empty", message: "Command is empty", phase, command: line });
    return issues;
  }

  if (policy.commands.deny.includes(name)) {
    issues.push({ code: "command-denied", message: `Command "${name}" is denied by policy`, phase, command: line });
  }

  if (!isAllowedCommand(policy, command, phase)) {
    issues.push({ code: "command-not-allowed", message: `Command "${line}" is not allowlisted for phase "${phase}"`, phase, command: line });
  }

  if (isShellEval(command)) {
    issues.push({ code: "shell-eval-denied", message: "Inline shell/eval command forms are denied", phase, command: line });
  }

  const lowerLine = line.toLowerCase();
  for (const denied of policy.commands.denyArgsContaining) {
    if (lowerLine.includes(denied.toLowerCase())) {
      issues.push({ code: "command-arg-denied", message: `Command contains denied token "${denied}"`, phase, command: line });
    }
  }

  if (policy.commands.maxTimeoutMs && command.timeoutMs && command.timeoutMs > policy.commands.maxTimeoutMs) {
    issues.push({
      code: "command-timeout-too-high",
      message: `Command timeout ${command.timeoutMs}ms exceeds policy max ${policy.commands.maxTimeoutMs}ms`,
      phase,
      command: line,
    });
  }

  try {
    resolveCommandCwd(repoRoot, command);
  } catch (err) {
    issues.push({ code: "cwd-outside-repo", message: err instanceof Error ? err.message : String(err), phase, command: line });
  }

  for (const [key, value] of Object.entries(command.env ?? {})) {
    if (!isAllowedEnvKey(policy, key)) {
      issues.push({ code: "env-denied", message: `Command env key "${key}" is denied by policy`, phase, command: line });
    }
    if (SECRET_KEY_RE.test(key) || SECRET_PATTERNS.some((p) => p.re.test(`${key}=${value}`))) {
      issues.push({ code: "env-secret", message: `Command env key "${key}" looks sensitive`, phase, command: line });
    }
  }

  return issues;
}

export function buildPolicyEnv(
  policy: SecurityPolicy,
  baseEnv: NodeJS.ProcessEnv,
  commandEnv?: Record<string, string>
): NodeJS.ProcessEnv {
  const out: NodeJS.ProcessEnv = {};

  if (policy.env.inherit) {
    for (const [key, value] of Object.entries(baseEnv)) {
      if (value !== undefined && !policy.env.deny.includes(key)) out[key] = value;
    }
  } else {
    for (const [key, value] of Object.entries(baseEnv)) {
      if (value !== undefined && isAllowedEnvKey(policy, key)) out[key] = value;
    }
  }

  for (const [key, value] of Object.entries(commandEnv ?? {})) {
    if (isAllowedEnvKey(policy, key)) out[key] = value;
  }

  return out;
}

export function validateManifestSecurity(params: {
  repoRoot: string;
  policy: SecurityPolicy;
  manifest: { commands?: Record<string, CommandLike[] | undefined> };
  phases?: string[];
}): SecurityIssue[] {
  const phases = params.phases ?? ["bootstrap", "format", "lint", "test", "build", "start"];
  const issues: SecurityIssue[] = [];
  for (const phase of phases) {
    const commands = params.manifest.commands?.[phase] ?? [];
    for (const command of commands) {
      issues.push(
        ...validateCommandSecurity({
          repoRoot: params.repoRoot,
          policy: params.policy,
          phase,
          command,
        })
      );
    }
  }
  return issues;
}

function policyPathMatches(relPath: string, policyEntry: string): boolean {
  const rel = relPath.replace(/\\/g, "/").replace(/^\/+/, "");
  const entry = policyEntry.replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
  if (!entry || entry === ".") return true;
  return rel === entry || rel.startsWith(`${entry}/`);
}

export function validateWritePathSecurity(params: {
  repoRoot: string;
  policy: SecurityPolicy;
  relPath: string;
}): SecurityIssue[] {
  const issues: SecurityIssue[] = [];
  const normalized = params.relPath.replace(/\\/g, "/").replace(/^\/+/, "");

  if (!normalized || path.isAbsolute(params.relPath)) {
    issues.push({ code: "write-path-invalid", message: `Refusing unsafe write path "${params.relPath}"`, path: params.relPath });
    return issues;
  }

  const abs = path.resolve(params.repoRoot, normalized);
  if (!isInsidePath(params.repoRoot, abs)) {
    issues.push({ code: "write-outside-repo", message: `Refusing write outside repo root: "${params.relPath}"`, path: params.relPath });
  }

  const inAllowedRoot = params.policy.filesystem.writeRoots.some((root) => policyPathMatches(normalized, root));
  if (!inAllowedRoot) {
    issues.push({ code: "write-root-denied", message: `Write path is outside allowed roots: "${params.relPath}"`, path: params.relPath });
  }

  for (const denied of [...params.policy.filesystem.denyWrites, ...params.policy.filesystem.protectedPaths]) {
    if (policyPathMatches(normalized, denied)) {
      issues.push({ code: "write-protected-path", message: `Write path is protected by policy: "${denied}"`, path: params.relPath });
    }
  }

  return issues;
}

export function redactText(text: string, policy: SecurityPolicy = DEFAULT_POLICY, extraSecrets: string[] = []): string {
  if (!policy.logs.redact || !text) return text;
  let out = text;

  out = out.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g, "<redacted-private-key>");
  out = out.replace(/\bAKIA[0-9A-Z]{16}\b/g, "<redacted-aws-access-key>");
  out = out.replace(/\bgh[pousr]_[A-Za-z0-9_]{30,}\b/g, "<redacted-github-token>");
  out = out.replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, "<redacted-api-token>");
  out = out.replace(
    /\b(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|credential)\b(\s*[:=]\s*["']?)([^"'\s,}]+)/gi,
    "$1$2<redacted>"
  );

  for (const secret of extraSecrets) {
    if (!secret || secret.length < 4) continue;
    out = out.split(secret).join("<redacted-env-secret>");
  }

  return out;
}

export function collectSecretEnvValues(env: NodeJS.ProcessEnv): string[] {
  const values: string[] = [];
  for (const [key, value] of Object.entries(env)) {
    if (value && value.length >= 4 && SECRET_KEY_RE.test(key)) values.push(value);
  }
  return values;
}

function shouldSkipRelPath(rel: string): boolean {
  const normalized = rel.replace(/\\/g, "/");
  return SKIP_PATH_PREFIXES.some((prefix) => normalized.startsWith(prefix));
}

function lineForIndex(text: string, index: number): number {
  return text.slice(0, index).split("\n").length;
}

function scanTextForSecrets(relPath: string, text: string): SecurityIssue[] {
  const issues: SecurityIssue[] = [];
  for (const pattern of SECRET_PATTERNS) {
    const re = new RegExp(pattern.re.source, pattern.re.flags.includes("g") ? pattern.re.flags : pattern.re.flags + "g");
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      const matchedText = match[0];
      if (
        pattern.code === "secret-assignment" &&
        /<redacted>|config\.|params\.|process\.env|getString\(|apiKeyFromEnv|provider\.runtime\.apiKey|undefined|null/i.test(matchedText)
      ) {
        continue;
      }
      issues.push({
        code: `secret-${pattern.code}`,
        message: `Potential secret pattern "${pattern.code}" at ${relPath}:${lineForIndex(text, match.index)}`,
        path: relPath,
      });
      if (issues.length >= 20) return issues;
    }
  }
  return issues;
}

async function scanDir(repoRoot: string, dir: string, issues: SecurityIssue[]): Promise<void> {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const abs = path.join(dir, entry.name);
    const rel = path.relative(repoRoot, abs).replace(/\\/g, "/");
    if (!rel || shouldSkipRelPath(rel)) continue;

    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      await scanDir(repoRoot, abs, issues);
      continue;
    }

    if (!entry.isFile()) continue;
    const stat = await fs.stat(abs).catch(() => null);
    if (!stat || stat.size > 1_000_000) continue;

    const buf = await fs.readFile(abs).catch(() => null);
    if (!buf || buf.includes(0)) continue;
    const text = buf.toString("utf8");
    issues.push(...scanTextForSecrets(rel, text));
    if (issues.length >= 50) return;
  }
}

export async function scanRepoForSecrets(repoRoot: string): Promise<SecurityIssue[]> {
  const issues: SecurityIssue[] = [];
  await scanDir(path.resolve(repoRoot), path.resolve(repoRoot), issues);
  return issues;
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function findPackageRoots(repoRoot: string, dir = repoRoot, roots: string[] = []): Promise<string[]> {
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    const abs = path.join(dir, entry.name);
    const rel = path.relative(repoRoot, abs).replace(/\\/g, "/");
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name) || shouldSkipRelPath(rel)) continue;
      await findPackageRoots(repoRoot, abs, roots);
      continue;
    }
    if (entry.isFile() && entry.name === "package.json") {
      roots.push(path.dirname(abs));
    }
  }
  return roots;
}

function spawnAudit(command: CommandLike, repoRoot: string, policy: SecurityPolicy): Promise<string> {
  const issues = validateCommandSecurity({ repoRoot, policy, phase: "dependency-audit", command });
  if (issues.length > 0) return Promise.resolve(formatSecurityIssues(issues));

  return new Promise((resolve) => {
    const cwd = resolveCommandCwd(repoRoot, command);
    const child = spawn(command.cmd, command.args ?? [], {
      cwd,
      env: buildPolicyEnv(policy, process.env, command.env),
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    const timeout = setTimeout(() => child.kill("SIGKILL"), 120_000);
    child.stdout?.on("data", (chunk: Buffer) => {
      output += chunk.toString("utf8");
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      output += chunk.toString("utf8");
    });
    child.on("error", (err) => {
      clearTimeout(timeout);
      resolve(err instanceof Error ? err.message : String(err));
    });
    child.on("close", (code, signal) => {
      clearTimeout(timeout);
      if (code === 0 && !signal) resolve("");
      else resolve(redactText(output || `audit failed exit=${code ?? "null"} signal=${signal ?? "null"}`, policy));
    });
  });
}

async function runDependencyAudit(repoRoot: string, policy: SecurityPolicy): Promise<SecurityIssue[]> {
  const issues: SecurityIssue[] = [];
  const roots = await findPackageRoots(path.resolve(repoRoot));
  for (const packageRoot of roots) {
    const rel = path.relative(repoRoot, packageRoot).replace(/\\/g, "/") || ".";
    const packageRaw = await fs.readFile(path.join(packageRoot, "package.json"), "utf8").catch(() => "");
    const hasDeclaredDeps = /"dependencies"\s*:|"devDependencies"\s*:|"optionalDependencies"\s*:/.test(packageRaw);
    const packageLock = path.join(packageRoot, "package-lock.json");
    if (!(await fileExists(packageLock))) {
      if (hasDeclaredDeps) {
        issues.push({
          code: "dependency-lock-missing",
          message: `package.json with dependencies has no package-lock.json at ${rel}`,
          path: rel,
        });
      }
      continue;
    }

    const auditOutput = await spawnAudit(
      { cmd: "npm", args: ["audit", "--audit-level=high"], cwd: rel === "." ? undefined : rel },
      repoRoot,
      policy
    );
    if (auditOutput) {
      issues.push({
        code: "dependency-audit-failed",
        message: `npm audit failed at ${rel}: ${truncateAuditOutput(auditOutput)}`,
        path: rel,
      });
    }
  }
  return issues;
}

function truncateAuditOutput(text: string): string {
  const compact = text.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 8).join(" | ");
  return compact.length > 1000 ? compact.slice(0, 1000) + "..." : compact;
}

export async function runSecurityCompletionGates(params: {
  repoRoot: string;
  policy: SecurityPolicy;
  manifest?: { commands?: Record<string, CommandLike[] | undefined> };
}): Promise<SecurityIssue[]> {
  const issues: SecurityIssue[] = [];
  if (params.policy.completionGates.commandPolicy && params.manifest) {
    issues.push(...validateManifestSecurity({ repoRoot: params.repoRoot, policy: params.policy, manifest: params.manifest }));
  }
  if (params.policy.completionGates.secretScan) {
    issues.push(...(await scanRepoForSecrets(params.repoRoot)));
  }
  if (params.policy.completionGates.dependencyAudit) {
    issues.push(...(await runDependencyAudit(params.repoRoot, params.policy)));
  }
  return issues;
}

export function formatSecurityIssues(issues: SecurityIssue[]): string {
  return issues
    .map((issue) => {
      const parts = [issue.code, issue.message];
      if (issue.phase) parts.push(`phase=${issue.phase}`);
      if (issue.command) parts.push(`command=${issue.command}`);
      if (issue.path) parts.push(`path=${issue.path}`);
      return `- ${parts.join(" | ")}`;
    })
    .join("\n");
}
