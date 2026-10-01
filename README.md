# jev-build-router

Reusable build orchestration extracted from a multi-engine sprint: **Jev (TypeSafe System One) decides which engine implements each work package**, a stdlib Python driver runs the external engines (`agy`, `codex`) headlessly, and two Claude Code Workflow scripts run the dependency-aware build with fresh-context review, adversarial refutation and fix rounds.

Pure Python 3.12 standard library. No dependencies (pytest only for tests).

```
jev-build-router/
  engines.json              default engine table (descriptions are what Jev reads)
  jbr/                      python -m jbr route|run|spawn|wait|compose-fix|status|probe|pick
    router.py               Jev batch routing -> ops/routing.json (engine + fallback chain per package)
    runner.py               prompt composition, agy/codex commands, spawn, .done markers, compose-fix
    state.py                engine availability: state file, reset-hint parsing, live probe, pick
    cli.py
  workflows/
    build_dag.js            Claude Code Workflow: DAG build (implement -> review -> refute -> fix)
    review_level.js         Claude Code Workflow: review + refute one level of packages
  skill/SKILL.md            Claude Code skill `build-router` (copy lives in ~/.claude/skills/build-router/)
  tests/                    pytest; TypeSafe mocked, engines never executed
```

## Quick start

```bash
JBR=/c/Users/a8878/dev/jev-build-router          # POSIX path of this checkout
cd /path/to/project                              # has ops/work_packages.json + ops/prompts/WPxx.md
export TYPESAFE_API_KEY=...                      # only needed for a real `route`

PYTHONPATH="$JBR" python -m jbr --help
PYTHONPATH="$JBR" python -m jbr route --dry-run                    # print the Jev questions, no API call
PYTHONPATH="$JBR" python -m jbr route --done WP01 WP02 --now 09:30 # writes ops/routing.json
PYTHONPATH="$JBR" python -m jbr --available codex_astra=yes route  # override engine availability
PYTHONPATH="$JBR" python -m jbr run WP05 --dry-run                 # compose prompt, print argv (engine from routing.json)
PYTHONPATH="$JBR" python -m jbr spawn WP05 WP06 --engine agy_gemini_pro   # detached runs, .done markers
PYTHONPATH="$JBR" python -m jbr wait WP05 WP06 --timeout-min 70
PYTHONPATH="$JBR" python -m jbr status
PYTHONPATH="$JBR" python -m jbr compose-fix review_output.json 1   # -> ops/reports/fix-WP05-round1.md
PYTHONPATH="$JBR" python -m jbr spawn WP05 --tag fix1 --extra-file ops/reports/fix-WP05-round1.md
```

## Engine chain (default since 2026-10-02)

agy is the base worker. Each implement/fix step prefers, in order (`priority` in `engines.json`):

| priority | engine | CLI |
|---|---|---|
| 1 | `agy_gemini_pro` | agy, gemini-3.1-pro-high |
| 2 | `agy_claude_opus` | agy, claude-opus-4-6-thinking |
| 3 | `codex_astra` | codex, gpt-6-astra (`-s workspace-write`) |
| 4 | `claude_subagent` | Claude Code workflow subagent; last resort, never quota-probed |

An engine that is out of quota is skipped; once its quota resets it is preferred again automatically. Availability is decided live, not by `available` flags:

```bash
PYTHONPATH="$JBR" python -m jbr probe agy_gemini_pro      # AVAILABLE (0) | EXHAUSTED <utc-until> (3) | UNAVAILABLE <reason> (4)
PYTHONPATH="$JBR" python -m jbr probe codex_astra --no-live   # state file only, never calls the engine (UNKNOWN -> 0)
PYTHONPATH="$JBR" python -m jbr pick                      # first available engine by priority; claude_subagent if none
PYTHONPATH="$JBR" python -m jbr pick --exclude agy_gemini_pro
```

