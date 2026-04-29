<p align="center">
  <img src="bully.png" alt="Bully claude code tool for linting enforcement" width="500">
</p>

<p align="center">
  <a href="https://github.com/dynamik-dev/bully/actions/workflows/ci.yml"><img src="https://github.com/dynamik-dev/bully/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache_2.0-green" alt="Apache 2.0">
  <img src="https://img.shields.io/badge/Claude_Code-plugin-5A67D8" alt="Claude Code plugin">
</p>

## A hybrid agent-harness sensor for Claude Code

Bully is a repository-local pipeline that runs computational rules (script + AST) and inferential rules (semantic, dispatched to a context-firewalled subagent) on every `Edit` and `Write` -- and at session boundaries (`Stop`) for changed-set rules that no per-edit lint can see. **Errors block the tool call -- Claude can't land the edit until it passes.** Any language; rules are scoped by file glob.

What you get:

- **Two enforcement lanes.** Deterministic checks for things that are unambiguous (a string is forbidden, a function call is dead). LLM evaluation for things that need judgment (single-use variables, ambiguous naming, audit-trail rules).
- **Subagent context firewall.** Semantic evaluation runs in a read-only subagent (`bully-evaluator`) with no `Read`/`Grep`/`Glob` and explicit trusted-policy / untrusted-evidence boundaries in the prompt. Adversarial diff content cannot redirect the harness.
- **Scoped feedforward, mechanical feedback.** `bully guide <file>` shows the agent what rules apply *before* it edits. Exit-2 on violation blocks the edit *after*.
- **Self-pruning telemetry.** `bully-review` and the scheduled `bully-scheduler` agent retire dead rules and downgrade noisy ones automatically -- the cybernetic-governor loop.
- **Behavior-harness lane.** Session rules (`engine: session`) fire at `Stop` over the cumulative changed-set: "auth changed without tests", "migration without rollback", "API changed without changelog".
- **Trust-gated, capability-scoped script execution.** `bully trust` is the first gate. Per-rule `capabilities: { network: false, writes: cwd-only }` is the second.

Bully is stdlib-only (no runtime deps) and ships as a Claude Code plugin.

**Linters are the lawmakers. Bully is the cop.** Your installed tools (ruff, biome, eslint, tsc, phpstan, clippy, …) own what "a violation" means. Bully owns making sure Claude can't slip one past the hook. The question is never *whether* bully enforces -- just where a rule's definition lives.

## The config

