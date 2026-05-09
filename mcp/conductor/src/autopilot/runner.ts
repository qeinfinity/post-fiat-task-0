import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as crypto from "node:crypto";
import { AutopilotManifestSchema, type AutopilotManifest, type CommandSpec } from "./schema.js";
import { AutopilotChangeSetSchema, type AutopilotChangeSet } from "./changeset.js";
import { lmStudioChatComplete, type LmStudioConfig } from "./lmstudio.js";
import {
  buildPolicyEnv,
  collectSecretEnvValues,
  formatSecurityIssues,
  loadSecurityPolicy,
  redactText,
  resolveCommandCwd,
  runSecurityCompletionGates,
  validateCommandSecurity,
  validateWritePathSecurity,
  type SecurityPolicy,
} from "../security.js";

export interface AutopilotConfig {
  repoRoot: string;
  specPath: string;
  manifestPath: string;
  runsDir: string;
  engine: "codex" | "lmstudio";
  codexModel?: string;
  codexSandbox?: string;
  codexFullAuto: boolean;
  codexExtraFlags?: string[];
  lmstudioBaseUrl?: string;
  lmstudioModel?: string;
  lmstudioApiKey?: string;
  lmstudioTemperature?: number;
  lmstudioMaxTokens?: number;
  lmstudioTimeoutMs?: number;
  scaffold: "auto" | "always" | "never";
  maxFixAttempts: number;
  requireStartCommand: boolean;
  requireCiWorkflow: boolean;
  securityPolicyPath?: string;
}

export interface AutopilotRunResult {
  ok: boolean;
  runId: string;
  runDir: string;
  manifestPath: string;
  summaryPath: string;
}

function nowRunId(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}-${pad(
    d.getUTCHours()
  )}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
}

function isFileNotFound(err: unknown): boolean {
  return (
    err instanceof Error &&
    "code" in err &&
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (err as any).code === "ENOENT"
  );
}

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

async function atomicWriteFile(filePath: string, contents: string): Promise<void> {
  const dir = path.dirname(filePath);
  await ensureDir(dir);
  const tmp = path.join(dir, `.${path.basename(filePath)}.${crypto.randomBytes(6).toString("hex")}.tmp`);
  await fs.writeFile(tmp, contents, "utf8");
  await fs.rename(tmp, filePath);
}

async function atomicWriteJson(filePath: string, value: unknown): Promise<void> {
  await atomicWriteFile(filePath, JSON.stringify(value, null, 2));
}

function redactLmConfig(config: LmStudioConfig): Omit<LmStudioConfig, "apiKey"> & { apiKey?: "<redacted>" } {
  const { apiKey, ...rest } = config;
  return apiKey ? { ...rest, apiKey: "<redacted>" } : rest;
}

function redactAutopilotConfig(config: AutopilotConfig): Record<string, unknown> {
  return {
    ...config,
    lmstudioApiKey: config.lmstudioApiKey ? "<redacted>" : undefined,
  };
}

async function readText(filePath: string): Promise<string> {
  return fs.readFile(filePath, "utf8");
}

function truncateLines(text: string, maxLines: number): string {
  const lines = text.split("\n");
  if (lines.length <= maxLines) return text;
  return lines.slice(-maxLines).join("\n");
}

