---
name: bully-author
description: Authors, modifies, or removes rules in `.bully.yml`. Use when the user says "add a lint rule for X", "ban Y", "tighten <rule-id>", "make <rule-id> a warning", "convert <rule-id> to semantic", "remove <rule-id>", "change the scope of <rule-id>", or asks to apply recommendations from `/bully-review`. Always tests a rule against a fixture before writing it to the config.
metadata:
  author: dynamik-dev
  version: 1.0.0
  category: workflow-automation
  tags: [linting, rule-authoring, config-editing, self-improvement]
---

# Agentic Lint Author

Interactive authoring for `.bully.yml`. Every proposed rule is tested against a fixture before being written.

If no `.bully.yml` exists, stop and tell the user to run `/bully-init` first.

See `docs/rule-authoring.md` for full field reference and the rule quality checklist.

## Triggers

- "Add a lint rule for X" / "Ban Y"
- "Tighten `<rule-id>`" / "Make `<rule-id>` a warning" / "Promote `<rule-id>` to error"
- "Convert `<rule-id>` to semantic" (or vice versa)
- "Change the scope of `<rule-id>`"
- "Remove `<rule-id>`"
- "Apply the `/bully-review` recommendations"

Not triggered by bootstrap (`bully-init`), audit (`bully-review`), or hook-output interpretation (`bully`).

## Where the rule lives (four routing options)

Bully is the cop; linters are the lawmakers. The PostToolUse hook runs on every Edit/Write regardless of *where* a rule's definition lives. The routing question is just which tool the hook invokes to check the file. In priority order:

1. **Linter passthrough** -- an installed (or reasonably installable) linter can enforce this with a rule-config change. The rule definition lives in the linter's config (`ruff.toml`, `biome.json`, `eslint.config.*`, `phpstan.neon`, …). `.bully.yml` gets a passthrough rule: `engine: script`, `script: "<linter> <args> {file}"`. Bully still enforces on every edit -- the linter just owns what "violation" means.
2. **ast** -- structural pattern, no linter covers it: "no `as any` cast", "no empty catch", "no public mutable property". Uses `ast-grep`, so matches ignore comments/strings/formatting. Prefer this over grep when meaning depends on syntactic context.
3. **script (grep/awk)** -- textual pattern, no structure needed: filename conventions, forbidden import paths, "no `TODO` without a ticket number", required header comments. Regex is the right tool here.
4. **semantic** -- judgment only an LLM can make: "inline single-use vars", "error messages should be actionable", "this migration isn't idempotent".

### Decision tree

Ask in order:

- Could an **installed** linter's existing rule or plugin cover this via a config change? → passthrough.
- Could an **installable** linter (ruff, biome, eslint-plugin-X, …) cover this? → propose installing, then passthrough (see pre-flight below).
- Does the meaning depend on syntactic context (call site, cast, declaration, nesting)? → `engine: ast`.
- Is it a textual pattern that a grep can express without false positives on comments/strings? → `engine: script`.
- Does it need judgment? → `engine: semantic`.

If unsure, ask the user. Do not silently skip a tier. In particular: do not reach for grep when an installed linter or ast-grep would catch it cleanly.

### Enforcement-guarantee line (say this when recommending a linter)

When recommending option 1, always include this clarification once per conversation:

> I'd enable this rule in `<linter>`'s config. Bully still enforces it on every Edit/Write via a passthrough rule -- the question is just *where the rule definition lives*, not whether bully enforces it.

The user might otherwise assume "put it in the linter" means "remove it from bully's scope." It doesn't. The passthrough rule is what makes the guarantee hold; if the user picks option 1 and you forget to add the passthrough rule, the linter is just sitting there hoping someone runs it.

### Linter passthrough pre-flight

Before proposing option 1, detect whether the linter is installed:

```bash
command -v <linter> >/dev/null && echo OK || echo MISSING
```

- **Installed**: propose the linter-config edit + the `.bully.yml` passthrough rule. Show both diffs before writing.
- **Missing**: present it as a *choice*, not a default: "I'd recommend installing `<linter>` so the rule can live there. Install command: `<cmd>`. Or, if you'd rather keep this in bully directly, I can write a grep/ast rule instead." Wait for the user's call. Installing touches project manifests or CI, so never install silently.

### ast-grep dependency pre-flight

Before proposing an `engine: ast` rule, probe availability:

```bash
command -v ast-grep >/dev/null && echo OK || echo MISSING
```

If `MISSING`, do not silently draft an `engine: ast` rule. Tell the user: "This rule would work best as `engine: ast`, but ast-grep isn't installed. Either: (a) run `brew install ast-grep` (or `cargo install ast-grep`) and I'll proceed, or (b) I'll fall back to `engine: script` with a grep pattern (with the usual false-positive tradeoffs)." Wait for their choice before drafting.

