import * as path from "node:path";
import * as fs from "node:fs/promises";
import * as readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { generateQuestionnaire, generateSpecFromAnswers, type IntakeAnswers } from "./autopilot/intake.js";
import { runAutopilot, type AutopilotConfig } from "./autopilot/runner.js";
import {
  loadModelProviderRegistry,
  redactProviderRuntime,
  resolveModelProvider,
  type ChatProvider,
} from "./modelProviders.js";
import { loadSecurityPolicy } from "./security.js";

function nowSessionId(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}-${pad(
    d.getUTCHours()
  )}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
}

function readArgValue(argv: string[], i: number): { value: string | null; nextIndex: number } {
  const a = argv[i];
  const eq = a.indexOf("=");
  if (eq !== -1) return { value: a.slice(eq + 1), nextIndex: i };
  const next = argv[i + 1];
  if (!next || next.startsWith("-")) return { value: null, nextIndex: i };
  return { value: next, nextIndex: i + 1 };
}

function parseArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const key = a.replace(/^--/, "").split("=")[0];
    const { value, nextIndex } = readArgValue(argv, i);
    if (value === null) {
      out[key] = true;
    } else {
      out[key] = value;
      i = nextIndex;
    }
  }
  return out;
}

function getString(
  args: Record<string, string | boolean>,
  key: string,
  fallback?: string
): string | undefined {
  const v = args[key];
  if (typeof v === "string") return v;
  return fallback;
}

function firstString(...values: Array<string | undefined>): string | undefined {
  return values.find((value) => typeof value === "string" && value.trim().length > 0);
}

function getNumber(
  args: Record<string, string | boolean>,
  key: string,
  fallback: number
): number {
  const v = args[key];
  if (typeof v === "string") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}

function getNumberOptional(args: Record<string, string | boolean>, key: string): number | undefined {
  const v = args[key];
  if (typeof v === "string") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return undefined;
}

function getEnum<T extends string>(
  args: Record<string, string | boolean>,
  key: string,
  allowed: readonly T[],
  fallback: T
): T {
  const v = args[key];
  if (typeof v === "string" && (allowed as readonly string[]).includes(v)) return v as T;
  return fallback;
}

async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

async function atomicWriteFile(filePath: string, contents: string): Promise<void> {
  await ensureDir(path.dirname(filePath));
  const tmp = `${filePath}.${Math.random().toString(16).slice(2)}.tmp`;
  await fs.writeFile(tmp, contents, "utf8");
  await fs.rename(tmp, filePath);
}

