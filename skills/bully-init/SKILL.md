---
name: bully-init
description: Bootstraps a project's .bully.yml by detecting the tech stack from manifest files, migrating rules from existing linting tools, and generating a baseline config. Use when user says "init agentic lint", "set up agentic lint", "bootstrap lint config", "create lint rules", "bully init", "initialize agentic lint", or asks to create or generate an agentic lint configuration.
metadata:
  author: dynamik-dev
  version: 1.0.0
  category: workflow-automation
  tags: [linting, code-quality, config-generation, stack-detection]
---

# Agentic Lint Init

Generate a baseline `.bully.yml` by detecting the stack, wiring installed linters as passthroughs, and routing project-specific rules to the right enforcement mechanism.

## Framing: cop vs lawmakers

Bully is the cop; native linters (ruff, biome, eslint, tsc, phpstan, rubocop, golangci-lint, clippy, …) are the lawmakers. The PostToolUse hook runs on every Edit/Write, so bully is always the entry point to enforcement. Where a rule *definition* lives is a separate question:

- **Linter passthrough** -- an installed linter owns the rule; `.bully.yml` has a passthrough (`engine: script`, `script: "<linter> <args> {file}"`) that invokes it on every edit.
- **ast / script / semantic** -- the rule lives directly in `.bully.yml`.

This skill is user-driven. Do not silently install tools or migrate rules. Every step below is a *proposal* the user accepts or declines.

## Step 1: Detect the stack

Read manifest files in the project root and map them to rule packs:

| Manifest | Pack candidates |
|---|---|
| `composer.json` | `php`, `laravel` (if `laravel/framework`), `symfony` (if `symfony/framework-bundle`) |
| `package.json` | `node`, `typescript` (if `tsconfig.json` or `devDependencies.typescript`), `react`, `vue`, `next`, `svelte` |
| `pyproject.toml` / `requirements.txt` | `python`, `django`, `fastapi`, `flask` |
| `Cargo.toml` | `rust` |
| `go.mod` | `go` |
| `Gemfile` | `ruby`, `rails` (if `gem 'rails'`) |

Present what was detected and wait for confirmation before continuing.

## Step 2: Wire up installed linters as passthroughs

Detect which lint/format/typecheck tools the project already has on `PATH` or declared in its manifest: ruff, biome, eslint, prettier, tsc, phpstan, pint, rubocop, rubyfmt, golangci-lint, gofmt, clippy, ast-grep, pytest, etc.

For each one, ask:

> I found `<linter>` configured. Add a passthrough rule so bully runs it on every Edit/Write? The linter keeps owning its own rules -- bully just enforces "pass the linter" whenever you touch a matching file.

If yes, queue a rule like:

```yaml
  ruff-check:
    description: "Code must pass ruff check."
    engine: script
    scope: ["*.py"]
    severity: error
    script: "ruff check --quiet {file}"
```

Keep lint / format / typecheck as **separate** passthrough rules -- failure modes and messages are distinct, and `bully-review` telemetry stays legible per tool.

### Linter precedence when multiple overlap

If several installed tools cover overlapping concerns, pick the superset and skip the others (wiring all three causes conflicts). Default precedence:

| Stack | Prefer | Skip (superseded) |
|-------|--------|-------------------|
| Python | `ruff check` + `ruff format` | `black`, `isort`, `flake8`, `pycodestyle`, `pydocstyle` |
| JS/TS | `biome` (if configured) OR `eslint` + `prettier` | do not wire both biome and eslint/prettier together |
| Ruby | `rubocop` | `standardrb` (choose one) |
| Go | `golangci-lint` | individual `staticcheck` / `revive` |

If the user *actively uses* a superseded tool (e.g. says "we still run isort in CI"), swap in their preferred tool instead. Call out what you're skipping and why before writing.

## Step 2b: Offer to install missing linters (optional, requires approval)

If the detected stack has no linter installed and one is conventional (ruff for Python, biome or eslint for JS/TS, golangci-lint for Go, rubocop for Ruby, phpstan for PHP, clippy for Rust), *offer* it as a choice -- do not push:

> You don't have a linter installed for `<stack>`. Most projects use `<tool>`. Want me to add it to `<manifest>` and wire up a passthrough? Or skip and we'll handle everything in bully directly.

Installing touches `package.json` / `pyproject.toml` / `composer.json` / CI, so this must be an explicit user opt-in. Never install silently. If the user declines, move on -- their `.bully.yml` can still cover everything via `ast`/`script`/`semantic` rules.

