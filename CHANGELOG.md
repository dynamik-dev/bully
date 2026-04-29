# Changelog
All notable changes documented here. Format per Keep a Changelog, semver adherence.

## [Unreleased]
### Planned
See docs/plan.md for the active improvement plan.

## 0.10.0 — 2026-04-28

- NEW: session-scope rules (`engine: session`) — fire at Stop time over the cumulative changed-set instead of per edit. First step into the article's "behavior harness" lane.
- NEW: `bully stop`, `bully subagent-stop`, `bully session-record` subcommands and matching hook entries (`Stop`, `SubagentStop`).
- NEW: `.bully/session.json` cumulative changed-set, appended to during PostToolUse.
- New telemetry record: `{"type": "subagent_stop"}` for sub-agent run accounting.

## 0.9.0 — 2026-04-28

- NEW: `bully guide <file>` and `bully explain <file>` for scoped feedforward -- show rules that apply to a specific file on demand, no generated manual.
- NEW: `bully session-start` and wired `SessionStart` hook entry -- agents see a tiny "bully active, N rules" banner at session boot.

## 0.8.0 — 2026-04-28

- BREAKING (subagent capability): the bully-evaluator subagent no longer has `Read`, `Grep`, or `Glob` tools. The diff is the only evidence by default. Closes prompt-injection layer 2 of 3.
- NEW: rule-level `context: { lines: N }` field. When set, the parent harness reads N lines around each diff hunk and bundles them in the payload as `<EXCERPT_FOR_RULE>`. Closes prompt-injection layer 3 of 3 (the substitute mechanism for the removed tools).
- `agents/bully-evaluator.md` updated to consume excerpts and reject directives in untrusted evidence.
- New docs at `docs/rule-config.md`.

Migration: rules that relied on the evaluator using `Read`/`Grep` to pull surrounding context will now see only the diff. Add `context: { lines: N }` to those rules. Audit candidates: rules whose descriptions reference "callsite", "imports", "surrounding code", or anything beyond the literal hunk.

## 0.7.2 — 2026-04-28

- Semantic evaluation payload now wraps rule descriptions in `<TRUSTED_POLICY>` and the file/diff in `<UNTRUSTED_EVIDENCE>`, with explicit instructions to the evaluator to treat the latter as data, not directives.
- The `_evaluator_input` field in the hook payload is now a pre-formatted string (was: dict). The bully skill passes it directly to the evaluator subagent without re-serialization. **Breaking for skill consumers:** bully harness ≥0.7.2 must be paired with bully skill ≥0.7.2; older skill versions would JSON-encode the string, producing escaped tags. The bundled SKILL.md is updated.
- Boundary tags in `_evaluator_input` are sanitized — diffs containing literal `</UNTRUSTED_EVIDENCE>` or other closing tags are neutralized so user-controlled content cannot break out of the untrusted block.
- Synthetic line-anchor metadata moves into the `<TRUSTED_POLICY>` block (was: appended after the closing tag).
- `agents/bully-evaluator.md` rewritten to consume the new boundaries and metadata.
- `skills/bully/SKILL.md` updated: pass `_evaluator_input` through verbatim.
- Renamed the dict-returning `build_semantic_payload` to `build_semantic_payload_dict` to disambiguate from the new string-returning helper. Internal callers in `pipeline/bench.py` and tests updated.
- `pipeline/bench.py`'s `count_tokens` and `full_dispatch` now accept `str | dict` payloads (was: dict only) so bench measurements remain accurate after the `_evaluator_input` shape change.
- Closes prompt-injection layer 1 of 3 (PR 1c addresses tool boundary and per-rule context-include).

## 0.7.1 — 2026-04-28

- Analyzer now consumes `semantic_verdict` and `semantic_skipped` records (previously emitted but ignored). Closes the live coherence drift between `docs/telemetry.md`, `pipeline/analyzer.py`, and `skills/bully-review/SKILL.md`.
- `format_report` adds a `skipped=` column.
- `bully-review` SKILL.md no longer claims semantic rules are unobservable.
- `README.md` corrects the bench description (`--full` does make real model calls).

