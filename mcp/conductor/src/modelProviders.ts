import * as fs from "node:fs/promises";
import * as path from "node:path";
import { z } from "zod";
import { redactText, type SecurityPolicy } from "./security.js";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatResult {
  content: string;
  raw: unknown;
}

export interface ChatProvider {
  readonly id: string;
  readonly profile: ModelProviderProfile;
  readonly runtime: ResolvedModelProvider;
  complete(messages: ChatMessage[]): Promise<ChatResult>;
}

const ModelProviderProfileSchema = z.object({
  kind: z.enum(["openai", "openai-compatible", "claude"]),
  locality: z.enum(["local", "cloud"]),
  defaultBaseUrl: z.string().optional(),
  baseUrlEnv: z.string().optional(),
  modelEnv: z.string().optional(),
  apiKeyEnv: z.string().optional(),
  defaultModel: z.string().optional(),
  anthropicVersion: z.string().optional(),
  requiresExplicitOptIn: z.boolean().default(false),
  allowPromptLogging: z.boolean().default(false),
  allowResponseLogging: z.boolean().default(false),
  defaultTemperature: z.number().optional(),
  defaultMaxTokens: z.number().int().positive().optional(),
  defaultTimeoutMs: z.number().int().positive().optional(),
});

const ModelProviderRegistrySchema = z.object({
  providerVersion: z.literal(1),
  defaultIntakeProvider: z.string().min(1),
  profiles: z.record(ModelProviderProfileSchema),
});

export type ModelProviderProfile = z.infer<typeof ModelProviderProfileSchema>;
export type ModelProviderRegistry = z.infer<typeof ModelProviderRegistrySchema>;

export interface ProviderOverrides {
  providerId?: string;
  baseUrl?: string;
  model?: string;
  apiKeyEnv?: string;
  promptDataClass?: PromptDataClass;
  temperature?: number;
  maxTokens?: number;
  timeoutMs?: number;
  allowCloud?: boolean;
}

export type PromptDataClass = "public" | "internal" | "sensitive" | "restricted";

export interface ResolvedModelProvider {
  id: string;
  kind: ModelProviderProfile["kind"];
  locality: ModelProviderProfile["locality"];
  baseUrl: string;
  model: string;
  apiKey?: string;
  apiKeyEnv?: string;
  anthropicVersion?: string;
  promptDataClass: PromptDataClass;
  temperature?: number;
  maxTokens: number;
  timeoutMs: number;
}

const DEFAULT_REGISTRY: ModelProviderRegistry = ModelProviderRegistrySchema.parse({
  providerVersion: 1,
  defaultIntakeProvider: "omlx-local",
  profiles: {
    "omlx-local": {
      kind: "openai-compatible",
      locality: "local",
      defaultBaseUrl: "http://127.0.0.1:8080/v1",
      baseUrlEnv: "OMLX_BASE_URL",
      modelEnv: "OMLX_MODEL",
      defaultModel: "local-model",
      allowPromptLogging: true,
      allowResponseLogging: true,
      defaultTemperature: 0,
      defaultMaxTokens: 4096,
      defaultTimeoutMs: 120000,
    },
    "openai-cloud": {
      kind: "openai",
      locality: "cloud",
      defaultBaseUrl: "https://api.openai.com/v1",
      baseUrlEnv: "OPENAI_BASE_URL",
      modelEnv: "OPENAI_MODEL",
      apiKeyEnv: "OPENAI_API_KEY",
      requiresExplicitOptIn: true,
      allowPromptLogging: false,
      allowResponseLogging: false,
      defaultMaxTokens: 4096,
      defaultTimeoutMs: 120000,
    },
    "claude-cloud": {
      kind: "claude",
      locality: "cloud",
      defaultBaseUrl: "https://api.anthropic.com",
      baseUrlEnv: "ANTHROPIC_BASE_URL",
      modelEnv: "ANTHROPIC_MODEL",
      apiKeyEnv: "ANTHROPIC_API_KEY",
      anthropicVersion: "2023-06-01",
      requiresExplicitOptIn: true,
      allowPromptLogging: false,
      allowResponseLogging: false,
      defaultMaxTokens: 4096,
      defaultTimeoutMs: 120000,
    },
  },
});

const PROMPT_DATA_CLASS_ORDER: Record<PromptDataClass, number> = {
  public: 0,
  internal: 1,
  sensitive: 2,
  restricted: 3,
};

function isFileNotFound(err: unknown): boolean {
  return err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT";
}

function joinUrl(baseUrl: string, suffix: string): string {
  const b = baseUrl.replace(/\/+$/, "");
  const s = suffix.replace(/^\/+/, "");
  return `${b}/${s}`;
}

function envValue(name?: string): string | undefined {
  if (!name) return undefined;
  const value = process.env[name];
  return value && value.trim().length > 0 ? value : undefined;
}

