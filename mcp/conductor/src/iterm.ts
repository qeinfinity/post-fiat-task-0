import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const OSASCRIPT_TIMEOUT = 10_000;

function escapeAppleScript(str: string): string {
  return str.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

async function runAppleScript(script: string): Promise<string> {
  const { stdout } = await execFileAsync("osascript", ["-e", script], {
    timeout: OSASCRIPT_TIMEOUT,
  });
  return stdout.trim();
}

function stripAnsi(text: string): string {
  // Strip ANSI escapes and common TUI control sequences
  return text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, "")
             .replace(/\x1b\][^\x07]*\x07/g, "");
}

export interface TabInfo {
  name: string;
  tty: string;
  index: number;
}

/**
 * Create a new iTerm2 tab and set its session name.
 * Returns the tty path of the new session.
 *
 * IMPORTANT: Always use the returned tty for subsequent lookups.
 * Tab name assignment in iTerm2 AppleScript has timing issues --
 * tty-based lookup is the only reliable method.
 */
export async function createTab(name: string): Promise<string> {
  const script = `
tell application "iTerm2"
  tell current window
    set newTab to (create tab with default profile)
    tell current session of newTab
      set name to "${escapeAppleScript(name)}"
      return tty
    end tell
  end tell
end tell`;
  return runAppleScript(script);
}

/**
 * Write text to a session identified by its tty path.
 * The text is followed by a newline (simulates pressing Enter).
 */
export async function writeText(tty: string, text: string): Promise<void> {
  const script = `
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "${escapeAppleScript(tty)}" then
          tell s to write text "${escapeAppleScript(text)}"
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "not_found"`;
  const result = await runAppleScript(script);
  if (result === "not_found") {
    throw new Error(`Session with tty "${tty}" not found in iTerm2`);
  }
}

/**
 * Read the visible screen contents of a session by tty.
 */
export async function readContents(tty: string): Promise<string> {
  const script = `
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "${escapeAppleScript(tty)}" then
          return contents of s
        end if
      end repeat
    end repeat
  end repeat
end tell
return "TAB_NOT_FOUND"`;
  const result = await runAppleScript(script);
  if (result === "TAB_NOT_FOUND") {
    throw new Error(`Session with tty "${tty}" not found in iTerm2`);
  }
  return stripAnsi(result);
}

/**
 * Read the full scrollback text of a session by tty.
 */
export async function readFullText(tty: string): Promise<string> {
  const script = `
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "${escapeAppleScript(tty)}" then
          return text of s
        end if
      end repeat
    end repeat
  end repeat
end tell
return "TAB_NOT_FOUND"`;
  const result = await runAppleScript(script);
  if (result === "TAB_NOT_FOUND") {
    throw new Error(`Session with tty "${tty}" not found in iTerm2`);
  }
  return stripAnsi(result);
}

/**
 * Send a control character to a session by tty.
 * Supports: "ctrl-c" (0x03), "ctrl-d" (0x04), "ctrl-z" (0x1a)
 */
export async function sendControl(tty: string, key: string): Promise<void> {
  const charCodes: Record<string, number> = {
    "ctrl-c": 3,
    "ctrl-d": 4,
    "ctrl-z": 26,
  };
  const code = charCodes[key];
  if (code === undefined) {
    throw new Error(`Unknown control key: ${key}. Use ctrl-c, ctrl-d, or ctrl-z`);
  }
  const script = `
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "${escapeAppleScript(tty)}" then
          tell s to write text (ASCII character ${code})
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "not_found"`;
  const result = await runAppleScript(script);
  if (result === "not_found") {
    throw new Error(`Session with tty "${tty}" not found in iTerm2`);
  }
}

/**
 * Close a tab containing the session with the given tty.
 */
export async function closeTab(tty: string): Promise<void> {
  const script = `
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "${escapeAppleScript(tty)}" then
          close t
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "not_found"`;
  const result = await runAppleScript(script);
  if (result === "not_found") {
    throw new Error(`Session with tty "${tty}" not found in iTerm2`);
  }
}

/**
 * List all tabs across all iTerm2 windows with their names and ttys.
 */
export async function listTabs(): Promise<TabInfo[]> {
  const script = `
tell application "iTerm2"
  set tabInfo to ""
  repeat with w in windows
    set tabIdx to 0
    repeat with t in tabs of w
      repeat with s in sessions of t
        set tabInfo to tabInfo & tabIdx & "\\t" & (name of s) & "\\t" & (tty of s) & "\\n"
      end repeat
      set tabIdx to tabIdx + 1
    end repeat
  end repeat
  return tabInfo
end tell`;
  const result = await runAppleScript(script);
  if (!result) return [];

  return result
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => {
      const [indexStr, name, tty] = line.split("\t");
      return { index: parseInt(indexStr, 10), name: name || "", tty: tty || "" };
    });
}

/**
 * Check if a session with the given tty is still alive.
 */
export async function isSessionAlive(tty: string): Promise<boolean> {
  try {
    const { stdout } = await execFileAsync("lsof", ["-t", tty], {
      timeout: 5_000,
    });
    return stdout.trim().length > 0;
  } catch {
    return false;
  }
}
