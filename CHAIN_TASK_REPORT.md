# CHAIN_TASK report: engine priority chain with self-adjusting availability

Worktree `C:/Users/a8878/dev/jbr-codex-chain`, branch `codex/engine-chain`. Not committed.
Implemented by Claude (Opus) after the Codex attempt hit its usage limit (`CHAIN_TASK.codex.log`)
and the agy Sonnet attempt was quota-cut (`jev-build-router/ops/partial-sonnet/`, used as reference only).

## Result

`python -m pytest -q`: **113 passed** (70 pre-existing + 43 new in `tests/test_chain.py`).
The workflow-script tests (node + `tests/workflow_harness.js`) are part of that run: `tests/test_workflows.py` 6 passed,
build_dag/review_level cases in `tests/test_quota.py` and `tests/test_chain.py` all pass. `node --check` is clean for both workflows.
No test touches the network, a real engine or `~/.jbr` (autouse `JBR_STATE` fixture; probes use a fake `run`; TypeSafe via mocked `urlopen`).

## Changed files

| file | change |
|---|---|
| `engines.json` | `priority` 1..4 (gemini 1, opus 2, codex 3, claude_subagent 4); all four `available: true`; descriptions note the shared Claude bucket and the last-resort role. Key order unchanged. |
| `jbr/state.py` (new) | state file, reset-hint parsing, locking, live probe, `pick`, `availability` |
| `jbr/runner.py` | `run()` writes `exhausted_until` on a QUOTA run, AVAILABLE on an OK run (`_record_engine_state`; errors logged to the run's `.log`, never fail the run). `build_command` for codex already used `-s workspace-write` and no `--full-auto`; left as is (asserted by a test). |
| `jbr/cli.py` | new `probe <engine> [--no-live]` and `pick [--exclude a,b] [--no-live]`; `route` probes every engine and passes availability to the router (`--no-probe`; `--dry-run` never probes live); stderr line per unusable engine. |
| `jbr/router.py` | `by_priority`, `usable_engines`, `chain_for`, `live_status`, `load_chains`; `build_request`/`route` take `availability`; Jev state gets `routing_policy` + `engine_chain` (priority, live_status) and per-engine priority/live status; unusable engines never criteria; Jev answer naming an unusable engine is overridden; routing.json gets `routing[wp].chain`, top-level `chains`, `availability`; `format_routing` prints the chain. |
| `workflows/build_dag.js` | engine chain (see below); `engineChain`, `jbrArgs` args; routing value string / list / `'auto'` / missing; run-wide `EXHAUSTED` set removed; runner prompt step 0 = `jbr probe`; fix file written by the runner after the probe (no separate `fixfile:` agent); fallback tags `fb-<engine>`; rows carry `chain`. |
| `tests/test_chain.py` (new) | 43 tests, listed below |
| `tests/conftest.py` | autouse `isolated_engine_state` (per-test `JBR_STATE`); `engines` fixture keeps the pre-chain availability (opus/codex off) so the old routing assertions hold. |
| `tests/test_router.py`, `tests/test_cli.py` | one override each (`agy_claude_opus`/`codex_astra` off) because engines.json now marks them available; assertions unchanged. |
| `tests/test_quota.py` | `EXT` args pin `engineChain: ['agy_gemini_pro']` (the pre-chain 2-engine shape) so the existing fallback/blocked assertions hold unchanged; `test_build_dag_quota_is_sticky_across_packages_but_crash_is_not` replaced by `test_build_dag_quota_is_not_a_run_wide_skip_the_probe_decides`, because the task removes exactly that sticky behaviour. |
| `README.md`, `skill/SKILL.md` | chain, state file, probe/pick, Jev-follows-chain, build_dag args, codex flag pitfall |
| `C:/Users/a8878/.claude/skills/build-router/SKILL.md` | = `skill/SKILL.md` (backup `SKILL.md.bak-20261002`) |
| `C:/Users/a8878/.agents/skills/build-router/SKILL.md` | same content, keeping that copy's own description line and "Codex Workflow tool" wording (backup `SKILL.md.bak-20261002`) |

## Design decisions

**State file** (`$JBR_STATE`, default `~/.jbr/engine_state.json`): one record per quota *bucket*
`{exhausted_until, last_probe, last_status, detail, engines}`, ISO UTC with `Z`. Every agy engine on a Claude
model maps to bucket `agy_claude` (Opus and Sonnet share one quota); every other engine is its own bucket.
Writes: lock file via `os.open(O_CREAT|O_EXCL)` (stale after 60 s, broken automatically), read-modify-write
under the lock, temp file in the same dir + `fsync` + `os.replace`; `PermissionError` (Windows replace/read
racing an open handle) is retried. Verified with two real processes doing 40 locked increments each: no lost
update, no leftover temp/lock files.

**Reset hints** (`state.parse_reset_hint`): durations (`2h9m31s`, `27m28s`, `Resets in 4h53m49s`, `in 5 minutes`)
are relative to now; `try again at 3:51 AM` is the next occurrence in the machine's local zone (the CLIs print
local time); `try again at Sep 25th, 2026 3:49 AM` is that local date-time; the space-stripped forms written into
`.done` markers (`3:51AM`, `Sep25th,20263:49AM`) parse identically. Unparseable -> now + 30 min.