async function readTail(filePath: string, maxLines: number): Promise<string> {
  try {
    const txt = await readText(filePath);
    return truncateLines(txt, maxLines);
  } catch (err) {
    if (isFileNotFound(err)) return "";
    throw err;
  }
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function collectReadinessFailures(params: {
  repoRoot: string;
  manifest: AutopilotManifest;
  requireStartCommand: boolean;
  requireCiWorkflow: boolean;
}): Promise<string[]> {
  const failures: string[] = [];

  if (!(await fileExists(path.join(params.repoRoot, "README.md")))) {
    failures.push("Missing README.md");
  }

  if (!(await fileExists(path.join(params.repoRoot, "scripts", "dev")))) {
    failures.push("Missing scripts/dev (required: visual testing must be one command: bash scripts/dev)");
  }

  if (!(await fileExists(path.join(params.repoRoot, "scripts", "ci")))) {
    failures.push("Missing scripts/ci (required for CI)");
  }

  if (params.requireCiWorkflow) {
    if (!(await fileExists(path.join(params.repoRoot, ".github", "workflows", "ci.yml")))) {
      failures.push("Missing .github/workflows/ci.yml (required)");
    }
  }

  if (params.requireStartCommand) {
    const startCount = params.manifest.commands.start?.length ?? 0;
    if (startCount === 0) {
      failures.push("Manifest missing commands.start (required: scripts/dev must be able to run the app)");
    }
  }

  return failures;
}

async function runStartCommandSanityCheck(params: {
  repoRoot: string;
  manifest: AutopilotManifest;
  runDir: string;
  attemptId: string;
  policy: SecurityPolicy;
}): Promise<{ ok: true } | { ok: false; logPath: string }> {
  const start = params.manifest.commands.start;
  if (!start || start.length === 0) {
    return { ok: false, logPath: "" };
  }
  const spec = start[0];
  const logPath = path.join(params.runDir, `start-check.${params.attemptId}.log`);
  const securityIssues = validateCommandSecurity({
    repoRoot: params.repoRoot,
    policy: params.policy,
    phase: "start",
    command: spec,
  });
  if (securityIssues.length > 0) {
    await atomicWriteFile(logPath, formatSecurityIssues(securityIssues) + "\n");
    return { ok: false, logPath };
  }

  const cwd = resolveCommandCwd(params.repoRoot, spec);
  const env = buildPolicyEnv(params.policy, process.env, spec.env);
  const args = spec.args ?? [];

  // Run briefly: if it errors immediately, treat as failure. If it keeps running, we SIGKILL and treat as success.
  const { code, signal } = await spawnLogged(spec.cmd, args, {
    cwd,
    env,
    logPath,
    timeoutMs: 5_000,
    policy: params.policy,
  });

  if (code === 0 && !signal) return { ok: true };
  if (signal === "SIGKILL") return { ok: true };
  return { ok: false, logPath };
}

function spawnLogged(
  cmd: string,
  args: string[],
  options: { cwd: string; env?: NodeJS.ProcessEnv; logPath: string; timeoutMs?: number; policy?: SecurityPolicy }
): Promise<{ code: number; signal: NodeJS.Signals | null }> {
  return new Promise((resolve) => {
    let settled = false;
    const secretValues = collectSecretEnvValues(options.env ?? process.env);
    const child = spawn(cmd, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    const logStream = createWriteStream(options.logPath, { flags: "a" });
    child.stdout?.on("data", (chunk: Buffer) => {
      logStream.write(redactText(chunk.toString("utf8"), options.policy, secretValues));
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      logStream.write(redactText(chunk.toString("utf8"), options.policy, secretValues));
    });

    const timeout =
      options.timeoutMs && options.timeoutMs > 0
        ? setTimeout(() => {
            child.kill("SIGKILL");
          }, options.timeoutMs)
        : null;

    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      if (timeout) clearTimeout(timeout);
      try {
        logStream.write(redactText(`${err instanceof Error ? err.message : String(err)}\n`, options.policy, secretValues));
      } catch {}
      logStream.end();
      resolve({ code: 127, signal: null });
    });

    child.on("close", (code, signal) => {
      if (settled) return;
      settled = true;
      if (timeout) clearTimeout(timeout);
      logStream.end();
      resolve({ code: code ?? 1, signal });
    });
  });
}

function buildCodexArgs(opts: {
  repoRoot: string;
  prompt: string;
  model?: string;
  sandbox?: string;
  fullAuto: boolean;
  extraFlags?: string[];
}): string[] {
  const args: string[] = ["exec", "-C", opts.repoRoot];
  if (opts.model) args.push("-m", opts.model);
  if (opts.fullAuto) args.push("--full-auto");
  if (opts.sandbox) args.push("--sandbox", opts.sandbox);
  if (opts.extraFlags?.length) args.push(...opts.extraFlags);
  args.push(opts.prompt);
  return args;
}

