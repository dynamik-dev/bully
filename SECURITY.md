# Security Policy

## Trust boundary: `.bully.yml`

**A `.bully.yml` can execute arbitrary shell commands on your machine.** Every `engine: script` rule runs its `script:` field through the shell against files you edit. That's the whole point of the tool -- but it also means a malicious or careless `.bully.yml` in a repo you clone is equivalent to running a setup script you didn't review.

bully gates this with a per-machine trust allowlist:

1. The first time you edit a file in a repo, the hook sees an untrusted `.bully.yml` and **refuses to execute any rules**. It prints a one-line stderr hint and does **not** block the edit itself -- your tool call succeeds, nothing runs.
2. To enable the config on this machine, review it, then run:
   ```
   bully trust          # from the repo root, or: --config <path>
   ```
   This records the SHA256 of the config (and every file it `extends:`) in `~/.bully-trust.json`.
3. Every subsequent run re-verifies the checksum. If the config changes, you see a "changed since last trust" message and rules stop running until you re-review:
   ```
   bully trust --refresh
   ```

The trust state is **machine-local**. It is never committed to repos and never shared between machines. A teammate cloning a repo you trust must trust it themselves.

### Bypass for CI and automation

Set `BULLY_TRUST_ALL=1` to disable the gate unconditionally. Use this only in environments where the config is already trusted through other means:

- **CI pipelines** -- the config is reviewed as part of the repo's code review; the CI runner treats the repo as trusted.
- **Dogfood / test scripts** -- the `.bully.yml` is the repo's own, and changes arrive through normal PR review.

Do **not** set `BULLY_TRUST_ALL=1` in an interactive developer shell as a default. That defeats the gate.

### What trust does NOT protect

- If you trust a config that was already malicious, you authorized the scripts. Trust gates subsequent changes, not initial review.
- Running `script:` rules that shell out to third-party linters (eslint, phpstan, etc.) inherits whatever risks those tools carry. Trust the tools, not just bully.
- The semantic evaluator subagent sees diff content. If your diffs contain secrets, those secrets are sent to the subagent. Linting is not exfiltration-safe by design.

## Reporting a vulnerability

bully runs deterministic shell commands and dispatches a Claude subagent. If you find a way to escape the sandboxed scope or inject commands through a crafted `.bully.yml`, through file contents that bypass the script-rule scoping, or through a bug in the trust gate, please report it privately.

**Preferred:** [GitHub private vulnerability reporting](https://github.com/dynamik-dev/bully/security/advisories/new)

**Alternative:** Email chris@arter.dev with subject line `[bully] security`.

Expected response: acknowledgement within 72 hours.

## Scope

In scope:
- Command injection through config parsing, filename handling, or diff content.
- Path traversal in rule `scope` globs or `extends:` targets.
- Trust gate bypass: getting rules to run against an untrusted config without `BULLY_TRUST_ALL` and without an entry in `~/.bully-trust.json`.
- Telemetry file tampering causing the analyzer to crash or misclassify.
- Hook exit-code bypass.

Out of scope:
- Rules themselves being poorly written (that's a config bug, not a security issue).
- The `bully` skill making a judgment error on a semantic rule.
- Third-party linters invoked by a rule's `script:` field.
- A user trusting a malicious config after reviewing it. Trust is a gate, not a sandbox.

## Supported versions

Only `main` is supported. Tagged releases may receive security fixes at maintainer discretion.
