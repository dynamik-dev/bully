# Bully rule configuration reference

## Context (semantic rules only)

By default the semantic evaluator sees only the diff under review. Some rules legitimately need upstream/downstream context (a callsite, a definition, an import block). For those, declare `context:` on the rule:

```yaml
rules:
  callsite-must-pass-typed-arg:
    description: >
      When a function whose typed signature changed is called, every callsite
      must update to match the new signature.
    severity: error
    engine: semantic
    context:
      lines: 30   # show 30 lines around each diff hunk
```

The pipeline reads `lines` lines above and below each diff hunk from the file on disk and includes them as an `<EXCERPT_FOR_RULE rule="...">` block inside the payload's `<UNTRUSTED_EVIDENCE>` region.

This is the *only* mechanism the evaluator has to see beyond the diff — the subagent has no `Read`, `Grep`, or `Glob` tools. If a rule needs a different shape of context (e.g., callers, definitions), file an issue: that's a deliberate boundary, not an oversight.

## Session-scope rules (`engine: session`)

Per-edit rules see one file at a time. Session-scope rules run at the `Stop` hook over the cumulative set of files edited in the session.

```yaml
rules:
  auth-changed-needs-tests:
    description: |
      Auth runtime changed but no auth tests were touched in this session.
    severity: error
    engine: session
    when:
      changed_any: ['src/auth/**']
    require:
      changed_any: ['tests/**/*auth*']
```

The pipeline maintains an append-only JSONL file at `.bully/session.jsonl` (one `{"file": ...}` record per line) with the changed-set; PostToolUse appends to it on every Edit/Write. At Stop time, each session rule whose `when.changed_any` matched is checked against `require.changed_any`; if the requirement is missing, the rule fires (severity-driven, exit 2 for `error`). On a clean Stop the session file is deleted. The append-only format is race-safe under parallel PostToolUse.

## Capabilities (script rules)

`bully trust` is the first safety gate (the user explicitly approved running this config). `capabilities:` is the second — a per-rule declaration of what each script needs:

```yaml
rules:
  lint-format:
    engine: script
    script: 'pnpm run lint'
    capabilities:
      network: false        # strip proxy vars; tripwire on accidental network use
      writes: cwd-only      # HOME and TMPDIR confined to cwd and cwd/.bully/tmp
```

This is declarative and best-effort, not kernel-level sandboxing. Tools that respect standard env vars (`HOME`, `TMPDIR`, `*_PROXY`, `NO_PROXY`) will be confined; tools that bypass them won't be. Treat capabilities as a clarity-and-tripwire mechanism — they document intent and surface accidents loudly. For real isolation, run the script under your platform's sandbox of choice (`firejail`, `bwrap`, `sandbox-exec`, container) outside bully.

Note: `writes: cwd-only` creates `.bully/tmp/` (eagerly) the first time a rule with that capability runs. The directory belongs to the same `.bully/` tree as telemetry, which `bully init` adds to `.gitignore`. Existing checkouts may need a one-line addition.

`bully validate --execute-dry-run` does NOT apply capabilities — it's a config-syntax probe, not a real run. If a script makes network calls during dry-run, the user's actual proxy will be visible. Treat it as expected; the runtime path is the one that matters.

## Suppressions

There are two ways to silence a rule on a specific line:

```php
// bully-disable next-line  -- silences ALL rules on the next line.
$x = 1;

// bully-disable: rule-id-a, rule-id-b -- silences specific rules on the next line.
$y = 2;
```

This is the lightweight format — use it for one-off cases. `bully` ignores the line and moves on; `bully review` does not flag these.

For governance-tracked debt with mandatory justification:

```php
// bully-disable-line rule-id reason: legacy api shape we're phasing out
compact('a', 'b');
```

The `bully-disable-line <rule> reason: <text>` format is what `bully debt` audits. The reason is mandatory and tracked. Use it when:
- The suppression should be visible to a future cleanup pass.
- The reason needs to survive blame churn.
- A reviewer should ask "is this still legacy, or shippable as-is?" later.

`bully debt` lists every `bully-disable-line` marker grouped by rule. `bully debt --strict` exits non-zero if any marker has a reason shorter than 12 characters.
