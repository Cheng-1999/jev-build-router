# jev-build-router

Reusable build orchestration extracted from a multi-engine sprint: **Jev (TypeSafe System One) decides which engine implements each work package**, a stdlib Python driver runs the external engines (`agy`, `codex`) headlessly, and two Claude Code Workflow scripts run the dependency-aware build with fresh-context review, adversarial refutation and fix rounds.

Pure Python 3.12 standard library. No dependencies (pytest only for tests).

```
jev-build-router/
  engines.json              default engine table (descriptions are what Jev reads)
  jbr/                      python -m jbr route|run|spawn|wait|compose-fix|status|probe|pick|prereview|handoff|done
    router.py               routing.json: commander `assign` or decision-model `route` (engine + fallback chain)
    paths.py                where jbr keeps files: ~/.jbr/runs/<project key>/ (never inside the project)
    queue.py                backlog lane: one engine (local Qwen), one package at a time, in git worktrees
    ledger.py               engine ledger + scorecard
    runner.py               prompt composition, agy/codex commands, spawn, .done markers, compose-fix, manual handoff
    state.py                engine availability: state file, reset-hint parsing, live probe, pick
    prereview.py            cheap first-pass review: open-code-review on local Ollama, before the Claude review
    cli.py
  workflows/
    build_dag.js            Claude Code Workflow: DAG build (implement -> review -> refute -> fix)
    review_level.js         Claude Code Workflow: review + refute one level of packages
  skill/SKILL.md            Claude Code skill `build-router` (copy lives in ~/.claude/skills/build-router/)
  tests/                    pytest; TypeSafe mocked, engines never executed
```

## Quick start

```bash
JBR=/c/Users/a8878/dev/jev-build-router          # Mac: /Users/ad/code/jev-build-router
cd /path/to/project                              # has ops/work_packages.json + ops/prompts/WPxx.md
export TYPESAFE_API_KEY=...                      # only needed for a real `route`

PYTHONPATH="$JBR" python -m jbr --help
PYTHONPATH="$JBR" python -m jbr route --dry-run                    # print the Jev questions, no API call
PYTHONPATH="$JBR" python -m jbr route --done WP01 WP02 --now 09:30 # writes ~/.jbr/runs/<key>/routing.json
PYTHONPATH="$JBR" python -m jbr --available codex_astra=yes route  # override engine availability
PYTHONPATH="$JBR" python -m jbr run WP05 --dry-run                 # compose prompt, print argv (engine from routing.json)
PYTHONPATH="$JBR" python -m jbr spawn WP05 WP06 --engine agy_gemini_pro   # detached runs, .done markers
PYTHONPATH="$JBR" python -m jbr wait WP05 WP06 --timeout-min 70
PYTHONPATH="$JBR" python -m jbr status
PYTHONPATH="$JBR" python -m jbr compose-fix review_output.json 1   # -> <reports_dir>/fix-WP05-round1.md (`jbr paths`)
PYTHONPATH="$JBR" python -m jbr spawn WP05 --tag fix1 --extra-file <reports_dir>/fix-WP05-round1.md
```

To route with a local Clef server, set `JBR_DECISION_URL=http://127.0.0.1:8000/v1/systemone`
and pass `route --model clef-flash-4bit`; no API token is needed, even when
`TYPESAFE_API_KEY` is present in the shell. For hosted Clef, point
`JBR_DECISION_URL` at the Workers AI `@cf/cloudflare/clef` or `clef-flash`
endpoint, set `CLOUDFLARE_AI_TOKEN`, and pass the matching `--model`. An
arbitrary HTTPS System One endpoint requires `JBR_DECISION_TOKEN`. A Jev key
is sent only to the default Jev URL.

## Engine chain (default since 2026-10-02; automated agy off since 2026-10-24)

