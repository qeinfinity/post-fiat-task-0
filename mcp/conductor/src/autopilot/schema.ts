import { z } from "zod";

export const CommandSpecSchema = z.object({
  cmd: z.string().min(1),
  args: z.array(z.string()).optional(),
  cwd: z.string().optional().describe("Relative to repo root unless absolute."),
  env: z.record(z.string()).optional(),
  timeoutMs: z.number().int().positive().optional(),
});

export type CommandSpec = z.infer<typeof CommandSpecSchema>;

export const AutopilotManifestSchema = z.object({
  manifestVersion: z.literal(1),
  project: z.object({
    name: z.string().min(1),
    description: z.string().min(1),
  }),
  stack: z
    .object({
      language: z.string().min(1).optional(),
      runtime: z.string().min(1).optional(),
      packageManager: z.string().min(1).optional(),
      framework: z.string().min(1).optional(),
    })
    .optional(),
  commands: z.object({
    bootstrap: z.array(CommandSpecSchema).optional(),
    build: z.array(CommandSpecSchema).optional(),
    test: z.array(CommandSpecSchema).optional(),
    lint: z.array(CommandSpecSchema).optional(),
    format: z.array(CommandSpecSchema).optional(),
    start: z.array(CommandSpecSchema).optional(),
  }),
  artifacts: z
    .array(
      z.object({
        path: z.string().min(1),
        description: z.string().min(1),
      })
    )
    .optional(),
});

export type AutopilotManifest = z.infer<typeof AutopilotManifestSchema>;