### Example passthrough rules

```yaml
  ruff-check:
    description: "Code must pass ruff check."
    engine: script
    scope: ["*.py"]
    severity: error
    script: "ruff check --quiet {file}"

  biome-lint:
    description: "Code must pass biome lint."
    engine: script
    scope: ["*.ts", "*.tsx", "*.js", "*.jsx"]
    severity: error
    script: "biome lint --reporter=summary {file}"
```

Keep lint, format, and typecheck as **separate** passthrough rules -- failure modes and messages are distinct, and `bully-review` telemetry stays legible.

## Scope globs

- `PurePath.match` is right-anchored. `*.ts` matches `foo.ts` and `src/foo.ts`. `src/*.ts` is single-level only. `**/foo.ts` for deep matches.
- Use the narrowest glob that covers the target files.
- List form for multiple extensions: `["*.php", "*.blade.php"]`.

## Severity

- `warning` for new or trial rules.
- `error` only when confidence is high and a false positive is acceptable as a block.

## Fixture-testing protocol (MANDATORY)

Never write a rule to `.bully.yml` without running this protocol first.

### Binary resolution

Plugin installs don't put `bully` on `$PATH`. Resolve it once at the top of the protocol and use `$BULLY` in every command below:

```bash
BULLY=$(command -v bully 2>/dev/null || ls -d ~/.claude/plugins/cache/*/bully/*/bully 2>/dev/null | sort -V | tail -1)
```

If `$BULLY` is empty, fall back to `python3 ~/.bully/pipeline/pipeline.py` for manual installs.

### Steps

1. Create two fixture files with the Write tool:
   - `/tmp/bully-probe-violating.<ext>` -- must trigger the rule.
   - `/tmp/bully-probe-clean.<ext>` -- must not trigger.
2. Copy the current config to a draft:
   ```bash
   cp .bully.yml /tmp/bully-draft.yml
   ```
3. Edit `/tmp/bully-draft.yml` to append the proposed rule.
4. **Trust the draft** so script and ast rules will actually execute. Untrusted configs return `status: untrusted` with rules silently skipped, so this step is what makes the lint result meaningful:
   ```bash
   $BULLY --trust --config /tmp/bully-draft.yml
   ```
   Use `--trust --refresh` instead if you re-edit the draft after this step (each edit invalidates the trust seal).
5. Run the pipeline with `--rule` against each fixture:
   ```bash
   # Script rule -- violating must exit 2, clean must exit 0
   $BULLY --file /tmp/bully-probe-violating.<ext> \
     --config /tmp/bully-draft.yml \
     --rule <new-rule-id>

   $BULLY --file /tmp/bully-probe-clean.<ext> \
     --config /tmp/bully-draft.yml \
     --rule <new-rule-id>
   ```
6. For **semantic rules**, use `--print-prompt` instead of asserting exit codes. Read the rendered prompt and confirm it would correctly judge both fixtures. If unclear, sharpen the description and re-test.

   Then run `--explain` against the violating fixture to confirm the rule is actually being dispatched, not silently dropped by the can't-match heuristics:

   ```bash
   $BULLY --file /tmp/bully-probe-violating.<ext> \
     --config /tmp/bully-draft.yml \
     --rule <new-rule-id> \
     --explain
   ```

   The line for `<new-rule-id>` must show `dispatched`, not `skipped (empty-diff)`, `skipped (whitespace-only-additions)`, `skipped (comment-only-additions)`, or `skipped (pure-deletion-add-perspective-rule)`. The last fires when the rule's description contains an "avoid" trigger word (`avoid`, `no`, `ban`, `forbid`, `don't`) and the diff is pure deletions — an "avoid X" rule cannot fire on a pure deletion because deletions don't introduce X. If skipped, ensure the fixture contains real (non-comment, non-whitespace) added lines or supply a `--diff` that does.
7. For **ast rules**, the same exit-code protocol as script rules: violating must exit 2, clean must exit 0. Additionally verify the pattern directly with ast-grep before writing to the draft:
   ```bash
   ast-grep run --pattern '<pattern>' --lang <ts|csharp|php|…> /tmp/bully-probe-violating.<ext>
   ast-grep run --pattern '<pattern>' --lang <ts|csharp|php|…> /tmp/bully-probe-clean.<ext>
   ```
   The first invocation must print at least one match; the second must print nothing.
8. Only on pass, proceed to the write step.
9. Clean up: `rm -f /tmp/bully-probe-*.* /tmp/bully-draft.yml`.

Invariants: fixtures exist before testing; both violating and compliant fixtures (or `--print-prompt`) are exercised; the draft is trusted before linting; the draft config is used, not the real one; exit codes match expectations before writing.

## YAML edit pattern for `.bully.yml`

The parser is fixed-indent. Do not reformat the file.

```yaml
  rule-id:          # 2-space indent, trailing colon
    description: … # 4-space indent
    engine: script | semantic | ast
    scope: "*.ext" # or ["*.a", "*.b"]
    severity: warning | error
    script: "…{file}… && exit 1 || exit 0"   # script rules only
    pattern: "$EXPR as any"                    # ast rules only
    language: ts                                # ast rules only (optional; inferred from scope)
