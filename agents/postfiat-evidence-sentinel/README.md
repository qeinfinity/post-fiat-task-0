# Post Fiat Public Evidence Sentinel

Public Evidence Sentinel is a proposed Post Fiat agent that helps Task Node contributors and verifiers preflight evidence URLs before submission.

The first command is intentionally narrow:

```text
/preflight https://github.com/example/repo/commit/abc123
```

The agent runs the repository checker:

```bash
python3 scripts/check_tasknode_public_evidence_preflight.py <url> --pretty
```

It replies with JSON verdicts only from the checker:

- `pass` when the URL appears login-free and publicly reviewable.
- `block` when the URL contains auth/session indicators or appears private.
- `needs_human_review` when the URL could not be safely classified automatically.

## Why This Helps Alignment

Task Node rewards depend on verifiable public evidence. This agent gives contributors a simple pre-submit guardrail for malformed commit evidence, auth-bound URLs, session-token links, unsafe signed URLs, login redirects, and ambiguous status/content-type results.

It is deliberately not a trading, wallet-analysis, or private-research agent. It does not request account data, cookies, mnemonics, private keys, MNPI, trading signals, venue rankings, model thresholds, execution advice, or proprietary strategy logic.

## Proposed Directory Registration

Use the MCP `register_bot` tool with the payload in `registration.json` after a dedicated bot wallet is created, funded, and configured.

Suggested name:

```text
Public Evidence Sentinel
```

Suggested description:

```text
Preflight public Task Node evidence URLs before submission. Send /preflight <url> to check login-free accessibility, unsafe query/session indicators, auth-bound paths, redirects, status/content type, and safe failure reasons. Replies with deterministic JSON verdicts and never asks for wallet secrets, cookies, MNPI, trading signals, or private account data.
```

## Secret Boundary

Do not use the operator's main wallet for this bot. Use a dedicated bot wallet with minimal funds.

The bot seed must be handled outside this repository:

```bash
mkdir -p ~/.postfiat-agents/evidence-sentinel
chmod 700 ~/.postfiat-agents/evidence-sentinel
printf '%s\n' '<BOT_SEED_GOES_HERE>' > ~/.postfiat-agents/evidence-sentinel/BOT_SEED
chmod 600 ~/.postfiat-agents/evidence-sentinel/BOT_SEED
```

Do not commit the seed, `.keystone-api-key`, terminal scrollback containing a seed, or wallet material.

## Operator Flow

1. Start `@postfiatorg/pft-chatbot-mcp` without a seed in a trusted MCP client and create a dedicated bot wallet, or create the wallet by another trusted method.
2. Save the seed to `~/.postfiat-agents/evidence-sentinel/BOT_SEED` with `0600` permissions.
3. Deposit at least `10 PFT` into the bot wallet to activate it.
4. Configure the MCP server from `mcp.cursor.json.example`, using `BOT_SEED_FILE`.
5. Register the agent with `registration.json`.
6. Keep the MCP server running so liveness pings keep the agent visible.
7. Use `OPERATOR_PROMPT.md` as the runtime policy for the LLM/client that handles inbound messages.

## Verification Commands

```bash
python3 scripts/check_tasknode_public_evidence_preflight.py --self-test --pretty
python3 scripts/check_tasknode_public_evidence_preflight.py https://github.com/qeinfinity/post-fiat-task-0/commit/6b571480a163d0f62b543082f13c773e926f4926 --pretty
python3 scripts/check_tasknode_public_evidence_preflight.py https://github.com/settings/profile --pretty || true
```

