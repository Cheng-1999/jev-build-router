# open-code-review as a cheap first-pass reviewer (proposal, 2026-10-18)

Question: should [alibaba/open-code-review](https://github.com/alibaba/open-code-review) (`ocr`) run ahead of the
Claude review -> refute -> fix rounds, so that cheap deterministic findings land first and the Claude reviewers
spend their effort on what it misses?

**Recommendation: merge the stage as opt-in (default off); do not turn it on for builds yet.** On one real
package diff (~1,100 lines), local Qwen 27B behind ocr took **30-70 min** per review. Across two complete runs its
16 comments held **no functional bug**: 4 real stale-documentation defects, 5 valid maintainability points,
1 plausible design issue, 1 artifact of my snapshot, and **5 false positives**. The false ones were its
highest-severity comments: miscounts of a long literal list, presented as "this test will fail". In a clean
seeded run (C2) it **did catch a real defect class**: a test-gaming hack that Claude's round-2 review had caught,
flagged at high severity on the exact lines in 25 min. Its precision on this sample was far below the benchmark's
claim, and the wall-clock cost would serialize on one GPU across parallel packages. Use it
where time is free (the unattended Qwen backlog lane, out-of-band `review_level.js`). Decide on wider use from the
`prereview.confirmed` counts the workflows now record. Details below.

## What ocr is (checked on 2026-10-18, v1.13.1)

| | |
|---|---|
| Distribution | npm `@alibaba-group/open-code-review` is a node launcher, and the 55 MB Go binary comes from a per-platform optional dependency (`@alibaba-group/ocr-darwin-arm64`, ...). GitHub releases publish the same binaries with a `sha256sum.txt`; the darwin-arm64 binary in npm matched it. Apache-2.0. |
| Launcher side effect | Every 18 min the node launcher spawns `scripts/update.js`, which asks registry.npmjs.org for a newer version, unless `OCR_NO_UPDATE` is set. jbr sets it, and calling the binary (`opencodereview`) directly skips the launcher entirely. |
| Modes | `ocr review` (workspace = `git diff HEAD` + untracked, `--from/--to` merge-base range, `--commit`), `ocr scan` (whole files, no diff). `--format json` gives `{status, llm, summary{files_reviewed, comments, *_tokens, elapsed}, comments[{path, start_line, end_line, content, existing_code, suggestion_code, thinking}], warnings, session_id}`. Exit 1 only when the run is fatal (bad flags, no endpoint, every sub-agent failed). |
| What it skips | Test files are excluded by default (`exclude_reason: default_path`). Its agent may still read them for context. |
| LLM resolution | `--provider` picks a saved entry first. Without it the order is saved config, then complete `OCR_LLM_*` env, then complete Claude Code `ANTHROPIC_*` env, then shell rc files. A shell that exports `ANTHROPIC_*` would therefore send diffs to the cloud by default. |
| Telemetry | OpenTelemetry, off by default. Sessions and transcripts stay under `~/.opencodereview/`. |
| Requirements | Git >= 2.41 (this Mac: 2.54). The model needs native tool calling. |
| Claims not verified here | AACR-Bench (self-reported): higher precision and F1 than Claude Code on the same model at ~1/9 of the tokens, but lower recall. The star count is not used as evidence. |

## Design

### A pre-review stage, not an engines.json entry

`engines.json` is the implementer chain. `pick`, `route`, `assign`, the Jev/Clef questions, the fallback chains in
`build_dag.js` and the scorecard all iterate it and treat every key as something that can build a package. A
reviewer in that table would be pickable as an implementer, and every consumer would need an exclusion. Its failure
semantics also differ: an implementer failure walks the chain, while a pre-review failure must change nothing.

So it is a stage that reuses the engine plumbing instead of joining it:

- `jbr prereview <WP>` (`jbr/prereview.py`) calls ocr as an external process, the same way `jbr run` calls agy and codex.
- The model defaults to `ollama_qwen`'s, and that engine's `available` switch (and `--available ollama_qwen=no`)
  turns the pre-review off too. Readiness is checked with one GET of `<url>/models`, not with `jbr probe`. A
  probe records its result in the shared state file, and a probe timing out against a busy Ollama would mark
  `ollama_qwen` UNAVAILABLE for the implementer chain for 15 min. Off the Mac the GET fails fast and the
  pre-review is SKIPPED.
- `--detach` plus a `.done` marker (`runner.launch_detached`, factored out of `spawn`) gives the workflows the
  same poll loop the engine runner agents already use.
- Artifacts go in the reports dir (`~/.jbr/runs/<key>/reports/prereview-<WP>[-tag].*`). Nothing is written into the project.

```
implement (engine chain) ──> [preReview on] low-effort agent: jbr prereview WP --tag rN --detach, poll .done
                                  │  status ok + findings > 0          │ anything else
                                  ▼                                    ▼
                          Claude reviewer, step 0:              plain Claude review
                          triage prereview-WP-rN.json           (prompt identical to today)
                                  └──────────────┬─────────────────────┘
                                                 ▼
                     failures -> refute agents -> confirmed_failures -> compose-fix / fix round
```

### How its output maps into review/fix

1. ocr comment -> finding: `{path, start_line, end_line, content, existing_code, suggestion_code}`. `thinking`
   is dropped: it is long and is not evidence.
2. The reviewer's step 0 says: these are unverified hints. Check each one at the cited lines. Report a real one as
   a failure whose `criterion` starts with `pre-review: `, with your own severity and a `file:line` evidence.
   Drop the rest, then spend your effort on what ocr does not cover (acceptance criteria, tests, ownership, the
   mutation check).
3. Those failures go through the refute agents like any other failure, then into `confirmed_failures`,
   `review_output.json` and `compose-fix`. compose-fix and the fix prompts are unchanged.
4. History rows (`build_dag.js`) and result rows (`review_level.js`) get `prereview: {status, findings, confirmed}`,
   where `confirmed` counts the `pre-review:` failures that survived refutation. Over a few builds that measures
   real-world precision and yield per package, the number that decides whether to keep the stage.

Rejected alternatives:

- **ocr findings straight into compose-fix.** Unverified findings from a 27B model would turn every false positive
  into an engine fix round (the expensive step). Claude's refutation exists because reviewers hallucinate, and a
  cheaper reviewer needs it more, not less.
- **One refute agent per ocr finding, skipping the reviewer.** That costs N Claude agents for possibly noisy
  output. The reviewer is reading those files anyway, so triage there is nearly free.
- **ocr findings to an engine fix step before the Claude review.** Same objection as the first, with an extra
  engine round on top.

### Scope in a shared checkout

`build_dag.js` builds packages side by side in one checkout, so ocr's workspace mode would review every package's
in-flight work. `jbr prereview` reads the changed files (`git diff --name-only HEAD` plus untracked, or the
`--commit`'s files) and keeps the package's owned files (`files` globs from work_packages.json, with brace
expansion and `**`). Every other changed path goes to `--exclude`, so the review stays in diff mode and only the
package's lines get comments. The CLI `--exclude` does not use ocr's gitignore matcher. With `--preview` (no LLM
call) against v1.13.1, each pattern was matched as a glob against the whole repo-relative path:
- a leading or trailing `/` matches nothing;
- `[[]` and `[*]` escape, while `?`, `{` and `}` cannot be escaped.

So jbr emits full paths, or `dir/**` for the shallowest directory without an owned change, and turns unescapable
characters into `?`. It drops any pattern that would also match an owned file. The package spec (`ops/prompts/<WP>.md`) is passed as `--background-file`.
In the queue lane each package has its own worktree, so the workspace there is exactly the package.

### Failure and timeout: the build continues without it

| Condition | Status | Effect on the build |
|---|---|---|
| ocr not installed | SKIPPED | plain review |
| `<url>/models` unreachable or without the model (Ollama down, other machine), or `ollama_qwen` disabled | SKIPPED | plain review |
| no owned file changed | SKIPPED | plain review |
| non-loopback `--url` without `--allow-remote` | SKIPPED | plain review |
| ocr exit != 0, or no JSON | FAILED | plain review (findings kept in the json for the log only) |
| budget (`--budget-min`, default 45) spent, lock wait included | TIMEOUT, or OK with a partial result (ocr gets SIGTERM, then is killed) | plain review, or the partial findings |
| another prereview holds the machine lock for 5 min | SKIPPED busy | plain review |
| jbr itself throws | FAILED (traceback in `.log`) | plain review |
| the workflow agent crashes, or the marker never appears | treated as failed / timeout | plain review |

`jbr prereview` always exits 0 and always writes the `.done` marker (except on `--dry-run`), so a poller never
hangs. `preReview` defaults to off, and with it off the workflows' prompts and results are unchanged (tested).

### Backend: local Ollama Qwen, nothing leaves the machine

- Ollama 0.40.6 serves `qwen3.8:27b-mlx` (Qwen 3.5 architecture, nvfp4, 262k context, `tools` capability) on
  `http://127.0.0.1:11434/v1`. `ocr llm test` passes, including its tool-call round trip.
- Thinking is off (`extra_body: {"reasoning_effort": "none"}`): this is meant to be a fast first pass.
- clef-local serves `/v1/systemone`, a decision endpoint, not chat completions. It cannot back ocr.
- system-check before loading: GO for `ollama-qwen-27b` (needs ~18 GB; 48 GB GPU budget free, 36.9 GB
  reclaimable, 2.1 GB swap).
- Contention: it is the same model the `ollama_qwen` implementer uses, and Ollama serves one request at a time. A
  machine-wide lock (`~/.jbr/ocr-home/prereview.lock`, holding the owner's pid and deadline) runs one prereview at
  a time instead of letting parallel packages time out inside Ollama's queue. A package that waits 5 min for the
  lock gets `busy` and a plain review. The default `rounds: 'first'` keeps it to one pre-review per package, and the
  45 min default budget comes from the spike below.

Isolation, enforced in code and covered by tests:

- ocr runs with `HOME` (and `USERPROFILE` on Windows) set to jbr's own `~/.jbr/ocr-home`. jbr writes the
  `.opencodereview/config.json` there with one custom provider, `jbr-local`, and passes `--provider jbr-local`
  explicitly. The user's own ocr config, shell rc files and `ANTHROPIC_*`/`OPENAI_*`/`OCR_LLM_*` variables never
  reach it: the environment is an allow-list (PATH, locale, temp dirs, the Windows process basics).
- `router.is_local()` guards the URL: anything other than plain http to 127.0.0.1/localhost/::1 is refused
  unless `--allow-remote` is passed. The skill says never to pass it without the owner's OK in chat.
- Your global git ignore file, line-ending settings and `safe.directory` are passed through `GIT_CONFIG_*`, so
  ocr's git sees the same files as jbr's.
- `OCR_NO_UPDATE=1`, and telemetry stays at its default (off). The ocr home's sessions dir is cleared before
  each run, so no diff archive builds up. A `JBR_OCR_HOME` that points at the real home is refused.

## Spike (2026-10-18)

### Setup

- ocr v1.13.1 darwin-arm64 release binary (checksum verified), run exactly as `jbr prereview` runs it: isolated HOME,
  env allow-list, provider `jbr-local` on Ollama `qwen3.8:27b-mlx`, thinking off, 900 s request timeout,
  `--commit` mode.
- Target: one past jbr work package from a private TypeScript game project, a map set-dressing package (4 source
  files, 2 test files, +1094/-6 lines). The snapshot is the commit that captured it after its **second fix round**:
  the build marked it "rework pending". I rebuilt a commit with only the package's owned files on top of that
  commit's parent, in a scratch `git clone --shared`. The project itself was not touched, and none of its code
  is in this repo.
- Existing review output: the build's compose-fix files. Round 1 had 9 confirmed findings and round 2 had 7, each
  describing the code *before* that fix round. The round-3 review that left the package "rework pending" was
  not saved.
- Caveat: the snapshot lacked one same-commit change from an upstream package (a builder method), which produced one
  artifact finding (run B).

### Runs

| Run | Input | Effort | Time | LLM tokens in / out (from cache) | Tool calls (failed) | Comments |
|---|---|---|---|---|---|---|
| A | diff only | medium (2 rounds) | 32 min | 4.16M / 15.9k (93%) | 157 (9) | 5 |
| B | diff + package spec (`--background-file`) | low (1 round) | 30 min | 3.72M / 21.9k (94%) | 104 (4) | 11 |
| C | B's tree + one re-injected round-2 defect; commit message named it | low | 71 min, then stopped by SIGTERM | 1.99M / 13.5k (85%) | 74 (3) | 4 (partial) |
| C2 | C's tree, neutral commit message, run through `jbr prereview` | low | 25 min | 2.62M / 18.5k (93%) | 101 (5) | 8 |

Run A's wall clock in my log reads 117 min because the Mac idle-slept for 86 min mid-run (pmset log). ocr's own
elapsed time (monotonic clock) is 32 min. Times are for two file groups, reviewed one after another (Ollama serves
one request at a time).

### Verdicts (each comment checked by hand against the snapshot)

| Comment class | A | B |
|---|---|---|
| Real defect: stale counts in doc comments, stale JSDoc | 1 | 3 |
| Valid maintainability / design point (hard-coded solid count, module-load probe, changed builder contract) | 1 | 4 (two say the same thing) |
| Plausible design issue, unconfirmed (props scattered before the new solids exist) | 1 | 0 |
| False positive | 2 (both rated high) | 3 (one rated medium) |
| Artifact of the spike snapshot (rated critical) | 0 | 1 |
| Functional bug in the package | 0 | 0 |

- **False-positive pattern:** every run miscounted a 188-entry literal array ("178", "153", "~130") and
  concluded that the package's count test would fail. A reviewer who runs the tests refutes that in seconds,
  which is what the refute step is for. In a fix file it would have cost an engine round.
- **The artifact:** "`blocker` is not a method on `MapBuilder`" is true for the tree I gave it, so ocr's agent
  does check cross-file APIs. In a real workspace run the upstream package's code is present.
- **The spec as background (B vs A):** twice the comments, still no functional bug, and the same miscount.
- **Run time varies:** the same tree at the same effort took 30 min (B), 25 min (C2) and more than 71 min (C).
- **C2 comments:**
  - the seeded defect, caught;
  - 1 real stale doc count;
  - 2 valid maintainability points;
  - 2 false, one rated critical (a "tag index drift" that cannot happen, because the earlier pass adds only visuals);
  - 2 artifacts of the snapshot's missing builder method.
- **Tool use:** the local model sent malformed `code_comment` arguments twice in run A (a flat object instead of
  `{comments: [...]}`), wrong file paths and line ranges, and unescaped regexes to `git grep`. ocr reported the
  errors back and the model recovered, but each one costs a round trip.

### Against the existing review output

- **Claude's round-2 findings (7), checked against the snapshot:** the three code-level highs are fixed in it:
  - a wrapper that faked a collection's size so a fixed-count test passed;
  - decor items without matching collision;
  - collision boxes with no visible decor.

  The rest are spec-level (missing content, missing report and screenshots) or in test files (lint, test
  fidelity). ocr flagged none of these: it skips test files by default and judges code, not deliverables.
- **Overlap is near zero on this package.** ocr found stale documentation and design fragilities that Claude's
  acceptance-driven review never reported. Claude found spec, test and test-gaming problems that ocr cannot see.
- **Seeded recall: caught (C2).** I re-injected the round-2 test-gaming wrapper into the snapshot: 12 lines,
  including a one-line comment saying why it was there.
  - The clean run (neutral commit message "WP27: skirmish map dressing and cover pass", run end to end through
    `jbr prereview --commit --no-spec --budget-min 90`) flagged it at **high** severity on exactly the injected
    lines.
  - Its reasoning matched Claude's round-2 evidence: consumers iterate the real array; serialization and cloning
    break; the test, not the producer, should change.
  - The first attempt (C) is void. Its commit message named the defect, ocr gives the commit message to the model
    as context in `--commit` mode, and the run was stopped at 71 min before reaching that file group. Stopping it
    did show that on SIGTERM ocr writes its partial JSON within 2 s (exit 1, status `failed`/cancelled, findings
    kept). `jbr prereview` uses such a result as OK with the partial findings (`run_group` sends SIGTERM 30 s
    before the kill).
  - C2 also exercised the production path end to end: pid+deadline lock taken and released, `--timeout` derived
    from the budget, no excludes needed (the commit held only owned files), status `OK`, `.done` =
    `0 24.9min OK`.
  - One seeded defect is one data point. It shows that ocr on local Qwen can find a code-level hack in a diff; it
    says nothing about its recall in general.

### Resource cost on this Mac

- **Memory:** Ollama's footprint grew from 18 GB (weights) to 31-33 GB as the KV cache filled on 80k-token
  prompts. Swap went from 2.1 to 8.9 GB while memory pressure stayed normal (40-60% free). system-check then
  rated Clef 27B NO-GO and Clef flash CAUTION. Do not run a pre-review alongside an `ollama_qwen` implementer
  or a Clef server.
- **Tokens:** ~4M local input tokens per package (~94% prompt-cache hits). That is free in money but not in
  time: about 30 GPU-minutes per package.

## Recommendation

1. **Merge the stage as opt-in** (this branch). It is isolated (local only, never fails a build, prompts are
   identical when off) and it gives the workflows the measurement hook: `prereview: {status, findings, confirmed}`.
2. **Do not enable it by default.** On this sample:
   - It found no functional bug in the real snapshot. It did find the seeded code-level hack (C2), but its
     loudest comments are often wrong, and it cannot see tests or deliverables.
   - Its ~30 min per package serializes on one GPU. In a parallel `build_dag.js` level, every package after the
     first gets `busy` and no hints, and the one that runs delays its Claude review by half an hour.
3. **Where it fits now:**
   - the Qwen backlog lane (unattended, one package at a time, reviewed later in a batch): `review_level.js` with
     `preReview: true`;
   - big refactors or security-relevant diffs, where its built-in rules (NPE, thread safety, XSS, SQLi) apply.
     This package was game data and had none of that.
4. **Before widening it:**
   1. Run it on 3-5 more packages whose pre-fix code is recoverable (queue-lane worktrees keep it), and compare
      `confirmed` with the Claude round-1 findings. The seeded rerun (C2) is done: caught.
   2. Try the main task with thinking on (`extra_body: {"reasoning_effort": "low"}`) or a stronger local model.
   3. Add prereview yield to `jbr record` / the scorecard.
   4. A cloud endpoint (where the benchmark's numbers come from) would be the fair precision test, but it is a
      privacy decision for the owner. This spike did not use one.
5. **Upstream feedback** worth filing:
   - the CLI `--exclude` matcher differs from the gitignore matcher (no anchoring, and `?`/`{`/`}` cannot be
     escaped);
   - local models send malformed `code_comment` arguments.

