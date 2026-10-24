---
name: build-router
description: "Use when a project has work packages to build (ops/work_packages.json + ops/prompts/WPxx.md) and they must be handed to engines (Claude Code subagent, agy on Gemini/Claude, Codex, local Qwen) and driven through fresh-context review, adversarial refutation and fix rounds. YOU (the main session) assign every package from the specs, the code and the engine scorecard; a decision model (local Clef / Jev) is only an optional second opinion. Wraps the jev-build-router repo: assign/record/scorecard, headless agy/codex driver, build_dag and review_level Workflow scripts."
---

# /build-router

Repo, per machine (pure Python 3.12 stdlib; no install needed, run with `PYTHONPATH`):

- Windows laptop: `C:\Users\a8878\dev\jev-build-router` (POSIX `/c/Users/a8878/dev/jev-build-router`)
- Mac: `/Users/ad/code/jev-build-router` (use the same path for `jbr`, `root` and `scriptPath`; `python` must be 3.12, not Apple's `/usr/bin/python3` 3.9)

The Windows paths below are examples; substitute the current machine's.

```
JBR=/c/Users/a8878/dev/jev-build-router   # Mac: /Users/ad/code/jev-build-router
cd <project> && PYTHONPATH="$JBR" python -m jbr --help
```

## Default engine chain (user rule 2026-10-02; local Qwen added 2026-10-10; automated agy OFF 2026-10-24)

**User rule 2026-10-24: automated agy is off.** `agy_gemini_pro` / `agy_claude_opus` ship `available: false` and are
not in build_dag's default chain (Antigravity terms: no wrapping agy in third-party agents on consumer plans).
Use `agy_manual` for agy work (below). Do not re-enable them with `--available ...=yes` unless the user asks.
The default chain is now codex_astra > ollama_qwen > lan_qwen > claude_subagent. The list below is the old
order, kept for when the user explicitly opts back in:

1. `agy_gemini_pro` (agy, `auto:gemini`: newest Gemini that `agy models` lists, e.g. gemini-4-argon-high on 2026-10-16)
2. `agy_claude_opus` (agy, `auto:claude-opus`, e.g. claude-opus-5-5-high; shares ONE Claude quota bucket with any other agy Claude model, e.g. Sonnet)
3. `codex_astra` (codex, gpt-6-astra; flags `-s workspace-write`, there is no `--full-auto`)
4. `ollama_qwen` (local Qwen 3.8 27B on Ollama, driven by codex `--oss --local-provider ollama`; free, never quota-limited)
4. `lan_qwen` (Qwen on a LAN OpenAI-compatible server, codex with `CODEX_HOME=~/.codex-local`; free, never quota-limited, 2-bit/32K so small packages only; same priority as `ollama_qwen`, after it)
5. `claude_subagent` (Claude Code workflow subagent; last resort, never quota-probed)

**Local Qwen saves Claude quota (user rule, 2026-10-10).** It is in every default chain before the Claude subagent, so a package whose cloud engines are all out of quota goes to Qwen first. It exists only where Ollama runs (the Mac): elsewhere its probe fails and the chain skips it. It is slow (codex sends ~140k tokens of context: several minutes even for a trivial package; build_dag runners wait up to ~140 min for it instead of ~70) and weaker than the cloud engines on multi-file work. Assign it directly for small, fully specified, low-risk packages. Its probe asks Ollama directly (`ollama run --think=false <m> "Reply only READY."`), seconds, not codex. Do not run the local Clef 27B server while Qwen is building: both are ~17 GB.

Router decisions may use Clef: set `JBR_DECISION_URL=http://127.0.0.1:8000/v1/systemone` and `route --model clef-flash-4bit` for a local server, with no key. Hosted Workers AI requires `CLOUDFLARE_AI_TOKEN` and a Workers AI endpoint in `JBR_DECISION_URL`; an arbitrary HTTPS System One endpoint requires `JBR_DECISION_TOKEN`. The default Jev endpoint alone reads `TYPESAFE_API_KEY`.

An engine out of quota is skipped until its reset time, then preferred again automatically. Availability is live, not configured: `engines.json` has `priority` 1..5 and every engine `available: true` except the two automated agy engines; the shared state file `~/.jbr/engine_state.json` (override `JBR_STATE`) records `exhausted_until` per quota bucket.

```
PYTHONPATH="$JBR" python -m jbr probe agy_gemini_pro   # AVAILABLE (exit 0) | EXHAUSTED <utc-until> (3) | UNAVAILABLE <reason> (4)
PYTHONPATH="$JBR" python -m jbr pick                   # first available engine by priority (claude_subagent if none)
PYTHONPATH="$JBR" python -m jbr pick --exclude agy_gemini_pro,codex_astra
```

`probe` never calls the engine while `exhausted_until` is in the future, caches AVAILABLE for 10 min, and otherwise runs one cheap live probe (`agy --model <m> --print-timeout 45s --print "Reply only READY."` / `codex exec --skip-git-repo-check -m <m> -s workspace-write "Reply only READY. Do not run tools."`, 120 s timeout). Quota signature -> EXHAUSTED until the parsed reset hint (`2h9m31s`, `try again at 3:51 AM`, `try again at Sep 25th, 2026 3:49 AM`; unparseable -> +30 min); any other failure -> UNAVAILABLE and skipped 15 min. A real `jbr run` that ends QUOTA writes the same state, so the next probe already knows.

## Manual agy (`agy_manual`, 2026-10-24)

Google's Antigravity FAQ does not support wrapping `agy` inside third-party coding agents on consumer plans
(account suspension risk), and the automatic `agy_*` engines do exactly that. When the user wants agy without
that risk, assign `WP=agy_manual`. Do NOT run agy for them: `jbr handoff <WP>` writes the packet and prints
the line they paste into an agy session they start themselves; they run `jbr done <WP> [--notes-file ...]`
when it finishes. Review with `review_level.js` ONLY when `done` recorded OK, then `compose-fix`. For
CRASH/QUOTA there is no engine work: hand it off again (same round), or pass `engineStatus: {WP: 'quota'}`. A fix round is
`handoff <WP> --tag fix<N> --extra-file <fix file>`, then `done <WP> --tag fix<N>`, then review with `logTag`.
Several at once: `jbr handoff --pending` writes one checklist, ordered by dependencies, covering every
outstanding manual package and open fix round. The user pastes its pointer line into one agy session they
start. agy prints a `=== REPORT <id> ===` block per package, and the user saves the output and runs
`jbr done --from <file>`. Offer this when two or more packages are assigned to `agy_manual`. Never automate
the pickup: no timer, loop or watcher that starts agy on the checklist.
In `build_dag.js` a manual-pinned package returns `final: 'manual'` (with its `handoff` command) and its
dependents return `'waiting'`: tell the user which packets to run, then stop. Do not poll, and do not
spawn agy "to help".

**You do the agy bookkeeping; the user only starts agy (user rule, 2026-10-24).** In every project session:
1. When you assign (step 2 below), give `agy_manual` the packages that suit it (rubric below). Do it without
   being asked: agy work is free diversity the user wants used.
2. Right after `assign`, if anything went to `agy_manual`: run `jbr --project . [--packages/--prompts ...]
   handoff --pending` (add `--done <ids>` for already-built dependencies a Claude subagent built). Give the user
   the one paste line and the NOT YET list. Then carry on building the rest with `build_dag.js`. Never wait for agy.
3. The user sees every project's waiting checklist with `python -m jbr inbox`, and pastes each line into an agy
   session they start in that project.
4. When the user says agy finished (or a saved agy output file appears where they said it would): run
   `done --from <file>`, review each OK package with `review_level.js` and the `logTag` that `done` prints,
   then `compose-fix`. Hand fixes back with `handoff <WP> --tag fix<N> --extra-file <fix file>`, then
   `handoff --pending` again, so the next agy session gets the fixes and anything newly unblocked.

## Who decides (user rule, 2026-10-10)

**The commander assigns, not a decision model.** You wrote the specs, read the code and can see what
each engine actually delivered; a System One model sees one line per engine and a title per package.
On 2026-10-06 local Clef-flash put two packages on Claude subagents against the chain order; the owner
overrode one to Codex and it was accepted after one fix round. A decision model is the right tool for
thousands of cheap inline calls (the map app's caption rungs), not for four calls that each start an
hour of engine work.

Decide from, in this order:
1. **Live availability**: `jbr probe` / `pick` (code, not judgment). Never assign a dead engine; `assign` refuses it.
2. **The scorecard**: `PYTHONPATH="$JBR" python -m jbr scorecard` (per engine: packages, accepted, fix rounds,
   confirmed defects, minutes, quota hits; ledger `~/.jbr/ledger.jsonl`). Prefer the engine with the
   lowest defects/fix rounds on packages of the same kind and size.
3. **The rubric**:
   - default chain order Codex > local Qwen > Claude subagent (spend Codex/Qwen before Claude's quota); agy only as `agy_manual`, which the user runs;
   - review diversity: the reviewers are Claude, so a non-Claude implementer gets its mistakes caught rather than shared;
     prefer Gemini or Codex for subtle state/billing/security code;
   - legal, privacy or user-facing wording: Claude subagent (and say why);
   - small, fully specified, low-risk packages (a doc edit, a config line, one function with its tests): assign `ollama_qwen` outright; it is free;
   - `muse_spark` (Meta Muse Code, assign only): never by default. Assign it only when the user agreed for
     this project, since the contributor tier lets Meta train on the package's code; never for private repos.
     It needs billing at dev.meta.ai (a probe that says `Billing verification failed` means not set up).
     Quality is unproven, so record its builds (`jbr record`) to build a scorecard before relying on it;
   - `agy_manual` (Gemini via agy, run by the user in batches): a well-specified, self-contained package of
     medium size, where a non-Claude family helps review (it is what `agy_gemini_pro` used to get: ~6 min,
     usually one fix round for spec-literal misses). Do NOT give it a package on the critical path that other
     work needs today, since it waits for the user. Do not give it legal or user-facing wording, or anything a
     Claude subagent must do in the host session (GPU/Blender, local apps). Prefer leaves and late packages of
     the DAG, and keep a dependency chain together on agy so one session can do it in order;
   - keep packages that edit the same file on the same engine or in dependency order.
4. **Optional second opinion**: `--second-opinion` asks the decision model at `JBR_DECISION_URL` and records
   its pick beside yours (never applied). Use the **local Clef 27B** (free):
   `cd ~/code/clef-local && .venv/bin/python models/clef-4bit/clef_mlx.py serve --port 8001` then
   `export JBR_DECISION_URL=http://127.0.0.1:8001/v1/systemone`. It needs ~17 GB: do not run it while a
   build's agents are running (the 2026-10-06 server was OOM-killed). A strong disagreement is a prompt to
   re-read the package, not an override. Never pay for Jev/hosted Clef for routing.

## Handoff packet checklist (what made the 2026-10-06 packages pass in one fix round)

Worked example: `examples/2026-10-06-clef-backend/` in this repo (plan, four packets, who built what).

Every `ops/prompts/<WP>.md` (or the plan's `--prompts` dir) must have, before you assign:
- **Why**: two or three sentences of context the engine cannot infer.
- **Owned files**: the exact paths; everything else is read-only.
- **What to build**: names, signatures, env vars, defaults, error behaviour; "keep X unchanged" lines for invariants.
- **Acceptance items**: checkable statements, mirrored in `work_packages.json` `acceptance`.
- **Tests**: the exact command (project venv path), the checks to add, and **a bug to inject** that must turn a check red.
- **Out of scope**: neighbouring packages' files and decisions, so engines do not "helpfully" rewrite them.
- **Project traps** from CLAUDE.md that apply (env vars set to "" not popped, no real network in tests, i18n).

## Preconditions in the target project

- `ops/work_packages.json` (or a per-build file passed with `--packages`, e.g. `ops/builds/<name>/work_packages.json` + `--prompts ops/builds/<name>/prompts`, so an older plan is never overwritten): `{"work_packages": [{id, title, goal, depends_on, files, acceptance, tests, spec_refs, routing_hints?}]}`. Optional top-level `rules` (prepended to the built-in ownership / no-commit / no-files-outside-repo rules) and `goal`.
- `ops/prompts/<WP>.md`: the handoff packet above.

## Steps

1. **Check the engines.** `PYTHONPATH="$JBR" python -m jbr probe <engine>` for each. Force one off for a call with `--available codex_astra=no`; do not edit `engines.json` for a one-off. agy engines use `auto:<family>` models (resolved per probe/run from `agy models`, cached 1 h; see README "agy model names"), so a retired agy id needs no edit. A probe failure "cannot resolve model" means agy lists nothing of that family (not logged in, or the family is gone): run `agy models`. A codex probe failure whose detail is a model list means codex renamed the model: fix `engines.json`.
2. **Assign.** `PYTHONPATH="$JBR" python -m jbr --project . [--packages ...] assign WP01=agy_manual WP02=codex_astra --reason "WP02=billing path, non-Claude family for review" [--second-opinion]`. Anything on `agy_manual`: run `handoff --pending` next (see "Manual agy"). Probes live, refuses unknown/exhausted engines, writes `routing.json` in the runs dir (`model: "commander"`, per package `engine`, `reason`, `chain` = your pick then the other usable engines by priority, ending in `claude_subagent`; `second_opinion` when asked). **Show the user the table with your reasons before building.** (`jbr route` still exists: the decision model routes everything itself. Use it only when the user asks for it.)
3. **Build with the DAG workflow.** Call the Claude Code Workflow tool with `scriptPath = C:\Users\a8878\dev\jev-build-router\workflows\build_dag.js` and
   ```
   args = { root: "<project root>", jbr: "/c/Users/a8878/dev/jev-build-router",
            reportsDir: "<jbr paths .reports_dir>",  // REQUIRED with external engines; jbr writes nothing into the project
            packages: [{id, depends_on}, ...],       // from work_packages.json
            done: [...],                              // already accepted packages
            routing: <routing.json .chains>,          // `jbr paths` .routing; per package: [chain] | "engine" (pin) | "auto"
            promptsDir: "<plan prompts dir>",         // absolute is fine (plans may live outside the repo)
            maxFixRounds: 2, testCmd: "python -m pytest -q" }
   ```
   Routing values: a list is the package's chain; a string pins that engine first and falls back along `engineChain` after it; `"auto"` or no entry walks the whole `engineChain` (default `['codex_astra','ollama_qwen','lan_qwen','claude_subagent']`; a pin on `agy_manual` stops at the handoff). For each external engine the low-effort runner agent first runs `jbr probe`; EXHAUSTED/UNAVAILABLE -> reported as `quota` without spawning and the step moves to the next engine. There is no run-wide skip list: every step probes again, so a recovered engine is used again later in the same run. Quota/crash runs are never reviewed and never consume a fix round; review/verify/fix stay Claude subagents. Without `jbr`, auto chains reduce to `claude_subagent`.
4. **Review an already-built level separately** (e.g. packages built outside the workflow): Workflow tool with `scriptPath = ...\workflows\review_level.js`, `args = { root, ids: ["WP01", "WP02"], reportsDir: <jbr paths .reports_dir> }`. Save the workflow output to a json file, then `PYTHONPATH="$JBR" python -m jbr compose-fix <output.json> <round>` writes `<reports_dir>/fix-<WP>-round<N>.md`; re-run on `$(python -m jbr pick)` with `PYTHONPATH="$JBR" python -m jbr spawn WP --engine <engine> --tag fix1 --extra-file <reports_dir>/fix-WP-round1.md`.
5. **Record.** After every build, save the Workflow's returned list (the per-package `[{wp, engine, final, history, low_open, ...}]`) to a JSON file and run `PYTHONPATH="$JBR" python -m jbr [--packages ...] record <result.json> --build <name>`. It appends one ledger line per package (implementer = the engine whose run succeeded, fix rounds, confirmed defects, quota hits, minutes from the run markers). Packages finished outside the workflow and never reviewed are not recorded: they would skew the scorecard.
6. **Monitor.** `PYTHONPATH="$JBR" python -m jbr status` (routing + `.done` markers), `PYTHONPATH="$JBR" python -m jbr wait WP05 WP06 --timeout-min 70` (exit 124 while a marker is still missing), `cat ~/.jbr/engine_state.json` (who is exhausted until when). Every `python -m jbr` call needs `PYTHONPATH="$JBR"` unless `pip install -e` was run.

## Optional pre-review: open-code-review on local Qwen (2026-10-18, opt-in)

`PYTHONPATH="$JBR" python -m jbr prereview <WP> [--tag r1] [--commit <sha>] [--dry-run]` runs alibaba/open-code-review
(`ocr`) on the package's changed owned files against local Ollama and writes `prereview-<WP>[-tag].json` in the
reports dir. It always exits 0: SKIPPED / FAILED / TIMEOUT are statuses, never build failures. In the workflows,
pass `preReview: true` (build_dag.js, review_level.js; both need `jbr` and `reportsDir`). Its findings become step 0
of the Claude reviewer's prompt as unverified hints; confirmed ones are refuted like any other failure.
- Local only: a non-loopback `--url` is refused without `--allow-remote`. **Never pass `--allow-remote` without the
  owner's OK in chat**: repository content (not only the diff) would leave the machine.
- Run the `system-check` skill before a build that uses it: it loads the same Qwen as `ollama_qwen` (its KV cache
  grows well past the 18 GB of weights on a long review). One machine-wide lock serializes prereviews (Ollama serves
  one request at a time); a package that waits 5 min for it is reviewed without hints (`busy`).
- It does not read test files and does not check acceptance criteria. It never replaces the review. See
  `docs/OPEN_CODE_REVIEW_PROPOSAL.md` for the spike numbers before you turn it on for a build.

## Qwen backlog lane (user rule, 2026-10-10): non-urgent or easy work, unattended

Local Qwen is free but slow, and this Mac serves one Ollama request at a time (two concurrent jobs: 72 s,
the same as back to back), so the "swarm" is a backlog worked off one package at a time, overnight or
while the user works on other things. Put a package in the lane when it is **not urgent** or **easy**
(small, fully specified, clear acceptance, no billing/security/legal surface). Urgent or risky work stays
in the normal build.

```
PYTHONPATH="$JBR" python -m jbr --project <root> --packages <plan>/work_packages.json --prompts <plan>/prompts \
    queue add WP07 WP08 [--base origin/main] [--test-cmd "..."]    # default test cmd: the package's `tests`
PYTHONPATH="$JBR" python -m jbr --project <root> queue work --detach                 # one worker; log in the runs dir
PYTHONPATH="$JBR" python -m jbr --project <root> queue status
```

- Each package runs in its own git worktree under the runs dir, on branch `jbr/ollama_qwen/<WP>-<stamp>`
  from the pinned base; `.venv`/`node_modules` are symlinked in. The worker never commits or pushes, and
  stops (leaving the rest pending) when Qwen's probe fails. One worker per project (pid lock).
- **Review in a batch** when the user is back: `queue review-args` prints one `review_level.js` args object per
  finished package (root = its worktree, `logTag` = its run). Run the review per package. Accept: commit in
  the worktree, push the branch, open a PR, merge as usual, then `queue clean WP` (refuses uncommitted or
  unmerged work unless `--force`). Fix: `compose-fix` the review output, then `queue fix WP <fix file>` puts it
  back in the queue in the same worktree.
- After each batch, `record` the reviewed results like a build so the scorecard shows what Qwen handles well.

## Known engine pitfalls (see README "Known pitfalls")

- agy needs `--dangerously-skip-permissions` in headless mode (runner adds it); Claude thinking models reject `--effort` (runner omits it for `claude*` models).
- codex command shape is fixed in `jbr/runner.py::build_command` (`-s workspace-write`; `--full-auto` does not exist in this codex and exits immediately); `-o` captures the final message.
- Prompts are handed over as a file pointer (Windows 32K command-line cap).
- Quota exhausted: no output, or exit 124 from `--print-timeout`, or instant exit 3 with `RESOURCE_EXHAUSTED`/429, or codex `You've hit your usage limit ... try again at 3:51 AM`. The `.done` marker says `QUOTA <reset-hint>`, the state file gets `exhausted_until`, and `build_dag.js` moves the step down the chain; packages where every engine is dead end `blocked`. Manual flow: pass `engineStatus` to `review_level.js` so dead runs are not reviewed.
- agy Opus and agy Sonnet drain the same Claude quota: one exhausts both (state key `agy_claude`).

## Do not

- Do not edit the target project's files from this skill. jbr writes only under `~/.jbr` (`jbr paths`: runs dir with reports, routing.json, queue and worktrees; ledger; engine state), never into the project tree.
- Do not put build-router material (plans, prompts, routing records) in the project's main branch: keep plans outside the repo or in jev-build-router `examples/` (user rule, 2026-10-10).
- Do not call the TypeSafe API when the user only wants to see the plan: use `route --dry-run`.
