---
name: build-router
description: "Use when a project has work packages to build (ops/work_packages.json + ops/prompts/WPxx.md) and you must decide which engine implements each one (Claude Code subagent, agy on Claude/Gemini, Codex) and then drive the build with fresh-context review, adversarial refutation and fix rounds. Wraps the jev-build-router repo: Jev routing via TypeSafe, headless agy/codex driver, build_dag and review_level Workflow scripts."
---

# /build-router

Repo: `C:\Users\a8878\dev\jev-build-router` (POSIX `/c/Users/a8878/dev/jev-build-router`). Pure Python 3.12 stdlib; no install needed, run with `PYTHONPATH`.

```
JBR=/c/Users/a8878/dev/jev-build-router
cd <project> && PYTHONPATH="$JBR" python -m jbr --help
```

## Default engine chain (user rule, 2026-10-02)

agy is the base worker. Every implement/fix step prefers, in order:

1. `agy_gemini_pro` (agy, gemini-3.1-pro-high)
2. `agy_claude_opus` (agy, claude-opus-4-6-thinking; shares ONE Claude quota bucket with any other agy Claude model, e.g. Sonnet)
3. `codex_astra` (codex, gpt-6-astra; flags `-s workspace-write`, there is no `--full-auto`)
4. `claude_subagent` (Claude Code workflow subagent; last resort, never quota-probed)

An engine out of quota is skipped until its reset time, then preferred again automatically. Availability is live, not configured: `engines.json` has `priority` 1..4 and every engine `available: true`; the shared state file `~/.jbr/engine_state.json` (override `JBR_STATE`) records `exhausted_until` per quota bucket.

```
PYTHONPATH="$JBR" python -m jbr probe agy_gemini_pro   # AVAILABLE (exit 0) | EXHAUSTED <utc-until> (3) | UNAVAILABLE <reason> (4)
PYTHONPATH="$JBR" python -m jbr pick                   # first available engine by priority (claude_subagent if none)
PYTHONPATH="$JBR" python -m jbr pick --exclude agy_gemini_pro,codex_astra
```

`probe` never calls the engine while `exhausted_until` is in the future, caches AVAILABLE for 10 min, and otherwise runs one cheap live probe (`agy --model <m> --print-timeout 45s --print "Reply only READY."` / `codex exec --skip-git-repo-check -m <m> -s workspace-write "Reply only READY. Do not run tools."`, 120 s timeout). Quota signature -> EXHAUSTED until the parsed reset hint (`2h9m31s`, `try again at 3:51 AM`, `try again at Sep 25th, 2026 3:49 AM`; unparseable -> +30 min); any other failure -> UNAVAILABLE and skipped 15 min. A real `jbr run` that ends QUOTA writes the same state, so the next probe already knows.

## Preconditions in the target project

- `ops/work_packages.json`: `{"work_packages": [{id, title, goal, depends_on, files, acceptance, tests, spec_refs, implementer_prompt, routing_hints?}]}`. Optional top-level `rules` (project line prepended to the built-in ownership / no-commit / no-files-outside-repo rules, never replacing them) and `goal` (routing goal).
- `ops/prompts/<WP>.md`: the full per-package spec (inlined verbatim into the engine prompt).
- `TYPESAFE_API_KEY` in the environment for `route` (not needed for `--dry-run` or when only one engine is usable).

## Steps

You (the main session) stay the commander and assign packages with Jev's help; Jev follows the chain.

