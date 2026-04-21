# Telemetry and self-improvement

Every pipeline run can append a record to a telemetry log. The `bully-review` skill reads that log and classifies rule health so the config can evolve with the codebase.

## Enabling

Telemetry is opt-in. Create the log directory next to `.bully.yml`:

```bash
mkdir .bully
```

From that point on, every pipeline run appends one line to `.bully/log.jsonl`. Removing the directory turns logging off.

The pipeline never auto-creates the directory. This is deliberate — a repo with no `.bully/` is an explicit "do not write here" signal.

## What gets logged

One JSONL record per pipeline run. Each record captures the overall result plus a per-rule breakdown.

```json
{
  "ts": "2026-04-16T18:00:00Z",
  "file": "src/Stores/EloquentRoleStore.php",
  "status": "blocked",
  "latency_ms": 20,
  "rules": [
    {
      "id": "no-compact",
      "engine": "script",
      "verdict": "violation",
      "severity": "error",
      "line": 42,
      "latency_ms": 9
    },
    {
      "id": "no-db-facade",
      "engine": "script",
      "verdict": "pass",
      "severity": "error",
      "latency_ms": 6
    },
    {
      "id": "inline-single-use-vars",
      "engine": "semantic",
      "verdict": "evaluate_requested",
      "severity": "error"
    }
  ]
}
```

### Fields

Record-level:

| Field | Description |
|-------|-------------|
| `ts` | ISO-8601 UTC timestamp (second precision). |
| `file` | File the pipeline ran against. |
| `status` | `pass`, `evaluate`, or `blocked`. |
| `latency_ms` | Total pipeline wall-clock time. |
| `rules` | Per-rule breakdown. |

Per-rule:

| Field | Description |
|-------|-------------|
| `id` | Rule id from `.bully.yml`. |
| `engine` | `script`, `ast`, or `semantic`. |
| `verdict` | `pass`, `violation`, or `evaluate_requested`. |
| `severity` | `error` or `warning`. |
| `line` | Line number of the first violation (deterministic rules only). |
| `latency_ms` | Per-rule latency (deterministic rules only). |
| `error` | `true` when the rule itself raised an exception during evaluation (converted to a blocking `severity=error` violation). Omitted otherwise. |

### Verdict meanings

- **`pass`** — script rule ran and returned exit 0.
- **`violation`** — script rule ran and returned non-zero.
- **`evaluate_requested`** — semantic rule was included in the payload sent to the agent. Paired later by a `semantic_verdict` record once the skill reports back.

## Semantic verdicts and skips

The pipeline ships two extra record types that close the semantic-rule telemetry loop.

### `semantic_verdict`

After the `bully` skill finishes evaluating a semantic payload, it calls:

```bash
bully --log-verdict \
  --rule inline-single-use-vars \
  --file src/Evaluators/CachedEvaluator.php \
  --verdict violation
```

which appends a record like:

```json
{
  "ts": "2026-04-16T18:00:05Z",
  "type": "semantic_verdict",
  "file": "src/Evaluators/CachedEvaluator.php",
  "rule": "inline-single-use-vars",
  "verdict": "violation",
  "severity": "error"
}
```

`verdict` is `pass` or `violation`. The record is keyed by rule id and file, which is enough for the analyzer to pair it with the earlier `evaluate_requested` line.

### `semantic_skipped`

Before dispatching the evaluator the pipeline applies cheap "can't possibly match" filters (whitespace-only hunks, pure deletions, comment-only hunks on identifier-targeting rules, <2 added lines). When a filter preempts a dispatch, the pipeline writes:

```json
{
  "ts": "2026-04-16T18:00:00Z",
  "type": "semantic_skipped",
  "file": "src/Foo.php",
  "rule": "inline-single-use-vars",
  "reason": "whitespace_only"
}
```

`reason` is one of `whitespace_only`, `deletion_only`, `comment_only`, `insufficient_added_lines`. These records make the skip lane visible so a skip pattern that hides real violations shows up in the analyzer instead of vanishing.

### Note on skill version

`semantic_verdict` depends on the `bully` skill being up to date — older versions do not call `--log-verdict`. If verdict records are missing for known-firing semantic rules, update the skill or bypass the evaluator manually (`bully --log-verdict` is a plain CLI). `semantic_skipped` is pipeline-side and independent of the skill.