## [0.7.0] - 2026-04-26
### Fixed
- **Script output parser swallowed errors from tools with columnar or wrapped output.** A live user report on a Laravel package showed phpstan's indented table produced a single `line ?:` violation whose description was 500 chars of separator noise truncated mid-identifier (`"🪪  mis"`). Root cause: `parse_script_output` matched regexes without per-line lstrip, so indented rows fell into the unmatched bucket, then the entire unmatched output was joined and head-truncated to 500 chars — eating the preamble and dropping the signal.
  - Per-line lstrip before regex match so `  11     Method Foo::bar()...` matches `_LINE_CONTENT`.
  - Stateful continuation-joining: a numbered line opens a violation; following unnumbered, non-separator lines concatenate onto its description (captures phpstan/pest/psalm wrapped messages).
  - Table separator rows (`----`, `====`, `____`) dropped before parsing.
  - When nothing parses, the *tail* of unmatched lines is preserved as up to 20 individual violations (each capped at 500 chars) instead of a single head-truncated blob.
  - Stderr is now parsed too. Numbered results from either stream are preferred; tails are combined across streams as a last resort.
  - End-to-end: phpstan output that previously produced `1 violation, line=None, description="------ ... Illuminate\\Database\\El"` now produces `3 violations, line=11, line=11, line=18, description=<full error text>`.

### Added
- **`output: passthrough` rule field.** Escape hatch for tools whose output format defies the continuation heuristic. Skips structured parsing and emits one violation carrying the tail of stdout+stderr. Opt-in per rule; default unchanged.
- **`bully lint --strict` flag.** For CI callers. Exit non-zero on any non-pass status (untrusted, config error). Default posture stays advisory (exit 0 on untrusted) so the PostToolUse hook never blocks edits on infra issues. Exit codes: 0 pass, 2 blocked, 3 strict-only non-pass.
- **`release-bully` skill.** Codifies the version-bump + tag + publish flow so future release sessions don't have to rederive which fields move together.

### Changed
- **Violation rendering in blocked stderr drops the `line ?:` placeholder.** When a violation has no line number, the header is `- [rule]: description` instead of `- [rule] line ?: description`. Removes a rough edge on script rules that run whole-file tools and can't attribute a specific line.

## [0.6.0] - 2026-04-23
### Changed
- **`bully-init` hardened against real-world config hallucinations.** Based on a live install transcript where the skill wrote a `.bully.yml` with a hallucinated `telemetry:` top-level key and `exclude:` list (the real key is `skip`), then spent output budget explaining a PostToolUse `[FAIL]` line that's a known false positive for plugin installs.
  - New **mandatory draft-then-validate protocol**: write the proposed `.bully.yml` to a scratch path, run `bully --validate --config <path>`, only `mv` onto `.bully.yml` on exit-0. Invalid configs never land on the user's filesystem.
  - New **stack-aware default skip globs** table (Python / Node / PHP / Go / Rust / Ruby) so the LLM has a reference instead of inventing per-session.
  - New **linter precedence** guidance: when ruff AND black+isort are installed, default to ruff; when biome AND eslint+prettier, pick one stack -- wiring conflicting tools causes noise.
  - Expanded doctor false-positive list to cover (a) PostToolUse-hook-not-in-`settings.json` (plugins load `hooks/hooks.json` dynamically) and (b) stale-cache skill/agent version mismatches. Skill now notes them instead of attempting to "fix" them.
  - New **binary-resolution one-liner** (`command -v bully || ls -d ~/.claude/plugins/cache/*/bully/*/bully | sort -V | tail -1`) that prefers `$PATH` and falls back to the newest plugin-cache version, avoiding version-pinned paths.
  - **Telemetry is on by default.** `/bully-init` now creates `.bully/` and adds it to `.gitignore` unless the user explicitly opts out. `/bully-review` has data to read the first time it runs, instead of reporting "empty log" weeks later.
