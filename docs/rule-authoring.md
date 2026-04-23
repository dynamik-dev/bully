# Rule authoring

How to write, scope, and test rules in `.bully.yml`.

This doc covers the concepts. For the interactive flow — drafting a rule from a natural-language request, testing it against fixtures, and writing to the config — use the `bully-author` skill:

```
> add a lint rule that bans var_dump() in PHP
> tighten no-db-facade -- it's noisy
> remove deprecated-carbon
```

The skill applies the discipline described here. This doc is the reference; the skill is the workflow.

## Where rules live (four options, one cop)

Bully is the cop; native linters (ruff, biome, eslint, tsc, phpstan, rubocop, clippy, …) are the lawmakers. The PostToolUse hook runs on every Edit/Write regardless; the routing question is just *where a rule's definition lives*.

Priority order when authoring a rule:

1. **Linter passthrough** -- an installed (or reasonably installable) linter can express the rule via a config change. The rule definition lives in the linter's config; `.bully.yml` gets a one-line passthrough (`engine: script`, `script: "<linter> <args> {file}"`). This is the default for anything a linter already covers cleanly.
2. **`engine: ast`** -- structural pattern, no linter covers it. Matches code structure, so it ignores comments/strings/formatting. Requires `ast-grep` on `$PATH`.
3. **`engine: script`** -- textual pattern with no structural false-positive risk (filename conventions, forbidden imports, required header comments, "no `TODO` without a ticket number").
4. **`engine: semantic`** -- judgment only an LLM can make ("inline single-use vars", "error messages should be actionable", "this migration isn't idempotent").

Engine-wise there are only two lanes -- `script` (bash subprocess) and `semantic` (evaluator subagent) -- but the four authoring categories pick the sharpest tool for each rule.

| Use a linter passthrough when… | Use an `ast` rule when… | Use a `script` (grep) rule when… | Use a `semantic` rule when… |
|---|---|---|---|
| An installed linter's existing rule (or a rule plugin) covers the concern. Enabling it there is cheaper than writing a fresh pattern. | The violation is a **code-structure** pattern (call, cast, declaration) and grep would false-positive on strings/comments. | The violation is a genuinely textual pattern (path, header, literal string) with no structural ambiguity. | The violation requires **judgment**. A mechanical rule would have too many false positives. |
| You want CI, IDE, and pre-commit to get the same rule for free. | You want deterministic, fast, whitespace-invariant matching. | You want the rule to work in CI without any extra dependency. | The rule depends on context (how a variable is used elsewhere) or is a prose style guideline. |

**Why passthrough usually wins over grep.** Native linters already have mature rule catalogues, author-tested parsers, and IDE integration. Competing with them via grep produces brittle rules that break on edge cases they solved years ago. Reach for grep only when the pattern is genuinely textual (no code-structure nuance) and no linter expresses it.

**Performance ballpark.** Linter passthroughs are a subprocess call -- whatever the linter costs (usually tens of ms). Raw `grep` script rules are milliseconds. AST rules are ~10-50 ms per file. Semantic rules cost an LLM turn. Promote noisy semantic rules to ast or passthrough when a stable mechanical fix exists.

**AST prerequisite:** `ast-grep` must be on `$PATH` (`brew install ast-grep` or `cargo install ast-grep`). If it isn't, `engine: ast` rules are skipped at runtime with a one-line stderr hint — they do not block edits. Run `bully doctor` to see which rules would be skipped.

## Script rule skeleton

```yaml
rule-id:
  description: "One-sentence description."
  engine: script
  scope: "*.php"
  severity: error   # or warning
  script: "command {file}"
```

