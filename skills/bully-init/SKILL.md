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

Generate a baseline `.bully.yml` by detecting the stack, migrating existing linter rules, and extending shared rule packs.

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

## Step 2: Migrate existing linter config

Scan for `CLAUDE.md`/`AGENTS.md` style sections, Pest `arch()` tests, and configs from ESLint, Biome, PHPStan, Ruff, RuboCop. For each custom rule, prefer migrating to a native script rule (grep/awk) or a semantic rule over shelling out to the linter binary on every edit.

For each external linter found, ask:

> I found `<linter>`. (a) Migrate its rules into native script/semantic rules (recommended). (b) Keep it running via the pipeline (adds latency, requires binary). (c) Leave it outside the pipeline (CI/pre-commit only).

If the user picks (b), generate a shell-out rule with `severity: warning`.

## Step 3: Ask setup questions

Before writing, ask:

- Default severity for new rules (`error` or `warning`)?
- Enable telemetry directory (`.bully/telemetry/`) for rule-health review?
- Any globs to exclude (e.g. `vendor/`, `node_modules/`, generated code)?

## Step 4: Seed rules from the examples catalog

Bully ships an `examples/rules/` directory -- a catalog of common rules organized by tech (react-ts, nextjs, django, fastapi, go, rails, rust-cli). **These are examples, not a blessed baseline.** Do not auto-`extends:` them. Instead, for the detected stack, open the matching `examples/rules/<stack>.yml`, show the user the rule list with one-line summaries, and ask which ones to seed. Copy the selected rules inline into the generated `.bully.yml` -- the user owns them from then on.

If the user declines all of them, write an empty `rules:` block and let the `bully-author` skill add rules as they come up.

## Step 5: Write `.bully.yml`

Write to the project root. The parser expects 2-space indentation for rule IDs under `rules:` and 4-space indentation for each rule's fields. Scope is an inline list.

```yaml
rules:
  rule-id:
    description: "What the rule enforces"
    engine: script        # or semantic
    scope: ["*.ts", "*.tsx"]
    severity: error       # or warning
    script: "command {file}"   # script engine only
```

For multi-line descriptions use a folded scalar (`description: >`). Quote all script values with double quotes. Use `{file}` as the target file placeholder. Formatters use `severity: warning`; correctness rules use `severity: error`.

**Binary note.** Bully ships a `bully` wrapper at its repo root that `exec`s `python3 pipeline/pipeline.py`. Plugin installs don't run `pip install`, so `bully` is not on `PATH` by default -- the wrapper lives at `~/.claude/plugins/cache/bully-marketplace/bully/<version>/bully`. If subsequent steps fail with "command not found", tell the user to either alias it (`alias bully='~/.claude/plugins/cache/bully-marketplace/bully/<version>/bully'`) or symlink it into `~/.local/bin`. Until that's done, fall back to `python3 <plugin-path>/pipeline/pipeline.py ...` with the equivalent flags.

## Step 6: Verify and enable

Before handing off, bring the config into a runnable state:

1. **Trust the config** so script/ast rules can execute: `bully trust` (fallback: `python3 <plugin-path>/pipeline/pipeline.py --trust --config .bully.yml`).
2. **Run `bully doctor`** and surface any `[FAIL]` lines. Plugin installs may emit false positives for skill/agent checks -- if doctor reports a missing skill or agent file, note it but do not block.
3. **Telemetry directory** (only if the user answered yes in Step 3): `mkdir -p .bully/` and ensure `.gitignore` contains `.bully/` (append it if missing).
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