function manifestSchemaText(): string {
  return `Manifest location: .autopilot/manifest.json
Manifest must be valid JSON and conform to:
{
  "manifestVersion": 1,
  "project": { "name": string, "description": string },
  "stack": { "language"?: string, "runtime"?: string, "packageManager"?: string, "framework"?: string }?,
  "commands": {
    "bootstrap"?: [{ "cmd": string, "args"?: string[], "cwd"?: string, "env"?: { [k: string]: string }, "timeoutMs"?: number }],
    "build"?:     [CommandSpec],
    "test"?:      [CommandSpec],
    "lint"?:      [CommandSpec],
    "format"?:    [CommandSpec],
    "start"?:     [CommandSpec]
  },
  "artifacts"?: [{ "path": string, "description": string }]
}`;
}

function changeSetSchemaText(): string {
  return `RESPONSE FORMAT (JSON ONLY; no markdown, no backticks):
{
  "changesetVersion": 1,
  "notes"?: string,
  "files": [
    { "path": string, "contents": string, "executable"?: boolean }
  ]
}

Rules:
- Output MUST be valid JSON and nothing else.
- All file paths MUST be repo-relative (no leading "/" and no "..").
- Always include ".autopilot/manifest.json" in files.`;
}

function buildScaffoldPrompt(specText: string): string {
  return [
    "You are generating a brand-new project from scratch with ZERO human intervention.",
    "",
    "AUTHORITATIVE INPUT (spec):",
    specText.trim(),
    "",
    "HARD CONSTRAINTS:",
    "- Do NOT ask questions; make reasonable defaults and record them in README.",
    "- Prefer deterministic tooling (lockfiles, pinned versions where feasible).",
    "- Follow the repository security policy: no secrets, no destructive commands, no writes outside the repo, no hidden network/exfiltration commands.",
    "- Create a minimal but complete project that builds and has at least one automated test.",
    "- The user MUST be able to try it visually with one command: `bash scripts/dev`.",
    "- CI MUST exist at .github/workflows/ci.yml and run: `bash scripts/ci`.",
    "- Create the manifest described below (exact path + valid JSON).",
    "- If you must choose a stack, prefer a widely-supported default for the spec.",
    "- Do NOT modify the autopilot infrastructure (keep mcp/conductor/** and scripts/autopilot unchanged).",
    "",
    manifestSchemaText(),
    "",
    "OUTPUT REQUIREMENTS:",
    "- Ensure the repo contains clear build/test instructions in README.",
    "- Ensure .autopilot/manifest.json exists and matches the schema.",
  ].join("\n");
}

function buildLmStudioScaffoldPrompt(specText: string): string {
  return [
    "You are generating a brand-new project from scratch with ZERO human intervention.",
    "",
    "AUTHORITATIVE INPUT (spec):",
    specText.trim(),
    "",
    "HARD CONSTRAINTS:",
    "- Do NOT ask questions; make reasonable defaults and record them in README.",
    "- Prefer deterministic tooling (lockfiles, pinned versions where feasible).",
    "- Follow the repository security policy: no secrets, no destructive commands, no writes outside the repo, no hidden network/exfiltration commands.",
    "- Create a minimal but complete project that builds and has at least one automated test.",
    "- Create .autopilot/manifest.json matching the schema below (valid JSON).",
    "- Do NOT modify the autopilot runner itself (keep mcp/conductor/src/autopilot* and scripts/autopilot unchanged).",
    "",
    manifestSchemaText(),
    "",
    changeSetSchemaText(),
  ].join("\n");
}

function buildFixPrompt(params: {
  specText: string;
  manifestText: string;
  failingPhase: string;
  failingCommand: CommandSpec;
  tail: string;
}): string {
  const { failingCommand } = params;
  return [
    "Fix the project so validation passes with ZERO human intervention.",
    "",
    "AUTHORITATIVE INPUT (spec):",
    params.specText.trim(),
    "",
    "CURRENT MANIFEST (.autopilot/manifest.json):",
    params.manifestText.trim(),
    "",
    `FAILING PHASE: ${params.failingPhase}`,
    `FAILING COMMAND: ${failingCommand.cmd} ${(failingCommand.args || []).join(" ")}`.trim(),
    "",
    "FAILURE OUTPUT (tail):",
    params.tail.trim() || "(no output captured)",
    "",
    "HARD CONSTRAINTS:",
    "- Do NOT ask questions.",
    "- Make the smallest change that fixes the failure.",
    "- If the manifest commands are wrong, fix .autopilot/manifest.json to match the correct commands.",
    "- If this is a security-policy failure, replace unsafe commands/env/logging with policy-compliant alternatives.",
    "- Do NOT modify the autopilot infrastructure (keep mcp/conductor/** and scripts/autopilot unchanged).",
    "",
    manifestSchemaText(),
  ].join("\n");
}

