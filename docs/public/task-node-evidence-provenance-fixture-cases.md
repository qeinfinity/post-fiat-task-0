# Task Node Evidence Provenance Fixture Cases

Purpose: provide a synthetic public-safe fixture table for testing Task Node evidence provenance labels and expected automation decisions.

Source standard: `Task Node Evidence Provenance Manifest Label Reference`.

These rows are synthetic examples only. They contain no real wallet material, auth/session data, cookies, browser storage, customer data, private account data, confidential employer/client information, private alpha, or MNPI.

## Decision Values

- `pass`: Evidence is public, directly reviewable, safe to share, and matches the verification method.
- `needs_human_review`: Evidence may be acceptable, but provenance, degradation, screenshot use, or direct-observation risk requires human confirmation.
- `block`: Evidence should not be submitted by an autonomous agent.

## Fixture Table

| Case ID | Scenario | source_type | shareability | contains_mnpi | contains_private_account_data | public_urls | validation_commands | redaction_status | degraded_status | Expected Decision | Review Or Block Reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `public_repo_url_pass` | Public repository URL evidence | `public_repo` | `public` | `false` | `false` | `https://github.com/example/repo/blob/main/docs/public-runbook.md` | `curl -fsSL -o /dev/null -w '%{http_code}\n' <public_url>` -> `HTTP 200` | `not_needed` | `degraded=false; reason=""` | `pass` | Public Markdown artifact is directly reviewable and matches URL verification. |
| `public_api_alpha_pass` | Public API alpha evidence | `public_api` | `public` | `false` | `false` | `https://api.example.com/public/market-data` | `curl -fsSL <public_api_url>` -> public timestamp or data field returned | `not_needed` | `degraded=false; reason=""` | `pass` | Public endpoint supports the observation and no-MNPI status is backed by public provenance. |
| `screenshot_fallback_review` | Screenshot fallback instead of direct API records | `public_url` | `public` | `false` | `false` | `https://example.com/public-dashboard` | Manual screenshot review -> direct API record not captured | `redacted` | `degraded=true; reason="Screenshot fallback used instead of direct API records; lower-fidelity visual evidence."` | `needs_human_review` | Human should confirm screenshot was allowed, sufficiently redacted, and acceptable despite missing direct records. |
| `private_account_data_block` | Private account data | `operator_observation` | `restricted` | `unknown` | `true` | `<none>` | `<none>` | `needs_review` | `degraded=true; reason="Evidence depends on private account view."` | `block` | Autonomous agents must not submit private account views or account-derived records as public evidence. |
| `mnpi_unknown_source_review` | MNPI-unknown source | `unknown` | `unknown` | `unknown` | `false` | `<none>` | `<none>` | `needs_review` | `degraded=true; reason="Source cannot be verified as public."` | `needs_human_review` | Absence of obvious secrets does not prove a claim is public or non-MNPI. |
| `auth_bound_url_block` | Auth-bound URL | `public_url` | `unknown` | `unknown` | `unknown` | `https://example.com/private/session-page` | `curl -fsSL -o /dev/null -w '%{http_code}\n' <url>` -> redirected to login | `blocked` | `degraded=true; reason="URL is auth-bound and cannot be verified publicly."` | `block` | URL evidence must load without login, paywall, private repo access, cookies, or expiring session state. |
| `missing_redaction_block` | Missing redaction | `operator_observation` | `restricted` | `false` | `true` | `<none>` | Manual artifact scan -> full account identifier and balance visible | `blocked` | `degraded=true; reason="Artifact contains visible private account fields."` | `block` | Artifact exposes unnecessary private identifiers or account state. |
| `degraded_missing_reason_block` | Degraded evidence without a reason | `local_public_scan` | `public` | `false` | `false` | `https://example.com/public-source` | Sanitized local scan -> partial output only | `not_needed` | `degraded=true; reason=""` | `block` | Degraded evidence can pass only when the lower-fidelity source or missing coverage is explained. |

## Fixture Expectations

- Treat `unknown` as unsafe for autonomous submission unless a human resolves it.
- Prefer public URLs and commands that can be independently re-run.
- Keep validation outputs summarized instead of storing raw terminal scrollback.
- Do not include browser cookies, auth headers, session storage, local storage, wallet secrets, seed phrases, private keys, private account exports, customer data, employer/client data, or private alpha.
- Use human review for ambiguous provenance, direct-observation claims, screenshots, and any operator-supplied evidence.
