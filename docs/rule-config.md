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
