import * as path from "node:path";
import { runAutopilot, type AutopilotConfig } from "./autopilot/runner.js";

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

function getBool(args: Record<string, string | boolean>, key: string, fallback: boolean): boolean {
  const v = args[key];
  if (typeof v === "boolean") return true;
  if (typeof v === "string") {
    const s = v.trim().toLowerCase();
    if (["1", "true", "yes", "y", "on"].includes(s)) return true;
    if (["0", "false", "no", "n", "off"].includes(s)) return false;
  }
  return fallback;
}

function abs(repoRoot: string, p: string): string {
  return path.isAbsolute(p) ? p : path.join(repoRoot, p);
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  const repoRoot = path.resolve(getString(args, "repo-root", process.cwd())!);

  const specPath = abs(repoRoot, getString(args, "spec", ".autopilot/spec.md")!);
  const manifestPath = abs(repoRoot, getString(args, "manifest", ".autopilot/manifest.json")!);
  const runsDir = abs(repoRoot, getString(args, "runs-dir", ".autopilot/runs")!);

  const envEngine = process.env.AUTOPILOT_ENGINE;
  const engineFallback = envEngine === "lmstudio" || envEngine === "codex" ? envEngine : "codex";

  const config: AutopilotConfig = {
    repoRoot,
    specPath,
    manifestPath,
    runsDir,
    engine: getEnum(args, "engine", ["codex", "lmstudio"] as const, engineFallback),
    codexModel: getString(args, "model", process.env.AUTOPILOT_CODEX_MODEL),
    codexSandbox: getString(args, "sandbox", process.env.AUTOPILOT_CODEX_SANDBOX || "workspace-write"),
    codexFullAuto: getEnum(args, "full-auto", ["true", "false"] as const, "true") === "true",
    codexExtraFlags: getString(args, "extra-flags", process.env.AUTOPILOT_CODEX_EXTRA_FLAGS)
      ?.split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    lmstudioBaseUrl: getString(args, "lmstudio-base-url", process.env.LMSTUDIO_BASE_URL),
    lmstudioModel: getString(args, "lmstudio-model", process.env.LMSTUDIO_MODEL),
    lmstudioApiKey: getString(args, "lmstudio-api-key", process.env.LMSTUDIO_API_KEY),
    lmstudioTemperature: getNumberOptional(args, "lmstudio-temperature"),
    lmstudioMaxTokens: getNumberOptional(args, "lmstudio-max-tokens"),
    lmstudioTimeoutMs: getNumberOptional(args, "lmstudio-timeout-ms"),
    scaffold: getEnum(args, "scaffold", ["auto", "always", "never"] as const, "auto"),
    maxFixAttempts: getNumber(args, "max-fix-attempts", 3),
    requireStartCommand: getBool(args, "require-start", true),
    requireCiWorkflow: getBool(args, "require-ci", true),
    securityPolicyPath: getString(args, "security-policy", process.env.AUTOPILOT_SECURITY_POLICY),
  };

  const result = await runAutopilot(config);
  // eslint-disable-next-line no-console
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.ok ? 0 : 1);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(2);
});