- **README and design docs realigned with linter-passthrough routing.** The config, engine choice, and init-workflow docs were still pushing the pre-v0.5.0 framing (grep-first, external linters as opt-in). Lead README config example is now a `ruff-check` passthrough. Added a "Where rules live" section, a cop-vs-lawmakers framing line, and updated `/bully-init` description to mention linter detection and install-on-approval behavior. `docs/rule-authoring.md` priority order rewritten as passthrough → ast → script → semantic. `docs/design.md` Migration/Baselines subsections rewritten to treat linter routing as the default, not an opt-in.

## [0.5.0] - 2026-04-23
### Changed
- **Skills now route rules through installed linters before falling back to bully-owned enforcement.** Reframes bully as the cop and native linters (ruff, biome, eslint, tsc, phpstan, rubocop, clippy, …) as the lawmakers: the PostToolUse hook always enforces, but rule *definitions* should live wherever they express best.
  - `bully-author`: replaces the old script/ast/semantic split with a four-option decision tree — **linter passthrough → ast → grep → semantic** — plus a linter installed-vs-missing pre-flight and an enforcement-guarantee line the skill must say once when recommending a linter, so users don't assume moving a rule into a linter removes it from bully's scope. Review-recommendation table gains a row for "grep rule a linter could cover → passthrough."
  - `bully-init`: Step 2 rebuilt into three sub-steps — (2) detect installed linters and offer passthrough rules, (2b) *offer* (never silently install) missing conventional linters with explicit approval, (2c) route project-specific rules (CLAUDE.md sections, arch tests, team conventions) through the same four-option tree.
  - `bully-review`: two new FYI rows flagging mis-routed rules — grep matching a structural pattern → propose `engine: ast`; grep matching something an installed linter covers → propose moving the rule into the linter's config and replacing the bully rule with a passthrough.
- No schema, engine, or hook contract changes. Existing `.bully.yml` configs continue to work unchanged — only the skill guidance shifts going forward.

## [0.4.1] - 2026-04-21
### Fixed
- **Critical regression: every rule stopped running on 0.4.0.** 0.4.0's hand-rolled `_scope_glob_matches` (added in `edb362f` to provide recursive `**` on Python 3.10-3.12) anchored the first pattern segment at `parts[0]`, which is `"/"` for absolute paths. The PostToolUse hook always passes absolute paths, so every `**` scope missed and `filter_rules` returned `[]` for every file. The symptom in a user's `.bully/log.jsonl` was `rules: []` on every entry -- telemetry kept writing `"status": "pass"` rows but no script / AST / semantic rule ever executed. The fix restores the 0.3.x right-anchored semantic (matching `PurePath.match`) by trying the first segment at every path-parts offset while keeping recursive `**` intact. Reproduced against a real Laravel + Inertia config (`pipeline/tests/fixtures/groups4.bully.yml`) and pinned with 6 regression tests, including negative cases that guard against over-matching (`notapp/`, `appetite/`, wrong extensions).

### Added
- `ruff-format-clean` rule in the dogfood `.bully.yml`. Closes a gap where `ruff check` (run by the existing `ruff-clean` rule) and `ruff format --check` are separate subcommands: files passing lint but needing reformatting slipped through the in-session hook and tripped CI's `ruff format --check .` step after push. Now blocks unformatted Python edits before commit.

## [0.4.0] - 2026-04-21
### Added
- Parallel execution of script and AST rules within a single file (`execution.max_workers` config, `BULLY_MAX_WORKERS` env). Default `min(8, os.cpu_count() or 4)`. Set `max_workers: 1` to restore serial execution if a rule script needs exclusive access to a resource. Single-rule phases skip the pool and run inline.
- Bench (`bench/fixtures`): warm `wall_p50` ~38ms vs ~40ms on the prior release (~−5% on the mixed fixture suite). The big-extends-chain fixture goes ~12ms → ~8ms (−37%). Cold start is within noise.
- Per-rule `error: true` telemetry field in `rule_records` (see `docs/telemetry.md`), emitted when the rule's evaluator raised a Python exception.