function apiKeyFromEnv(name?: string): string | undefined {
  const value = envValue(name);
  return value && value.length > 0 ? value : undefined;
}

function extractOpenAiContent(json: unknown): string | null {
  const choice0 = (json as any)?.choices?.[0];
  const messageContent = choice0?.message?.content;
  if (typeof messageContent === "string") return messageContent;
  if (Array.isArray(messageContent)) {
    const text = messageContent
      .map((part: unknown) => {
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return null;
        const p: any = part;
        if (typeof p.text === "string") return p.text;
        if (typeof p.content === "string") return p.content;
        return null;
      })
      .filter((s: unknown): s is string => typeof s === "string" && s.length > 0)
      .join("");
    if (text.length > 0) return text;
  }
  if (typeof choice0?.text === "string") return choice0.text;
  return null;
}

function extractClaudeContent(json: unknown): string | null {
  const content = (json as any)?.content;
  if (!Array.isArray(content)) return null;
  const text = content
    .map((part: unknown) => {
      if (!part || typeof part !== "object") return null;
      const p: any = part;
      return p.type === "text" && typeof p.text === "string" ? p.text : null;
    })
    .filter((s: unknown): s is string => typeof s === "string" && s.length > 0)
    .join("");
  return text.length > 0 ? text : null;
}

function splitClaudeMessages(messages: ChatMessage[]): { system?: string; messages: Array<{ role: "user" | "assistant"; content: string }> } {
  const system = messages
    .filter((m) => m.role === "system")
    .map((m) => m.content)
    .join("\n\n")
    .trim();
  const nonSystem = messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role as "user" | "assistant", content: m.content }));
  return { system: system || undefined, messages: nonSystem };
}

function timeoutSignal(timeoutMs: number): { signal: AbortSignal; clear: () => void } {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

async function postJson(params: {
  url: string;
  headers: Record<string, string>;
  body: unknown;
  timeoutMs: number;
  label: string;
}): Promise<unknown> {
  const { signal, clear } = timeoutSignal(params.timeoutMs);
  try {
    const res = await fetch(params.url, {
      method: "POST",
      headers: { "content-type": "application/json", ...params.headers },
      body: JSON.stringify(params.body),
      signal,
    });
    const text = await res.text();
    let json: unknown;
    try {
      json = JSON.parse(text);
    } catch {
      json = { nonJsonResponse: text };
    }
    if (!res.ok) {
      throw new Error(`${params.label} request failed (${res.status} ${res.statusText}). Response: ${text.slice(0, 2000)}`);
    }
    return json;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`${params.label} request timed out after ${params.timeoutMs}ms.`);
    }
    throw err;
  } finally {
    clear();
  }
}

class ResolvedChatProvider implements ChatProvider {
  constructor(
    readonly id: string,
    readonly profile: ModelProviderProfile,
    readonly runtime: ResolvedModelProvider
  ) {}

  async complete(messages: ChatMessage[]): Promise<ChatResult> {
    if (this.runtime.kind === "claude") return this.completeClaude(messages);
    return this.completeOpenAiChat(messages);
  }

  private async completeOpenAiChat(messages: ChatMessage[]): Promise<ChatResult> {
    const body: Record<string, unknown> = {
      model: this.runtime.model,
      messages,
      stream: false,
    };

    if (this.runtime.kind === "openai") {
      body.max_completion_tokens = this.runtime.maxTokens;
    } else {
      body.max_tokens = this.runtime.maxTokens;
    }
    if (typeof this.runtime.temperature === "number") body.temperature = this.runtime.temperature;

    const json = await postJson({
      url: joinUrl(this.runtime.baseUrl, "/chat/completions"),
      headers: this.runtime.apiKey ? { authorization: `Bearer ${this.runtime.apiKey}` } : {},
      body,
      timeoutMs: this.runtime.timeoutMs,
      label: this.id,
    });

    const content = extractOpenAiContent(json);
    if (!content || content.trim().length === 0) {
      throw new Error(`${this.id} response missing assistant content.`);
    }
    return { content, raw: json };
  }

  private async completeClaude(messages: ChatMessage[]): Promise<ChatResult> {
    if (!this.runtime.apiKey) throw new Error(`${this.id} requires API key env ${this.runtime.apiKeyEnv ?? "<unset>"}.`);
    const split = splitClaudeMessages(messages);
    const body: Record<string, unknown> = {
      model: this.runtime.model,
      max_tokens: this.runtime.maxTokens,
      messages: split.messages,
    };
    if (split.system) body.system = split.system;
    if (typeof this.runtime.temperature === "number") body.temperature = this.runtime.temperature;

    const json = await postJson({
      url: joinUrl(this.runtime.baseUrl, "/v1/messages"),
      headers: {
        "x-api-key": this.runtime.apiKey,
        "anthropic-version": this.runtime.anthropicVersion || "2023-06-01",
      },
      body,
      timeoutMs: this.runtime.timeoutMs,
      label: this.id,
    });

    const content = extractClaudeContent(json);
    if (!content || content.trim().length === 0) {
      throw new Error(`${this.id} response missing text content.`);
    }
    return { content, raw: json };
  }
}