Each implement/fix step prefers, in order (`priority` in `engines.json`). **The two automated agy engines ship
`available: false` and are not in `build_dag.js`'s default `engineChain`** (see "Manual agy" below for why and
for `agy_manual`). To opt back in at your own risk: `--available agy_gemini_pro=yes --available agy_claude_opus=yes`
on the jbr CLI, and list them in `args.engineChain`.

| priority | engine | CLI |
|---|---|---|
| 1 | `agy_gemini_pro` | **off by default.** agy, `auto:gemini` (newest Gemini `agy models` lists; 2026-10-16: gemini-4-argon-high) |
| 2 | `agy_claude_opus` | **off by default.** agy, `auto:claude-opus` (newest Claude Opus; 2026-10-16: claude-opus-5-5-high) |
| 3 | `codex_astra` | codex, gpt-6-astra (`-s workspace-write`) |
| 4 | `ollama_qwen` | local Qwen 3.8 27B on Ollama via codex `--oss` (Mac only; free, slow; runners wait ~140 min) |
| 4 | `lan_qwen` | Qwen on a LAN OpenAI-compatible server, codex with `CODEX_HOME=~/.codex-local` (free; needs `LOCAL_LLM_KEY`; runners wait ~140 min) |
| 5 | `claude_subagent` | Claude Code workflow subagent; last resort, never quota-probed |
| (4) | `muse_spark` | **assign only**: Meta Muse Code, `muse exec` on `muse-spark-1.3-contributor` (Meta may train on what it is sent; needs billing set up at dev.meta.ai). Never in a chain, `pick` or Jev |

**agy model names** (`jbr/models.py`): an agy engine's `"model"` may be `auto:<family>` instead of a pinned id, so a
retired id (agy dropped gemini-3.1-pro-high on 2026-10-16) no longer breaks the chain. `jbr probe` and `jbr run`
resolve it from `agy models`: highest leading version, then the suffix equal to the engine's `effort`, then the
highest effort, then the id. The family must end right before the version (`auto:claude-opus`, not `auto:claude`).
The listing is cached for 1 h in `agy_models.json` next to the engine state; a failed listing falls back to a stale
cache, and with no cache the probe reports UNAVAILABLE. A dry-run reads only the cache. The run log header shows
`model=<resolved id> (auto:<family>)`. Pinned ids keep working; `auto:` is agy-only (codex has no model list).

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

