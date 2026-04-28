# Bully through the harness-engineering lens

**Status:** Synthesized review (incorporates a second-pass critique that caught factual errors in the first draft and added several good ideas)
**Date:** 2026-04-28
**Source:** [Harness Engineering: The Emerging Discipline of Making AI Agents Reliable](https://dev.to/truongpx396/harness-engineering-the-emerging-discipline-of-making-ai-agents-reliable-42gf)

## Article thesis (compressed)

**Agent = Model + Harness.** A *professional* harness has:

- Feedforward **guides** (anticipate behavior — AGENTS.md, skills, ADRs) ⊕ feedback **sensors** (observe results — linters, tests, code review).
- Both **computational** (deterministic, fast) and **inferential** (LLM, judgment) lanes.
- Sub-agents as **context firewalls**, not role-play personas.
- Context as a **scarce, managed resource** (progressive disclosure; success silent, only failures surface).
- Mechanical enforcement of invariants — not prompts.
- Continuous **entropy management** (background GC, scheduled refactor PRs).
- The cybernetic-governor model: maintainability + architecture-fitness + behavior harnesses regulating the codebase toward desired states.
- Hashimoto's principle: "Anytime you find an agent makes a mistake, engineer a solution such that the agent never makes that mistake again."

The article's distinguishing claim, citing HumanLayer: **"It's not a model problem. It's a configuration problem."** Harness improvements move benchmarks more than model upgrades.

## Where bully is already pro-grade

| Article principle | Bully today |
|---|---|
| Computational + inferential lanes | `script` + `ast` (deterministic) ↔ `semantic` (LLM) — clean split, both first-class. Most OSS harnesses pick one. |
| Sub-agents as context firewalls | `bully-evaluator` is exactly this: read-only, returns condensed verdicts, parent applies fixes. Strips `passed_checks` from subagent input via `_evaluator_input`. |
| Context-efficient backpressure | "Success is silent; failures surface" — exit 0 on pass, exit 2 + structured stderr on block. Matches HumanLayer's pattern verbatim. |
| Hashimoto loop | Telemetry → `bully-review` → `bully-author` is the canonical implementation. Few tools ship this. |
| Mechanical enforcement | Exit 2 blocks the tool call. Not advisory — the edit literally cannot land. |
| Repository-local knowledge | `.bully.yml` is repo-local and mechanically verifiable. |
| Token-cost instrumentation | `bully bench --config` reports per-rule marginal token cost. Almost nothing else in OSS does this. |
| Trust gate on script execution | `bully trust` + machine-local store + checksum re-validation on config change + `BULLY_TRUST_ALL=1` automation bypass. Not unsandboxed-by-default; it's trust-gated-then-unsandboxed. |

That's a strong floor. The gaps are about scope, missing layers, and internal coherence — not broken implementation.

## Live coherence drift (must-fix)

Before anything else: bully's own docs, code, and skills disagree about what the harness knows. Verified against the repo:

- `docs/telemetry.md` (lines 83–175) describes `semantic_verdict` and `semantic_skipped` records and says the analyzer consumes them when classifying rules.
- `pipeline/pipeline.py` (lines 2597–2611) **does** emit those records — producer side works.
- `pipeline/analyzer.py` contains **zero** references to either string — consumer side does not exist.
- `skills/bully-review/SKILL.md` (lines 20–26) tells the agent "semantic rules are not logged."

Three sources of truth, three different stories. Because bully's primary consumer is an agent, stale or contradictory docs become wrong context injected into future work — a harness-specific failure mode, not just a documentation hygiene issue.

A second instance: `README.md` line 302 says "the bench does not make real model calls — only `count_tokens`." `pipeline/bench.py:690` exposes `--full`; line 171 calls `client.messages.create`. The capability exists; the public guide denies it.

These are bug-bash items (one to two days), but they motivate a permanent control: doc-code coherence as a checked property of the harness, not a habit. See Tier 2 #9 below.

## Gaps, ranked by leverage

### Tier 1 — architectural

**1. Zero feedforward.** The article's #1 mental model is guides ⊕ sensors. Bully is 100% sensor. Every rule fires *after* the model has paid tokens to make the wrong edit. The right shape is **scoped feedforward**, not a generated manual (the article's "one big file" warning applies):

- `bully guide <file>` — show only rules that apply to that file, on demand.
- `bully explain <file>` — show which rules match and why.
- `PreToolUse` (when supported) injects only the rule subset for the file about to be edited.
- `SessionStart` prints a tiny "bully is active, N rules in scope, run `bully guide <file>` for details" summary — not the full rule body.

