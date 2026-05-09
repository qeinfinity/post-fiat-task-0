# Incident Response

Use this process if secrets, restricted data, or unsafe commands are discovered.

## Immediate Actions

1. Stop the current task.
2. Do not copy the sensitive value into chat, logs, memory files, commits, or PR text.
3. Record a redacted summary and affected file paths.
4. Remove or redact the sensitive content from generated artifacts and local logs where safe.
5. Ask the operator whether credentials must be rotated or history rewritten.

## If Data Was Committed

- Do not push.
- Identify affected commits and files.
- Ask the operator before rewriting history.
- After remediation, run secret scan again.

## If Data Was Sent To A Model Or External Service

- Record the provider/service, timestamp, and redacted data type.
- Ask the operator for incident handling requirements.
- Do not retry with the same payload.

## Completion

Close the incident only after:

- Sensitive content is removed or intentionally quarantined.
- Relevant credentials are rotated if needed.
- Security gates pass.
- The initiative log contains a redacted summary of what happened and how it was handled.
