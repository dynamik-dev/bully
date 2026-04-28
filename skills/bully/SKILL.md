---
name: bully
description: Interprets bully PostToolUse hook output after Edit/Write -- fixes blocked-stderr violations or dispatches the bully-evaluator subagent for semantic payloads.
metadata:
  author: dynamik-dev
  version: 2.0.0
  category: workflow-automation
  tags: [linting, hooks, code-quality, post-tool-use]
---

# Agentic Lint

Interpret and act on bully PostToolUse hook output. Not user-invocable.

## When blocked (hook exited 2)

Tool result stderr begins with `AGENTIC LINT -- blocked`. Format:

```
AGENTIC LINT -- blocked. Fix these before proceeding:

- [no-compact] line 42: return compact('result');
- [no-db-facade] line 58: $users = DB::table('users')->get();

Passed checks: rule-a, rule-b
```

Fix every listed violation in the affected file before any other tool call. The hook re-fires on the next Edit and re-checks. Repeat until clear.

## When semantic eval requested (additionalContext)

`hookSpecificOutput.additionalContext` begins with `AGENTIC LINT SEMANTIC EVALUATION REQUIRED` and carries a JSON payload:

```
AGENTIC LINT SEMANTIC EVALUATION REQUIRED:

{
  "file": "src/Evaluators/CachedEvaluator.php",
  "diff": "--- ...before\n+++ ...after\n@@ -28,6 +28,11 @@ ...",
  "passed_checks": ["no-compact", "no-db-facade"],
  "evaluate": [
    {"id": "no-inline-single-use", "description": "...", "severity": "error"},
    {"id": "full-type-hints", "description": "...", "severity": "warning"}
  ],
  "_evaluator_input": "SEMANTIC EVALUATION REQUIRED\n\n<TRUSTED_POLICY>\n...rule policy...\n</TRUSTED_POLICY>\n\n<UNTRUSTED_EVIDENCE>\n...file + diff...\n</UNTRUSTED_EVIDENCE>\n"
}
```

If `evaluate` is empty, proceed with no dispatch and no inline eval.

### Dispatch vs. inline

If the `diff` is short (roughly under 15 lines) AND there is only one rule in `evaluate`, judge it yourself inline against the diff and produce the same VIOLATIONS / NO_VIOLATIONS format below -- skip the subagent. Otherwise dispatch the `bully-evaluator` subagent.

### Dispatch (multi-rule or larger diffs)

Parse the `additionalContext` JSON. If it contains a top-level `_evaluator_input` field, pass that field's value DIRECTLY as the subagent `prompt` -- it's already formatted as a string with `<TRUSTED_POLICY>` and `<UNTRUSTED_EVIDENCE>` boundaries. Do NOT re-serialize it as JSON. If `_evaluator_input` is missing (older harness), fall back to re-serializing the full payload as JSON. This keeps `passed_checks` out of the subagent's context while preserving it for your own use.

Call the Agent tool with `subagent_type: bully-evaluator` and a 3-5 word `description` (e.g. "Evaluate lint rules"). The agent returns:

```
VIOLATIONS:
- [rule-id] line N: <what's wrong>
  fix: <suggestion>

NO_VIOLATIONS:
- rule-id-a
```

If the response is malformed, re-dispatch once. If still malformed, evaluate inline against the diff using the same output format.

### Handling the verdict

For each entry in `VIOLATIONS:`, look up severity in the original `evaluate` array:

- **error**: fix immediately via Edit, using the agent's `fix:` as a starting point, before any other tool call.
- **warning**: note in one sentence, continue.

### Log verdicts for telemetry

After parsing VIOLATIONS / NO_VIOLATIONS (whether from the subagent or from inline eval), record each rule's verdict. For every rule id in the original `evaluate` array, invoke the Bash tool once with:

```
bully --log-verdict --rule <rule-id> --verdict <pass|violation> --file <file-path>
```

Use `violation` if the rule appears in VIOLATIONS, `pass` if it appears in NO_VIOLATIONS. This is a no-op when telemetry is disabled, so always invoke. If `bully` is not on `$PATH`, fall back to invoking the pipeline directly: `python3 "$(ls -d ~/.claude/plugins/cache/*/bully/*/ 2>/dev/null | tail -1)pipeline/pipeline.py"` for plugin installs or `python3 ~/.bully/pipeline/pipeline.py` for the manual install.

## passed_checks

Rules already verified by deterministic script checks. Do not re-investigate their concerns. Use them to catch cross-rule interactions (e.g. a semantic rule that overlaps a passed script rule on an indirect code path).