```

- 2-space indent for rule ids, 4-space for fields, 6+ for folded scalar continuations.
- Double-quote script values containing special chars.
- Inline comments allowed.
- Append new rules to the end of the `rules:` block.
- Only touch lines belonging to the rule being added, modified, or removed.

## Adding a new rule

1. Route using the four-option decision tree in "Where the rule lives". If the answer is **linter passthrough**, run the linter pre-flight (installed vs missing), propose the linter-config edit + the `.bully.yml` passthrough rule, and say the enforcement-guarantee line. If **ast**, run the ast-grep pre-flight.
2. Collect `id` (kebab-case, unique), `description`, `engine`, `scope`, `severity`, plus `script` (script and linter-passthrough rules), `pattern` + optional `language` (ast rules), or no extra field (semantic rules).
3. Run the fixture-testing protocol.
4. Edit `.bully.yml` to append the rule. For linter passthroughs, also edit the linter's config in the same step and show both diffs before writing.
5. Sanity-check against 2-3 existing project files (use the `$BULLY` you resolved during the protocol; re-resolve with the one-liner above if running in a fresh shell):
   ```bash
   $BULLY --file <existing-file> --rule <new-rule-id> --config .bully.yml
   ```
   In this repo, also run `bash scripts/dogfood.sh`. If the rule mass-flags the codebase, narrow it or treat the flags as real cleanup.
6. Report and invite the user to review before committing.

## Modifying an existing rule

1. Use Read to locate the `  <rule-id>:` block (runs to the next `  <next-id>:` or EOF).
2. Apply the change:
   - Severity: swap `severity: error` / `severity: warning`.
   - Scope: replace the `scope:` line.
   - Script: replace the `script:` line; keep `{file}` as the placeholder.
   - Description: replace the `description:` line (or the indented continuation for folded scalars).
   - Engine switch: change `engine:` and add/remove the `script:` line; rewrite the description accordingly.
3. Rerun the fixture-testing protocol against fresh fixtures. Cosmetic-looking changes can shift behavior.
4. Sanity-check and report.

## Removing a rule

1. Confirm it is genuinely unused:
   ```bash
   grep '"id": "<rule-id>"' .bully/log.jsonl | tail -10
   ```
   Noisy != dead. If the rule has fired recently, challenge the removal and propose tightening.
2. Delete from `  <rule-id>:` through the last field line of that block.
3. Sanity-check (re-resolve `$BULLY` with the one-liner from the fixture-testing protocol if needed):
   ```bash
   bash scripts/dogfood.sh
   # or
   $BULLY --file <existing-file> --config .bully.yml
   ```

## Applying review recommendations

Apply one recommendation at a time. Test each before moving on. Never batch.

| Finding | Action |
|---|---|
| Noisy script rule | Tighten regex (word boundaries, exclude docblocks). Re-test. |
| Noisy semantic rule | Sharpen the description; add an example. Re-test with `--print-prompt`. |
| Dead rule, scope wrong | Broaden the scope; if still dead, propose removal. |
| Dead rule, obsolete | Remove. |
| Slow rule | Demote to `warning` or move to CI. |
| Semantic rule with stable mechanical fix | Draft an equivalent script or ast rule, test, layer it alongside -- do not replace. |
| Script rule noisy due to string/comment false positives | Convert to `engine: ast` with a structural `pattern:`. Verify ast-grep is installed first. |
| Script rule grep-matching a pattern an installed linter could express | Move the rule into the linter's config; replace the `.bully.yml` rule with a passthrough (`script: "<linter> … {file}"`). Say the enforcement-guarantee line. |

## Troubleshooting

- **Rule id collision**: propose a semantic alternative (not a version suffix), or treat as a modification.
- **Pattern matches too much**: add `[^a-zA-Z_]` guards, anchor at line start, exclude comments via `grep -v`.
- **Pattern does not match**: try `grep -E` or `-P`; test the raw pattern against the fixture before wrapping in `&& exit 1 || exit 0`.
- **Scope mismatches**: test in Python -- `PurePath(path).match(glob)`.
- **Editing `.bully.yml` triggers the hook**: harmless unless a `*.yml`-scoped rule flags the config itself; fix the scope.
