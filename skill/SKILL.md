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

## Preconditions in the target project

- `ops/work_packages.json`: `{"work_packages": [{id, title, goal, depends_on, files, acceptance, tests, spec_refs, implementer_prompt, routing_hints?}]}`. Optional top-level `rules` (engineer rules line) and `goal` (routing goal).
- `ops/prompts/<WP>.md`: the full per-package spec (inlined verbatim into the engine prompt).
- `TYPESAFE_API_KEY` in the environment for `route` (not needed for `--dry-run` or when only one engine is available).

## Steps

1. **Read the engine table** `C:\Users\a8878\dev\jev-build-router\engines.json`. Decide what is available right now (quota, login). Override per call with `--available agy_claude_opus=no --available codex_astra=yes`; do not edit the file for a one-off.
2. **Route.** `PYTHONPATH="$JBR" python -m jbr --project . route --done WP01 WP02 --now 09:30` writes `ops/routing.json` (`{WP: {engine, confidence, probabilities}}`) and prints one line per package. `--dry-run` prints the exact Jev questions without calling the API. Show the user the routing table before building.
3. **Build with the DAG workflow.** Call the Claude Code Workflow tool with `scriptPath = C:\Users\a8878\dev\jev-build-router\workflows\build_dag.js` and
   ```
   args = { root: "<project root>", jbr: "/c/Users/a8878/dev/jev-build-router",
            packages: [{id, depends_on}, ...],       // from work_packages.json
            done: [...],                              // already accepted packages
            routing: { WP04: "claude_subagent", ... },// ops/routing.json .routing[wp].engine
            maxFixRounds: 2, testCmd: "python -m pytest -q" }
   ```
   Packages routed to `claude_subagent` are implemented by workflow subagents; every other engine is launched through `python -m jbr spawn` by a low-effort runner agent that waits on the `.done` marker. Every package, whatever the engine, gets fresh-context review, refutation and up to `maxFixRounds` fix rounds.
4. **Review an already-built level separately** (e.g. packages built outside the workflow): Workflow tool with `scriptPath = ...\workflows\review_level.js`, `args = { root, ids: ["WP01", "WP02"] }`. Save the workflow output to a json file, then `python -m jbr compose-fix <output.json> <round>` writes `ops/reports/fix-<WP>-round<N>.md`; re-run the engine with `python -m jbr spawn WP --tag fix1 --extra-file ops/reports/fix-WP-round1.md`.
5. **Monitor.** `python -m jbr status` (routing + `.done` markers), `python -m jbr wait WP05 WP06 --timeout-min 70`.

## Known engine pitfalls (see README "Known pitfalls")

- agy needs `--dangerously-skip-permissions` in headless mode (runner adds it); Claude thinking models reject `--effort` (runner omits it for `claude*` models).
- codex command shape is fixed in `jbr/runner.py::build_command`; `-o` captures the final message.
- Prompts are handed over as a file pointer (Windows 32K command-line cap).
- Quota exhausted: no output, or exit 124 from `--print-timeout`. Re-route with `--available <engine>=no`.

## Do not

- Do not edit the target project's files from this skill; only `ops/routing.json` and `ops/reports/*` are written.
- Do not call the TypeSafe API when the user only wants to see the plan: use `route --dry-run`.
