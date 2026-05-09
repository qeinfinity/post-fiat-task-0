# Amendments for Proposed Hardening Directive

Date: 2026-04-18
Owner: agent
Status: ready to dispatch

Purpose: patch the proposed hardening directive so it is implementable against the current
`agent-template-1504` sources without leaving known gaps behind. Apply these edits on top of
the existing directive; they are not optional cleanups.

## Global edits to the directive

1. Replace any blanket ban on touching `docs/agent/` with:
   `Do not modify unrelated initiative content. Mandatory docs/agent bookkeeping required by AGENTS.md is allowed.`

2. Add this requirement:
   `Update operator-facing docs that still describe conductor state as purely in-memory: README.md and mcp/conductor/ORCHESTRATION_RUNBOOK.md.`

3. Add this requirement:
   `Add $CONDUCTOR_REPO_ROOT/.conductor-state.json to the root .gitignore.`

4. Add this requirement:
   `Introduce a test harness in mcp/conductor/package.json using Node's native test runner (node --test or equivalent), and include npm test in validation.`

5. Remove any implication that a shared `atomicWriteFile()` helper must be extracted as part of this patch.
   Keep the hardening scope narrow. Utility extraction is an optional follow-up, not an acceptance criterion.

## Item 1 amendments - Persistent state sidecar

The current directive is internally inconsistent: it requires persistence before the mutator returns,
then later asks for fire-and-forget persistence. Fix that contradiction directly.

### Required contract changes

1. `ConductorState.add()`, `remove()`, and `update()` must become async and return `Promise`.
   They must not resolve until the persist attempt for that mutation has completed.

2. Persistence must be serialized through a single in-memory promise chain so writes cannot land
   out of order. Rapid `add/remove/update` calls must not allow an older snapshot rename to replace
   a newer one.

3. `persist()` must write a sanitized sidecar snapshot, not raw `AgentSession` objects.
   Do not persist:
   - `prompt`
   - `lastScreenContent`
   - `lastScreenAt`

   Persist only the fields needed for recovery, reconciliation, and operator visibility.

4. `loadFrom()` must validate the sidecar with a runtime schema before populating the map.
   On any `ENOENT`, parse error, or schema mismatch:
   - log one stderr line
   - return an empty state
   - do not throw

5. Keep atomic write behavior (`temp -> rename`) as specified, but do not use fire-and-forget persistence.

### Startup and recovered-session behavior

1. Startup must load the sidecar before MCP tool registration begins serving requests.

2. Every recovered session must initially be treated as untrusted and loaded with `status: "unknown"`.

3. After load, run one reconciliation pass in `main()`:
   - call `iterm.isSessionAlive()` for each recovered `tty`
   - if alive, set `status: "running"`
   - if not alive or unreadable, leave `status: "unknown"`

4. Idle detection must continue to work for recovered sessions after that reconciliation pass.

5. `reconnect_agent` remains the explicit manual escape hatch for sessions that could not be
   reconciled automatically. Update tool text and docs to reflect that narrower role.

## Item 2 amendments - Intake provider detection / selection

The directive needs an explicit precedence contract. Do not leave provider selection implicit.

### Required precedence

Provider resolution order must be:

1. `--intake-provider`
2. `AUTOPILOT_INTAKE_PROVIDER`
3. auto-detection from provider-specific environment variables
4. registry default provider

### Required clarifications

1. If both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are present during auto-detection, the chosen
   winner must be intentional and documented. If Anthropic is preferred, say so explicitly.

2. `--allow-cloud-intake=true|false` must only gate whether the selected provider is allowed to be
   cloud-hosted. It must not participate in provider selection. Explicit CLI opt-in wins over
   detection defaults.

3. If a new `detectIntakeProvider()` helper is introduced, keep it pure and directly unit-testable.

4. Deprecated LM Studio intake flags must be rejected consistently from the detection path too.

## Item 3 amendments - Template variable validation

The current repo already contains intentional angle-bracket usage in `README.md`, so a generic
`<...>` scanner is not acceptable.

### Required contract changes

1. Do not implement a generic angle-bracket placeholder detector.

2. Use one of these two strategies:
   - validate only an explicit allowlist of known template placeholder tokens in the intended files, or
   - migrate template placeholders to a dedicated marker syntax such as `{{PROJECT_NAME}}` and scan only that syntax

3. The template repo must not fail its own CI forever. Add an explicit template-root skip mechanism,
   for example:
   - `.template-root`, or
   - frontmatter/metadata in `AGENTS.md`

   When that marker is present, `check-template` must exit `0` and print a clear skip message.

4. `check-template` must not require manifest loading or security policy loading.

5. Add operator docs for manual invocation in both:
   - `mcp/conductor/ORCHESTRATION_RUNBOOK.md`
   - `README.md`

## Test requirements to add to the directive

Hardening without tests is not enough here. Add these as explicit acceptance criteria:

1. `ConductorState.loadFrom()` tests:
   - missing sidecar
   - corrupt JSON
   - schema mismatch
   - valid sidecar

2. Persistence ordering test:
   - multiple mutations in quick succession do not produce stale final state

3. Recovered-session reconciliation test:
   - recovered `unknown` session becomes `running` only after liveness verification

4. Intake provider selection tests:
   - CLI override
   - env override
   - auto-detect with OpenAI only
   - auto-detect with Anthropic only
   - both cloud API keys present
   - registry default fallback
   - `allowCloud` gating behavior

5. `check-template` tests:
   - filled fixture passes
   - unfilled fixture fails
   - template-root sentinel skips
   - README angle-bracket usage examples do not false-positive

## Validation block to replace the current one

After completing the work, run:

```bash
cd mcp/conductor
npm run build
npm test
node dist/security-cli.js check-template --repo-root ../..
```

Expected results:

- `npm run build` succeeds with zero TypeScript errors
- `npm test` succeeds
- `check-template` exits `0` in the template repo and prints the configured skip message

If the implementation keeps the template repo failing its own `check-template`, the directive has not
fully addressed the operational defect.
