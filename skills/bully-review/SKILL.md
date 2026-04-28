---
name: bully-review
description: Reviews bully rule health from the telemetry log. Use when the user says "review my lint rules", "check rule health", "which lint rules are noisy", "find dead lint rules", "bully review", or asks for an audit of `.bully.yml`. Surfaces noisy, dead, and slow rules and suggests which to adjust, remove, or promote.
metadata:
  author: dynamik-dev
  version: 1.0.0
  category: workflow-automation
  tags: [linting, rule-health, telemetry, self-improvement]
---

# Agentic Lint Review

Audit `.bully.yml` using the telemetry log at `.bully/log.jsonl`. See `docs/telemetry.md` for log schema and scope.

## Prerequisites

- `.bully.yml` and `.bully/log.jsonl` both exist.
- If the log is empty, stop and tell the user to make a handful of edits first -- classifying an empty log flags every rule as dead.

## Semantic rule observability

Both script and semantic rule verdicts are logged. Semantic rules emit two extra record types beyond the per-edit `rules:` array:

- `semantic_verdict` — pass/violation reported by the evaluator skill once it finishes (see `docs/telemetry.md`).
- `semantic_skipped` — pre-dispatch can't-match filters fired (whitespace only, comment only, etc.).

The analyzer counts `semantic_verdict` `violation` as a fire and `pass` as a pass. `semantic_skipped` keeps a rule out of the dead bucket while contributing zero to the violation rate. If a semantic rule appears dead, it genuinely was never considered in the window — recommend the same retirement path you would for a dead script rule.

## Step 1: Run the analyzer

```bash
python3 pipeline/analyzer.py --log .bully/log.jsonl
```

Add `--config .bully.yml --json` when you need structured output to reason over. Thresholds `--noisy-threshold` (default 0.5) and `--slow-threshold-ms` (default 500) are tunable.

## Step 2: Classify

The analyzer returns three buckets plus a `by_rule` table with `fires`, `passes`, `evaluate_requested`, `mean_latency_ms`, `files_touched`, and `violation_rate`.

- **noisy**: violation rate above threshold. Rule is too broad or the codebase is systemically at odds with it.
- **dead**: zero hits in the log window (script rules only -- see gap above).
- **slow**: mean latency above threshold. Usually external shell-outs (PHPStan, ESLint, Pint).

## Step 3: Recommend

| Finding | Action |
|---------|--------|
| Noisy script rule | Tighten pattern, narrow scope glob, or demote severity to `warning`. |
| Noisy semantic rule | Sharpen the description (description IS the prompt) or split into two rules. |
| Dead script rule | Check scope glob first; if correct, remove the rule. |
| Slow rule | Cache, narrow scope, or move to pre-commit/CI. |
| Semantic rule with high `evaluate_requested` and no downstream edits | Candidate for promotion to a `script` rule. |
| Script rule grep-matching a structural pattern (likely noise from comments/strings) | FYI: propose conversion to `engine: ast`. Verify ast-grep is installed first. |
| Script rule catching a pattern an installed linter could express | FYI: propose moving the rule into the linter's config and replacing the bully rule with a passthrough (`script: "<linter> … {file}"`). Bully still enforces it via the hook. |

## Step 4: Present findings

Lead with a short prioritized punch list:

```
[rule-id] — <action> — <why>
```

Follow with brief noisy / dead / slow sections. Do not dump `by_rule` unless asked.

## Step 5: Hand off

Do not edit `.bully.yml` directly. When the user confirms a recommendation, hand off to the `bully-author` skill to apply it -- that skill tests rules against fixtures before writing.