- **State file** `~/.jbr/engine_state.json` (override with env `JBR_STATE`): per quota bucket `{exhausted_until, last_probe, last_status, detail, engines}`, ISO UTC. Writes take a lock file (`O_CREAT|O_EXCL`, stale after 60 s) and go through temp file + `os.replace`, so concurrent jbr processes neither lose updates nor read half-written files.
- **Buckets**: every agy engine on a Claude model (Opus, Sonnet) shares one Claude quota, stored as `agy_claude`; exhausting one exhausts all. Every other engine is its own bucket.
- **probe**: `exhausted_until` in the future -> `EXHAUSTED <until>` without calling the engine. An AVAILABLE result is cached 10 min. Otherwise one cheap live probe with a 120 s timeout: agy `--model <m> --print-timeout 45s --print "Reply only READY."`, codex `exec --skip-git-repo-check -m <m> -s workspace-write "Reply only READY. Do not run tools."`. A `READY` line -> AVAILABLE (clears exhausted); a quota signature (`runner.QUOTA_RE`) -> EXHAUSTED until the parsed reset hint; anything else (timeout, missing binary, error) -> UNAVAILABLE and skipped for 15 min. Parallel probes of one bucket serialize on a per-bucket probe lock and reuse the first result. Binaries: `$JBR_AGY_BIN` / `$JBR_CODEX_BIN`, else `PATH`, else `%LOCALAPPDATA%\agy\bin\agy.exe`.
- **Reset hints** (`state.parse_reset_hint`): `2h9m31s`, `27m28s`, `Resets in 4h53m49s` (relative); `try again at 3:51 AM` (next occurrence, machine local time); `try again at Sep 25th, 2026 3:49 AM` (local); also their space-stripped `.done` forms (`3:51AM`, `Sep25th,20263:49AM`). Unparseable -> now + 30 min.
- **Real runs feed it too**: `jbr run` that classifies `QUOTA` writes `exhausted_until`; an `OK` run marks the bucket AVAILABLE. A state-file error is logged to the run's `.log` and never fails the run.

`pip install -e .` also works (adds a `jbr` console script) but is not required.