### Changed
- **Behaviour change:** a Python exception inside a rule's evaluator (not its shell exit code — an exception from inside bully's rule dispatch) no longer crashes the pipeline. It now becomes a blocking `severity=error` violation with description `"internal error: <ExcType>: <msg>"`. Exit code and gate behaviour are unchanged (the edit/commit is still blocked), but the output format for this case is different. Shell-level non-zero exits from script rules are unaffected.

## [0.3.0] - 2026-04-21
### Added
- `bully validate --execute-dry-run` runs each script rule against `/dev/null` at config time, surfacing shell and regex syntax errors before a hook ever fires.
- `bully` shell wrapper at the repo root so plugin-installed users can alias or symlink it without `pip install`.
- New test suites covering YAML escape handling, recursive `**` globs, Python version gating, plugin-cache path resolution, and dry-run execution.
- `docs/rule-authoring.md` section documenting YAML escapes, `**` compatibility notes, and the `--explain` / `--execute-dry-run` flags.

### Changed
- `bully-init` Step 6 now runs trust, doctor, telemetry-dir setup, and per-rule smoke tests before handing off — previously it dropped users at a config that might silently fail.
- `doctor` resolves skills and the semantic-evaluator agent from either the legacy `~/.claude/skills/...` path or the plugin cache (`~/.claude/plugins/cache/*/bully/*/{skills,agents}/...`).
- `ruff-clean` rule in the dogfood config prefers `ruff` on `PATH` but now falls back to `.venv/bin/ruff` so Claude Code hooks (which don't inherit the activated venv) catch violations locally instead of only in CI.

### Fixed
- **YAML parser silently miscompiled escapes.** `_parse_scalar` now processes double-quoted escapes (`\\`, `\"`, `\n`, `\t`, `\r`, `\/`, `\0`) and single-quoted `''`. Previously `"\\."` stayed as two backslashes, so every shipped `examples/rules/*.yml` pattern exited 0 on grep's "parentheses not balanced" error instead of matching.
- `filter_rules` now handles `**` recursively on Python 3.10–3.12 via a custom `_scope_matches`. `PurePath.match` only gained recursive `**` in 3.13, so older versions were silently skipping deep paths.
- `doctor` actually compares `sys.version_info` against 3.10 instead of unconditionally printing `[OK] Python X.Y >= 3.10`.
- Three `ruff` SIM violations (`SIM110`, `SIM102`, `SIM108`) in `pipeline.py` that CI caught but the hook's `ruff-clean` rule missed because `ruff` wasn't on `PATH` in the hook env.
- `pipeline/tests/test_validate_cli.py` formatting to satisfy `ruff format --check` in CI.

## [0.2.0] - 2026-04-18
### Added
- `bully bench` command for measuring rule-suite performance: Mode A runs a fixture suite and writes per-run history, Mode B analyzes configured cost, `--compare` diffs the last two runs, and `--full` calls real `messages.create` to record output tokens and actual dollar cost.
- 8 bench fixtures covering script, ast, and semantic engines.
- `phase_timer` hook on `run_pipeline` so callers can observe per-phase latency without patching internals.
- `ruff-clean` rule in the dogfood `.bully.yml` (tolerates a missing `ruff` binary rather than erroring).
- CI runs on every push, not just PRs.

### Changed
- README tagline: "Bully doesn't" → "Bully enforces".
- `bully bench --config` and `--compare` are now mutually exclusive.
- Semantic-evaluator pipeline short-circuits token counting when no semantic rules match.
- Dogfooded new ast rules across the repo.

### Fixed
- ruff `F841` and formatting violations on P1 epic test files that were blocking CI.

## [0.1.0] - 2026-04-16
### Added
- Two-phase lint pipeline (script + semantic rules).
- PostToolUse hook integration for Claude Code.
- Four skills: bully, bully-init, bully-author, bully-review.
- Semantic evaluator subagent with strict VIOLATIONS/NO_VIOLATIONS output contract.
- Opt-in JSONL telemetry (`.bully/log.jsonl`).
- Laravel example rule pack.
- Dogfood config (`.bully.yml`) enforcing stdlib-only runtime, strict bash mode, and more.
