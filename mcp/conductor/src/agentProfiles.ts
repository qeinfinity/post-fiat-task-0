import * as fs from "node:fs/promises";
import * as path from "node:path";
import { z } from "zod";
import { buildCodexCommand, shellQuote } from "./codex.js";

const AgentProfileSchema = z.object({
  kind: z.enum(["codex", "generic"]),
  command: z.string().min(1),
  tabPrefix: z.string().default("[Agent]"),
  baseArgs: z.array(z.string()).default([]),
  promptPlacement: z.enum(["positional"]).default("positional"),
  cwdFlag: z.string().nullable().default(null),
  allowFullAuto: z.boolean().default(false),
  defaultFullAuto: z.boolean().default(false),
  defaultSandbox: z.string().optional(),
  allowedFlags: z.array(z.string()).default([]),
  allowedSandboxes: z.array(z.string()).default([]),
  idlePattern: z.string().optional(),
});

const AgentProfileRegistrySchema = z.object({
  profileVersion: z.literal(1),
  defaultProfile: z.string().min(1),
  profiles: z.record(AgentProfileSchema),
});

export type AgentProfile = z.infer<typeof AgentProfileSchema>;
export type AgentProfileRegistry = z.infer<typeof AgentProfileRegistrySchema>;

export interface DispatchOptions {
  prompt: string;
  worktreePath: string;
  mode: "interactive" | "exec";
  model?: string;
  fullAuto?: boolean;
  sandbox?: string;
  extraFlags?: string[];
}

const DEFAULT_REGISTRY: AgentProfileRegistry = AgentProfileRegistrySchema.parse({
  profileVersion: 1,
  defaultProfile: "codex-standard",
  profiles: {
    "codex-standard": {
      kind: "codex",
      command: "codex",
      tabPrefix: "[Codex]",
      allowFullAuto: false,
      defaultFullAuto: false,
      defaultSandbox: "workspace-write",
      allowedFlags: ["-m", "--sandbox"],
      allowedSandboxes: ["read-only", "workspace-write"],
      idlePattern: "\\?\\s+for shortcuts\\s+\\d+%\\s+context left",
    },
  },
});

function isFileNotFound(err: unknown): boolean {
  return err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT";
}

export async function loadAgentProfiles(repoRoot: string, profilePath?: string): Promise<AgentProfileRegistry> {
  const resolved = path.resolve(repoRoot, profilePath ?? ".agents/agent-profiles.json");
  try {
    const raw = await fs.readFile(resolved, "utf8");
    return AgentProfileRegistrySchema.parse(JSON.parse(raw));
  } catch (err) {
    if (isFileNotFound(err)) return DEFAULT_REGISTRY;
    throw err;
  }
}

export function getProfile(registry: AgentProfileRegistry, profileId?: string): { id: string; profile: AgentProfile } {
  const id = profileId || registry.defaultProfile;
  const profile = registry.profiles[id];
  if (!profile) {
    throw new Error(`Unknown agent profile "${id}". Available profiles: ${Object.keys(registry.profiles).join(", ")}`);
  }
  return { id, profile };
}

function validateExtraFlags(profile: AgentProfile, extraFlags: string[] | undefined): void {
  for (const token of extraFlags ?? []) {
    if (!token.startsWith("-")) continue;
    if (!profile.allowedFlags.includes(token)) {
      throw new Error(`Flag "${token}" is not allowed by selected agent profile`);
    }
  }
}

export function normalizedDispatchOptions(profile: AgentProfile, opts: DispatchOptions): DispatchOptions {
  const fullAuto = opts.fullAuto ?? profile.defaultFullAuto;
  if (fullAuto && !profile.allowFullAuto) {
    throw new Error("full_auto was requested but the selected agent profile does not allow it");
  }

  const sandbox = opts.sandbox ?? profile.defaultSandbox;
  if (sandbox && profile.allowedSandboxes.length > 0 && !profile.allowedSandboxes.includes(sandbox)) {
    throw new Error(`Sandbox "${sandbox}" is not allowed by selected agent profile`);
  }

  validateExtraFlags(profile, opts.extraFlags);
  return { ...opts, fullAuto, sandbox };
}

export function buildAgentCommand(profile: AgentProfile, opts: DispatchOptions): string {
  const normalized = normalizedDispatchOptions(profile, opts);

  if (profile.kind === "codex") {
    return buildCodexCommand({
      command: profile.command,
      prompt: normalized.prompt,
      worktreePath: normalized.worktreePath,
      mode: normalized.mode,
      model: normalized.model,
      fullAuto: normalized.fullAuto,
      sandbox: normalized.sandbox,
      extraFlags: normalized.extraFlags,
    });
  }

  const parts = [`cd ${shellQuote(normalized.worktreePath)}`];
  const commandParts = [shellQuote(profile.command), ...profile.baseArgs.map(shellQuote)];
  if (profile.cwdFlag) {
    commandParts.push(shellQuote(profile.cwdFlag), shellQuote(normalized.worktreePath));
  }
  if (normalized.extraFlags) {
    commandParts.push(...normalized.extraFlags.map(shellQuote));
  }
  commandParts.push(shellQuote(normalized.prompt));
  parts.push(commandParts.join(" "));
  return parts.join(" && ");
}