function buildLmStudioFixPrompt(params: {
  specText: string;
  manifestText: string;
  failingPhase: string;
  failingCommand: CommandSpec;
  tail: string;
  relatedFiles?: Array<{ path: string; contents: string }>;
}): string {
  const related =
    params.relatedFiles && params.relatedFiles.length > 0
      ? [
          "",
          "RELATED FILES (read-only context):",
          ...params.relatedFiles.flatMap((f) => [
            `--- ${f.path} ---`,
            f.contents.trimEnd(),
            `--- end ${f.path} ---`,
            "",
          ]),
        ].join("\n")
      : "";

  return [
    "Fix the project so validation passes with ZERO human intervention.",
    "",
    "AUTHORITATIVE INPUT (spec):",
    params.specText.trim(),
    "",
    "CURRENT MANIFEST (.autopilot/manifest.json):",
    params.manifestText.trim(),
    "",
    `FAILING PHASE: ${params.failingPhase}`,
    `FAILING COMMAND: ${params.failingCommand.cmd} ${(params.failingCommand.args || []).join(" ")}`.trim(),
    "",
    "FAILURE OUTPUT (tail):",
    params.tail.trim() || "(no output captured)",
    related,
    "",
    "HARD CONSTRAINTS:",
    "- Do NOT ask questions.",
    "- Make the smallest change that fixes the failure.",
    "- If the manifest commands are wrong, update .autopilot/manifest.json to match the correct commands.",
    "- If this is a security-policy failure, replace unsafe commands/env/logging with policy-compliant alternatives.",
    "- Do NOT modify the autopilot runner itself (keep mcp/conductor/src/autopilot* and scripts/autopilot unchanged).",
    "",
    manifestSchemaText(),
    "",
    changeSetSchemaText(),
  ].join("\n");
}

async function loadAndValidateManifest(manifestPath: string): Promise<AutopilotManifest> {
  const raw = await readText(manifestPath);
  const json = JSON.parse(raw) as unknown;
  const manifest = AutopilotManifestSchema.parse(json);
  const testCount = manifest.commands.test?.length ?? 0;
  if (testCount === 0) {
    throw new Error(
      'Invalid manifest: commands.test must contain at least one CommandSpec (autopilot requires a test command).'
    );
  }
  return manifest;
}

function extractJsonObject(text: string): unknown {
  const trimmed = text.trim();
  try {
    return JSON.parse(trimmed);
  } catch {}

  const first = trimmed.indexOf("{");
  const last = trimmed.lastIndexOf("}");
  if (first === -1 || last === -1 || last <= first) {
    throw new Error("Expected JSON object in LM Studio response but could not locate '{...}'.");
  }
  const candidate = trimmed.slice(first, last + 1);
  return JSON.parse(candidate);
}

function safeResolveRepoPath(repoRoot: string, relPath: string): string {
  const normalized = relPath.replace(/\\/g, "/").replace(/^\/+/, "");
  if (!normalized || path.isAbsolute(normalized)) {
    throw new Error(`Refusing to write absolute/empty path: "${relPath}"`);
  }
  const abs = path.resolve(repoRoot, normalized);
  const rootWithSep = repoRoot.endsWith(path.sep) ? repoRoot : repoRoot + path.sep;
  if (abs !== repoRoot && !abs.startsWith(rootWithSep)) {
    throw new Error(`Refusing to write outside repo root: "${relPath}"`);
  }
  return abs;
}

async function applyChangeSet(repoRoot: string, changeSet: AutopilotChangeSet, policy: SecurityPolicy): Promise<void> {
  for (const f of changeSet.files) {
    const rel = f.path.replace(/\\/g, "/").replace(/^\/+/, "");
    if (
      rel === "scripts/autopilot" ||
      rel === "mcp/conductor/src/autopilot-cli.ts" ||
      rel.startsWith("mcp/conductor/src/autopilot/")
    ) {
      throw new Error(`Refusing to modify autopilot infrastructure file: "${f.path}"`);
    }
    const pathIssues = validateWritePathSecurity({ repoRoot, policy, relPath: f.path });
    if (pathIssues.length > 0) {
      throw new Error(formatSecurityIssues(pathIssues));
    }
    const absPath = safeResolveRepoPath(repoRoot, f.path);
    await atomicWriteFile(absPath, f.contents);
    if (f.executable) {
      await fs.chmod(absPath, 0o755);
    }
  }
}