- `{file}` is replaced with the target file path.
- The diff is available on stdin if the script reads it.
- Exit 0 = pass. Exit non-zero = violation.
- Stdout on failure should list violations. The pipeline parses common formats (see [Output formats](#output-formats)).
- Each rule has a 30-second timeout.

### YAML string escapes

Double-quoted scalars process YAML escapes: `\\` becomes `\`, `\"` becomes `"`, `\n` becomes newline, `\t` becomes tab. Single-quoted scalars only process `''` as `'` (YAML spec); backslashes pass through literally.

The practical upshot for regex in `script:` values: write `"grep -nE 'console\\.log\\(' {file}"` — YAML turns each `\\` into `\`, so grep sees `console\.log\(` and matches the intended pattern. Under-escaping (`"grep -nE 'console\.log\('"`) leaves grep with `console.log(`, where `.` matches any character and the parens become a group.

For a literal backslash in the script, use single-quoted YAML (`'grep "\\"'`) or double up explicitly in double quotes.

### Minimal grep pattern

```yaml
no-compact:
  description: "Do not use compact() -- use explicit arrays"
  engine: script
  scope: "*.php"
  severity: error
  script: "grep -n 'compact(' {file} && exit 1 || exit 0"
```

The `&& exit 1 || exit 0` idiom inverts grep's default: non-zero when a match is found (violation), zero otherwise (pass).

### Using negative lookahead (grep -P)

```yaml
todo-with-ticket:
  description: "TODO/FIXME must reference a ticket id (#123 or ABC-45)"
  engine: script
  scope: "*"
  severity: warning
  script: "grep -nE '(TODO|FIXME)' {file} | grep -vE '(#[0-9]+|[A-Z]+-[0-9]+)' && exit 1 || exit 0"
```

### Header check

```yaml
bash-strict-mode:
  description: "Bash scripts must set -euo pipefail in the first five lines"
  engine: script
  scope: "*.sh"
  severity: error
  script: "head -5 {file} | grep -q 'set -euo pipefail' || exit 1"
```

### Shelling out to an existing linter

```yaml
pint-formatting:
  description: "Code must pass Laravel Pint formatting"
  engine: script
  scope: "*.php"
  severity: warning   # slow external tool — prefer warning to avoid blocking every edit
  script: "vendor/bin/pint --test {file}"
```

Slow shell-outs block the edit loop. Use `severity: warning` unless you are certain the tool runs quickly enough to run on every edit.

### Optional: `fix_hint`

Add a `fix_hint` to any script rule to give the agent a one-line mechanical suggestion:

```yaml
no-compact:
  description: "Do not use compact() -- use explicit arrays"
  engine: script
  scope: "*.php"
  severity: error
  script: "grep -n 'compact(' {file} && exit 1 || exit 0"
  fix_hint: "replace compact('foo', 'bar') with ['foo' => $foo, 'bar' => $bar]"
```

The pipeline passes the string through unchanged as `suggestion` on every `Violation` the rule produces. The `bully` skill already renders `suggestion`, so the hint shows up next to the violation text with no other plumbing.

Keep hints short, mechanical, and universally applicable to the rule — anything that depends on surrounding code belongs in a semantic rule's `description` instead. There is no placeholder syntax; the hint is static text per rule.

## AST rule skeleton

```yaml
rule-id:
  description: "One-sentence description."
  engine: ast
  scope: ["*.ts", "*.tsx"]
  severity: error
  pattern: "$EXPR as any"
  language: ts        # optional; inferred from the scope's file extension when unambiguous
```

- `pattern` is an [ast-grep pattern](https://ast-grep.github.io/guide/pattern-syntax.html): literal code with `$NAME` for single-node captures and `$$$REST` for variadic captures.
- `language` picks the tree-sitter grammar (`ts`, `tsx`, `js`, `python`, `go`, `rust`, `php`, `csharp`, `java`, …). If omitted, bully infers it from the edited file's extension. Set it explicitly when a pack covers multiple extensions that map to different grammars (e.g. `.ts` vs `.tsx`).
- No `script` field.
- Exit is implicit: a non-empty match list = violations; empty = pass.
- Each rule has a 30-second timeout, same as `script`.

### Minimal ast pattern

```yaml
no-var-dump:
  description: "Do not leave var_dump() calls in committed code."
  engine: ast
  scope: "*.php"
  severity: error
  pattern: "var_dump($$$)"
```

`var_dump($$$)` matches any call to `var_dump` regardless of argument shape or count, and ignores matches in strings or comments — the same rule as `grep 'var_dump'` but without the false positives.

### Why prefer ast over grep for structural rules

```yaml
# Fragile: grep matches inside strings and comments
no-db-facade-script:
  engine: script
  scope: "*.php"
  script: "grep -n 'DB::' {file} && exit 1 || exit 0"

# Precise: only real static method calls on the DB class match
no-db-facade-ast:
  engine: ast
  scope: "*.php"
  pattern: "DB::$METHOD($$$)"
```

The grep version fires on `// the DB:: facade is banned` and on `$msg = "DB::something"`. The ast version matches only actual scope-resolution calls on the identifier `DB`.

### When ast-grep isn't installed

If you author an `engine: ast` rule but ast-grep isn't on `$PATH`, the pipeline prints a one-line stderr hint and skips the rule — it does not block the edit. `bully validate` surfaces this as a `[WARN]`; `bully doctor` surfaces it as a `[FAIL]` so installs can be caught in CI.

## Semantic rule skeleton

```yaml
rule-id:
  description: >
    Full description that explains exactly what the rule enforces.
    This text IS the evaluation prompt the LLM uses -- write it with
    that in mind. Be specific about what counts as a violation.
  engine: semantic
  scope: "*.php"
  severity: error
```

Key points:
- No `script` field.
- Description should be prescriptive: what counts as a violation, what the fix looks like.
- Longer descriptions are fine when they disambiguate (use YAML folded scalars `>` for multi-line).
- Think of the description as instructions to a careful reviewer, not as documentation for a human reader.

### Well-scoped semantic rule

```yaml
inline-single-use-vars:
  description: >
    Inline variables that are only referenced once after assignment,
    unless the variable name significantly clarifies intent that would
    be lost by inlining. Example violation:
      $result = $this->query->get(); return $result;
    Example compliant:
      return $this->query->get();
  engine: semantic
  scope: "*.php"
  severity: error
```

Including an inline example tightens the LLM's interpretation without bloating the prompt. Keep it short.

### Over-broad semantic rule (avoid)

```yaml
good-code:
  description: "Code should be clean and maintainable"
  engine: semantic
  scope: "*"
  severity: warning
```

This fires unpredictably. It is a noise source, not a rule. If you cannot describe a specific behavior that counts as a violation, the rule is not ready.

## Scoping

Scope is right-anchored glob matching via `PurePath.match`.

| Pattern | Matches |
|---------|---------|
| `*.php` | any `.php` file at any depth |
| `src/*.ts` | `.ts` files directly under `src/` |
| `src/**/*.ts` | `.ts` files anywhere under `src/` |
| `*` | everything |
| `["*.php", "*.blade.php"]` | any file matching either glob |

`**` works on all supported Python versions because bully's matcher implements recursive globbing explicitly, not via `PurePath.match` (whose recursive-`**` support only landed in 3.13).

Scope narrowly. Broad scopes (like `"*"`) run more rules per edit and inflate the noise floor. Save `"*"` for truly cross-language rules like orchestration-label bans.

## Output formats

Script rules may print violations in several formats. The pipeline's adapter recognizes, in order:

1. **JSON object** with `line`/`message` keys.
2. **JSON array** of such objects.
3. **`file:line:col: message`** — ESLint, Ruff, clang, PHPStan compact output.
4. **`file:line: message`** — mypy, many compilers.
5. **`line:content`** — `grep -n` and similar.
6. **Anything else** — the raw output becomes the violation description (truncated to 500 chars). No output is dropped silently.

Prefer formats that include a line number. Violations with line numbers are more actionable for the agent.

## Sharing rules across repos with `extends:`

Bully does not ship blessed packs. If you want to maintain a shared baseline across your own repos, point `extends:` at any path:

```yaml
schema_version: 1
extends:
  - "../shared/bully-base.yml"
  - "/opt/company/lint/security.yml"

rules:
  # override an inherited rule by redefining it locally
  no-console-log:
    description: "No console.log in production code."
    engine: script
    scope: ["src/**/*.ts", "src/**/*.tsx"]
    severity: warning   # was error upstream
    script: "grep -nE 'console\\.log\\(' {file} && exit 1 || exit 0"
```

Resolution:

- `./path` and `../path` resolve relative to the config file.
- Absolute paths resolve as-is.
- Local `rules:` override inherited rules by id (whole-rule replacement — fields are not merged).

Looking for rules to copy in directly? Browse `examples/rules/` -- a catalog of common rules by tech. Copy what fits, skip the rest; these are examples, not a baseline.

Cycles and unknown references fail loud at parse time. See [design.md#extends](design.md#extends) for the full semantics.

## Validating a rule

Before committing a rule, run the validator:

```bash
bully validate
```

It parses `.bully.yml` (plus anything it extends) with the same hardened reader the hook uses. The validator reports:

- Unknown keys with the line number where they appeared.
- Wrong types (`severity: "fatal"`, `scope: 42`, etc.).
- Duplicate rule ids across the extends chain.
- Tab-indented lines.
- Missing required fields (script rules without `script:`, any rule without `scope:`).
- Unresolvable `extends:` targets and cycles.

Exit code 0 means clean. Exit code 1 prints a report and a non-zero count. Wire it into CI to catch config drift before a hook run does.

`hook.sh` calls `--validate` once per session, so a malformed config surfaces on the first edit rather than silently dropping rules across hundreds of edits.

## Disabling per line

For one-off suppressions — a known-safe pattern the rule can't see around — add a directive comment on the offending line:

```python
token = os.environ["TEST_TOKEN"]  # bully-disable: no-hardcoded-secret env lookup, not a literal
```

```typescript
const debug = (...args: unknown[]) => console.log(...args); // bully-disable: no-console-log dev-only helper
```

Rules:

- Syntax: `<comment-prefix> bully-disable: <rule-id>[,<rule-id>...] <reason>`.
- Any comment prefix works: `#`, `//`, `--`, `;`.
- Reason is required. No reason, no suppression.
- Scoped to the single line the directive appears on. There is no block or file-level form.

The directive is enforced by `pipeline/pipeline.py:parse_disable_directive`. Misspelled rule ids or missing reasons are surfaced as warnings in the hook output so dead directives don't accumulate.

Prefer baselining existing codebases via `.bully/baseline.json` (see [design.md#baseline-and-disables](design.md#baseline-and-disables)); reserve per-line disables for genuine exceptions.

## Testing a rule

Without triggering an Edit, run the pipeline manually:

```bash
# Full pipeline against a file
bully lint src/foo.php

# Just one rule (isolate it)
bully lint src/foo.php --rule no-compact

# See the semantic evaluation prompt without calling an LLM
bully lint src/foo.php --print-prompt

# Supply a diff manually (bypasses stdin/file-state inference)
bully lint src/foo.php --diff "$(git diff src/foo.php)"
```

Two more flags worth knowing about:

- **`bully --explain --file <path>`** — prints one line per rule in scope with its verdict: `fire`, `pass`, `skipped <reason>`, or `dispatched`. Reach for this when a rule *should* match a file but isn't firing — the output tells you whether the scope filter dropped it or the rule ran and passed. Already present; surfaced here because it's easy to miss.
- **`bully validate --execute-dry-run`** — runs every script rule against empty input and flags rules that error out at the shell or regex level. Catches the "grep: parentheses not balanced" class of bugs at config time instead of at hook time. Run it before committing a new regex rule.

Exit codes:

- `0` — pass or evaluate. Stdout is JSON.
- `1` — usage error.
- `2` — blocked. Stderr is agent-readable text; stdout is JSON.

Combine `--rule` with `--print-prompt` while iterating on a semantic rule's description: you see exactly what the LLM would be asked, without spending a turn.

## Severity choice

- **error** — blocks the edit via exit 2. Use for correctness invariants: banned patterns, type safety, architectural boundaries, critical formatting.
- **warning** — reported but non-blocking. Use for:
  - New rules being trialed (promote to error once confidence is high).
  - Slow external linters (pint, phpstan) where the signal matters but blocking every edit is too costly.
  - Style preferences where a violation is not strictly wrong.

Promotion pathway: start a new rule as `warning`. Run it for a few hundred edits. Check the `bully-review` report. If the violation rate is low and fixes are clean, promote to `error`. If noisy or flapping, adjust the rule or keep it a warning.

## Rule quality checklist

Before committing a new rule, verify:

- [ ] The description reads as a clear instruction, not as project documentation.
- [ ] The scope is as narrow as possible.
- [ ] For script rules: the command exits 0 on a clean file and non-zero on a violation.
- [ ] For semantic rules: the description includes at least one example of a violation and one compliant alternative, or is so specific that examples are redundant.
- [ ] The id is unique and uses `kebab-case`.
- [ ] The severity matches intent (error blocks; warning reports).
- [ ] The rule has been tested with `--rule` against both a clean and a violating file.
