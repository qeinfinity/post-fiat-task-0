export interface LmStudioChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface LmStudioConfig {
  baseUrl: string;
  model: string;
  apiKey?: string;
  temperature?: number;
  maxTokens?: number;
  timeoutMs?: number;
}

export interface LmStudioChatResult {
  content: string;
  raw: unknown;
}

class LmStudioResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LmStudioResponseError";
  }
}

class LmStudioProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LmStudioProtocolError";
  }
}

function joinUrl(baseUrl: string, path: string): string {
  const b = baseUrl.replace(/\/+$/, "");
  const p = path.replace(/^\/+/, "");
  return `${b}/${p}`;
}

function v1FallbackBaseUrl(baseUrl: string): string | null {
  try {
    const u = new URL(baseUrl);
    const pathname = u.pathname.replace(/\/+$/, "");
    if (pathname === "/v1") return null;
    if (pathname === "" || pathname === "/") {
      u.pathname = "/v1";
      return u.toString();
    }
    return null;
  } catch {
    return null;
  }
}

function ipv4LoopbackFallbackBaseUrl(baseUrl: string): string | null {
  try {
    const u = new URL(baseUrl);
    const host = u.hostname.toLowerCase();
    if (host !== "localhost" && host !== "0.0.0.0" && host !== "::") return null;
    u.hostname = "127.0.0.1";
    return u.toString();
  } catch {
    return null;
  }
}

function candidateBaseUrls(baseUrl: string): string[] {
  const out: string[] = [];
  const add = (u: string | null) => {
    if (!u) return;
    if (out.includes(u)) return;
    out.push(u);
  };

  add(baseUrl);
  add(v1FallbackBaseUrl(baseUrl));
  const ipv4 = ipv4LoopbackFallbackBaseUrl(baseUrl);
  add(ipv4);
  add(ipv4 ? v1FallbackBaseUrl(ipv4) : null);

  return out;
}

function describeNodeNetCause(cause: unknown): string | null {
  if (!cause || (typeof cause !== "object" && typeof cause !== "function")) return null;
  const c: any = cause;
  const bits: string[] = [];
  if (typeof c.code === "string" && c.code.length > 0) bits.push(`code=${c.code}`);
  if (typeof c.errno === "string" || typeof c.errno === "number") bits.push(`errno=${c.errno}`);
  if (typeof c.syscall === "string" && c.syscall.length > 0) bits.push(`syscall=${c.syscall}`);
  if (typeof c.address === "string" && c.address.length > 0) bits.push(`address=${c.address}`);
  if (typeof c.port === "string" || typeof c.port === "number") bits.push(`port=${c.port}`);
  const msg = typeof c.message === "string" && c.message.length > 0 ? c.message : null;
  if (msg && bits.length > 0) return `${msg} (${bits.join(", ")})`;
  if (msg) return msg;
  if (bits.length > 0) return bits.join(", ");
  return null;
}

function extractContentFromChatCompletion(json: unknown): string | null {
  const choice0 = (json as any)?.choices?.[0];
  if (!choice0) return null;

  const messageContent = choice0?.message?.content;
  if (typeof messageContent === "string") return messageContent;

  // Some OpenAI-compatible servers may return content parts instead of a single string.
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

  // Some servers may respond like the legacy Completions API.
  if (typeof choice0?.text === "string") return choice0.text;

  return null;
}

export async function lmStudioChatComplete(
  config: LmStudioConfig,
  messages: LmStudioChatMessage[]
): Promise<LmStudioChatResult> {
  const baseUrls = candidateBaseUrls(config.baseUrl);

  let lastErr: unknown = null;
  const attempted: string[] = [];

  for (const baseUrl of baseUrls) {
    const url = joinUrl(baseUrl, "/chat/completions");
    attempted.push(url);

    const controller = new AbortController();
    const timeout =
      config.timeoutMs && config.timeoutMs > 0
        ? setTimeout(() => controller.abort(), config.timeoutMs)
        : null;

      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            "content-type": "application/json",
          ...(config.apiKey ? { authorization: `Bearer ${config.apiKey}` } : {}),
        },
        body: JSON.stringify({
          model: config.model,
          messages,
          temperature: config.temperature ?? 0,
          max_tokens: config.maxTokens ?? 4096,
          stream: false,
        }),
        signal: controller.signal,
      });

      const text = await res.text();
      let json: unknown;
        try {
          json = JSON.parse(text);
        } catch {
          json = { nonJsonResponse: text };
        }

        if (!res.ok) {
          throw new LmStudioResponseError(
            `LM Studio request failed (${res.status} ${res.statusText}) (${url}). Response: ${text.slice(0, 2000)}`
          );
        }

        const content = extractContentFromChatCompletion(json);
        if (typeof content !== "string" || content.trim().length === 0) {
          throw new LmStudioProtocolError(
            `LM Studio response missing assistant content (${url}). Response: ${text.slice(0, 2000)}`
          );
        }

        return { content, raw: json };
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          lastErr = new Error(`LM Studio request timed out after ${config.timeoutMs}ms (${url}).`);
        } else {
          lastErr = err;
        }
        // retry next baseUrl (if any)
      } finally {
        if (timeout) clearTimeout(timeout);
      }
    }

  const err = lastErr;
  const msg = err instanceof Error ? err.message : String(err);
  const cause = err && typeof err === "object" ? (err as any).cause : undefined;
  const causeMsg = describeNodeNetCause(cause);
  const tried =
    attempted.length > 0 ? `Tried:\n${attempted.map((u) => `- ${u}`).join("\n")}\n` : "";
  const suggestIpv4 = config.baseUrl.toLowerCase().includes("localhost");
  const suggestV1 = (() => {
    try {
      const u = new URL(config.baseUrl);
      const pathname = u.pathname.replace(/\/+$/, "");
      return pathname === "" || pathname === "/";
    } catch {
      return false;
    }
  })();

  throw new Error(
    `LM Studio error: ${msg}` +
      (causeMsg ? `\nCause: ${causeMsg}` : "") +
      `\n${tried}` +
      `Ensure LM Studio is running and its OpenAI-compatible server is enabled (base URL: ${config.baseUrl}).` +
      (suggestV1 ? `\nTip: your base URL usually ends with /v1` : "") +
      (suggestIpv4 ? `\nTip: try --lmstudio-base-url=http://127.0.0.1:1234/v1` : "")
  );
}
