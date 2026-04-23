# Design

`bully` is a Claude Code skill system that enforces project coding standards through a two-phase evaluation pipeline: deterministic checks (`script` rules shell out; `ast` rules run structural patterns through ast-grep) for pattern-matchable rules, followed by LLM semantic evaluation for judgment-based rules. It fires on every `Edit` and `Write` tool call via the `PostToolUse` hook.

## Core principles

- **Agent-first.** The primary consumer is an AI coding agent in its tool loop. Not a CLI for humans (though a CLI exists for authoring and CI).
- **Language-agnostic.** PHP, TypeScript, Rust, Python, Go, Ruby — anything. Rules are scoped by file glob, not by language declaration.
- **Hard gate.** Error-severity violations block the tool call via exit code 2. The agent must fix the code before proceeding. Warning severity is reported but non-blocking.
- **Hybrid evaluation.** Deterministic scripts handle what is greppable. The LLM handles judgment calls. Scripts are a fast pre-filter; the LLM always sees code that passed the script phase.
- **Replaces the tool zoo.** One config file, one enforcement point, one violation format. Replaces fragmenting rules across PHPStan, Pint, ESLint, Pest arch tests, and prose in CLAUDE.md.
- **Self-improving over time.** Every run appends a record to a telemetry log. A review skill surfaces noisy, dead, and slow rules so the config can evolve with the codebase.

## Rule format