A `.bully.yml` is a flat list of rules. Each rule says what to check, where it applies, how bad it is, and which engine runs it -- `script` (shell command, including a passthrough to an installed linter), `ast` (structural pattern via [ast-grep](https://ast-grep.github.io/)), or `semantic` (natural-language rule the agent evaluates against the diff):

```yaml
schema_version: 1

rules:
  ruff-check:
    description: "Code must pass ruff check."
    engine: script
    scope: ["*.py"]
    severity: error
    script: "ruff check --quiet {file}"

  no-any-cast:
    description: "No `as any` casts -- use a precise type or `unknown` plus narrowing."
    engine: ast
    scope: ["src/**/*.ts", "src/**/*.tsx"]
    severity: error
    pattern: "$EXPR as any"

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

The first rule is a **linter passthrough** -- bully runs `ruff check` on every edited `.py` file and blocks on nonzero exit. Ruff owns *which* checks run (configured in `ruff.toml`); bully owns enforcement on every edit. The second rule matches a structural pattern with ast-grep -- ignores comments, strings, and formatting variants. The third ships the diff to the agent with the description as the evaluation prompt. No plugins, no DSL -- just globs, shell, ast patterns, and prose.

### Where rules live (four options, one cop)

When authoring a rule, `/bully-author` routes it in priority order:

1. **Linter passthrough** -- an installed (or reasonably installable) linter can express the rule via a config change. Rule definition lives in the linter's config; `.bully.yml` gets a one-line passthrough (`engine: script`, `script: "<linter> <args> {file}"`).
2. **`engine: ast`** -- structural pattern no linter covers; `ast-grep` handles comments/strings/formatting correctly.
3. **`engine: script`** -- textual pattern with no false-positive risk (filename conventions, forbidden imports, required headers).
4. **`engine: semantic`** -- judgment only an LLM can make ("don't derive state with `useEffect`", "error messages should be actionable").

Engine-wise there are only two lanes -- `script` (bash subprocess) and `semantic` (evaluator subagent) -- but the four authoring categories pick the sharpest tool for each rule.

`engine: ast` requires `ast-grep` on `$PATH` (`brew install ast-grep`, `cargo install ast-grep`, or `pip install ast-grep-cli`). If missing, ast rules are skipped at runtime with a one-line stderr hint and `bully doctor` flags it.

Need to share rules across your own repos? `extends:` accepts any relative or absolute path to another `.bully.yml`:

```yaml
schema_version: 1
extends: ["../shared/bully-base.yml"]

rules:
  # project-specific overrides + additions here
```

Local rules override inherited rules of the same id.

### Parallelism

bully evaluates script and AST rules concurrently within a single file. By default it uses `min(8, os.cpu_count() or 4)` workers. You can override this via config:

```yaml
execution:
  max_workers: 4
```

Or via env (wins over config):

```
BULLY_MAX_WORKERS=2 git commit
```

Set `max_workers: 1` to restore fully serial execution if a rule script has side effects that require exclusive access to a resource. Files that match only a single rule skip the pool and run inline — the knob only matters when two or more deterministic rules apply to the same file.

If a rule's evaluator itself raises a Python exception (not just a non-zero shell exit), bully now catches it and emits a blocking `severity=error` violation with description `internal error: <ExcType>: <msg>`. The other rules in the phase still run to completion, so one bad rule cannot take down the whole check.

## How it works

<p align="center">
  <img src="bully-flow.png" alt="Bully flow: Edit/Write → PostToolUse hook → script phase → semantic phase → block or pass" width="500">
</p>

1. **Script + AST phase** -- deterministic checks. `script` rules shell out (grep, awk, linters); `ast` rules run structural patterns through ast-grep. Both are fast and fail the tool call on error-severity violations via exit code 2.
2. **Semantic phase** -- if the deterministic phase passes, the pipeline hands a unified diff plus rule descriptions to the evaluator subagent. Structured verdicts come back; the parent session surfaces them.

Deterministic rules stay as shell or ast patterns. Judgment rules ("inline single-use variables", "don't derive state with `useEffect`") live as plain English the agent evaluates against the diff. Same trigger, same output format, same fix loop -- across every language in the repo.

## Prerequisites

- [Claude Code](https://claude.com/claude-code)
- Python 3.10+ (`python3 --version`)

The pipeline is stdlib-only Python and the hook is a five-line bash wrapper. You do **not** `pip install` anything to use it.

## Install

Bully ships as a Claude Code plugin. Two slash commands:

```
/plugin marketplace add https://github.com/dynamik-dev/bully
/plugin install bully
```

That wires up everything: skills (`bully`, `bully-init`, `bully-author`, `bully-review`), the `bully-evaluator` subagent, and the `PostToolUse` hook that runs the pipeline on every `Edit` / `Write`. No clone, no symlinks, no `settings.json` surgery. Restart Claude Code to pick it up.

To change the evaluator model, set the plugin's agent override or edit `model:` in `agents/bully-evaluator.md` in your local plugin cache (default is `sonnet`).

### Verify the install

```bash
bully doctor
```

`doctor` checks Python version, config presence and parse-ability, hook wiring, evaluator-agent registration, and each skill. One line per check, `[OK]` or `[FAIL]`.

If `bully` isn't on `$PATH` (e.g., you skipped `pip install -e .`), call the pipeline directly as a fallback:

```bash
python3 "$(ls -d ~/.claude/plugins/cache/*/bully/*/ | tail -1)pipeline/pipeline.py" --doctor
```

### Manual install (fallback)

If you can't use the plugin system:

```bash
git clone https://github.com/dynamik-dev/bully.git ~/.bully
mkdir -p ~/.claude/skills ~/.claude/agents
for s in bully bully-init bully-author bully-review; do
  ln -sf ~/.bully/skills/$s ~/.claude/skills/$s
done
ln -sf ~/.bully/agents/bully-evaluator.md ~/.claude/agents/bully-evaluator.md
```

Then add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.bully/pipeline/hook.sh"
          }
        ]
      }
    ]
  }
}
```

## Quick start (per project)

### 1. Bootstrap a config

```
> /bully-init
```

The init skill detects your stack, finds which linters are already installed (ruff, biome, eslint, tsc, phpstan, clippy, …), and offers to wire each one as a passthrough rule so bully runs it on every Edit/Write. If a conventional linter for your stack is missing, it *offers* to install it -- never silently, always with approval. Migrates custom rules from CLAUDE.md / arch tests through the same four-option routing (passthrough → ast → script → semantic). Creates `.bully/` so `/bully-review` has telemetry to read. Review, tweak, commit.

### 2. Adopting in a repo with existing violations

A fresh rule across an existing codebase lights up every pre-existing problem. Baseline the current state so only _new_ violations block edits:

```bash
bully baseline-init --glob "src/**/*.ts"
```

That writes `.bully/baseline.json`. Future runs ignore anything recorded there. See [docs/design.md](docs/design.md) for the contract.

### 3. Silencing a specific line

When a rule is right in general but wrong on one line:

```ts
eval(expr); // bully-disable: no-eval reason: sandboxed input
```

Use sparingly. Telemetry tracks disables so noisy rules surface in `/bully-review`.

### 4. Telemetry

`/bully-init` creates `.bully/` and adds it to `.gitignore` by default. One JSONL record per pipeline run lands in `.bully/log.jsonl` -- per-developer data, never committed. After a few hundred edits, run `/bully-review` for noisy / dead / slow rule analysis. If you opted out of the directory during init, create it manually with `mkdir .bully` whenever you want telemetry to start recording.

### 5. Evolve the config

```
> add a lint rule that bans var_dump() in PHP
> tighten no-db-facade -- it's noisy
> apply the recommendations from the last review
```

The `bully-author` skill walks through engine choice, drafts the rule, tests it against fixtures, and only then writes to `.bully.yml`.

## Manual invocation

For authoring and debugging rules without triggering an Edit:

```bash
bully validate                                  # parse + enum checks
bully lint src/foo.php                          # full pipeline on a file
bully lint src/foo.php --rule no-compact        # isolate one rule
bully lint src/foo.php --print-prompt           # see the semantic prompt
bully show-resolved-config                      # rules after extends:
```

`bully` is the console script installed by `pip install -e .`. If you can't install the package, call the pipeline directly: `python3 ~/.bully/pipeline/pipeline.py --validate` (or with `--file`, `--show-resolved-config`, etc.).

### Scoped feedforward

Bully's per-edit feedback is the loud half of the loop. The quiet half -- what the agent should know *before* writing -- is exposed as scoped feedforward, not a generated manual:

- `bully guide path/to/file` -- show every rule whose scope matches the file, with descriptions.
- `bully explain path/to/file` -- show every rule and whether/why it matches.
- `bully session-start` -- one-line banner used by the SessionStart hook. Wire it via `hooks/hooks.json`; it prints "bully active, N rules configured. Run `bully guide <file>` to see rules in scope."

## Uninstall

Plugin install:

```
/plugin uninstall bully
/plugin marketplace remove bully-marketplace
```

Manual install:

```bash
rm ~/.claude/skills/bully{,-init,-author,-review}
rm ~/.claude/agents/bully-evaluator.md
# Then remove the PostToolUse block from ~/.claude/settings.json
rm -rf ~/.bully
```

## Development

```bash
cd ~/.bully
pip install -e ".[dev]"   # ruff, shellcheck-py, pytest, pre-commit
bash scripts/lint.sh      # ruff + shellcheck + pytest + dogfood
```

`scripts/dogfood.sh` runs the pipeline against every source file in this repo. `.github/workflows/ci.yml` runs the same checks on every PR.

## Docs

- [Design](docs/design.md) -- architecture, data flow, baseline contract, trade-offs.
- [Rule authoring](docs/rule-authoring.md) -- script and semantic rules, testing.
- [Telemetry](docs/telemetry.md) -- log format, analyzer, self-improvement workflow.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Contributing

Issues and PRs welcome. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating, [SECURITY.md](SECURITY.md) for how to report vulnerabilities, and [CHANGELOG.md](CHANGELOG.md) for release notes.

## Test Bench

Bully ships with a local bench for watching its own speed and input-token cost over time. Two modes:

### Mode A — fixture suite (regression trend)

```bash
bully bench                    # run all bench/fixtures/, append to bench/history.jsonl
bully bench --compare          # diff the last two runs
bully bench --no-tokens        # skip Anthropic API call, use char-count proxy
bully bench --json             # emit the raw run record on stdout
```

Results are written to `bench/history.jsonl`, one line per run. Commit a fresh run alongside changes that touch `pipeline/pipeline.py` to make speed/token impact visible in PRs.

### Mode B — config cost analysis

```bash
bully bench --config path/to/.bully.yml
```

Reports the input-token cost of the given config per invocation: floor tokens, per-rule marginal cost (sorted), diff scaling at 1/10/100/1000 added lines, and per-scope grouping. Useful for deciding whether a rule or rule pack earns its keep.

### Real token counts

Both modes use Anthropic's `messages/count_tokens` endpoint when `ANTHROPIC_API_KEY` is set and the optional `anthropic` SDK is installed (`pip install -e ".[bench]"`). Without either, both modes fall back to a `len(json.dumps(payload))` proxy and tag the output `method: proxy`.

By default the bench only calls `count_tokens`, which is free. Pass `--full` to dispatch real evaluator runs against fixtures (uses `messages.create` and spends credits — opt-in only).
