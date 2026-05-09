# Security Validation Gates

Work is not complete until security gates pass or an explicit operator exception is recorded.

## Required Gates

- Command policy: all manifest, CI, dev, and agent-dispatch commands comply with `.agents/security-policy.json`.
- Environment policy: commands run with an allowlisted environment only.
- Model provider policy: intake uses an allowed provider profile; cloud providers require explicit opt-in and an allowed data class.
- Secret scan: repo files and durable logs are scanned for known secret patterns.
- Filesystem boundary: generated writes stay inside the repo and avoid protected paths.
- Dependency review: dependency changes are visible, lockfiles are present where applicable, and audit commands are run where feasible.
- Browser/user validation: user-facing work is exercised as a user, including error states, console errors, network requests, cookies/storage, and screenshots.
- Provenance: validation commands and outcomes are recorded in the initiative log without raw secrets or sensitive payloads.

## Browser Validation

When Playwright/MCP browser validation is applicable:

- Start the app with the documented one-command path.
- Inspect the app manually as a user would.
- Check browser console and page errors.
- Check network requests for unexpected domains or secret-bearing URLs.
- Check cookies, local storage, and session storage for accidental secrets.
- Capture screenshots only when they do not reveal restricted data.
- Continue fixing until the user-facing path and security checks are clean.

## Exceptions

Exceptions must include:

- Exact gate bypassed
- Reason
- Scope and duration
- Risk accepted by operator
- Follow-up action
