export interface CodexOptions {
  command?: string;
  prompt: string;
  worktreePath: string;
  mode: "interactive" | "exec";
  model?: string;
  fullAuto?: boolean;
  sandbox?: string;
  extraFlags?: string[];
}

/**
 * Escape a string for use inside single quotes in a shell command.
 */
function shellEscapeSingleQuote(str: string): string {
  return str.replace(/'/g, "'\\''");
}

export function shellQuote(str: string): string {
  return `'${shellEscapeSingleQuote(str)}'`;
}

/**
 * Build the shell command string to launch a Codex instance.
 *
 * Produces: cd '{worktree}' && codex [exec] -C '{worktree}' [-m model] [--full-auto] [flags] '{prompt}'
 */
export function buildCodexCommand(opts: CodexOptions): string {
  const parts: string[] = [];

  parts.push(`cd ${shellQuote(opts.worktreePath)}`);

  const codexParts: string[] = [shellQuote(opts.command ?? "codex")];

  if (opts.mode === "exec") {
    codexParts.push("exec");
  }

  codexParts.push("-C", shellQuote(opts.worktreePath));

  if (opts.model) {
    codexParts.push("-m", shellQuote(opts.model));
  }

  if (opts.fullAuto !== false) {
    codexParts.push("--full-auto");
  }

  if (opts.sandbox) {
    codexParts.push("--sandbox", shellQuote(opts.sandbox));
  }

  if (opts.extraFlags) {
    codexParts.push(...opts.extraFlags.map(shellQuote));
  }

  codexParts.push(shellQuote(opts.prompt));

  parts.push(codexParts.join(" "));

  return parts.join(" && ");
}
