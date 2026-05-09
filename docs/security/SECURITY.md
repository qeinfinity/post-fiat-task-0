# Security Charter

Security, confidentiality, least privilege, and non-exfiltration override every delivery goal in this template.

Agents must treat project files, prompts, logs, browser state, terminal scrollback, local environment variables, and generated artifacts as potentially sensitive until classified otherwise.

## Required Contracts

- `.agents/security-policy.json` is the machine-readable authority for command, environment, network, filesystem, log, and completion-gate policy.
- `.agents/agent-profiles.json` is the machine-readable authority for which agent CLIs may be spawned and with which permissions.
- `.agents/model-providers.json` is the machine-readable authority for intake model providers, base URLs, model env vars, and prompt/response logging behavior.
- `docs/security/DATA_CLASSIFICATION.md` defines what may be logged, persisted, committed, or sent to a model provider.
- `docs/security/VALIDATION_GATES.md` defines the security checks that must pass before work is complete.

Network policy is currently enforced at the command/policy layer. It blocks known network and exfiltration tools, but it is not a replacement for OS-level network/process sandboxing on high-risk runs.

## Hard Rules

- Do not read, print, persist, or forward secrets unless the operator explicitly authorizes that access for the task.
- Do not inherit the full process environment into generated commands.
- Do not execute commands outside the policy allowlist.
- Do not silently use external network access.
- Do not send intake prompts to cloud model providers unless the operator explicitly opts in and the prompt data class is allowed by policy.
- Do not modify security policy, agent profiles, auth config, CI secrets, or MCP config without an explicit operator request.
- Do not persist full terminal scrollback, browser storage, cookies, request headers, or production payloads.
- Prune local autopilot logs according to `.agents/security-policy.json` with `node mcp/conductor/dist/security-cli.js prune-logs --repo-root .`.
- If a security check conflicts with convenience or speed, block and record the reason.

## Stop Conditions

Stop and ask for operator review if any task requires:

- Secrets, tokens, private keys, cookies, or password material
- Production data, customer data, or personal data
- Disabling authentication, authorization, validation, encryption, audit trails, or provenance
- Running destructive commands
- Expanding agent permissions or bypassing sandbox/policy controls
- External network access beyond the policy
- Committing logs or generated artifacts that might contain sensitive content