`.bully.yml` stays the source of truth; disclosure is per-moment.

**2. No behavior harness — only maintainability.** Article: "behaviour harness — functional correctness — the hardest category; least mature tooling." Bully is purely maintainability today. The natural bridge is **session/changed-set rules** that fire at `Stop` time over the cumulative diff, not per-edit:

- auth runtime changed without auth tests
- public API changed without docs or changelog
- migration changed without rollback / idempotency evidence
- config or schema changed without regenerated artifacts
- UI component changed without screenshot or browser verification

Not a general test runner. A second rule shape that operates on a session diff. This is the article's "behavior harness" lane and the one place a hybrid (script + LLM) approach has the biggest edge.

**3. No harness-coverage metric.** The article calls this an open problem. Bully has telemetry per *run* but no answer to "what fraction of risky edits are caught by at least one rule?" Even a crude `bully coverage` (per-file count of rules that match its scope, weighted by historical violation rate) would be ahead of the field.

**4. Prompt-injection surface in semantic eval — three-layer fix.** The evaluator reads user-controlled diff content and currently has `Read`, `Grep`, and `Glob` tools. The professional pattern is treating it as a judge over a prepared evidentiary packet, not a general repo explorer:

- **Prompt boundary.** Explicitly label rule descriptions as trusted policy and diff/file content as untrusted evidence in the system prompt.
- **Tool boundary.** Default the evaluator to diff-only (no `Read`/`Grep`/`Glob`). Adversarial diff content cannot redirect tools that aren't in scope.
- **Context boundary.** When a rule legitimately needs wider context, the *parent* prepares a bounded excerpt and passes it in the payload. This requires a per-rule mechanism (e.g., `context_lines: 20` or `include_callers: true`); without that mechanism, defaulting to diff-only will silently degrade rules that need upstream/downstream view. Spec the mechanism alongside the default.

### Tier 2 — missing professional plumbing

**5. Capability-scoped script execution after trust.** `bully trust` is the first gate. The second is missing: even after trust, `script:` rules run with full developer privileges. Add capability declarations per rule (`network: false`, `writes: cwd-only`) with deny-by-default. Trust = "I approved this config." Capability scope = "and these are the limits even within trust."

**6. No model routing / cost ceiling.** Evaluator is hardcoded `sonnet`. Article: "use cheaper models for sub-agents." Simplest sufficient design: a per-rule `model: haiku|sonnet` override, defaulting to sonnet, with bench reporting cost-by-model. Resist the ladder of `cheap|standard|strong` tiers until there's evidence three tiers are needed — it's an indirection layer that's easy to add later and hard to remove.

**7. No background entropy agent.** Article's Step 8: scheduled agents that scan telemetry and open small refactor PRs. `bully-review` is on-demand. The harness should self-prune: a weekly scheduled agent runs `bully-review` and opens a PR retiring a dead rule or downgrading a noisy one. The `/schedule` skill already exists — wiring, not new design.

**8. Only PostToolUse Edit|Write is wired.** Stop, SubagentStop, SessionStart, Notification all do nothing.

