# agentic-lint

An agent-native lint pipeline for Claude Code. One config file (`.agentic-lint.yml`), one enforcement point (`PostToolUse` hook), one violation format. Works for any language — rules are scoped by file glob, not by language declaration.

## The config

A `.agentic-lint.yml` is a flat list of rules. Each rule says what to check, where it applies, how bad it is, and which engine runs it — `script` (deterministic shell command) or `semantic` (natural-language rule the agent evaluates against the diff):

```yaml
rules:
  no-console-log:
    description: "No `console.log` in committed source -- use the project logger."
    engine: script
    scope: ["src/**/*.ts", "src/**/*.tsx"]
    severity: error
    script: "grep -nE 'console\\.log\\(' {file} && exit 1 || exit 0"

  prefer-derived-state:
    description: >
      React components should not use `useEffect` to derive state from
      props. Compute the value directly during render (or with `useMemo`
      if expensive). Effect-based derivation causes unnecessary renders
      and stale reads.
    engine: semantic
    scope: "src/**/*.tsx"
    severity: warning
```

That's the whole surface area. The first rule runs a grep on every edited `.ts`/`.tsx`; the second ships the diff to the agent with the description as the evaluation prompt. No plugins, no DSL — just globs, shell, and prose.

## What it does

Every time an agent edits a file, the hook runs a two-phase evaluation:

1. **Script phase** — deterministic checks (grep, awk, or shell-out to a linter). Fast. Fails the tool call on error-severity violations via exit code 2.
2. **Semantic phase** — if the script phase passes, the pipeline hands a unified diff plus rule descriptions to the agent for judgment-based evaluation (e.g. "inline single-use variables").

Violations block the agent's tool call until fixed. Passes are silent.

```
Edit/Write tool call
        |
        v
  find .agentic-lint.yml
        |
        v
  filter rules by scope glob
        |
        +--- Phase 1: script rules
        |       |
        |       +--- error? exit 2, violations on stderr (blocks)
        |       |
        |       +--- pass? continue
        |
        +--- Phase 2: semantic payload
                |
                +--- injected as additionalContext
                     for the agent to evaluate
```

## Why

Traditional linters fragment across tools (PHPStan, Pint, ESLint, Pest arch tests, CLAUDE.md prose). Each has its own config, its own violation format, its own trigger. Agents have to understand all of them.

`agentic-lint` collapses that into a single config the agent actually reads as part of its tool loop. Deterministic rules stay deterministic. Judgment rules live in natural language where the agent reads them directly.

## Prerequisites