async function atomicWriteJson(filePath: string, value: unknown): Promise<void> {
  await atomicWriteFile(filePath, JSON.stringify(value, null, 2));
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function abs(repoRoot: string, p: string): string {
  return path.isAbsolute(p) ? p : path.join(repoRoot, p);
}

async function interactiveIntake(params: {
  repoRoot: string;
  sessionDir: string;
  provider: ChatProvider;
}): Promise<{ specMarkdown: string; maxFixAttempts: number }> {
  const rl = readline.createInterface({ input, output });
  try {
    const seedIdea = (await rl.question("What do you want to build? (1-3 sentences)\n> ")).trim();

    const { questionnaire, raw: qRaw, prompt: qPrompt } = await generateQuestionnaire(params.provider, seedIdea);
    await atomicWriteJson(path.join(params.sessionDir, "intake.questionnaire.json"), questionnaire);
    await atomicWriteJson(
      path.join(params.sessionDir, "intake.questionnaire.response.json"),
      params.provider.profile.allowResponseLogging ? qRaw : { redacted: true, provider: params.provider.id }
    );
    await atomicWriteFile(
      path.join(params.sessionDir, "intake.questionnaire.prompt.txt"),
      params.provider.profile.allowPromptLogging ? qPrompt : `Prompt redacted for provider ${params.provider.id}\n`
    );

    // Ask questions locally (model authored them, but we keep IO deterministic).
    const answers: IntakeAnswers = {};
    for (const q of questionnaire.questions) {
      const header = `\n[${q.id}] ${q.question}`;
      const choices =
        q.choices && q.choices.length > 0
          ? `\nChoices: ${q.choices.map((c) => `"${c}"`).join(", ")}`
          : "";
      const prompt = `${header}${choices}\n> `;

      if (q.type === "boolean") {
        const a = (await rl.question(prompt)).trim().toLowerCase();
        answers[q.id] = a === "y" || a === "yes" || a === "true" || a === "1";
      } else if (q.type === "multi_choice") {
        const a = (await rl.question(prompt)).trim();
        answers[q.id] = a
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean);
      } else {
        answers[q.id] = (await rl.question(prompt)).trim();
      }
    }
    await atomicWriteJson(path.join(params.sessionDir, "intake.answers.json"), answers);

    const { intake, raw: sRaw, prompt: sPrompt } = await generateSpecFromAnswers({
      provider: params.provider,
      seedIdea,
      questionnaire,
      answers,
    });
    await atomicWriteJson(path.join(params.sessionDir, "intake.output.json"), intake);
    await atomicWriteJson(
      path.join(params.sessionDir, "intake.output.response.json"),
      params.provider.profile.allowResponseLogging ? sRaw : { redacted: true, provider: params.provider.id }
    );
    await atomicWriteFile(
      path.join(params.sessionDir, "intake.output.prompt.txt"),
      params.provider.profile.allowPromptLogging ? sPrompt : `Prompt redacted for provider ${params.provider.id}\n`
    );

    const specPath = path.join(params.repoRoot, ".autopilot", "spec.md");
    await atomicWriteFile(specPath, intake.specMarkdown);
    await atomicWriteJson(path.join(params.repoRoot, ".autopilot", "settings.json"), intake.autopilot);

    return { specMarkdown: intake.specMarkdown, maxFixAttempts: intake.autopilot.maxFixAttempts };
  } finally {
    rl.close();
  }
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  const repoRoot = path.resolve(getString(args, "repo-root", process.cwd())!);
  const intakeEnabled = getEnum(args, "intake", ["true", "false"] as const, "true") === "true";

  const sessionId = nowSessionId();
  const sessionDir = path.join(repoRoot, ".autopilot", "sessions", sessionId);
  await ensureDir(sessionDir);
  await atomicWriteJson(path.join(sessionDir, "session.json"), {
    sessionId,
    startedAt: new Date().toISOString(),
  });

  let maxFixAttempts = getNumber(args, "max-fix-attempts", 10);

  if (intakeEnabled) {
    const securityPolicy = await loadSecurityPolicy(repoRoot, getString(args, "security-policy", process.env.AUTOPILOT_SECURITY_POLICY));
    const providerRegistry = await loadModelProviderRegistry(repoRoot, getString(args, "model-providers", process.env.AUTOPILOT_MODEL_PROVIDERS));
    const deprecatedLmStudioFlag = Object.keys(args).find((key) => key.startsWith("lmstudio-"));
    if (deprecatedLmStudioFlag) {
      throw new Error(
        `--${deprecatedLmStudioFlag} is no longer accepted for intake. Use --intake-provider, --intake-model, and --intake-base-url.`
      );
    }
    const dataClassEnv = process.env.AUTOPILOT_INTAKE_DATA_CLASS;
    const defaultDataClass: "public" | "internal" | "sensitive" | "restricted" =
      dataClassEnv === "public" || dataClassEnv === "internal" || dataClassEnv === "sensitive" || dataClassEnv === "restricted"
        ? dataClassEnv
        : "internal";
    const providerId = firstString(getString(args, "intake-provider"), process.env.AUTOPILOT_INTAKE_PROVIDER);
    const provider = resolveModelProvider({
      registry: providerRegistry,
      securityPolicy,
      overrides: {
        providerId,
        baseUrl: firstString(getString(args, "intake-base-url"), process.env.AUTOPILOT_INTAKE_BASE_URL),
        model: firstString(getString(args, "intake-model"), process.env.AUTOPILOT_INTAKE_MODEL),
        apiKeyEnv: getString(args, "intake-api-key-env", process.env.AUTOPILOT_INTAKE_API_KEY_ENV),
        promptDataClass: getEnum(
          args,
          "intake-data-class",
          ["public", "internal", "sensitive", "restricted"] as const,
          defaultDataClass
        ),
        temperature: getNumberOptional(args, "intake-temperature"),
        maxTokens: getNumberOptional(args, "intake-max-tokens"),
        timeoutMs: getNumberOptional(args, "intake-timeout-ms"),
        allowCloud:
          getEnum(
            args,
            "allow-cloud-intake",
            ["true", "false"] as const,
            process.env.AUTOPILOT_ALLOW_CLOUD_INTAKE === "true" ? "true" : "false"
          ) === "true",
      },
    });
    await atomicWriteJson(path.join(sessionDir, "intake.provider.json"), redactProviderRuntime(provider, securityPolicy));
    const { maxFixAttempts: fromIntake } = await interactiveIntake({ repoRoot, sessionDir, provider });
    maxFixAttempts = fromIntake;
  } else {
    const specPath = path.join(repoRoot, ".autopilot", "spec.md");
    if (!(await fileExists(specPath))) {
      throw new Error(`Emergency: missing spec at ${specPath}. Enable intake or create the spec file.`);
    }
  }

  const runsDir = path.join(sessionDir, "runs");

  const config: AutopilotConfig = {
    repoRoot,
    specPath: abs(repoRoot, getString(args, "spec", ".autopilot/spec.md")!),
    manifestPath: abs(repoRoot, getString(args, "manifest", ".autopilot/manifest.json")!),
    runsDir,
    engine: "codex",
    codexModel: getString(args, "model", process.env.AUTOPILOT_CODEX_MODEL),
    codexSandbox: getString(args, "sandbox", process.env.AUTOPILOT_CODEX_SANDBOX || "workspace-write"),
    codexFullAuto: getEnum(args, "full-auto", ["true", "false"] as const, "true") === "true",
    codexExtraFlags: getString(args, "extra-flags", process.env.AUTOPILOT_CODEX_EXTRA_FLAGS)
      ?.split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    scaffold: getEnum(args, "scaffold", ["auto", "always", "never"] as const, "auto"),
    maxFixAttempts,
    requireStartCommand: true,
    requireCiWorkflow: true,
    securityPolicyPath: getString(args, "security-policy", process.env.AUTOPILOT_SECURITY_POLICY),
  };

  const result = await runAutopilot(config);

  if (result.ok) {
    // eslint-disable-next-line no-console
    console.log("READY");
    // eslint-disable-next-line no-console
    console.log('Run: `bash scripts/dev`');
  }

  // eslint-disable-next-line no-console
  console.log(JSON.stringify({ ...result, sessionDir }, null, 2));
  process.exit(result.ok ? 0 : 1);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(2);
});
