# Data Classification

## Public

Examples: open-source source files, public docs, public package metadata.

Allowed:
- Commit
- Include in prompts
- Persist in logs when relevant

## Internal

Examples: unpublished design notes, implementation plans, local run metadata.

Allowed:
- Commit only when intended
- Summarize in memory logs
- Include in prompts only when needed for the task

## Sensitive

Examples: terminal scrollback, model prompts/responses, local file paths, browser screenshots, local config, unpublished product details.

Allowed:
- Persist locally only when needed for reproducibility
- Redact before logs or summaries
- Prefer local model providers for prompts
- Do not commit unless explicitly intended and reviewed

## Restricted

Examples: API keys, tokens, passwords, private keys, cookies, session storage, SSH agent material, OS keychain content, production payloads, customer data, PII.

Allowed:
- Do not read unless explicitly authorized
- Do not send to model providers
- Do not commit
- Do not persist in logs
- If discovered, stop, redact, and follow `docs/security/INCIDENT_RESPONSE.md`

## Logging Rules

- Prefer summaries over raw dumps.
- Record exact commands and file paths when needed for reproducibility.
- Redact values for keys containing `token`, `secret`, `password`, `passwd`, `api_key`, `apikey`, `private_key`, or `credential`.
- Do not paste full browser storage, request headers, or terminal scrollback into memory files.
- Cloud provider use requires explicit opt-in and an allowed `--intake-data-class`.