- **SessionStart** → tiny "bully active, N rules in scope" summary as feedforward anchor.
- **Stop** → re-validate cumulative session diff (the natural surface for behavior-harness session rules from #2).
- **SubagentStop** → log subagent token cost to telemetry.
- **Notification** → surface critical-severity counts to the user.

**9. Doc-code coherence as a checked property.** Once the live drift documented above is fixed, prevent regression:

- A test that loads `docs/telemetry.md` schema examples and validates them against the analyzer.
- `bully verify-docs` (or `bully doctor --docs`) that diffs documented CLI flags against `argparse` registrations and skill claims against pipeline behavior.
- A bully rule on the bully repo that fails when README mentions a feature the code doesn't expose.

This isn't a permanent architectural priority class — it's a routine guardrail that earns its keep because bully's primary consumer is an agent reading those docs as operational truth.

**10. `bully debt` — baselines and disables as governance signals.** Bully has baselines and per-line disables. A professional harness treats them as managed debt:

- `bully debt` summarizes baselines and disables by age, rule, file.
- Stale-disable detection (disable comment older than N days, or surrounding code changed since the disable was written).
- Reason quality check (reject one-word reasons; require minimum context).
- `bully-review` factors rising suppression rate into rule recommendations.

Maps cleanly to the article's entropy-management pillar with a small surface area.

**11. No org-level rollout / canary semantics.** `extends:` is local. No "ship rule X as warning everywhere → flip to error in 2 weeks → revert if violation rate doesn't fall." Real harnesses have this lifecycle once they're shared.

### Tier 3 — instrumentation & UX

**12. No context-cost preview.** `bully bench --config` runs over the whole config. There's no `bully cost-for-file src/foo.ts` that tells the agent "editing this file injects ~600 tokens." Lets the model budget. Data is already there; query is missing.

**13. No A/B agent benchmark.** Bench measures bully's overhead. Doesn't measure agent behavior change *with* vs *without* bully on a task suite. The article's headline finding ("harness improvements move benchmarks more than model upgrades") is exactly what a project named bully should measure on itself. Small fixture suite, run twice (config on/off), report iteration count delta.

**14. CLAUDE.md migration is one-shot.** Init reads CLAUDE.md once. CLAUDE.md keeps drifting; new style rules added there are never re-migrated. Need a periodic sync (`bully sync-claude-md`) or a rule that watches CLAUDE.md edits.

**15. Skill files don't progressively disclose.** `bully-author` chunks well; `bully-review`, `bully-init` load fully on activation. Article's pattern: top-level decision tree + references loaded on demand.

### Tier 4 — positioning

**16. README undersells the harness story.** "Linters are the lawmakers, bully is the cop" is a great tagline but loses the sale to anyone reading harness-engineering literature. Re-pitch as a hybrid agent-harness sensor — computational + inferential lanes, subagent context firewall, self-pruning telemetry, trust-gated execution. That's a stronger and more accurate claim than "lint pipeline."

## Recommended elevation path

In execution order:

1. **Fix the live drift, harden the evaluator.** Reconcile `docs/telemetry.md` ↔ `pipeline/analyzer.py` ↔ `skills/bully-review/SKILL.md` ↔ README bench claim. Then ship the three-layer prompt-injection fix (#4) including the per-rule context-include mechanism, not just the diff-only default. Bug-bash + small design — one to two days. Removes a real hazard and a real source of agent-context noise.
2. **Scoped feedforward + SessionStart.** `bully guide <file>` / `bully explain <file>` (#1) plus a tiny SessionStart hook (#8). Closes the feedforward gap without violating the "one big file" anti-pattern.
3. **Stop / SubagentStop hooks and session changed-set rules.** The Stop hook from #8 plus the second rule shape from #2. Turns bully from per-edit lint into a session-aware harness and lays the groundwork for the behavior-harness lane.
4. **Coverage metric + scheduled review agent.** `bully coverage` (#3) paired with the background entropy agent (#7). Now the system measures itself and prunes itself — the cybernetic-governor loop.
5. **`bully debt` and capability-scoped scripts.** Governance for suppressions (#10) and a second safety gate after trust (#5).
6. **README repositioning.** (#16) Cheap, comes after the substance is real.

Tier 3 items (#12–15) and org-level rollout (#11) are real but optional. Steps 1–4 turn bully from "great PostToolUse linter" into "the sensor layer of a professional harness, with a beachhead in the behavior-harness lane."

## Mapping to the article's CAR decomposition

| CAR layer | Bully today | Gap |
|---|---|---|
| **Control** (constraints, guardrails, permissions) | Exit-2 hard gate, severity levels, per-line disables, baseline, `bully trust` checksum gate | Capability profile for `script:` rules after trust; canary/rollout for shared rules |
| **Agency** (planning, decision-making, self-evaluation) | `bully-review` analyzer + `bully-author` skill, per-rule semantic skip filters | Background scheduled agent; self-pruning loop; coverage metric |
| **Runtime** (execution, tools, infra) | Stdlib-only Python pipeline; subagent dispatch; `--full` real-API bench mode | Sandbox capability profile; SessionStart/Stop/SubagentStop surfaces; per-rule model routing; doc-code coherence checks |

## Mapping to the article's three harness dimensions

| Dimension | Coverage |
|---|---|
| **Maintainability harness** (linters, complexity, coverage) | ✅ Bully's home turf. Strong. |
| **Architecture-fitness harness** (perf, observability, security) | ⚠️ Possible via custom rules; nothing ships first-class. |
| **Behavior harness** (functional correctness) | ❌ Out of scope today. Biggest expansion opportunity — natural fit for session changed-set rules at Stop time. |

## One-line positioning, after the lift

> A repository-local hybrid harness sensor for coding agents — computational and inferential lanes, subagent context firewall, scoped feedforward before edits, mechanical blocking after, session-aware verification at Stop, and continuous self-pruning of its own rule set.

That's a defensible claim once steps 1–4 land. Today's "lint pipeline" framing leaves most of the actual machinery uncredited.