export async function loadModelProviderRegistry(repoRoot: string, providerPath?: string): Promise<ModelProviderRegistry> {
  const resolved = path.resolve(repoRoot, providerPath ?? ".agents/model-providers.json");
  try {
    const raw = await fs.readFile(resolved, "utf8");
    return ModelProviderRegistrySchema.parse(JSON.parse(raw));
  } catch (err) {
    if (isFileNotFound(err)) return DEFAULT_REGISTRY;
    throw err;
  }
}

export function resolveModelProvider(params: {
  registry: ModelProviderRegistry;
  securityPolicy: SecurityPolicy;
  overrides: ProviderOverrides;
}): ChatProvider {
  const id = params.overrides.providerId || params.registry.defaultIntakeProvider;
  const profile = params.registry.profiles[id];
  if (!profile) {
    throw new Error(`Unknown model provider "${id}". Available providers: ${Object.keys(params.registry.profiles).join(", ")}`);
  }

  const allowedProfiles = params.securityPolicy.modelProviders?.allowedProfiles ?? Object.keys(params.registry.profiles);
  if (!allowedProfiles.includes(id)) {
    throw new Error(`Model provider "${id}" is not allowed by .agents/security-policy.json.`);
  }

  const modelPolicy = params.securityPolicy.modelProviders;
  const promptDataClass = params.overrides.promptDataClass ?? "internal";
  if (modelPolicy?.forbidRestrictedData && promptDataClass === "restricted") {
    throw new Error(`Model provider "${id}" cannot receive restricted data by policy.`);
  }
  const maxPromptDataClass = modelPolicy?.maxPromptDataClass ?? "sensitive";
  if (PROMPT_DATA_CLASS_ORDER[promptDataClass] > PROMPT_DATA_CLASS_ORDER[maxPromptDataClass]) {
    throw new Error(
      `Model provider "${id}" prompt data class "${promptDataClass}" exceeds policy max "${maxPromptDataClass}".`
    );
  }

  const cloudRequiresOptIn =
    profile.locality === "cloud" &&
    (profile.requiresExplicitOptIn || modelPolicy?.remoteRequiresExplicitOptIn !== false || modelPolicy?.remoteDefault === "deny");
  if (cloudRequiresOptIn && !params.overrides.allowCloud) {
    throw new Error(`Model provider "${id}" is cloud-hosted. Re-run with --allow-cloud-intake=true to opt in explicitly.`);
  }

  const apiKeyEnv = params.overrides.apiKeyEnv || profile.apiKeyEnv;
  const runtime: ResolvedModelProvider = {
    id,
    kind: profile.kind,
    locality: profile.locality,
    baseUrl: params.overrides.baseUrl || envValue(profile.baseUrlEnv) || profile.defaultBaseUrl || "",
    model: params.overrides.model || envValue(profile.modelEnv) || profile.defaultModel || "",
    apiKey: apiKeyFromEnv(apiKeyEnv),
    apiKeyEnv,
    anthropicVersion: profile.anthropicVersion,
    promptDataClass,
    temperature: params.overrides.temperature ?? profile.defaultTemperature,
    maxTokens: params.overrides.maxTokens ?? profile.defaultMaxTokens ?? 4096,
    timeoutMs: params.overrides.timeoutMs ?? profile.defaultTimeoutMs ?? 120000,
  };

  if (!runtime.baseUrl) throw new Error(`Model provider "${id}" is missing base URL.`);
  if (!runtime.model) throw new Error(`Model provider "${id}" is missing model. Provide --intake-model or set ${profile.modelEnv ?? "a model env var"}.`);
  if (profile.locality === "cloud" && !runtime.apiKey) {
    throw new Error(`Model provider "${id}" requires API key env ${apiKeyEnv ?? "<unset>"}.`);
  }

  return new ResolvedChatProvider(id, profile, runtime);
}

export function redactProviderRuntime(provider: ChatProvider, policy: SecurityPolicy): Record<string, unknown> {
  return {
    id: provider.id,
    kind: provider.runtime.kind,
    locality: provider.runtime.locality,
    baseUrl: provider.runtime.baseUrl,
    model: provider.runtime.model,
    apiKeyEnv: provider.runtime.apiKeyEnv,
    apiKey: provider.runtime.apiKey ? "<redacted>" : undefined,
    allowPromptLogging: provider.profile.allowPromptLogging,
    allowResponseLogging: provider.profile.allowResponseLogging,
    promptDataClass: provider.runtime.promptDataClass,
    temperature: provider.runtime.temperature,
    maxTokens: provider.runtime.maxTokens,
    timeoutMs: provider.runtime.timeoutMs,
    note: redactText("Provider runtime is redacted before persistence.", policy),
  };
}
