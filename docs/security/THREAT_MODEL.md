# Threat Model

## Assets

- Source code and generated patches
- Git history, branches, worktrees, and pull requests
- Agent prompts, model responses, run logs, screenshots, videos, and terminal scrollback
- Local environment variables, credentials, tokens, SSH agent access, and OS keychain material
- Browser cookies, local storage, session storage, and request/response payloads
- CI configuration, MCP configuration, and agent policy files

## Trust Boundaries

- Operator to orchestrator
- Orchestrator to dispatched agents
- Dispatched agents to shell commands
- Generated manifest to command executor
- Intake provider profiles to local or cloud model APIs
- Local model server to generated changesets
- Browser automation to application runtime
- Local repo to external package registries and model providers

## Primary Risks

- Secret exfiltration through logs, prompts, model responses, terminal scrollback, browser traces, or network commands
- Generated manifests running arbitrary commands
- Agents silently expanding permissions through flags, shell wrappers, MCP config edits, or profile changes
- Package scripts performing hidden network or destructive actions
- Prompt injection from repository files, test output, web pages, or model-generated specs
- Cross-agent contamination through shared branches, hot files, caches, and global environment
- Persisting degraded or sensitive outputs as authoritative artifacts

## Required Mitigations

- Enforce command allowlists and deny dangerous shell forms.
- Build command environments from an allowlist instead of inheriting the full process environment.
- Redact logs before persistence and keep raw sensitive data out of memory files.
- Validate generated manifests before execution.
- Require security gates before completion.
- Keep agent profiles explicit and operator-approved.
- Keep model provider profiles explicit, data-class gated, and cloud opt-in only.
- Treat policy changes as security-sensitive code changes.