**probe**: claude_subagent -> AVAILABLE, never probed, no state write. `available: false` (or `--available x=no`)
-> UNAVAILABLE, no call. `exhausted_until` in the future -> `EXHAUSTED <until>` exit 3, no call (a backoff that
came from an UNAVAILABLE probe also prints EXHAUSTED, with `(after UNAVAILABLE: ...)` appended, per the spec's
"exhausted_until in the future -> EXHAUSTED"). AVAILABLE within 10 min -> AVAILABLE, no call. Otherwise one live
probe (120 s timeout) under a per-bucket probe lock that re-checks the file after acquiring it, so parallel runner
agents trigger a single live call. Classification: quota signature (`runner.QUOTA_RE`) first -> EXHAUSTED from the
hint; a line that is just `READY` -> AVAILABLE (codex echoes the prompt "Reply only READY. ..." in its transcript,
so a substring match would be wrong); timeout / missing binary / anything else -> UNAVAILABLE, backed off 15 min.
Binaries: `$JBR_AGY_BIN` / `$JBR_CODEX_BIN`, else PATH, else `%LOCALAPPDATA%\agy\bin\agy.exe`.

**pick**: engines by `priority` (ties: engines.json order), probing each until one is usable; `claude_subagent` if none.

**How build_dag.js consults `jbr probe` so a recovered engine is preferred again within a run.**
The old run-wide `EXHAUSTED` Set (once quota, skipped for the rest of the run) is removed. Each package has a chain
computed at start (`routing` list as given; string = pin + the `engineChain` entries after it; `'auto'` or no entry =
the whole `engineChain`, default `agy_gemini_pro > agy_claude_opus > codex_astra > claude_subagent`; `fallbackEngine`
appended last). Every implement/fix step walks that chain **from the top**. For an external engine the low-effort
runner agent's first command is `python -m jbr probe <engine>`; exit 3/4 -> it returns `engine_status 'quota'` with
the probe line in `probe` without spawning, and the step moves to the next engine. Because the probe answers from
the shared state file, a known-exhausted engine costs one cheap agent turn and no engine call; once
`exhausted_until` has passed, the same probe goes live, and on READY the engine is AVAILABLE again, so the very next
step (any package, same run) uses it. The quota information itself comes from three sources that all land in the
same file: live probes, `jbr run` QUOTA classifications (written by `runner.run`), and other concurrent jbr
processes. Quota/crash steps are still never reviewed and never consume a fix round; review/verify stay Claude
subagents. Without `args.jbr`, auto chains are reduced to `claude_subagent` (logged); an explicit external routing
entry without `jbr` still throws.

**Jev keeps the assignment role (addendum).** `jbr route` probes every engine first (`--dry-run`/`--no-probe`: file
only). The request state carries `engine_chain` (engine, priority, live_status), each engine description gains
`chain priority N` and `live_status`, and `routing_policy` + the question text ask Jev to prefer the highest-priority
available engine unless the package's hints (numerical difficulty, state consistency, size) clearly favour another
available one. EXHAUSTED/UNAVAILABLE engines are not answer criteria; an answer naming one anyway is replaced by the
highest-priority usable engine (`overridden`). Per package chain = Jev's pick, then the other usable engines by
priority, ending in `claude_subagent` (a `claude_subagent` pick is the whole chain: it never runs out). Written as
`routing[wp].chain` and top-level `chains`, which is passed to build_dag.js as `routing` unchanged.
`load_routing()` still returns `{wp: engine}`, so `jbr run/spawn` without `--engine` keep working.

## New tests (`tests/test_chain.py`)

- reset hints: 12 parametrized cases (4 required formats, spaced and space-stripped, `Resets in`, words, unparseable, empty, None) + round trip from real agy/codex transcripts through `classify_run`
- probe: exact agy/codex argv (no `--full-auto`); READY cached 10 min then live again; codex quota -> EXHAUSTED until 19:51Z, no call inside window, live + AVAILABLE after reset; echoed prompt is not READY; other failure -> UNAVAILABLE 15 min then recovery; timeout / missing binary; claude_subagent never probed; Opus/Sonnet shared bucket; `--no-live` -> UNKNOWN
- CLI: `probe` exit codes 0/3/4 and no engine call while exhausted; `pick` output and `--exclude`
- pick: priority order, fall-through to codex, cache reuse, all out -> claude_subagent; exclude
- state file: two processes x 40 locked updates; stale lock broken
- runner: QUOTA run writes `exhausted_until` (~2h9m31s), next probe answers from the file; agy Opus QUOTA marks `agy_claude`; OK run marks AVAILABLE; state error never fails a run
- routing (mocked TypeSafe): priority + live status in state, exhausted engine absent from criteria, policy in questions, chains in routing.json; unusable answer overridden; all external out -> claude without API; CLI `route` with mocked live probes end to end; `--dry-run` makes no probe
- build_dag (node, stubbed agents): auto is default and walks gemini -> opus -> codex; probe precedes spawn and no sticky set exists; recovered engine reused in the same run; fix round restarts at the top of the chain and has no `fixfile:` agent; string / list / auto routing; no-jbr behaviour; whole chain dead -> blocked, not reviewed

## Notes / not done

- One unintended live call happened during a manual CLI smoke test (with `JBR_STATE` pointing at a scratch file, not
  `~/.jbr`): `jbr probe agy_claude_opus` ran a real agy probe. It returned the Claude quota error
  (`Resets in 4h41m3s`) and was classified `EXHAUSTED` correctly, which confirms the live path against the real agy
  binary. No live codex or Gemini probe was run.
- The installed skills point at `C:\Users\a8878\dev\jev-build-router`, which does not have these changes until this
  branch is merged there; the docs describe the new commands already.
- `review_level.js` is unchanged (it reviews already-built packages; the chain does not apply).