- [Claude Code](https://claude.com/claude-code)
- Python 3.10+ (`python3 --version`)
- `jq` — the hook uses it for JSON parsing (`brew install jq` on macOS, standard on most Linux)

No other runtime dependencies. The pipeline is stdlib-only Python; you do **not** `pip install` anything to use it.

## Install

### 1. Clone somewhere stable

The hook path has to be stable so Claude Code can find it every time. Pick a location and commit to it:

```bash
git clone https://github.com/dynamik-dev/agentic-lint.git ~/.agentic-lint
```

You can put it anywhere — `~/.agentic-lint`, `~/code/agentic-lint`, `/opt/agentic-lint`. Just use the same path in step 3.

### 2. Install the four skills

Symlink each skill into your Claude Code skills directory so Claude picks them up:

```bash
mkdir -p ~/.claude/skills
ln -sf ~/.agentic-lint/skills/agentic-lint         ~/.claude/skills/agentic-lint
ln -sf ~/.agentic-lint/skills/agentic-lint-init    ~/.claude/skills/agentic-lint-init
ln -sf ~/.agentic-lint/skills/agentic-lint-author  ~/.claude/skills/agentic-lint-author
ln -sf ~/.agentic-lint/skills/agentic-lint-review  ~/.claude/skills/agentic-lint-review
```

Project-scope alternative: replace `~/.claude/skills` with `.claude/skills` inside a project — the skills will only activate in that project.

### 3. Install the evaluator subagent

Semantic evaluations are dispatched to a dedicated subagent that runs on Sonnet (cheaper than the parent session and isolated from its context). Symlink the agent definition:

```bash
mkdir -p ~/.claude/agents
ln -sf ~/.agentic-lint/agents/agentic-lint-evaluator.md ~/.claude/agents/agentic-lint-evaluator.md
```

To change the model, edit the `model:` field in `~/.agentic-lint/agents/agentic-lint-evaluator.md` (`opus`, `sonnet`, or `haiku`). Heavier rules benefit from Sonnet; simple presence checks may run fine on Haiku.

### 4. Register the PostToolUse hook

Add this block to `~/.claude/settings.json` (applies across all projects):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          { "type": "command", "command": "$HOME/.agentic-lint/pipeline/hook.sh" }
        ]
      }
    ]
  }
}
```

If you cloned somewhere other than `~/.agentic-lint`, substitute that path. Project-scope alternative: put the same block in `.claude/settings.json` at your project root.

### 5. Restart Claude Code

Start a new Claude Code session so it picks up the new skills, agent, and hook.

### 6. Verify the install

```bash
python3 ~/.agentic-lint/pipeline/pipeline.py --help
```

You should see usage output. If Python errors out, your Python is older than 3.10 — check with `python3 --version`.

That's install done. The pipeline sits silent until a project has a `.agentic-lint.yml`.

## Quick start (per project)

### 1. Bootstrap a config

In a project you want to lint, start Claude Code and run:

```
> /agentic-lint-init
```

The init skill detects your stack, scans for existing linter configs, asks a couple of questions, and writes a baseline `.agentic-lint.yml` at your project root. Review it, tweak to taste, commit it.

### 2. Make an edit

Ask Claude to edit a file in the project. The hook fires on every `Edit` / `Write` tool call:

- **No violations** — silent, the edit goes through.
- **Error-severity script violation** — Claude's tool call is blocked, the violation text is fed back to Claude, and Claude fixes it before moving on.
- **Semantic evaluation** — the pipeline hands Claude a diff + rule descriptions, and Claude evaluates them as part of its next turn.

### 3. Enable telemetry (optional)

```bash
mkdir .agentic-lint
```

One JSONL record per pipeline run lands in `.agentic-lint/log.jsonl`. Already in `.gitignore` — per-developer data.

### 4. Review rule health

After a few hundred edits:

```
> /agentic-lint-review
```

Surfaces noisy rules (fire on most edits), dead rules (never fire), and slow rules. Recommends concrete adjustments.

### 5. Evolve the config

```
> add a lint rule that bans var_dump() in PHP
> tighten no-db-facade — it's noisy
> apply the recommendations from the last review
> remove deprecated-carbon
```

The `agentic-lint-author` skill walks through engine choice, drafts the rule, tests it against fixtures, and only then writes to `.agentic-lint.yml`.

## Manual invocation

For authoring and debugging rules without triggering an Edit:

```bash
PIPE=~/.agentic-lint/pipeline/pipeline.py

# Full pipeline against a file
python3 "$PIPE" --config .agentic-lint.yml --file src/foo.php

# Isolate one rule
python3 "$PIPE" --config .agentic-lint.yml --file src/foo.php --rule no-compact

# See the semantic prompt that would be sent to the LLM
python3 "$PIPE" --config .agentic-lint.yml --file src/foo.php --print-prompt
```

## Uninstall

```bash
rm ~/.claude/skills/agentic-lint{,-init,-author,-review}
rm ~/.claude/agents/agentic-lint-evaluator.md
# Then remove the PostToolUse block from ~/.claude/settings.json
rm -rf ~/.agentic-lint
```

## Development

If you want to hack on agentic-lint itself:

```bash
cd ~/.agentic-lint
pip install -e ".[dev]"   # pulls ruff, shellcheck-py, pytest, pre-commit
bash scripts/lint.sh      # ruff + shellcheck + pytest + dogfood
```

- `scripts/lint.sh` — full quality gate.
- `scripts/dogfood.sh` — runs the pipeline against every source file in this repo.
- `.github/workflows/ci.yml` — the same checks on every PR.
- `.pre-commit-config.yaml` — optional: `pre-commit install` to run checks before every commit.

## Docs

- [Design](docs/design.md) — architecture, data flow, decisions, trade-offs.
- [Rule authoring](docs/rule-authoring.md) — how to write script and semantic rules, how to test them.
- [Telemetry](docs/telemetry.md) — log format, analyzer usage, self-improvement workflow.

## Layout

```
agentic-lint/
├── pipeline/
│   ├── pipeline.py      # two-phase lint engine
│   ├── analyzer.py      # rule-health analyzer
│   ├── hook.sh          # PostToolUse hook entry point
│   └── tests/           # 66 tests, stdlib-only
├── skills/
│   ├── agentic-lint/          # interprets hook output, dispatches evaluator
│   ├── agentic-lint-init/     # bootstraps .agentic-lint.yml
│   ├── agentic-lint-author/   # adds, modifies, removes rules
│   └── agentic-lint-review/   # audits rule health
├── agents/
│   └── agentic-lint-evaluator.md  # semantic eval subagent (Sonnet by default)
├── scripts/             # lint.sh, dogfood.sh
├── examples/            # sample configs
└── .agentic-lint.yml    # this repo's own lint rules (dogfood)
```