## Step 2c: Migrate project-specific rules (CLAUDE.md sections, arch tests, team conventions)

For each custom rule found (`CLAUDE.md`/`AGENTS.md` guidelines, Pest `arch()` tests, team docs, prose rules), route it using the same four-option decision tree as `bully-author`:

1. **Linter passthrough** -- can an installed linter (or one the user just opted into) enforce this with a rule-config edit? If yes, edit the linter's config AND queue a passthrough rule (if not already added in Step 2).
2. **ast** -- structural pattern, no linter covers it. Queue an `engine: ast` rule. Requires ast-grep installed.
3. **script (grep)** -- textual pattern with no false-positive risk on comments/strings.
4. **semantic** -- judgment only an LLM can make.

For each migration, state the enforcement-guarantee line once: *"Bully still runs on every Edit/Write -- we're just deciding where the rule definition lives."* Then present the chosen routing and wait for confirmation before queueing.

## Step 3: Ask setup questions

Before writing, ask:

- Default severity for new rules (`error` or `warning`)?
- Any additional globs to **skip** beyond the stack defaults? (See the defaults table below.) These become a top-level `skip:` list in `.bully.yml` -- the key is `skip`, not `exclude` or `ignore`.

**Telemetry is on by default.** Step 6 always creates `.bully/` and adds it to `.gitignore` -- that's what enables `bully-review` to work later. Telemetry is never a key inside `.bully.yml`. If the user explicitly says they don't want it, skip the `mkdir` in Step 6 and tell them `bully-review` will report "empty log" until they create `.bully/` themselves.

### Stack-aware default skip globs

Seed `skip:` with the defaults for the detected stack(s). Merge across stacks if multiple apply (e.g. a repo with `package.json` and `pyproject.toml`).

| Stack | Default skip globs |
|-------|-------------------|
| Python | `.venv/**`, `venv/**`, `**/__pycache__/**`, `.pytest_cache/**`, `.ruff_cache/**`, `.mypy_cache/**`, `build/**`, `dist/**`, `*.egg-info/**` |
| Node/JS/TS | `node_modules/**`, `dist/**`, `build/**`, `.next/**`, `.nuxt/**`, `.output/**`, `coverage/**`, `*.min.js` |
| PHP | `vendor/**`, `storage/**`, `bootstrap/cache/**`, `public/build/**` |
| Go | `vendor/**`, `bin/**` |
| Rust | `target/**` |
| Ruby | `vendor/**`, `tmp/**`, `log/**`, `public/assets/**` |
| Universal | `.git/**`, `.idea/**`, `.vscode/**` (only add if present) |

Present the merged list and ask the user to add/remove before writing.

## Step 4: Seed rules from the examples catalog

Bully ships an `examples/rules/` directory -- a catalog of common rules organized by tech (react-ts, nextjs, django, fastapi, go, rails, rust-cli). **These are examples, not a blessed baseline.** Do not auto-`extends:` them. Instead, for the detected stack, open the matching `examples/rules/<stack>.yml`, show the user the rule list with one-line summaries, and ask which ones to seed. Copy the selected rules inline into the generated `.bully.yml` -- the user owns them from then on.

If the user declines all of them, write an empty `rules:` block and let the `bully-author` skill add rules as they come up.

## Step 5: Write `.bully.yml`

Write to the project root. The parser expects 2-space indentation for rule IDs under `rules:` and 4-space indentation for each rule's fields. Scope is an inline list.

### Allowed top-level keys (exhaustive)

**Only these keys are valid at the top level of `.bully.yml`:**

| Key | Purpose |
|-----|---------|
| `schema_version` | Optional integer, reserved for future migrations. |
| `extends` | List of shared rule-pack paths. |
| `rules` | Map of rule-id → rule definition. |
| `skip` | List of globs excluded from all rules. **The key is `skip` -- not `exclude`, `ignore`, `excludes`, or `ignores`.** |
| `execution` | Map with `max_workers` (int). |

**Do not invent other top-level keys.** In particular, there is no `telemetry:` key -- telemetry is enabled by the presence of the `.bully/` directory at the project root, which Step 6 creates.

### Rule shape

```yaml
schema_version: 1
skip:
  - "vendor/**"
  - "node_modules/**"
rules:
  rule-id:
    description: "What the rule enforces"
    engine: script        # script | ast | semantic
    scope: ["*.ts", "*.tsx"]
    severity: error       # error | warning
    script: "command {file}"   # script engine only
```

