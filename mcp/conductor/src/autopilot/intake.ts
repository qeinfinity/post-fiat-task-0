import { z } from "zod";
import type { ChatProvider } from "../modelProviders.js";

const QuestionSchema = z.object({
  id: z.string().min(1),
  question: z.string().min(1),
  type: z.enum(["short_text", "long_text", "choice", "multi_choice", "boolean"]).default("short_text"),
  choices: z.array(z.string().min(1)).optional(),
});

export type IntakeQuestion = z.infer<typeof QuestionSchema>;

const QuestionnaireSchema = z.object({
  questionnaireVersion: z.literal(1),
  title: z.string().min(1),
  questions: z.array(QuestionSchema).min(6).max(18),
});

export type IntakeQuestionnaire = z.infer<typeof QuestionnaireSchema>;

export const IntakeAnswerSchema = z.record(
  z.union([z.string(), z.boolean(), z.array(z.string())])
);

export type IntakeAnswers = z.infer<typeof IntakeAnswerSchema>;

const IntakeOutputSchema = z.object({
  intakeVersion: z.literal(1),
  project: z.object({
    name: z.string().min(1).describe("kebab-case or snake_case is ok"),
    description: z.string().min(1),
  }),
  autopilot: z.object({
    requireStartCommand: z.boolean().default(true),
    requireCiWorkflow: z.boolean().default(true),
    maxFixAttempts: z.number().int().min(1).max(25).default(10),
  }),
  specMarkdown: z.string().min(1),
});

export type IntakeOutput = z.infer<typeof IntakeOutputSchema>;

function jsonOnlyRules(): string {
  return [
    "RESPONSE FORMAT (JSON ONLY; no markdown, no backticks):",
    "Return a single JSON object and nothing else.",
    "Do not include trailing commentary.",
  ].join("\n");
}

export async function generateQuestionnaire(
  provider: ChatProvider,
  seedIdea: string
): Promise<{ questionnaire: IntakeQuestionnaire; raw: unknown; prompt: string }> {
  const prompt = [
    "Generate a comprehensive but bounded project intake questionnaire for a brand-new software project.",
    "",
    "INPUT IDEA (may be vague):",
    seedIdea.trim() || "(empty)",
    "",
    "GOAL:",
    "- Ask enough questions so implementation can proceed with ZERO follow-up questions.",
    "- Optimize for producing something the user can test visually very simply (prefer a local dev UI unless explicitly not).",
    "",
    "CONSTRAINTS:",
    "- Max 18 questions, min 6 questions.",
    "- Each question must have a stable id (snake_case).",
    "- Avoid asking for secrets (API keys, passwords).",
    "- Use choice/multi_choice when helpful and include choices.",
    "",
    "OUTPUT JSON SCHEMA:",
    "{",
    '  "questionnaireVersion": 1,',
    '  "title": string,',
    '  "questions": [',
    '    { "id": string, "question": string, "type": "short_text"|"long_text"|"choice"|"multi_choice"|"boolean", "choices"?: string[] }',
    "  ]",
    "}",
    "",
    jsonOnlyRules(),
  ].join("\n");

  const messages = [
    { role: "system" as const, content: "You are a meticulous software product lead." },
    { role: "user" as const, content: prompt },
  ];

  const result = await provider.complete(messages);
  const obj = JSON.parse(extractJsonObjectString(result.content));
  const questionnaire = QuestionnaireSchema.parse(obj);
  return { questionnaire, raw: result.raw, prompt };
}

export async function generateSpecFromAnswers(params: {
  provider: ChatProvider;
  seedIdea: string;
  questionnaire: IntakeQuestionnaire;
  answers: IntakeAnswers;
}): Promise<{ intake: IntakeOutput; raw: unknown; prompt: string }> {
  const prompt = [
    "Synthesize a complete, implementation-ready project spec from the questionnaire + answers.",
    "",
    "INPUT IDEA:",
    params.seedIdea.trim() || "(empty)",
    "",
    "QUESTIONNAIRE JSON:",
    JSON.stringify(params.questionnaire, null, 2),
    "",
    "ANSWERS JSON:",
    JSON.stringify(params.answers, null, 2),
    "",
    "REQUIREMENTS:",
    "- The resulting project MUST be testable by eyes with a single simple command: `bash scripts/dev`.",
    "- The project MUST have automated tests and a CI workflow at `.github/workflows/ci.yml` that runs `bash scripts/ci`.",
    "- Prefer deterministic tooling (lockfiles).",
    "- Do NOT require any secrets for local development.",
    "",
    "OUTPUT JSON SCHEMA:",
    "{",
    '  "intakeVersion": 1,',
    '  "project": { "name": string, "description": string },',
    '  "autopilot": { "requireStartCommand": boolean, "requireCiWorkflow": boolean, "maxFixAttempts": number },',
    '  "specMarkdown": string',
    "}",
    "",
    "The specMarkdown must be a complete replacement for .autopilot/spec.md.",
    "",
    jsonOnlyRules(),
  ].join("\n");

  const messages = [
    { role: "system" as const, content: "You are a meticulous software architect and delivery lead." },
    { role: "user" as const, content: prompt },
  ];

  const result = await params.provider.complete(messages);
  const obj = JSON.parse(extractJsonObjectString(result.content));
  const intake = IntakeOutputSchema.parse(obj);
  return { intake, raw: result.raw, prompt };
}

function extractJsonObjectString(text: string): string {
  const trimmed = text.trim();
  if (trimmed.startsWith("{") && trimmed.endsWith("}")) return trimmed;
  const first = trimmed.indexOf("{");
  const last = trimmed.lastIndexOf("}");
  if (first === -1 || last === -1 || last <= first) {
    throw new Error("Expected a JSON object but could not locate '{...}' in model output.");
  }
  return trimmed.slice(first, last + 1);
}
