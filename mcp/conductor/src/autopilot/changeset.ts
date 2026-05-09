import { z } from "zod";

export const AutopilotFileChangeSchema = z.object({
  path: z.string().min(1).describe("Repo-relative file path (no absolute paths)."),
  contents: z.string(),
  executable: z.boolean().optional().describe("If true, chmod 755 after writing."),
});

export type AutopilotFileChange = z.infer<typeof AutopilotFileChangeSchema>;

export const AutopilotChangeSetSchema = z.object({
  changesetVersion: z.literal(1),
  notes: z.string().optional(),
  files: z.array(AutopilotFileChangeSchema).min(1),
});

export type AutopilotChangeSet = z.infer<typeof AutopilotChangeSetSchema>;