Rules live in `.bully.yml` at the project root.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique identifier. Used in violation payloads and `passed_checks` context. |
| `description` | yes | What the rule enforces. Natural language. For semantic rules, this IS the evaluation prompt. |
| `engine` | yes | `script`, `ast`, or `semantic`. |
| `scope` | yes | File glob or list of globs this rule applies to. See [Scope](#scope). |
| `severity` | yes | `error` (blocks) or `warning` (reported, non-blocking). |
| `script` | script engine only | Shell command to run. `{file}` is replaced with the file path. Diff provided on stdin. Exit non-zero on violation. |
| `pattern` | ast engine only | An [ast-grep pattern](https://ast-grep.github.io/guide/pattern-syntax.html). Literal code with `$NAME` for single-node captures, `$$$REST` for variadic. |
| `language` | ast engine, optional | Explicit ast-grep language (`ts`, `python`, `php`, ...). Inferred from the matched file extension when omitted. |

### Scope

Scope accepts either a single glob or a list of globs. The rule runs when any glob matches.

```yaml
scope: "*.php"                    # single glob
scope: "src/**/*.ts"              # pathed glob
scope: ["*.php", "*.blade.php"]   # list of globs
scope: "*"                        # everything
```

Matching is right-anchored (`PurePath.match`): `*.php` matches any `.php` file at any depth, `src/*.ts` matches `.ts` files directly under `src/`.

### Example

```yaml
rules:
  no-compact:
    description: "Do not use compact() -- use explicit arrays"
    engine: script
    scope: "*.php"
    severity: error
    script: "grep -n 'compact(' {file} && exit 1 || exit 0"

  bash-strict-mode:
    description: "Bash scripts must set -euo pipefail"
    engine: script
    scope: ["*.sh", "scripts/*"]
    severity: error
    script: "head -5 {file} | grep -q 'set -euo pipefail' || exit 1"

  inline-single-use-vars:
    description: >
      Inline variables that are only referenced once after assignment,
      unless the variable name significantly clarifies intent that would
      be lost by inlining.
    engine: semantic
    scope: "*.php"
    severity: error
```

### Optional fields

- `fix_hint` — script rules only. A one-line suggestion (e.g. `"replace compact() with an explicit array"`) that the pipeline attaches to each emitted `Violation` as `suggestion`. The `bully` skill surfaces it verbatim. Keep it short and mechanical; anything that needs judgment belongs in the `description` of a semantic rule.

### Design decisions

- **No prescribed transformations.** `fix_hint` is a one-liner, not a codemod. The agent still performs the edit — prescribed transformations couple the rule to specific syntax and rot fast.
- **No rule dependencies or ordering.** Rules are independent. If two rules interact, the LLM sees both via the `passed_checks` context.
- **`language` is optional even on ast rules.** Scope globs do most of the targeting work; `language` is escape-hatch for cases where the file extension can't disambiguate (e.g. `.h` could be C or C++).
- **ast-grep is an optional runtime dep.** Without it, `engine: ast` rules are skipped at runtime with a stderr hint and `bully doctor` reports `[FAIL]`. The pipeline itself stays stdlib-only.

## Validation

`.bully.yml` is parsed by a hardened reader in `pipeline/pipeline.py:parse_config`. It stays stdlib-only — no PyYAML — but every failure is loud.

Checks performed on load:

- **Unknown keys** at the config root or inside a rule raise a line-numbered error. A typo like `sevrity:` no longer silently skips the rule.
- **Type-checked fields.** `engine` must be `script` or `semantic`; `severity` must be `error` or `warning`; `scope` must be a string or a list of strings; `script` must be a string.
- **Duplicate rule ids** across the config (or after `extends:` merge) are rejected.
- **Tab characters** in indentation are rejected with the offending line number — the dialect is spaces-only and tabs are the single most common silent-drop cause.
- **Required fields** (`description`, `engine`, `scope`, `severity`, and `script` on script rules) are enforced per rule with the rule id and line number in the error.

Run the checks without firing the hook:

```bash
bully validate
```

Exits 0 on a clean config, 1 with a human-readable report otherwise. `hook.sh` runs `--validate` once per session (keyed off a tmpdir marker) so malformed configs surface on the first edit instead of silently dropping a rule forever.

The stdlib-only invariant holds: the parser is stricter, not heavier.

## Extends

A config can inherit rules from other local or shared `.bully.yml` files:

```yaml
extends:
  - "../shared/bully-base.yml"
  - "./packs/custom.yml"
rules:
  no-console-log:
    severity: warning   # locally override the inherited error
```

Resolution rules (`pipeline/pipeline.py:_resolve_extends_target`):

- `./path` or `../path` resolves relative to the config file containing the `extends:` entry.
- Absolute paths are accepted and used as-is.
- Each referenced file is parsed with the same validator.

Bully does not ship blessed packs. `examples/rules/` is a browseable catalog of common rules organized by tech -- copy what you want, do not `extends:` from it.

Merge order: **local wins**. Rules are merged left-to-right through `extends:`, then the local `rules:` block overrides any inherited rule with the same id. Merging is per-rule, not per-field — redefining `no-console-log` locally replaces the whole rule, it does not patch its `severity` onto the inherited body.

Cycles are detected: if `a.yml` extends `b.yml` which extends `a.yml`, the parser errors with the full chain rather than recursing forever.

## Baseline and disables

Adopting bully on a codebase with pre-existing violations is handled two ways.

### Baseline file

`.bully/baseline.json` records known-at-adoption violations that should not block edits:

```json
{
  "generated_at": "2026-04-16T18:00:00Z",
  "entries": [
    {"rule": "no-console-log", "file": "src/legacy/Debug.tsx", "line": 42, "checksum": "a1b2c3"}
  ]
}
```

`checksum` is a short hash of the offending line's content. On every run, `pipeline.py` filters matching violations before deciding `blocked` vs `evaluate`. An entry drops off the baseline the moment the file stops producing that violation with that checksum — when the code is fixed, the suppression goes with it.

Generate and refresh with `bully baseline init` (run once at adoption) or by hand. The file lives under `.bully/` so it shares the telemetry opt-in shape: no directory, no baseline.

### Per-line disables

For one-off suppressions, a directive comment on the offending line opts that line out of a specific rule:

```python
password = config.get("secret")  # bully-disable: no-hardcoded-secret not a secret, just the key name
```

The syntax is `# bully-disable: <rule-id> <reason>`. Parser is comment-prefix-agnostic — `//`, `#`, `--`, `;` all work. Multiple rule ids can be comma-separated. The reason is required (short human text) so suppressions carry their own justification; `pipeline.py:parse_disable_directive` rejects disables without one.

Directives are scoped to the line they appear on. There is no file-level or block-level form — deliberate; line-scoped disables do not silently widen.

## Short-circuit

Before any rule loads, the pipeline skips files matching a merged set of skip globs. The merge order is:

1. **Built-in defaults** (`pipeline/pipeline.py:SKIP_PATTERNS`):

   ```
   package-lock.json, yarn.lock, pnpm-lock.yaml, poetry.lock, Cargo.lock,
   *.min.js, *.min.css, *.min.*, dist/**, build/**, __pycache__/**,
   *.generated.*, *.pb.go, *.g.dart, *.freezed.dart
   ```

2. **User-global**: `~/.bully-ignore` (one glob per line, `#` comments allowed). Per-machine, never committed.
3. **Project-local**: a top-level `skip:` key in `.bully.yml` -- inline (`skip: ["_build/**", "vendor/**"]`) or block-list form. Inherited from anything the config `extends:`.

```yaml
schema_version: 1
skip: ["_build/**", "vendor/**"]
rules: ...
```

Matches return `pass` before config parse, scope match, or script dispatch. The built-in list is always active; the user-global and project-local lists merge on top. Lockfiles and build artifacts never benefit from lint and firing on them burns cycles on every edit.

## Evaluation pipeline

```
PostToolUse fires (Edit or Write)
       |
       v
  hook.sh walks up to find .bully.yml
       |
       v
  pipeline.py receives JSON payload
  (tool_name, file_path, old_string, new_string)
       |
       v
  build_diff_context:
    - Edit → unified diff with file-anchored line numbers
    - Write → full file content, line-numbered
       |
       v
  filter rules by scope glob
       |
       +-- No matching rules? --> exit 0 (pass)
       |
       v
  Phase 1: run script + ast rules (per-rule 30s timeout)
       - script rules shell out (`{file}` substituted in)
       - ast rules invoke ast-grep with rule.pattern
         (skipped with stderr hint if ast-grep is missing)
       - rules in a single phase run concurrently on a thread pool
         (default workers = min(8, cpu_count or 4); configurable
         via execution.max_workers in .bully.yml or the
         BULLY_MAX_WORKERS env var). Single-rule phases skip the
         pool and run inline. Declaration order is preserved in
         all outputs regardless of completion order.
       - per-rule Python exceptions are isolated: they become a
         blocking severity=error violation with description
         "internal error: <ExcType>: <msg>" and do not abort the
         other rules in the phase.
       |
       +-- Any error-severity violations?
       |     --> print text to stderr
       |         exit 2 (blocks the tool call)
       |
       v
  Phase 2: build semantic evaluation payload
       - file, diff (line-numbered)
       - passed_checks (script rule IDs that passed)
       - evaluate (rule IDs, descriptions, severities)
       |
       v
  hook.sh injects payload via additionalContext
  agent's Edit/Write continues; bully skill
  evaluates the payload on the next turn
       |
       v
  Append telemetry record to .bully/log.jsonl
  (if directory exists)
```

### Diff construction

The pipeline synthesizes the pre-edit file state so it can emit a unified diff with line numbers anchored to the real file on disk.

- **Edit**: reads the current file, replaces `new_string` with `old_string` (first occurrence) to reconstruct the before state, then `difflib.unified_diff(...)` with five lines of context. Line numbers in the diff are real, so the agent can cite them in violations.
- **Write**: emits the full file content with each line prefixed `NNNN:`. Line numbers remain usable for citing violations.
- **Fallback**: if `new_string` cannot be found in the file (e.g. a subsequent edit already replaced it), the pipeline emits a best-effort synthetic diff of the strings alone. The diff is prefixed with a `WARNING: could not anchor to file on disk, line numbers are synthetic` line and the surrounding payload carries `"line_anchors": "synthetic"`. The `bully` skill reads that flag and refuses to cite line numbers it cannot trust; the fallback also emits a `diff_anchor_fallback` record to telemetry so the case is visible in the analyzer.

### Script output adapters

Script rules may print violations in several formats. `parse_script_output` recognizes:

1. JSON object or array with `line`/`message` keys.
2. `file:line:col: message` (ESLint, Ruff, clang, PHPStan compact output).
3. `file:line: message` (mypy, many compilers).
4. `line:content` (grep -n and similar).
5. Anything else — one violation with the raw output as the description.

Unrecognized output is never dropped silently.

### Exit code contract

| Status | stdout | stderr | exit code | Meaning |
|--------|--------|--------|-----------|---------|
| `pass` | JSON result | — | 0 | No matching rules, or all rules passed. |
| `evaluate` | JSON payload | — | 0 | Script phase passed; semantic payload is ready. |
| `blocked` | JSON result | Agent-readable text | 2 | Error-severity script violation. Tool call is blocked. |

Claude Code treats `exit 2` on a `PostToolUse` hook as a blocking signal and surfaces stderr to the agent.

### In-pipeline short-circuits

On top of the auto-generated-file skip (see [Short-circuit](#short-circuit) above), the pipeline bails out early in three cases:

- No matching rules for the file: pipeline returns `pass` immediately.
- Script phase finds error-severity violations: phase 2 is skipped (fix the obvious stuff first).
- No semantic rules for this file type: phase 2 is skipped, pipeline returns `pass`.

### Violation payload (blocked)

```json
{
  "status": "blocked",
  "file": "src/Stores/EloquentRoleStore.php",
  "violations": [
    {
      "rule": "no-compact",
      "engine": "script",
      "severity": "error",
      "line": 42,
      "description": "return compact('result');",
      "suggestion": null
    }
  ],
  "passed": ["no-db-facade", "strict-types"]
}
```

### Semantic payload (evaluate)

```json
{
  "status": "evaluate",
  "file": "src/Evaluators/CachedEvaluator.php",
  "diff": "--- src/Evaluators/CachedEvaluator.php.before\n+++ src/Evaluators/CachedEvaluator.php.after\n@@ -28,6 +28,11 @@\n...",
  "passed_checks": ["no-compact", "no-db-facade"],
  "evaluate": [
    {
      "id": "inline-single-use-vars",
      "description": "Inline variables that are only referenced once after assignment...",
      "severity": "error"
    }
  ]
}
```

The `passed_checks` list is load-bearing. It tells the LLM which concerns have already been verified deterministically — so the LLM does not re-investigate them, and so it can catch cross-rule interactions the scripts miss independently.

## Telemetry and self-improvement

Telemetry is opt-in: the pipeline writes to `.bully/log.jsonl` only when that directory exists next to the config.

Each record:

```json
{
  "ts": "2026-04-16T18:00:00Z",
  "file": "src/Foo.php",
  "status": "blocked",
  "latency_ms": 20,
  "rules": [
    {"id": "no-compact", "engine": "script", "verdict": "violation", "severity": "error", "line": 15, "latency_ms": 9},
    {"id": "no-db-facade", "engine": "script", "verdict": "pass",      "severity": "error", "latency_ms": 6},
    {"id": "inline-single-use-vars", "engine": "semantic", "verdict": "evaluate_requested", "severity": "error"}
  ]
}
```

The `bully-review` skill runs `analyzer.py` over this log and classifies each rule as **noisy** (violation rate above threshold), **dead** (never fired in the window), or **slow** (mean latency above threshold). See [telemetry.md](telemetry.md).

## Stack detection and baseline generation

The `bully-init` skill bootstraps the project config. Run once.

### Detection

Scans the project root for manifest files. Detection reads specific version and framework information (e.g. `composer.json` with `laravel/framework` at `^12.0` and `php: ^8.4`), and that specificity shapes the baseline.

### Linter routing (the cop, not the lawmaker)

The init skill detects installed linters (ruff, biome, eslint, tsc, phpstan, rubocop, clippy, …) and offers to wire each one as a passthrough rule -- an `engine: script` rule whose command invokes the linter on the edited file. The linter keeps owning its rule definitions; bully's PostToolUse hook enforces "pass the linter" on every edit.

This is the default routing for any rule an installed linter can express. Passthroughs:

1. Get the linter's full rule catalogue, author-tested parser, and IDE integration for free.
2. Keep `.bully.yml` short -- a single rule replaces dozens of grep patterns.
3. Stay legible in `bully-review` telemetry as one entry per tool.

If a conventional linter for the stack is missing, the init skill *offers* to install it (with explicit user approval -- never silently, because it touches project manifests or CI). If the user declines, the rules route through `ast`/`script`/`semantic` inside `.bully.yml` directly.

Custom rules (CLAUDE.md sections, arch tests, team conventions that no off-the-shelf linter expresses) go through the same four-option routing as `bully-author`: linter passthrough (if a plugin covers it) → ast → script → semantic.

### Baselines

The init skill seeds with whatever the installed linters already bring plus any custom rules the user migrated. The baseline set stays deliberately small -- the installed linters do the heavy lifting; `.bully.yml` holds what they can't express.

## CLAUDE.md integration

`.bully.yml` is the primary config. CLAUDE.md is a secondary rule source during init.

- **Init-time migration, not runtime parsing.** The `bully-init` skill reads CLAUDE.md once during bootstrap, extracts structured style rules, and migrates them into `.bully.yml`. At runtime, the pipeline reads only the YAML.
- **Deduplication at init time.** The init skill compares migrated CLAUDE.md rules against rules it generated from other sources and deduplicates before writing the YAML.
- **CLAUDE.md stays readable.** The init skill does not strip rules from CLAUDE.md. Those rules still serve as human-readable documentation; they stop being the enforcement mechanism.
- **No magic parsing.** The init skill looks for structured sections (bulleted lists under headings like "Style Rules", "Conventions", "Coding Standards") and treats each bullet as a candidate rule. Unstructured prose is ignored.

## What this replaces

For a Laravel package:

**Eliminated entirely:**
- Pest arch tests (namespace boundaries, contract enforcement, dependency direction).
- CLAUDE.md style rules as an enforcement mechanism (migrated to YAML; prose stays as documentation).

**Kept but demoted (optional, opt-in):**
- Pint: can still run via a script rule, or stay in pre-commit/CI.
- PHPStan: same.

For a different stack (Next.js, Rust CLI, Django), the same pipeline with different YAML rules replaces that stack's equivalent fragmented tooling. Same shape, different rules.

## Skills

Four skills cover the lifecycle:

- **`bully-init`** — bootstraps `.bully.yml` once per project (zero → something).
- **`bully`** — interprets hook output (blocked text or semantic evaluation payload) during every Edit/Write cycle. Not user-invocable.
- **`bully-author`** — adds, modifies, or removes rules. Tests every rule against a fixture before writing to the config. User-invocable.
- **`bully-review`** — audits rule health from the telemetry log and hands concrete recommendations off to the author skill. User-invocable.