For multi-line descriptions use a folded scalar (`description: >`). Quote all script values with double quotes. Use `{file}` as the target file placeholder. Formatters use `severity: warning`; correctness rules use `severity: error`.

### Draft-then-validate protocol (MANDATORY)

Never write directly to `.bully.yml` on init. Draft first, parse-check the draft, and only rename on pass. This prevents polluting the user's project with an invalid config if the LLM hallucinates a key (e.g. the historical `telemetry:` / `exclude:` mistakes).

```bash
# 1. Write the draft to a scratch path (use the Write tool):
#      /tmp/bully-init-draft.yml  <-- full proposed .bully.yml contents
#
# 2. Parse-check the draft:
BULLY=$(command -v bully 2>/dev/null || ls -d ~/.claude/plugins/cache/*/bully/*/bully 2>/dev/null | sort -V | tail -1)
"$BULLY" --validate --config /tmp/bully-init-draft.yml
```

Exit code `0` + no `[FAIL]` output = draft is valid. Nonzero exit = draft is broken: read the `[FAIL]` line (e.g. `unknown top-level key 'telemetry'`), fix the draft in place, re-check. **Do not overwrite `.bully.yml` until `--validate` exits clean.**

Once the draft validates:

```bash
mv /tmp/bully-init-draft.yml .bully.yml
```

### Binary resolution

Bully ships a `bully` wrapper at its repo root that `exec`s `python3 pipeline/pipeline.py`. Plugin installs don't run `pip install`, so `bully` is often not on `PATH`. Resolve the binary with this one-liner (used above):

```bash
BULLY=$(command -v bully 2>/dev/null || ls -d ~/.claude/plugins/cache/*/bully/*/bully 2>/dev/null | sort -V | tail -1)
```

`sort -V | tail -1` picks the newest version from the plugin cache, avoiding stale `0.3.0`/`0.4.x` entries that older installs leave behind. If `$BULLY` is empty after that, tell the user to either alias it (`alias bully='<path>'`) or symlink into `~/.local/bin`, and fall back to `python3 <plugin-path>/pipeline/pipeline.py ...`.

## Step 6: Verify and enable

Before handing off, bring the config into a runnable state:

1. **Trust the config** so script/ast rules can execute: `bully trust` (fallback: `python3 <plugin-path>/pipeline/pipeline.py --trust --config .bully.yml`).
2. **Run `bully doctor`** and surface any `[FAIL]` lines. **Known false positives for plugin installs** -- note them but do not try to "fix" them:
   - `[FAIL] no PostToolUse hook invoking hook.sh found in .claude/settings.json` -- the plugin loads `hooks/hooks.json` dynamically via the Claude Code plugin system. `.claude/settings.json` is *not* where the hook lives for plugin installs, so this FAIL is expected and harmless.
   - Skill/agent paths pointing at an older version (e.g. binary is `0.5.0` but doctor resolves skills from `0.3.0`). Doctor picks the first match in the plugin cache; stale cache directories from prior versions can shadow the current one. Either tell the user to delete old cache dirs under `~/.claude/plugins/cache/bully-marketplace/bully/` or just note it.

   Any `[FAIL]` outside that list is real -- surface it.
3. **Telemetry directory** (always, unless the user explicitly opted out in Step 3): `mkdir -p .bully/` and ensure `.gitignore` contains `.bully/` (append it if missing). This is what enables `bully-review` to read the rule-health log.
4. **Smoke-test script rules.** For each rule with a concrete `script:`, pick the first in-scope file (e.g. `git ls-files | grep -E '\.(ts|tsx)$' | head -1` against the rule's scope) and run `bully lint <file> --rule <rule-id>`. Report each verdict. If a rule that is *meant* to fire on a known pattern returns pass, flag it as a likely miscompile -- surface it now, not after 40 edits.

## Step 7: Summarize and hand off

After the verification pass, print:

```
.bully.yml generated.

Stack: <detected>
Extends: <packs>
Migrated: <count> rules from <sources>
Overrides: <count>
Excluded globs: <list>
Trust: <granted|failed>
Doctor: <pass|N failures>
Smoke test: <N passed, N flagged>
```

Tell the user: "To add project-specific rules, use `/bully-author`. To audit rule health later, use `/bully-review`."

## Troubleshooting

- **No manifests found**: Ask the user for the stack and extend the matching pack.
- **Existing `.bully.yml`**: Offer overwrite, merge (append new rules only), or abort.
- **Binary referenced by a shell-out rule missing**: Write the rule anyway and note the install command in the summary.
