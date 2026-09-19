# jev-build-router

Reusable build orchestration extracted from a multi-engine sprint: **Jev (TypeSafe System One) decides which engine implements each work package**, a stdlib Python driver runs the external engines (`agy`, `codex`) headlessly, and two Claude Code Workflow scripts run the dependency-aware build with fresh-context review, adversarial refutation and fix rounds.

Pure Python 3.12 standard library. No dependencies (pytest only for tests).

```
jev-build-router/
  engines.json              default engine table (descriptions are what Jev reads)
  jbr/                      python -m jbr route|run|spawn|wait|compose-fix|status
    router.py               Jev batch routing -> ops/routing.json
    runner.py               prompt composition, agy/codex commands, spawn, .done markers, compose-fix
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
| `available` | Only available engines appear as answer criteria. Override per call with `--available k=yes|no`; a single available engine short-circuits routing without an API call. |

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

Output `ops/routing.json`: `{"model": "jev-1.13.0", "state": {...}, "routing": {"WP04": {"engine": "agy_gemini_pro", "confidence": 0.62, "probabilities": {...}}}}`.

## Run artifacts (`ops/reports/`)

`run-<WP>[-tag].prompt.md` (self-contained prompt), `.log` (engine transcript), `.done` (`"<exit> <minutes>min"`; `-1` when `jbr run` itself crashed, traceback appended to `.log`), `.stdout` (detached wrapper), `.last.md` (codex final message). `spawn` exits 2 before launching anything when the engine key is not in `engines.json`. `compose-fix` writes `review-<WP>-round<N>.json` and `fix-<WP>-round<N>.md`.

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
  routing: { WP04: "claude_subagent", WP05: "agy_gemini_pro" },   // ops/routing.json .routing[wp].engine
  engineArgs: { agy_gemini_pro: "--timeout 55m" },   // optional extra `jbr spawn` args per engine
  maxFixRounds: 2,
  testCmd: "python -m pytest -q",
  promptsDir: "ops/prompts", reportsDir: "ops/reports",
  sourceHint: "src layout under src/pkg",  // optional one-liner in the engineer rules
  rules: "..."                              // optional: replace the COMMON rules text entirely
}
```

Each package starts when its deps finish. `claude_subagent` packages get an implementer agent (effort high); external engines get a low-effort runner agent that executes `python -m jbr spawn`, polls the `.done` marker, and reports. Every package then goes through review -> refute -> fix (up to `maxFixRounds`). Returns one row per package: `{wp, engine, final, rounds, history, unresolved, low_open, mutation, ownership}`.

**`workflows/review_level.js`**

```js
{ root: "C:\\path\\to\\project", ids: ["WP01", "WP02"], testCmd: "python -m pytest -q", logPrefix: "run" }
```

Returns `[{wp, verdict, confirmed_failures, refuted_findings, low_findings, pytest_summary, test_count, ownership_violations, mutation_check, passed_count}]`; save it as json and feed to `python -m jbr compose-fix <file> <round>`.

The skill in `skill/SKILL.md` (installed as `/build-router`) walks Claude Code through: read `engines.json` -> `jbr route` -> Workflow `build_dag.js` with the routing -> `review_level.js` + `compose-fix` for out-of-band reviews.

## Known pitfalls (measured 2026-09-19/20)

- **agy headless needs `--dangerously-skip-permissions`**; without it every tool call is auto-denied (`a tool required the "command" permission that headless mode cannot prompt for`). The runner adds the flag. Under Claude Code auto mode the flag may be blocked by the classifier; the user has to allow it once (or add a Bash allow rule).
- **Claude thinking models reject `--effort`** on agy (thinking is the boost). The runner omits `--effort` when the model name starts with `claude`.
- **codex command shape**: `codex exec -s workspace-write --skip-git-repo-check -m <model> -c model_reasoning_effort=<effort> -C <root> -o <last.md> <pointer>`. `workspace-write` keeps it inside the repo; codex can spawn its own subagents.
- **Windows 32K command-line cap**: the full prompt is written to `run-<WP>.prompt.md` and the engine gets a one-line pointer telling it to read that file first.
- **Quota exhausted** shows up as an empty transcript, a `.done` with exit 124 (agy `--print-timeout`), or codex refusing with a usage-limit message. Re-route with `--available <engine>=no`. Observed: Claude Opus via agy exhausts after ~2 packages and stalls ~90 min; codex limits reset at a fixed clock time.
- **Gemini via agy** is fast (~6 min/package) but spec-literal misses are common (missing validators, stubbed functions, weakened tests): budget one fix round. Its different model family means Claude reviewers actually catch its errors instead of sharing blind spots.
- **Reviewers and implementers share a checkout**: the workflow prompts forbid `git checkout/stash/reset` and tell reviewers to restore mutations by reverse edit (untracked files make `git checkout --` unsafe).
- `spawn` sets `PYTHONPATH` to this repo for the child; the parent still needs `PYTHONPATH="$JBR"` (or `pip install -e .`) to import `jbr`.

## Tests

```bash
cd /c/Users/a8878/dev/jev-build-router && python -m pytest -q
```

Router tests monkeypatch `urllib.request.urlopen` (no real TypeSafe calls); runner tests use `--dry-run` / a fake `subprocess.run` (agy/codex never executed).
