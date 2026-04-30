## Repository layout

Top level:
- `src/bully/` — the package (everything below).
- `tests/` — pytest suite (flat layout). `tests/fixtures/` holds shared `.bully.yml` and sample-source fixtures used by multiple tests. `tests/conftest.py` sets `BULLY_TRUST_ALL=1` so the trust gate doesn't short-circuit every test; tests that exercise the gate itself unset it via monkeypatch.
- `hooks/` — Claude Code plugin hook entry points. `hook.sh` shells into `python3 -m bully` with the right `PYTHONPATH`; `hooks.json` wires PostToolUse/SessionStart/Stop/SubagentStop to it.
- `agents/` — agent definitions distributed with the plugin (e.g. `bully-evaluator.md`).
- `skills/` — skill definitions distributed with the plugin (`bully`, `bully-init`, `bully-author`, `bully-review`).
- `bench/` — bench fixture suite (`bench/fixtures/*/{config.yml,fixture.json}`) plus the `history.jsonl` log produced by `bully bench` runs.
- `scripts/` — `lint.sh` (ruff + shellcheck + pytest + dogfood) and `dogfood.sh` (run bully against the repo).
- `.claude-plugin/` — plugin manifest (`plugin.json`, `marketplace.json`).
- `.bully.yml` — bully's own ruleset (the project dogfoods itself).
- `bully` — bash wrapper that invokes `python3 -m bully` with the local `src/` on `PYTHONPATH`. Use this for ad-hoc runs without `pip install -e .`.

Inside `src/bully/`:
- `__init__.py` — public re-exports + `BULLY_VERSION`. Underscore-prefixed aliases (`_parse_scalar`, `_cmd_trust`, etc.) are compat shims for callers that imported from the pre-restructure `pipeline.py`; new code should use the unprefixed names.
- `__main__.py` — `python -m bully` entry. Delegates to `cli.main()`.
- `cli/` — argparse-based CLI dispatcher and one file per subcommand. `cli/__init__.py:main()` is the entry point. `args.py` parses the unified flag namespace; `validate.py`, `doctor.py`, `baseline.py`, `guide.py`, `explain.py`, `session.py`, `stop.py`, `coverage.py`, `debt.py`, `log_verdict.py`, `hook_mode.py` each implement one verb.
- `config/` — `.bully.yml` parsing and rule resolution. `parser.py` (the hand-rolled YAML-subset parser; `Rule`, `Violation`, `ConfigError`), `loader.py` (extends resolution + `parse_config`), `skip.py` (`SKIP_PATTERNS`, project skip, user-global `.bully-ignore`), `scope.py` (recursive `**` glob matching + `filter_rules`).
- `engines/` — rule executors. `script.py` runs `engine: script` rules (subprocess + capability env shaping); `ast_grep.py` runs `engine: ast` rules; `output.py` is the shared script-output → `Violation` adapter.
- `diff/` — diff handling. `context.py` builds the unified-diff hook payload (Edit and Write modes); `analysis.py` is the can't-match filter (`_can_match_diff`, `_rule_add_perspective`) and per-rule excerpt builder.
- `semantic/` — semantic-rule machinery. `payload.py` builds the `<TRUSTED_POLICY>` / `<UNTRUSTED_EVIDENCE>` payload sent to the bully-evaluator subagent; `analyzer.py` is the rule-health analyzer (reads `.bully/log.jsonl`, surfaces noisy/dead/slow rules).
- `state/` — stateful infra. `baseline.py` (grandfather lines via `.bully/baseline.json` + per-line `bully-disable:` parsing), `trust.py` (per-machine trust gate at `~/.bully-trust.json`), `telemetry.py` (`.bully/log.jsonl` writer).
- `runtime/` — pipeline orchestration. `runner.py` is `run_pipeline` (two-phase: deterministic engines first, then semantic dispatch); `rule_runner.py` is the per-rule executor + thread pool; `hook_io.py` formats blocked-stderr output, reads stdin payloads, and renders prompts.
- `bench/` — the `bully bench` harness. `cli.py` dispatches between modes; `fixtures.py` loads bench fixtures; `timing.py` is `PhaseTimer` + percentile helper; `dispatch.py` calls the Anthropic SDK (`count_tokens`, `full_dispatch`); `git_meta.py` stamps git SHA + SDK version onto run records; `modes/` has one file per mode (`mode_a` = fixture suite, `mode_b` = config token-cost analysis, `compare` = diff last two history entries, `single` = one-fixture timing).

Hot paths to know:
- The PostToolUse loop is `hooks/hook.sh` → `python3 -m bully --hook-mode` → `bully.cli.hook_mode.run_hook_mode` → `bully.runtime.runner.run_pipeline`.
- The trust gate runs before any rule execution; `BULLY_TRUST_ALL=1` in the env bypasses it (used by tests and CI).
- All runtime code stays stdlib-only — `anthropic` is only imported lazily inside `bench/dispatch.py`.

<!-- BACKLOG.MD MCP GUIDELINES START -->

<CRITICAL_INSTRUCTION>

## BACKLOG WORKFLOW INSTRUCTIONS

This project uses Backlog.md MCP for all task and project management activities.

**CRITICAL GUIDANCE**

- If your client supports MCP resources, read `backlog://workflow/overview` to understand when and how to use Backlog for this project.
- If your client only supports tools or the above request fails, call `backlog.get_backlog_instructions()` to load the tool-oriented overview. Use the `instruction` selector when you need `task-creation`, `task-execution`, or `task-finalization`.

- **First time working here?** Read the overview resource IMMEDIATELY to learn the workflow
- **Already familiar?** You should have the overview cached ("## Backlog.md Overview (MCP)")
- **When to read it**: BEFORE creating tasks, or when you're unsure whether to track work

These guides cover:
- Decision framework for when to create tasks
- Search-first workflow to avoid duplicates
- Links to detailed guides for task creation, execution, and finalization
- MCP tools reference

You MUST read the overview resource to understand the complete workflow. The information is NOT summarized here.

</CRITICAL_INSTRUCTION>

<!-- BACKLOG.MD MCP GUIDELINES END -->
