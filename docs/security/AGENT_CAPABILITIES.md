# Agent Capabilities

Agents may only be launched through profiles in `.agents/agent-profiles.json`.

## Profile Rules

- A profile defines the executable, allowed flags, default sandbox, whether full-auto is allowed, and how prompts are passed.
- Operator runtime choice selects a profile; it does not permit arbitrary binaries or flags.
- `full_auto` is denied unless the selected profile explicitly allows it.
- Additional flags are denied unless listed in the profile.
- Profile and policy edits are security-sensitive and require explicit operator intent.

## Dispatch Rules

Before dispatch:
- Identify write scope and hot files.
- Confirm no concurrent agent will modify the same hot file.
- Choose the least-privileged profile that can complete the task.
- Include security constraints and validation commands in the prompt.

During execution:
- Monitor for approval prompts, permission escalation, unexpected network use, and out-of-scope file edits.
- Do not approve prompts that grant broader filesystem, network, or credential access unless the operator requested it.

After execution:
- Review diff and run security gates before PR creation or merge.
- Verify no logs, screenshots, traces, or local-only artifacts are included in the diff.