## Running the analyzer

```bash
python3 pipeline/analyzer.py \
  --log .bully/log.jsonl \
  --config .bully.yml
```

Output:

```
Rule health report
==================
Total edits analyzed: 284
Window: 2026-03-01T12:00:00Z → 2026-04-16T18:00:00Z

Noisy rules (2): fire on most edits -- consider relaxing or splitting.
  - no-db-facade  fires=176 passes=108 requested=0 rate=62% avg_ms=6
  - no-event-helper  fires=164 passes=120 requested=0 rate=58% avg_ms=5

Dead rules (1): never invoked in this window -- consider removing or widening scope.
  - deprecated-carbon  fires=0 passes=0 requested=0 rate=0% avg_ms=0

Slow rules (2): mean latency is high -- consider simplifying or caching.
  - pint-formatting  fires=68 passes=216 requested=0 rate=24% avg_ms=1412
  - phpstan-check  fires=42 passes=242 requested=0 rate=15% avg_ms=892

All rules:
  - ... (per-rule table)
```

### Options

```
--json                   Emit machine-readable JSON instead of formatted text.
--noisy-threshold 0.5    Violation rate above which a rule is flagged noisy (default 0.5).
--slow-threshold-ms 500  Mean latency ms above which a rule is flagged slow (default 500).
```

### Classification rules

- **Noisy** — `violation_rate = fires / (fires + passes)` exceeds the noisy threshold. Defaults to 50%. Semantic `violation` verdicts count as fires alongside script violations, so a prose-style rule that flags every edit surfaces as noisy now rather than hiding behind `evaluate_requested`.
- **Dead** — the rule is configured but never appeared in any log entry's `rules` list AND has no `semantic_verdict` records and no `semantic_skipped` records for this rule id. A semantic rule that was dispatched and came back `pass` still counts as alive. A rule that is skipped only by the can't-match filters counts as alive too — the dead classifier only flags rules that never get considered.
- **Slow** — mean per-run latency exceeds the slow threshold. Defaults to 500 ms. Usually external shell-outs. Candidates for demotion from the per-edit pipeline to pre-commit or CI.

## Using the review skill

The `bully-review` skill wraps the analyzer and produces a prioritized punch list instead of a raw table:

```
> /bully-review
```

The skill runs the analyzer, interprets the findings in context, and recommends concrete actions. It never modifies `.bully.yml` without your confirmation.

## Workflow: introducing a new rule

1. Add the rule to `.bully.yml` with `severity: warning`.
2. Let it run across a few hundred edits.
3. `/bully-review`.
4. If the rule is noisy, sharpen its pattern or description before promoting.
5. If the rule is quiet with clean fixes, promote to `severity: error`.
6. If the rule never fires, check the scope glob first; if scope is right, consider removing.

## Workflow: removing a rule

1. `/bully-review` identifies a dead rule.
2. Verify the scope isn't misconfigured. (A common cause: rule scoped `src/*.ts` when the project uses `packages/*/src/*.ts`.)
3. If the rule is genuinely unused, remove it from `.bully.yml`.
4. The telemetry log retains history; removed rules simply stop appearing in future records.

## Privacy and log hygiene

- The log contains file paths and rule outcomes — no file contents, no diffs, no code.
- Log lines are append-only. The pipeline never rewrites or truncates.
- Rotate manually when the log grows beyond your tolerance. `jq` over multi-MB JSONL is cheap; the analyzer has no pagination built in yet.
- Gitignore `.bully/` if you don't want telemetry in version control. It's per-developer data, not project config.

## What telemetry does not do (yet)

The substrate is in place; some autonomous improvements are still deferred:

- **Semantic-to-script promotion** — once the pipeline knows a semantic rule fires with identical mechanical fixes N times in a row, it could draft the equivalent script rule. Not wired. (The inputs — paired `evaluate_requested` + `semantic_verdict` records — now exist.)
- **Rule discovery from unflagged fixes** — when the agent edits the same pattern repeatedly without any rule firing, that could suggest a new rule. Not wired.

These are the logical next features if the substrate proves useful. Deferred deliberately — they need real usage data to be meaningful rather than speculative.