async function buildRelatedFileContext(repoRoot: string, text: string): Promise<Array<{ path: string; contents: string }>> {
  const candidates = new Set<string>();
  const patterns: RegExp[] = [
    /-->\s+([^\s:]+):\d+:\d+/g, // rustc
    /File\s+"([^"]+)",\s+line\s+\d+/g, // python
    /(^|\s)([A-Za-z0-9_.\/-]+\.[A-Za-z0-9]+):\d+:\d+/g, // ts/jest/etc
  ];

  for (const re of patterns) {
    let m: RegExpExecArray | null;
    // eslint-disable-next-line no-cond-assign
    while ((m = re.exec(text)) !== null) {
      const p = m[1] || m[2];
      if (typeof p === "string" && p.trim()) candidates.add(p.trim());
      if (candidates.size >= 6) break;
    }
    if (candidates.size >= 6) break;
  }

  const out: Array<{ path: string; contents: string }> = [];
  for (const p of Array.from(candidates)) {
    let absPath: string;
    try {
      absPath = path.isAbsolute(p) ? p : path.join(repoRoot, p);
      absPath = path.resolve(absPath);
      const rootWithSep = repoRoot.endsWith(path.sep) ? repoRoot : repoRoot + path.sep;
      if (absPath !== repoRoot && !absPath.startsWith(rootWithSep)) continue;
      const stat = await fs.stat(absPath).catch(() => null);
      if (!stat || !stat.isFile()) continue;
    } catch {
      continue;
    }

    const rel = path.relative(repoRoot, absPath).replace(/\\/g, "/");
    const content = await readText(absPath).catch(() => "");
    if (!content) continue;
    out.push({ path: rel, contents: content.slice(0, 12_000) });
    if (out.length >= 4) break;
  }
  return out;
}

async function runCommandPhase(params: {
  repoRoot: string;
  phase: string;
  commands: CommandSpec[];
  runDir: string;
  policy: SecurityPolicy;
}): Promise<{ ok: true } | { ok: false; failingCommand: CommandSpec; logPath: string }> {
  for (let i = 0; i < params.commands.length; i++) {
    const command = params.commands[i];
    const logPath = path.join(params.runDir, `${params.phase}.${String(i + 1).padStart(2, "0")}.log`);
    const securityIssues = validateCommandSecurity({
      repoRoot: params.repoRoot,
      policy: params.policy,
      phase: params.phase,
      command,
    });
    if (securityIssues.length > 0) {
      await atomicWriteFile(logPath, formatSecurityIssues(securityIssues) + "\n");
      await atomicWriteFile(
        path.join(params.runDir, `${params.phase}.${String(i + 1).padStart(2, "0")}.meta.json`),
        JSON.stringify({ cmd: command.cmd, args: command.args ?? [], securityIssues }, null, 2)
      );
      return { ok: false, failingCommand: command, logPath };
    }

    const cwd = resolveCommandCwd(params.repoRoot, command);
    const env = buildPolicyEnv(params.policy, process.env, command.env);
    const args = command.args ?? [];
    const { code, signal } = await spawnLogged(command.cmd, args, {
      cwd,
      env,
      logPath,
      timeoutMs: command.timeoutMs,
      policy: params.policy,
    });

    if (code !== 0 || signal) {
      await atomicWriteFile(
        path.join(params.runDir, `${params.phase}.${String(i + 1).padStart(2, "0")}.meta.json`),
        JSON.stringify({ cmd: command.cmd, args, cwd, exitCode: code, signal }, null, 2)
      );
      return { ok: false, failingCommand: command, logPath };
    }
  }
  return { ok: true };
}