Global options go before the subcommand: `--project <root>` (default cwd), `--packages ops/work_packages.json`, `--prompts ops/prompts`, `--reports ops/reports`, `--engines <path>` (default: this repo's `engines.json`), `--available <engine>=yes|no` (repeatable). They are also accepted after the subcommand (the spawned child relies on that).

## engines.json

Mapping `engine_key -> {description, runner, model, effort, parallel_limit, available}`.

| field | meaning |
|---|---|
| `description` | Free text **for Jev**: observed speed, defect rate, quota behaviour, model family vs reviewers. Keep it honest and dated; it is the evidence Jev weighs. |
| `runner` | `agy` \| `codex` \| `claude_subagent`. Only the first two are executed by `jbr run`; `claude_subagent` packages are implemented by workflow subagents inside `build_dag.js`. |
| `model`, `effort` | Passed to the CLI (`--model`/`-m`, `--effort`/`model_reasoning_effort`). |
| `parallel_limit` | Informational for Jev (and you); not enforced by `spawn`. |
| `available` | Static on/off switch, `true` for all four defaults since the chain (live probes decide). Override per call with `--available k=yes|no` (a `no` engine probes `UNAVAILABLE disabled`). A single usable engine short-circuits routing without an API call. |
| `priority` | Chain order, 1 = most preferred. `jbr pick`, `route` (Jev's policy and the fallback chains) and the default `engineChain` follow it. |

Default keys: `claude_subagent`, `agy_claude_opus`, `agy_gemini_pro`, `codex_astra`.

## work_packages.json

```json
{
  "rules": "Rules: Python 3.12, src layout under src/pkg, pytest.",
  "goal": "Finish Sprint 1 fastest without sacrificing numerical correctness.",
  "work_packages": [
    {
      "id": "WP04",
      "title": "Black-Scholes analytic engine",
      "goal": "one paragraph",
      "depends_on": ["WP03"],
      "files": ["src/pkg/pricing/{__init__.py,bs.py}", "tests/test_bs.py"],
      "acceptance": ["tests/test_bs.py::test_put_call_parity", "..."],
      "tests": ["see acceptance node ids"],
      "spec_refs": ["PLAN section 7"],
      "implementer_prompt": "short pointer; full spec lives in ops/prompts/WP04.md",
      "routing_hints": { "numerical_difficulty": "high", "sign_convention_critical": true, "in_flight_on": "claude_subagent" }
    }
  ]
}
```

Top-level keys (both optional): `rules` is a project-specific line PREPENDED to the built-in engineer rules (`runner.DEFAULT_RULES`: do not modify files owned by other packages, no git commit, no files outside the repo, run the full suite and report); it never replaces them. `goal` is the routing goal sentence given to Jev. A package-level `rules` field works the same way. Package ids should not contain `-` unless `ops/work_packages.json` is present when running `status` (it resolves hyphenated ids against the known ids).

A bare list of packages is accepted too. `routing_hints` is free-form and passed to Jev verbatim; `depth` (1 = root) is computed from `depends_on` unless the hints set it. `ops/prompts/<WP>.md` is inlined verbatim into the engine prompt when present.

Output `ops/routing.json`: `{"model": "jev-1.13.0", "state": {...}, "routing": {"WP04": {"engine": "agy_gemini_pro", "confidence": 0.62, "probabilities": {...}, "chain": ["agy_gemini_pro", "agy_claude_opus", "codex_astra", "claude_subagent"]}}, "chains": {"WP04": [...]}, "availability": {...}}`.

**Jev follows the chain.** `route` first probes every engine (`--dry-run` / `--no-probe`: state file only, no live probe). Jev's request state gets every engine's `priority` and `live_status` (`engine_chain`, plus the same in each engine description) and a `routing_policy`: prefer the highest-priority available engine unless the package's routing hints (numerical difficulty, state consistency, size) clearly favour another available one. EXHAUSTED / UNAVAILABLE engines are never answer criteria; if an answer names one anyway it is replaced by the highest-priority usable engine (`overridden` in the row). Each package's `chain` is Jev's pick, then the other usable engines by priority, ending in `claude_subagent` (a `claude_subagent` pick is the whole chain). Pass `.chains` straight to `build_dag.js` as `routing`. `router.load_chains()` reads them back (pre-chain files: `[engine]`).

## Run artifacts (`ops/reports/`)

`run-<WP>[-tag].prompt.md` (self-contained prompt), `.log` (engine transcript), `.done` (`"<exit> <minutes>min <STATUS> [reset-hint]"`, STATUS = `OK` | `QUOTA` | `CRASH`, e.g. `3 0.0min QUOTA 2h9m31s`; `-1 <minutes>min` without a status token when `jbr run` itself crashed, traceback appended to `.log`; `wait`/`status` print the status, `runner.engine_status()` reads old and new markers), `.stdout` (detached wrapper), `.last.md` (codex final message). `spawn` exits 2 before launching anything when the engine key is not in `engines.json`. `compose-fix` writes `review-<WP>-round<N>.json` and `fix-<WP>-round<N>.md`.

## Calling the workflows from Claude Code

Both scripts read everything from `args`; nothing project-specific is hard-coded. Use the Workflow tool with `scriptPath` pointing at the file and `args` as below.

**`workflows/build_dag.js`**

```js
{
  root: "C:\\path\\to\\project",           // required; Windows or POSIX
  posixRoot: "/c/path/to/project",         // optional, derived from root
  jbr: "/c/Users/you/dev/jev-build-router", // required when any package is routed to agy/codex
  packages: [{ id: "WP04", depends_on: ["WP03"] }, ...],   // from work_packages.json
  done: ["WP01", "WP02", "WP03"],
  routing: { WP04: ["agy_claude_opus", "codex_astra", "claude_subagent"],   // ops/routing.json .chains (list)
             WP05: "codex_astra",                  // string: pin, then the engineChain entries after it
             WP06: "auto" },                       // or omit the package: walk the whole engineChain (DEFAULT)
  engineChain: ["agy_gemini_pro", "agy_claude_opus", "codex_astra", "claude_subagent"],  // the default
  engineArgs: { agy_gemini_pro: "--timeout 55m" },   // optional extra `jbr spawn` args per engine
  jbrArgs: "--engines /c/path/engines.json",          // optional global jbr options for probe + spawn
  fallbackEngine: "claude_subagent",     // optional; appended to every chain as the last resort
  maxFixRounds: 2,
  testCmd: "python -m pytest -q",
  promptsDir: "ops/prompts", reportsDir: "ops/reports",
  sourceHint: "src layout under src/pkg",  // optional one-liner in the engineer rules
  rules: "..."                              // optional: replace the COMMON rules text entirely
}
```

Each package starts when its deps finish. Every implement/fix step walks the package's chain from the top. `claude_subagent` gets an implementer agent (effort high); an external engine gets a low-effort runner agent that **first runs `python -m jbr probe <engine>`**: EXHAUSTED/UNAVAILABLE -> it returns `engine_status: 'quota'` (with the probe line in `probe`) without spawning, and the step moves to the next engine; AVAILABLE -> it writes the fix file (fix steps), executes `python -m jbr spawn`, polls the `.done` marker and reports `engine_status` (`ok|quota|crash`). A `quota`/`crash` step is never reviewed and never consumes a fix round. **There is no run-wide skip list**: the old permanent in-run EXHAUSTED set is gone; every step asks `jbr probe` again, and the probe answers from the shared state file (instant while `exhausted_until` is in the future, live once it has passed), so an engine whose quota resets mid-run is preferred again by the next step. If every engine in the chain is dead the package ends `final: "blocked"` (no review of the dead run; `blocked: {stage, tried}`). Without `args.jbr`, auto chains are reduced to `claude_subagent` (logged); an explicit external routing entry without `jbr` is an error. Review/verify/fix-review stay Claude subagents. Returns one row per package: `{wp, engine, chain, final, rounds, history, unresolved, low_open, mutation, ownership[, blocked]}`; `history` stages carry `engines: [{engine, status, reset_hint[, probe]}]`.

**`workflows/review_level.js`**

```js
{ root: "C:\\path\\to\\project", ids: ["WP01", "WP02"], testCmd: "python -m pytest -q", logPrefix: "run",
  engineStatus: { WP02: "QUOTA" } }   // optional, from `jbr wait`/`status`: quota/crash packages are not reviewed
```

Returns `[{wp, verdict, confirmed_failures, refuted_findings, low_findings, pytest_summary, test_count, ownership_violations, mutation_check, passed_count}]`; save it as json and feed to `python -m jbr compose-fix <file> <round>`. Rows with `verdict: "blocked"` (engine did no work) get no fix file: re-run that package on another engine for the same round.

The skill in `skill/SKILL.md` (installed as `/build-router` in `~/.claude/skills/build-router/` and `~/.agents/skills/build-router/`) walks Claude Code through: `jbr probe` / `pick` -> `jbr route` (live availability + chain) -> Workflow `build_dag.js` with `routing.json .chains` -> `review_level.js` + `compose-fix` for out-of-band reviews.

## Known pitfalls (measured 2026-09-19/20)

- **agy headless needs `--dangerously-skip-permissions`**; without it every tool call is auto-denied (`a tool required the "command" permission that headless mode cannot prompt for`). The runner adds the flag. Under Claude Code auto mode the flag may be blocked by the classifier; the user has to allow it once (or add a Bash allow rule).
- **Claude thinking models reject `--effort`** on agy (thinking is the boost). The runner omits `--effort` when the model name starts with `claude`.
- **codex command shape**: `codex exec -s workspace-write --skip-git-repo-check -m <model> -c model_reasoning_effort=<effort> -C <root> -o <last.md> <pointer>`. `workspace-write` keeps it inside the repo; codex can spawn its own subagents.
- **Windows 32K command-line cap**: the full prompt is written to `run-<WP>.prompt.md` and the engine gets a one-line pointer telling it to read that file first.
- **Quota exhausted** shows up as an empty transcript, a `.done` with exit 124 (agy `--print-timeout`), or codex refusing with a usage-limit message (`You've hit your usage limit ... try again at 3:51 AM`). Since the engine chain this is handled automatically (state file + `jbr probe`); `--available <engine>=no` still forces an engine off. Observed: Claude Opus via agy exhausts after ~2 packages and stalls ~90 min (agy Sonnet shares that bucket); codex limits reset at a fixed clock time.
- **codex has no `--full-auto`** in the installed version (it exits immediately). The sandbox flag is `-s workspace-write`, used by both `build_command` and the probe.
- **Quota burned fix rounds (2026-10-01, fixed).** agy on Gemini failed instantly with exit 3 and `error: Individual quota reached ... Resets in 2h9m31s` / `AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","error_code":429,...}`. The runner only said "engine produced no test summary", `build_dag.js` reviewed the untouched code anyway and counted the dead run as a fix round, so every round was spent with no engine work and packages ended UNRESOLVED. Now `jbr run` classifies each run (`runner.classify_run`): `QUOTA` on `RESOURCE_EXHAUSTED` / 429 / `quota reached|exceeded` / `usage limit`, on exit 124 with an empty transcript, or on an exit within 1 min with < 1000 chars of transcript; `CRASH` on a jbr crash or a non-zero exit with no output; else `OK`. `build_dag.js` falls back (see above) instead of reviewing; for manual flows pass `engineStatus` to `review_level.js`. The fast-exit rule also catches a missing binary (`agy: command not found`) as QUOTA; the fallback is the right response either way, but read the log before re-routing for hours.
- **Gemini via agy** is fast (~6 min/package) but spec-literal misses are common (missing validators, stubbed functions, weakened tests): budget one fix round. Its different model family means Claude reviewers actually catch its errors instead of sharing blind spots.
- **Reviewers and implementers share a checkout**: the workflow prompts forbid `git checkout/stash/reset` and tell reviewers to restore mutations by reverse edit (untracked files make `git checkout --` unsafe).
- `spawn` sets `PYTHONPATH` to this repo for the child; the parent still needs `PYTHONPATH="$JBR"` (or `pip install -e .`) to import `jbr`.

## Tests

```bash
cd /c/Users/a8878/dev/jev-build-router && python -m pytest -q
```

Router tests monkeypatch `urllib.request.urlopen` (no real TypeSafe calls); runner and probe tests use `--dry-run` / a fake `subprocess.run` (agy/codex never executed); every test gets its own `JBR_STATE` (autouse fixture in `tests/conftest.py`), so `~/.jbr` is never touched. Workflow tests (`tests/test_workflows.py`, `tests/test_quota.py`, `tests/test_chain.py`) run the scripts under node via `tests/workflow_harness.js` with stubbed agents and are skipped when node is missing.