Global options go before the subcommand: `--project <root>` (default cwd), `--packages ops/work_packages.json`, `--prompts ops/prompts`, `--reports <dir>` (default `~/.jbr/runs/<project key>/reports`; relative = inside the project), `--engines <path>` (default: this repo's `engines.json`), `--available <engine>=yes|no` (repeatable). They are also accepted after the subcommand (the spawned child relies on that).

## Manual agy (`agy_manual`, since 2026-10-24)

Google's Antigravity FAQ (checked 2026-10-24) says using third-party software to access Antigravity violates
its terms and can get an account suspended. It also says `agy -p` headless mode is for "direct scripting and CI
workflows", and that wrapping `agy` inside third-party coding agents is not supported on consumer plans. Driving
`agy_gemini_pro` / `agy_claude_opus` from `build_dag.js` (Claude Code -> `jbr spawn` -> `agy -p`) is that pattern.
`agy_manual` keeps agy in the build without wrapping it. **You** start agy and paste the packet. jbr only writes
files and records what you tell it. Review stays in Claude Code.

```
PYTHONPATH="$JBR" python -m jbr assign WP05=agy_manual                 # chain is just [agy_manual]: no automatic fallback
PYTHONPATH="$JBR" python -m jbr handoff WP05                           # writes run-WP05.prompt.md, prints the line to paste into agy
PYTHONPATH="$JBR" python -m jbr handoff WP05 --print | pbcopy           # or paste the whole packet (agy may not read ~/.jbr)
#   ... you run agy yourself, in a session you started, on the model you pick ...
PYTHONPATH="$JBR" python -m jbr done WP05 --notes-file agy-report.txt  # .done "0 <min>min OK"; --status CRASH|QUOTA if it did nothing
#   review (OK only): workflows/review_level.js with ids ['WP05'] (it reads run-WP05.log, which now holds your notes).
#   CRASH/QUOTA: nothing to review; hand it off again (same round) or pass engineStatus {WP05: 'quota'} to review_level.js
PYTHONPATH="$JBR" python -m jbr compose-fix review-out.json 1            # -> fix-WP05-round1.md
PYTHONPATH="$JBR" python -m jbr handoff WP05 --tag fix1 --extra-file <reports>/fix-WP05-round1.md
PYTHONPATH="$JBR" python -m jbr done WP05 --tag fix1                    # then review_level.js again with logTag 'fix1'
```

**Several packages in one agy session (`--pending`).** One checklist covers every package assigned to a manual
engine with no recorded outcome, plus every open fix-round handoff, in dependency order:

```
PYTHONPATH="$JBR" python -m jbr assign WP01=agy_manual WP02=agy_manual WP03=agy_manual
PYTHONPATH="$JBR" python -m jbr handoff --pending            # writes <reports>/handoff-pending.md, prints the line to paste
PYTHONPATH="$JBR" python -m jbr handoff --pending --print    # or the checklist with every packet inlined
#   ... one agy session you start works through them in order and prints a block per package:
#       === REPORT WP01 ===  status: done|failed  tests: ...  files: ...  unsatisfied: ...  === END WP01 ===
#   save that session's output to a file, then:
PYTHONPATH="$JBR" python -m jbr done --from agy-session.txt   # status: done -> OK, anything else -> CRASH; block = notes
```

`python -m jbr inbox` (no `--project`) lists every project's checklist that still has open items, with the
line to paste and the `done --from` command. It only reads files under `~/.jbr/runs`. The `build-router`
skill has each project's Claude session do the assigning and write the checklist, so all that's left for you
is `jbr inbox` and starting agy.

New packages get their packet. Packets already handed off are reused as they are, keeping their start time and
fix instructions, and a package with an open fix round gets no new round-0 packet. Rerun `--pending` at any
time: it lists only what is still open, and deletes the checklist once nothing is. Each entry gives the exact REPORT ID to use (`WP03-fix1` for a fix round). A bare `REPORT WP03`
is recorded against WP03's one open fix round when round 0 is already recorded. Otherwise it is refused, and a
recorded outcome is never overwritten without `--force`. A package whose dependency is not built yet gets no
packet and is listed under NOT YET. A dependency counts as built when it is earlier on the checklist, has an OK
run, or is passed with `--done WP..`: use that for packages a Claude subagent built, since those leave no run
marker. Blocking carries down the dependency chain, and fix rounds are never blocked. `done --from` records
every block it finds (pass ids to limit it). Ids with no block stay open, and a recorded CRASH has to be handed
off again explicitly. In a batch, each package's minutes are measured from its own `handoff`, so later
packages show the time spent on earlier ones as well.

- runner `manual`: `run`, `spawn` and `queue add` refuse it (exit 2, before any marker is written). `probe` answers
  `UNAVAILABLE manual: ...` without calling anything. `pick`, `route` (Jev never sees it) and every chain skip it.
  Only an explicit `assign WP=agy_manual` or a `build_dag.js` routing pin hands work to it.
- `build_dag.js`: when a package's routing pins a manual engine (`args.manualEngines`, default `['agy_manual']`),
  its row ends `final: 'manual'` with the `handoff` command. Packages that depend on it end `final: 'waiting'`
  (`waiting_on`). Nothing is spawned or polled for either. A manual engine anywhere else in a chain is dropped.
  `jbr record` skips `manual` / `waiting` rows. Re-run the workflow with the package in `done` once it is accepted.
- `status` shows a handed-off package as ``handed off to agy_manual: waiting for `jbr done` ``. `done` without a
  prior `handoff` is refused, and so is a second `done` (`--force` overwrites). `--minutes` defaults to the time
  since `handoff`. `run` / `spawn` delete a stale `.handoff`, so a package re-assigned to an automatic engine
  cannot be marked done by hand.
- `agy_gemini_pro` / `agy_claude_opus` ship `available: false` and are out of the default `engineChain`, so no
  automatic build calls agy unless you opt in explicitly (see Engine chain).

## Commander routing and the engine ledger (default since 2026-10-10)

The main Claude Code session assigns packages; a decision model is an optional second opinion.

```bash
PYTHONPATH="$JBR" python -m jbr scorecard                      # per-engine track record (~/.jbr/ledger.jsonl)
PYTHONPATH="$JBR" python -m jbr assign WP01=agy_gemini_pro WP02=codex_astra \
    --reason "WP02=billing path; non-Claude family for review" [--second-opinion]
PYTHONPATH="$JBR" python -m jbr record build_result.json --build my-build   # after build_dag.js returns
```

- `assign` probes live, refuses unknown or exhausted engines (no silent substitution), and writes the same
  `routing.json` shape as `route` (`model: "commander"`, per package `engine`, `reason`, `chain`), so
  `build_dag.js`, `run` and `spawn` read it unchanged. `--second-opinion` also asks the model at
  `JBR_DECISION_URL` (use a local Clef server, free) and stores its pick as `second_opinion`; it is never applied.
- `record` takes the list `build_dag.js` returns and appends one line per package: the engine whose implement
  run succeeded (not the first one tried), final verdict, fix rounds, confirmed defects, quota hits, and
  minutes summed from the project's `run-<WP>*.done` markers. `scorecard` aggregates per engine.
- `route` (a decision model routes every package) remains for comparison.

## Where jbr keeps files

Nothing is written into the project. Per project (key = its absolute path with separators turned into `-`):

```
~/.jbr/runs/<key>/reports/       run prompts, engine logs, .done markers, fix files, review json
~/.jbr/runs/<key>/routing.json   commander / decision-model routing (ops/routing.json is still READ as a legacy fallback)
~/.jbr/runs/<key>/queue.json     the backlog lane, plus worker.lock, worker.log and worktrees/
~/.jbr/ledger.jsonl              engine ledger (all projects)
~/.jbr/engine_state.json         quota / availability state
~/.jbr/ocr-home/                 prereview's private HOME for ocr (config with the local provider only, ocr sessions)
```

`python -m jbr --project <root> paths` prints them; pass `.reports_dir` to the Workflow scripts as `reportsDir`.
`JBR_RUNS`, `JBR_LEDGER`, `JBR_STATE` override the locations. `--reports <dir>` still works (relative = inside the project).

## Backlog lane (`jbr queue`)

Not-urgent or easy packages, worked off unattended by one engine (default `ollama_qwen`), one at a time
(the Mac serves one Ollama request at a time: two concurrent jobs took as long as two in a row):

```bash
PYTHONPATH="$JBR" python -m jbr --project . --packages <plan>/work_packages.json --prompts <plan>/prompts queue add WP07 WP08
PYTHONPATH="$JBR" python -m jbr --project . queue work --detach      # background worker; one per project (pid lock)
PYTHONPATH="$JBR" python -m jbr --project . queue status
PYTHONPATH="$JBR" python -m jbr --project . queue review-args        # review_level.js args per finished package
PYTHONPATH="$JBR" python -m jbr --project . queue fix WP07 <fix.md>  # another round, same worktree
PYTHONPATH="$JBR" python -m jbr --project . queue clean WP07         # after merge (refuses unreviewed work without --force)
```

Each package gets a worktree on `jbr/<engine>/<WP>-<stamp>` from a pinned base, with `.venv`/`node_modules`
symlinked in; the worker runs the engine, then the package's test command, records changed files and the
test tail, and moves on. It never commits, pushes or reviews, and stops when the engine's probe fails.
On Windows the links are directory junctions when symlinks need a privilege you lack (no Developer Mode), and
a worktree path longer than 120 characters moves to `~/.jbr/runs/wt/<hash>/<WP>-<stamp>`: Git for Windows
refuses worktree paths past ~210 characters, and the project's own files must still fit under MAX_PATH.

## Pre-review: open-code-review on local Qwen (opt-in, since 2026-10-18)

[alibaba/open-code-review](https://github.com/alibaba/open-code-review) (`ocr`, Apache-2.0) turns a git diff into
line-level comments: deterministic file selection, bundling, rule matching and comment positioning around LLM
sub-agents. `jbr prereview` runs it against the local Ollama model before a Claude review, and the Claude
reviewer gets its findings as unverified hints. Design, spike numbers and the recommendation:
[docs/OPEN_CODE_REVIEW_PROPOSAL.md](docs/OPEN_CODE_REVIEW_PROPOSAL.md).

```bash
npm i -g @alibaba-group/open-code-review          # or a release binary; JBR_OCR_BIN=<path> points jbr at it
PYTHONPATH="$JBR" python -m jbr prereview WP05 --dry-run         # scope + argv, runs nothing
PYTHONPATH="$JBR" python -m jbr prereview WP05 --tag r1 [--detach] [--commit <sha>] [--effort low|medium|high] \
    [--budget-min 45] [--url http://127.0.0.1:11434/v1] [--model <id>] [--backend-engine ollama_qwen] [--no-spec]
```

- **Scope**: the package's owned files (`files` globs in work_packages.json) among the changed files of the
  workspace (`git diff HEAD` + untracked) or of `--commit`. Every other changed file goes to `--exclude` as a
  full repo-relative path, or as `dir/**` for its shallowest directory without an owned change, because
  `build_dag.js` builds packages side by side in one checkout. ocr matches each pattern against the whole path;
  this was checked with `--preview` against v1.13.1. A package without `files` is SKIPPED in
  workspace mode. `ops/prompts/<WP>.md` (`--prompts`) is passed as `--background-file` (`--no-spec` turns
  that off). ocr skips test files by default.
- **Local only**: the default endpoint is `http://127.0.0.1:11434/v1`, and the default model is `ollama_qwen`'s
  (that engine disabled = SKIPPED). A non-loopback `--url` is refused unless `--allow-remote` is given, and with
  it more than the diff leaves the machine: ocr's agent reads files of the repo. ocr runs with `HOME` set to
  jbr's own ocr home (`~/.jbr/ocr-home`, override `JBR_OCR_HOME`). That home holds only the `jbr-local`
  provider, and its sessions dir is cleared before each run. The environment is an allow-list: no
  `ANTHROPIC_*` / `OPENAI_*` / `OCR_LLM_*` from your shell, which ocr would otherwise pick up. Your global git
  ignore file and `safe.directory` are passed through `GIT_CONFIG_*`, plus `OCR_NO_UPDATE=1`.
- **Never fails a build**: always exits 0. `prereview-<WP>[-tag].done` reads `<exit> <minutes>min <STATUS>`
  (exit -1: no ocr exit code, because it never ran or was killed). STATUS values:
  - `OK`
  - `SKIPPED`: no ocr; backend not ready, from one GET of `<url>/models` that records nothing in the engine
    state; nothing owned changed; non-local URL; or `busy`, meaning another prereview held the machine-wide lock
    for 5 min (Ollama serves one request at a time).
  - `FAILED`
  - `TIMEOUT`: past `--budget-min`, default 45. ocr first gets SIGTERM and publishes a partial result, which is
    used as `OK` if it has findings; 30 s later the process tree is killed.

  Findings: `prereview-<WP>[-tag].json`; ocr's own output in `.raw.json`, its log in `.log`.
- **Workflows**: pass `preReview: true | {effort, budgetMin}` to `build_dag.js` (which also takes `rounds:
  'first'|'all'`, default `first`) and to `review_level.js`. Both need `jbr` and `reportsDir`. `jbrArgs` must
  carry `--packages <plan>/work_packages.json` when the plan is not at `ops/work_packages.json`; spawn already
  needs it there. A low-effort agent runs `jbr prereview --detach` and polls the marker.
  - With status `ok` and at least one finding, the reviewer prompt gets a step 0: check each finding at the cited
    lines, report real ones (on owned files) as failures whose criterion starts with `pre-review: `, then spend
    the effort on what ocr does not cover (acceptance criteria, tests, ownership, mutation check).
  - Those failures are refuted like any other and reach compose-fix unchanged.
  - Any other outcome means the plain review runs, with an identical prompt.
  - Review history rows get `prereview: {status, findings, confirmed}`. Default off.
- **Cost**: wall-clock only, no cloud tokens. The Claude review of a package waits for its prereview. Because
  prereviews run one at a time, packages that reach review together are delayed. After 5 min of waiting a
  package proceeds without hints (`busy`). The prereview shares Ollama with an `ollama_qwen` implementer run,
  and each slows the other.

## engines.json

Mapping `engine_key -> {description, runner, model, effort, parallel_limit, available}`.

| field | meaning |
|---|---|
| `description` | Free text **for Jev**: observed speed, defect rate, quota behaviour, model family vs reviewers. Keep it honest and dated; it is the evidence Jev weighs. |
| `runner` | `agy` \| `codex` \| `muse` \| `claude_subagent` \| `manual`. Only the first three are executed by `jbr run`; `claude_subagent` packages are implemented by workflow subagents inside `build_dag.js`; `manual` engines are run by the user (`jbr handoff` / `jbr done`, see Manual agy). |
| `model`, `effort` | Passed to the CLI (`--model`/`-m`, `--effort`/`model_reasoning_effort`). |
| `parallel_limit` | Informational for Jev (and you); not enforced by `spawn`. |
| `available` | Static on/off switch: `true` for every shipped engine except `agy_gemini_pro` / `agy_claude_opus` (off since 2026-10-24); live probes decide the rest. Override per call with `--available k=yes|no` (a `no` engine probes `UNAVAILABLE disabled`). A single usable engine short-circuits routing without an API call. |
| `priority` | Chain order, 1 = most preferred. `jbr pick`, `route` (Jev's policy and the fallback chains) and the default `engineChain` follow it. Ties keep engines.json order. |
| `env` | Optional `{VAR: value}` (`~` expanded) set for the engine's run and its probe, e.g. `lan_qwen`'s `CODEX_HOME`. |
| `windows_sandbox` | Optional codex sandbox used instead of `workspace-write` on Windows only (`danger-full-access` where the sandboxed shell fails with `CryptUnprotectData failed`). |
| `effort` | An empty string drops `model_reasoning_effort` (servers that take no effort). |
| `assign_only` | `true`: never in an automatic chain, `pick`, or Jev's choices, and no live probe from `availability`/`route`. Only `assign WP=<engine>` (or a build_dag pin) uses it, and that run's own probe checks it. For engines with a cost you choose per package, such as `muse_spark` sending code to Meta. |
| `contributor_mode` / `max_model_steps` | muse only: `--contributor-mode on\|off` and `--max-model-steps N`. |

Default keys: `claude_subagent`, `agy_claude_opus`, `agy_gemini_pro`, `agy_manual`, `codex_astra`, `ollama_qwen`, `lan_qwen`, `muse_spark`.

**muse_spark (2026-10-24).** Runs `muse exec --model <m> --workspace <root> --prompt-file <prompt> --disable-approval --disable-request-user-input --no-foreign-personal-context --reasoning-effort <e> --contributor-mode on`. The CLI reads the prompt file itself. Approvals are off because nobody can answer them headless; the OS sandbox stays on (never `--yolo`), and personal rules and skills from other agents are not loaded. The probe sends `Reply only READY.` with shell, writes, web and session log all off. `API error 402 ... Billing verification failed` gives UNAVAILABLE on probe and CRASH on a run (not QUOTA). On 2026-10-24 that is what this account returned, even on the contributor model, so it needs billing set up at dev.meta.ai. For private code, override with model `muse-spark-1.3` and `contributor_mode: off` (paid, not used for training).

**LAN Qwen setup (`lan_qwen`).** codex with the user's normal `~/.codex` sends ~108k tokens (plugins, skills, MCP
tools, AGENTS.md), more than a 32K local model holds. `lan_qwen` therefore runs codex with its own slim home,
`~/.codex-local/config.toml`, holding only the provider:

```toml
model_provider = "lan"
model = "<model id from GET /v1/models>"
model_context_window = 32768
approval_policy = "never"

[model_providers.lan]
name = "LAN model"
base_url = "http://<host>:<port>/v1"
env_key = "LOCAL_LLM_KEY"
wire_api = "responses"
```

The server must serve the Responses API (`POST /v1/responses`). The key lives in the user environment variable
`LOCAL_LLM_KEY`; jbr never reads or sends it. codex's base prompt is then ~9k tokens. Probe and run both use the
codex path (no direct HTTP call), so a server that is down, or a missing key, probes UNAVAILABLE and the chain skips it.

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

Output `routing.json` (in the runs dir; `jbr paths` prints it): `{"model": "jev-1.13.0", "state": {...}, "routing": {"WP04": {"engine": "agy_gemini_pro", "confidence": 0.62, "probabilities": {...}, "chain": ["agy_gemini_pro", "agy_claude_opus", "codex_astra", "claude_subagent"]}}, "chains": {"WP04": [...]}, "availability": {...}}`.

**Jev follows the chain.** `route` first probes every engine (`--dry-run` / `--no-probe`: state file only, no live probe). Jev's request state gets every engine's `priority` and `live_status` (`engine_chain`, plus the same in each engine description) and a `routing_policy`: prefer the highest-priority available engine unless the package's routing hints (numerical difficulty, state consistency, size) clearly favour another available one. EXHAUSTED / UNAVAILABLE engines are never answer criteria; if an answer names one anyway it is replaced by the highest-priority usable engine (`overridden` in the row). Each package's `chain` is Jev's pick, then the other usable engines by priority, ending in `claude_subagent` (a `claude_subagent` pick is the whole chain). Pass `.chains` straight to `build_dag.js` as `routing`. `router.load_chains()` reads them back (pre-chain files: `[engine]`).

## Run artifacts (`~/.jbr/runs/<project key>/reports/`)

`run-<WP>[-tag].prompt.md` (self-contained prompt), `.log` (engine transcript), `.done` (`"<exit> <minutes>min <STATUS> [reset-hint]"`, STATUS = `OK` | `QUOTA` | `CRASH`, e.g. `3 0.0min QUOTA 2h9m31s`; `-1 <minutes>min` without a status token when `jbr run` itself crashed, traceback appended to `.log`; `wait`/`status` print the status, `runner.engine_status()` reads old and new markers), `.stdout` (detached wrapper), `.last.md` (codex final message), `.handoff` (manual engines: `{engine, started, pointer}` from `jbr handoff`). `spawn` exits 2 before launching anything when the engine key is not in `engines.json`. `compose-fix` writes `review-<WP>-round<N>.json` and `fix-<WP>-round<N>.md`.

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
  routing: { WP04: ["agy_claude_opus", "codex_astra", "claude_subagent"],   // routing.json .chains (list)
             WP05: "codex_astra",                  // string: pin, then the engineChain entries after it
             WP06: "auto" },                       // or omit the package: walk the whole engineChain (DEFAULT)
  engineChain: ["codex_astra", "ollama_qwen", "lan_qwen", "claude_subagent"],  // the default (no agy since 2026-10-24)
  engineArgs: { agy_gemini_pro: "--timeout 55m" },   // optional extra `jbr spawn` args per engine
  jbrArgs: "--engines /c/path/engines.json",          // optional global jbr options for probe + spawn
  fallbackEngine: "claude_subagent",     // optional; appended to every chain as the last resort
  maxFixRounds: 2,
  testCmd: "python -m pytest -q",
  promptsDir: "ops/prompts", reportsDir: "<jbr paths .reports_dir>",   // reportsDir required with external engines
  sourceHint: "src layout under src/pkg",  // optional one-liner in the engineer rules
  rules: "...",                             // optional: replace the COMMON rules text entirely
  preReview: false                          // optional: true | {rounds: 'first'|'all', effort, budgetMin}; see "Pre-review"
}
```

Each package starts when its deps finish. Every implement/fix step walks the package's chain from the top. `claude_subagent` gets an implementer agent (effort high); an external engine gets a low-effort runner agent that **first runs `python -m jbr probe <engine>`**: EXHAUSTED/UNAVAILABLE -> it returns `engine_status: 'quota'` (with the probe line in `probe`) without spawning, and the step moves to the next engine; AVAILABLE -> it writes the fix file (fix steps), executes `python -m jbr spawn`, polls the `.done` marker and reports `engine_status` (`ok|quota|crash`). A `quota`/`crash` step is never reviewed and never consumes a fix round. **There is no run-wide skip list**: the old permanent in-run EXHAUSTED set is gone; every step asks `jbr probe` again, and the probe answers from the shared state file (instant while `exhausted_until` is in the future, live once it has passed), so an engine whose quota resets mid-run is preferred again by the next step. If every engine in the chain is dead the package ends `final: "blocked"` (no review of the dead run; `blocked: {stage, tried}`). Without `args.jbr`, auto chains are reduced to `claude_subagent` (logged); an explicit external routing entry without `jbr` is an error. Review/verify/fix-review stay Claude subagents. Returns one row per package: `{wp, engine, chain, final, rounds, history, unresolved, low_open, mutation, ownership[, blocked]}`; `history` stages carry `engines: [{engine, status, reset_hint[, probe]}]`.

**`workflows/review_level.js`**

```js
{ root: "C:\\path\\to\\project", ids: ["WP01", "WP02"], testCmd: "python -m pytest -q", logPrefix: "run",
  engineStatus: { WP02: "QUOTA" },    // optional, from `jbr wait`/`status`: quota/crash packages are not reviewed
  jbr: "/c/Users/you/dev/jev-build-router", preReview: false }   // optional pre-review (needs jbr + reportsDir)
```

Returns `[{wp, verdict, confirmed_failures, refuted_findings, low_findings, pytest_summary, test_count, ownership_violations, mutation_check, passed_count[, prereview]}]`; save it as json and feed to `python -m jbr compose-fix <file> <round>`. Rows with `verdict: "blocked"` (engine did no work) get no fix file: re-run that package on another engine for the same round.

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

Router tests monkeypatch `urllib.request.urlopen` (no real TypeSafe calls); runner and probe tests use `--dry-run` / a fake `subprocess.run` (agy/codex never executed); every test gets its own `JBR_STATE` (autouse fixture in `tests/conftest.py`), so `~/.jbr` is never touched. `tests/test_prereview.py` drives `jbr prereview` through a fake `run` (ocr and git never executed). Workflow tests (`tests/test_workflows.py`, `tests/test_quota.py`, `tests/test_chain.py`, `tests/test_prereview.py`) run the scripts under node via `tests/workflow_harness.js` with stubbed agents and are skipped when node is missing.