export async function runAutopilot(config: AutopilotConfig): Promise<AutopilotRunResult> {
  const repoRoot = path.resolve(config.repoRoot);
  const autopilotDir = path.join(repoRoot, ".autopilot");
  const specPath = path.isAbsolute(config.specPath) ? config.specPath : path.join(repoRoot, config.specPath);
  const manifestPath = path.isAbsolute(config.manifestPath)
    ? config.manifestPath
    : path.join(repoRoot, config.manifestPath);
  const runsDir = path.isAbsolute(config.runsDir) ? config.runsDir : path.join(repoRoot, config.runsDir);

  await ensureDir(autopilotDir);
  await ensureDir(runsDir);
  const policy = await loadSecurityPolicy(repoRoot, config.securityPolicyPath);

  const runId = nowRunId();
  const runDir = path.join(runsDir, runId);
  await ensureDir(runDir);

  const summaryPath = path.join(runDir, "summary.json");

  const specText = await readText(specPath).catch((err) => {
    if (isFileNotFound(err)) {
      throw new Error(
        `Missing spec file at ${specPath}. Create it (template: .autopilot/spec.md) and re-run.`
      );
    }
    throw err;
  });

  await atomicWriteJson(path.join(runDir, "inputs.json"), redactAutopilotConfig(config));
  await atomicWriteFile(path.join(runDir, "spec.md"), specText);

  const shouldScaffold =
    config.scaffold === "always" ||
    (config.scaffold === "auto" && !(await fileExists(manifestPath)));

  if (shouldScaffold) {
    if (config.engine === "codex") {
      const scaffoldPrompt = buildScaffoldPrompt(specText);
      await atomicWriteFile(path.join(runDir, "scaffold.codex.prompt.txt"), scaffoldPrompt);

      const codexArgs = buildCodexArgs({
        repoRoot,
        prompt: scaffoldPrompt,
        model: config.codexModel,
        sandbox: config.codexSandbox,
        fullAuto: config.codexFullAuto,
        extraFlags: config.codexExtraFlags,
      });

      await atomicWriteJson(path.join(runDir, "scaffold.codex.command.json"), {
        cmd: "codex",
        args: codexArgs,
        cwd: repoRoot,
      });

      const codexLog = path.join(runDir, "scaffold.codex.log");
      const { code, signal } = await spawnLogged("codex", codexArgs, {
        cwd: repoRoot,
        env: buildPolicyEnv(policy, process.env),
        logPath: codexLog,
        policy,
      });

      if (code !== 0 || signal) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "scaffold",
          engine: "codex",
          exitCode: code,
          signal,
          codexLog,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }
    } else {
      const scaffoldPrompt = buildLmStudioScaffoldPrompt(specText);
      await atomicWriteFile(path.join(runDir, "scaffold.lmstudio.prompt.txt"), scaffoldPrompt);

      const lmConfig: LmStudioConfig = {
        baseUrl: config.lmstudioBaseUrl || "http://localhost:1234/v1",
        model: config.lmstudioModel || "local-model",
        apiKey: config.lmstudioApiKey,
        temperature: config.lmstudioTemperature,
        maxTokens: config.lmstudioMaxTokens,
        timeoutMs: config.lmstudioTimeoutMs,
      };

      const messages = [
        { role: "system" as const, content: "You are an expert software engineer. Follow the user instructions exactly." },
        { role: "user" as const, content: scaffoldPrompt },
      ];

      await atomicWriteJson(path.join(runDir, "scaffold.lmstudio.request.json"), {
        lmConfig: redactLmConfig(lmConfig),
        messages,
      });

      let chat;
      try {
        chat = await lmStudioChatComplete(lmConfig, messages);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "scaffold",
          engine: "lmstudio",
          error: err instanceof Error ? err.message : String(err),
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }

      await atomicWriteJson(path.join(runDir, "scaffold.lmstudio.response.json"), chat.raw);
      await atomicWriteFile(path.join(runDir, "scaffold.lmstudio.response.txt"), chat.content);

      let changeSet: AutopilotChangeSet;
      try {
        const obj = extractJsonObject(chat.content);
        changeSet = AutopilotChangeSetSchema.parse(obj);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "scaffold",
          engine: "lmstudio",
          error: `Failed to parse changeset JSON: ${err instanceof Error ? err.message : String(err)}`,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }

      await atomicWriteJson(path.join(runDir, "scaffold.lmstudio.changeset.json"), changeSet);

      try {
        await applyChangeSet(repoRoot, changeSet, policy);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "scaffold",
          engine: "lmstudio",
          error: `Failed to apply changeset: ${err instanceof Error ? err.message : String(err)}`,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }
    }
  }

  let manifest: AutopilotManifest;
  try {
    manifest = await loadAndValidateManifest(manifestPath);
  } catch (err) {
    await atomicWriteFile(
      summaryPath,
      JSON.stringify(
        {
          ok: false,
          phase: "manifest",
          error: err instanceof Error ? err.message : String(err),
        },
        null,
        2
      )
    );
    return { ok: false, runId, runDir, manifestPath, summaryPath };
  }

  await atomicWriteFile(path.join(runDir, "manifest.snapshot.json"), JSON.stringify(manifest, null, 2));

  const phases: Array<keyof AutopilotManifest["commands"]> = [
    "bootstrap",
    "format",
    "lint",
    "test",
    "build",
  ];

  const attempts: Array<Record<string, unknown>> = [];
  for (let attempt = 0; attempt <= config.maxFixAttempts; attempt++) {
    const attemptId = String(attempt + 1).padStart(2, "0");
    let failing:
      | { phase: string; failingCommand: CommandSpec; logPath: string }
      | null = null;

    for (const phase of phases) {
      const cmds = manifest.commands[phase] ?? [];
      if (cmds.length === 0) continue;
      const result = await runCommandPhase({ repoRoot, phase, commands: cmds, runDir, policy });
      if (!result.ok) {
        failing = { phase, failingCommand: result.failingCommand, logPath: result.logPath };
        break;
      }
    }

    if (!failing) {
      const readinessFailures = await collectReadinessFailures({
        repoRoot,
        manifest,
        requireStartCommand: config.requireStartCommand,
        requireCiWorkflow: config.requireCiWorkflow,
      });
      if (readinessFailures.length > 0) {
        const readinessLogPath = path.join(runDir, `readiness.${attemptId}.log`);
        await atomicWriteFile(readinessLogPath, readinessFailures.join("\n") + "\n");
        failing = {
          phase: "readiness",
          failingCommand: { cmd: "readiness-check", args: [] },
          logPath: readinessLogPath,
        };
      } else if (config.requireStartCommand) {
        const startCheck = await runStartCommandSanityCheck({ repoRoot, manifest, runDir, attemptId, policy });
        if (!startCheck.ok) {
          failing = {
            phase: "start",
            failingCommand: { cmd: "start-check", args: [] },
            logPath: startCheck.logPath,
          };
        }
      }
    }

    if (!failing) {
      const securityIssues = await runSecurityCompletionGates({ repoRoot, policy, manifest });
      if (securityIssues.length > 0) {
        const securityLogPath = path.join(runDir, `security.${attemptId}.log`);
        await atomicWriteFile(securityLogPath, formatSecurityIssues(securityIssues) + "\n");
        failing = {
          phase: "security",
          failingCommand: { cmd: "security-check", args: [] },
          logPath: securityLogPath,
        };
      }
    }

    if (!failing) {
      await atomicWriteFile(summaryPath, JSON.stringify({ ok: true, runId, manifestPath, attempts }, null, 2));
      return { ok: true, runId, runDir, manifestPath, summaryPath };
    }

    attempts.push({
      attempt,
      failingPhase: failing.phase,
      failingCommand: failing.failingCommand,
      logPath: failing.logPath,
    });

    if (attempt >= config.maxFixAttempts) {
      await atomicWriteFile(
        summaryPath,
        JSON.stringify(
          {
            ok: false,
            runId,
            manifestPath,
            attempts,
            lastFailure: {
              phase: failing.phase,
              command: failing.failingCommand,
              logTail: await readTail(failing.logPath, 200),
            },
          },
          null,
          2
        )
      );
      return { ok: false, runId, runDir, manifestPath, summaryPath };
    }

    const manifestText = await readText(manifestPath).catch(() => "");
    const tail = await readTail(failing.logPath, 200);

    if (config.engine === "codex") {
      const fixPrompt = buildFixPrompt({
        specText,
        manifestText,
        failingPhase: failing.phase,
        failingCommand: failing.failingCommand,
        tail,
      });

      await atomicWriteFile(path.join(runDir, `fix.${attemptId}.codex.prompt.txt`), fixPrompt);

      const codexArgs = buildCodexArgs({
        repoRoot,
        prompt: fixPrompt,
        model: config.codexModel,
        sandbox: config.codexSandbox,
        fullAuto: config.codexFullAuto,
        extraFlags: config.codexExtraFlags,
      });

      await atomicWriteJson(path.join(runDir, `fix.${attemptId}.codex.command.json`), {
        cmd: "codex",
        args: codexArgs,
        cwd: repoRoot,
      });

      const codexLog = path.join(runDir, `fix.${attemptId}.codex.log`);
      const { code, signal } = await spawnLogged("codex", codexArgs, {
        cwd: repoRoot,
        env: buildPolicyEnv(policy, process.env),
        logPath: codexLog,
        policy,
      });

      if (code !== 0 || signal) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          runId,
          manifestPath,
          phase: "fix",
          engine: "codex",
          attempt,
          exitCode: code,
          signal,
          codexLog,
          attempts,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }
    } else {
      const relatedFiles = await buildRelatedFileContext(repoRoot, tail);
      const fixPrompt = buildLmStudioFixPrompt({
        specText,
        manifestText,
        failingPhase: failing.phase,
        failingCommand: failing.failingCommand,
        tail,
        relatedFiles,
      });

      await atomicWriteFile(path.join(runDir, `fix.${attemptId}.lmstudio.prompt.txt`), fixPrompt);

      const lmConfig: LmStudioConfig = {
        baseUrl: config.lmstudioBaseUrl || "http://localhost:1234/v1",
        model: config.lmstudioModel || "local-model",
        apiKey: config.lmstudioApiKey,
        temperature: config.lmstudioTemperature,
        maxTokens: config.lmstudioMaxTokens,
        timeoutMs: config.lmstudioTimeoutMs,
      };

      const messages = [
        { role: "system" as const, content: "You are an expert software engineer. Follow the user instructions exactly." },
        { role: "user" as const, content: fixPrompt },
      ];

      await atomicWriteJson(path.join(runDir, `fix.${attemptId}.lmstudio.request.json`), {
        lmConfig: redactLmConfig(lmConfig),
        messages,
      });

      let chat;
      try {
        chat = await lmStudioChatComplete(lmConfig, messages);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "fix",
          engine: "lmstudio",
          attempt,
          error: err instanceof Error ? err.message : String(err),
          attempts,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }

      await atomicWriteJson(path.join(runDir, `fix.${attemptId}.lmstudio.response.json`), chat.raw);
      await atomicWriteFile(path.join(runDir, `fix.${attemptId}.lmstudio.response.txt`), chat.content);

      let changeSet: AutopilotChangeSet;
      try {
        const obj = extractJsonObject(chat.content);
        changeSet = AutopilotChangeSetSchema.parse(obj);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "fix",
          engine: "lmstudio",
          attempt,
          error: `Failed to parse changeset JSON: ${err instanceof Error ? err.message : String(err)}`,
          attempts,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }

      await atomicWriteJson(path.join(runDir, `fix.${attemptId}.lmstudio.changeset.json`), changeSet);

      try {
        await applyChangeSet(repoRoot, changeSet, policy);
      } catch (err) {
        await atomicWriteJson(summaryPath, {
          ok: false,
          phase: "fix",
          engine: "lmstudio",
          attempt,
          error: `Failed to apply changeset: ${err instanceof Error ? err.message : String(err)}`,
          attempts,
        });
        return { ok: false, runId, runDir, manifestPath, summaryPath };
      }
    }

    // Reload manifest in case it changed.
    try {
      manifest = await loadAndValidateManifest(manifestPath);
      await atomicWriteJson(path.join(runDir, `manifest.snapshot.after-fix.${attemptId}.json`), manifest);
    } catch (err) {
      await atomicWriteJson(summaryPath, {
        ok: false,
        phase: "manifest",
        stage: "after_fix",
        attempt,
        error: err instanceof Error ? err.message : String(err),
        attempts,
      });
      return { ok: false, runId, runDir, manifestPath, summaryPath };
    }
  }

  await atomicWriteFile(summaryPath, JSON.stringify({ ok: false, runId, manifestPath }, null, 2));
  return { ok: false, runId, runDir, manifestPath, summaryPath };
}