1. **Check the engines.** `PYTHONPATH="$JBR" python -m jbr probe <engine>` for each (or just let `route` do it). Force an engine off for one call with `--available codex_astra=no`; do not edit `engines.json` for a one-off.
2. **Route.** `PYTHONPATH="$JBR" python -m jbr --project . route --done WP01 WP02 --now 09:30` probes every engine live, passes each engine's priority and live status to Jev (exhausted/unavailable engines are never offered), asks Jev to prefer the highest-priority available engine unless a package's hints (numerical difficulty, state consistency, size) clearly favour another, and writes `ops/routing.json`: `routing[wp] = {engine, confidence, probabilities, chain}` plus top-level `chains: {wp: [Jev's pick, other available engines by priority, ..., "claude_subagent"]}`. `--dry-run` (or `--no-probe`) uses only the state file, no live probes, no API call. Show the user the routing table before building.
3. **Build with the DAG workflow.** Call the Claude Code Workflow tool with `scriptPath = C:\Users\a8878\dev\jev-build-router\workflows\build_dag.js` and
   ```
   args = { root: "<project root>", jbr: "/c/Users/a8878/dev/jev-build-router",
            packages: [{id, depends_on}, ...],       // from work_packages.json
            done: [...],                              // already accepted packages
            routing: <ops/routing.json .chains>,      // per package: [chain] | "engine" (pin) | "auto"; omit -> auto
            maxFixRounds: 2, testCmd: "python -m pytest -q" }
   ```
   Routing values: a list is the package's chain; a string pins that engine first and falls back along `engineChain` after it; `"auto"` or no entry walks the whole `engineChain` (default `['agy_gemini_pro','agy_claude_opus','codex_astra','claude_subagent']`). For each external engine the low-effort runner agent first runs `jbr probe`; EXHAUSTED/UNAVAILABLE -> reported as `quota` without spawning and the step moves to the next engine. There is no run-wide skip list: every step probes again, so a recovered engine is used again later in the same run. Quota/crash runs are never reviewed and never consume a fix round; review/verify/fix stay Claude subagents. Without `jbr`, auto chains reduce to `claude_subagent`.
4. **Review an already-built level separately** (e.g. packages built outside the workflow): Workflow tool with `scriptPath = ...\workflows\review_level.js`, `args = { root, ids: ["WP01", "WP02"] }`. Save the workflow output to a json file, then `PYTHONPATH="$JBR" python -m jbr compose-fix <output.json> <round>` writes `ops/reports/fix-<WP>-round<N>.md`; re-run on `$(python -m jbr pick)` with `PYTHONPATH="$JBR" python -m jbr spawn WP --engine <engine> --tag fix1 --extra-file ops/reports/fix-WP-round1.md`.
5. **Monitor.** `PYTHONPATH="$JBR" python -m jbr status` (routing + `.done` markers), `PYTHONPATH="$JBR" python -m jbr wait WP05 WP06 --timeout-min 70` (exit 124 while a marker is still missing), `cat ~/.jbr/engine_state.json` (who is exhausted until when). Every `python -m jbr` call needs `PYTHONPATH="$JBR"` unless `pip install -e` was run.

## Known engine pitfalls (see README "Known pitfalls")

- agy needs `--dangerously-skip-permissions` in headless mode (runner adds it); Claude thinking models reject `--effort` (runner omits it for `claude*` models).
- codex command shape is fixed in `jbr/runner.py::build_command` (`-s workspace-write`; `--full-auto` does not exist in this codex and exits immediately); `-o` captures the final message.
- Prompts are handed over as a file pointer (Windows 32K command-line cap).
- Quota exhausted: no output, or exit 124 from `--print-timeout`, or instant exit 3 with `RESOURCE_EXHAUSTED`/429, or codex `You've hit your usage limit ... try again at 3:51 AM`. The `.done` marker says `QUOTA <reset-hint>`, the state file gets `exhausted_until`, and `build_dag.js` moves the step down the chain; packages where every engine is dead end `blocked`. Manual flow: pass `engineStatus` to `review_level.js` so dead runs are not reviewed.
- agy Opus and agy Sonnet drain the same Claude quota: one exhausts both (state key `agy_claude`).

## Do not

- Do not edit the target project's files from this skill; only `ops/routing.json` and `ops/reports/*` are written (plus the engine state file under `~/.jbr`).
- Do not call the TypeSafe API when the user only wants to see the plan: use `route --dry-run`.
