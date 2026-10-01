// Claude Code Workflow script: dependency-aware parallel build of work packages.
// Implement (Claude subagent or external engine via jbr), fresh-context review, adversarial
// refutation, fix loop, per package.
//
// Engine chain: every implement/fix step walks the package's chain (default agy_gemini_pro >
// agy_claude_opus > codex_astra > claude_subagent). Before spawning an external engine its runner
// agent runs `python -m jbr probe <engine>` (shared state file ~/.jbr/engine_state.json + a cheap
// live probe, AVAILABLE cached 10 min): EXHAUSTED/UNAVAILABLE -> engine_status 'quota' without
// spawning, and the step moves to the next engine. There is NO run-wide skip list: every step
// asks `jbr probe` again, so an engine whose quota has reset is preferred again later in the
// same run. Quota/crash runs are never reviewed and never consume a fix round.
//
// Everything project-specific comes from `args`:
//
// args = {
//   root: 'C:\\path\\to\\project',            // Windows or POSIX path (required)
//   posixRoot: '/c/path/to/project',          // path usable from Git Bash (default: derived from root)
//   jbr: '/c/path/to/jev-build-router',       // POSIX path of this repo (required when any engine is external)
//   packages: [{ id: 'WP04', depends_on: ['WP03'] }, ...],   // required
//   done: ['WP01', 'WP02'],                   // already finished (deps satisfied)
//   routing: { WP04: 'claude_subagent',       // pin: that engine first, then the engineChain entries after it
//              WP05: ['agy_gemini_pro', 'codex_astra', 'claude_subagent'],  // explicit chain (ops/routing.json .chains)
//              WP06: 'auto' },                // or no entry at all (the DEFAULT): walk engineChain
//   engineChain: ['agy_gemini_pro', 'agy_claude_opus', 'codex_astra', 'claude_subagent'],  // default priority order
//   engineArgs: { agy_gemini_pro: '--timeout 55m' },   // extra `python -m jbr spawn` args per engine (optional)
//   jbrArgs: '--engines /c/path/engines.json',          // global jbr options for probe + spawn (optional)
//   fallbackEngine: 'claude_subagent',        // appended to every chain as the last resort (default claude_subagent)
//   maxFixRounds: 2,
//   promptsDir: 'ops/prompts', reportsDir: 'ops/reports',
//   testCmd: 'python -m pytest -q',           // run from posixRoot
//   rules: '...',                             // COMMON rules text override (optional)
//   sourceHint: 'src layout under src/pkg'    // one line about where code lives (optional)
// }
export const meta = {
  name: 'jbr-build-dag',
  description: 'Dependency-aware parallel build of work packages: implement (Claude subagent or external engine via jbr), fresh-context review, adversarial refutation, fix loop, per package',
  phases: [
    { title: 'Implement', detail: 'one implementer per package as soon as its deps are done' },
    { title: 'Review', detail: 'fresh-context reviewer per package per round' },
    { title: 'Verify', detail: 'refute each reported failure' },
    { title: 'Fix', detail: 'fix agent per confirmed round' },
  ],
}

const A = args || {}
if (!A.root) throw new Error('args.root (project root) is required')
if (!Array.isArray(A.packages) || !A.packages.length) throw new Error('args.packages [{id, depends_on}] is required')

const ROOT = A.root
const SEP = ROOT.includes('\\') ? '\\' : '/'
const toPosix = p => p.replace(/^([A-Za-z]):[\\/]/, (m, d) => '/' + d.toLowerCase() + '/').replace(/\\/g, '/')
const POSIX = A.posixRoot || toPosix(ROOT)
const JBR = A.jbr || ''
const join = (...parts) => [ROOT, ...parts].join(SEP).replace(/[\\/]+/g, SEP)
const PROMPTS = A.promptsDir || 'ops/prompts'
const REPORTS = A.reportsDir || 'ops/reports'
const TEST_CMD = A.testCmd || 'python -m pytest -q'
const MAX_FIX_ROUNDS = Number.isInteger(A.maxFixRounds) ? A.maxFixRounds : 2
const PKGS = Object.fromEntries(A.packages.map(p => [p.id, p.depends_on || []]))
const DONE = new Set(A.done || [])
const ROUTING = A.routing || {}
const ENGINE_ARGS = A.engineArgs || {}
const JBR_ARGS = A.jbrArgs ? ' ' + A.jbrArgs : ''
const RUN_PREFIX = 'run'
const LAST_RESORT = 'claude_subagent'
const DEFAULT_CHAIN = ['agy_gemini_pro', 'agy_claude_opus', 'codex_astra', LAST_RESORT]
const ENGINE_CHAIN = Array.isArray(A.engineChain) && A.engineChain.length ? A.engineChain : DEFAULT_CHAIN
const FALLBACK = A.fallbackEngine || LAST_RESORT
const isExternal = e => e !== LAST_RESORT
const uniq = xs => [...new Set(xs.filter(Boolean))]
// chain-derived engines (auto / after a pin) need args.jbr when external; without it they are dropped
const fromChain = es => JBR ? es : es.filter(e => !isExternal(e))
if (!JBR && ENGINE_CHAIN.some(isExternal)) log(`args.jbr not set: external engines dropped from the engine chain (${ENGINE_CHAIN.filter(isExternal).join(', ')}); auto-routed packages run on ${LAST_RESORT}`)
// routing entry per package: string (pin, then the chain after it) | array (explicit chain) | 'auto' / missing (whole chain)
function chainFor(id) {
  const r = ROUTING[id]
  let c
  if (Array.isArray(r) && r.length) c = r
  else if (!r || r === 'auto') c = fromChain(ENGINE_CHAIN)
  else {
    const i = ENGINE_CHAIN.indexOf(r)
    c = [r, ...fromChain(i >= 0 ? ENGINE_CHAIN.slice(i + 1) : ENGINE_CHAIN)]
  }
  return uniq([...c, FALLBACK])
}
const CHAINS = Object.fromEntries(Object.keys(PKGS).map(id => [id, chainFor(id)]))
const engineOf = id => CHAINS[id][0]
if (!JBR && Object.values(CHAINS).some(c => c.some(isExternal))) throw new Error('args.jbr (POSIX path of jev-build-router) is required when a package is routed to an external engine')
// every dependency must be a package in this run or already done; a silent drop would start a package with its dep unbuilt
for (const [id, deps] of Object.entries(PKGS)) for (const d of deps) if (!DONE.has(d) && !PKGS[d]) throw new Error(`unknown dependency ${d} of ${id}: not in args.packages or args.done`)

const specPath = id => join(PROMPTS, `${id}.md`)
const runStem = (id, tag) => `${RUN_PREFIX}-${id}${tag ? '-' + tag : ''}`

const runnerPrompt = (id, eng, tag, extraFile, extraText) => {
  const stem = runStem(id, tag)
  const extra = extraFile ? ` --extra-file "${extraFile}"` : ''
  const engArgs = ENGINE_ARGS[eng] ? ' ' + ENGINE_ARGS[eng] : ''
  const writeFix = extraFile ? `
0b. Write the text between the two marker lines below verbatim to the file ${join(extraFile)} (create it; overwrite if present; marker lines excluded).
<<<<<<<< FIX INSTRUCTIONS
${extraText}
>>>>>>>> FIX INSTRUCTIONS` : ''
  return `You are the runner for work package ${id} on the external engine "${eng}". You do NOT write code yourself; you check the engine, launch it, wait for it, and report.
0. Availability gate (cheap; never skip it): cd ${POSIX} && PYTHONPATH="${JBR}" python -m jbr${JBR_ARGS} probe ${eng}; echo "probe-exit=$?"
   It prints AVAILABLE (exit 0), EXHAUSTED <until> (exit 3: out of quota until that UTC time) or UNAVAILABLE <reason> (exit 4).
   Exit 3 or 4: STOP HERE. Do not spawn, do not run tests. Return engine_status 'quota', reset_hint = the text after EXHAUSTED/UNAVAILABLE, probe = the whole probe line, pytest_summary 'no engine work: probe <EXHAUSTED|UNAVAILABLE>', files_created_or_changed [], unsatisfied [], notes = the probe line.
   Any other exit (jbr itself failed): return engine_status 'crash' with the output in notes. Exit 0: continue.${writeFix}
1. cd ${POSIX} && PYTHONPATH="${JBR}" python -m jbr${JBR_ARGS} --project "${POSIX}" spawn ${id} --engine ${eng}${tag ? ' --tag ' + tag : ''}${extra}${engArgs}
   (returns immediately and prints the marker path ${REPORTS}/${stem}.done)
2. Wait for the marker with repeated Bash calls, each: cd ${POSIX} && for i in $(seq 1 17); do [ -f ${REPORTS}/${stem}.done ] && break; sleep 30; done; cat ${REPORTS}/${stem}.done 2>/dev/null || echo STILL-RUNNING
   Repeat until it prints an exit code (up to 8 times, ~70 minutes). The marker reads "<exit> <minutes>min <STATUS> [reset-hint]": STATUS QUOTA = the engine hit its quota (429 / RESOURCE_EXHAUSTED / fast exit with no work) and did nothing; CRASH = it died without output; OK = it ran. A marker with only two tokens: exit -1 is CRASH, anything else OK. No marker after 8 waits and no agy/codex process running: CRASH.
3. Only if STATUS is OK: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -5 ; and read the last 60 lines of ${REPORTS}/${stem}.log. Otherwise do NOT run tests; read the last 20 lines of the log for the notes.
A QUOTA run is already recorded in the engine state file by jbr (later probes say EXHAUSTED until the reset).
Return: engine_status ('ok' | 'quota' | 'crash', from STATUS), reset_hint (the text after STATUS, else ''), the engine's final test summary (or 'no engine work: <status>'), the files it says it created/changed, any acceptance items it reported as unsatisfied, and the exit code in notes.`
}

const IMPL_SCHEMA = { type: 'object', properties: {
  wp: { type: 'string' }, pytest_summary: { type: 'string' },
  files_created_or_changed: { type: 'array', items: { type: 'string' } },
  unsatisfied: { type: 'array', items: { type: 'string' } },
  notes: { type: 'string' } }, required: ['wp', 'pytest_summary', 'files_created_or_changed', 'unsatisfied', 'notes'] }
// external engine runs also report whether the engine did any work at all
const RUNNER_SCHEMA = { type: 'object', properties: { ...IMPL_SCHEMA.properties,
  engine_status: { type: 'string', enum: ['ok', 'quota', 'crash'] }, reset_hint: { type: 'string' }, probe: { type: 'string' } },
  required: [...IMPL_SCHEMA.required, 'engine_status'] }
const REVIEW_SCHEMA = { type: 'object', properties: {
  wp: { type: 'string' }, pytest_summary: { type: 'string' }, test_count: { type: 'number' },
  passed_criteria: { type: 'array', items: { type: 'string' } },
  failures: { type: 'array', items: { type: 'object', properties: {
    criterion: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] },
    evidence: { type: 'string' }, fix: { type: 'string' } }, required: ['criterion', 'severity', 'evidence', 'fix'] } },
  mutation_check: { type: 'string' },
  ownership_violations: { type: 'array', items: { type: 'string' } },
  verdict: { type: 'string', enum: ['accept', 'fix'] } },
  required: ['wp', 'pytest_summary', 'test_count', 'passed_criteria', 'failures', 'mutation_check', 'ownership_violations', 'verdict'] }
const VERDICT_SCHEMA = { type: 'object', properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } }, required: ['refuted', 'reason'] }

const COMMON = A.rules || `Repository root: ${ROOT} (other agents are working on OTHER packages in the same checkout right now).
Rules: ${A.sourceHint ? A.sourceHint + '. ' : ''}Own only the files listed for your package; you may append an export line to another package's index/__init__ file, nothing else. Never edit another package's tests or source: if they block you, work around it inside your own files and report it in notes. Do not run git commit/checkout/stash/reset. Do not create files outside the repo.
Test discipline (other agents run the suite concurrently): run your package's own test files while developing. Run the full suite only at the end: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -15. If a full-suite failure is in a file you do not own, wait 60 seconds and re-run once; if it persists, report it in notes and do not touch that file.`

const implPrompt = id => `You are the software engineer on work package ${id}. ${COMMON}
Your complete, authoritative specification is ${specPath(id)}: read it in full first (goal, owned files, acceptance criteria, tests, locked decisions, conventions, doc references). Existing packages you build on are already in the repo (read their public APIs before coding against them; do not guess signatures).
If some of your owned files already exist from an interrupted earlier attempt, read them and continue from them rather than starting over. Implement everything the spec asks for, with the named tests, and make them pass. A fresh reviewer will check every acceptance criterion literally (field names, enum members, signatures, tolerances). Return the final test summary line, the exact list of files you created/changed, and any acceptance item you could not satisfy with the reason.`

const fixPrompt = (id, round, findings) => `You are the software engineer on work package ${id}, fix round ${round}. ${COMMON}
The package code already exists on disk (read it; do not start over). Spec: ${specPath(id)}. A fresh reviewer confirmed the defects below. Fix every item, keep everything else, re-run your package tests and then the full suite, and report per item what you changed (file:line).
CONFIRMED DEFECTS:
${findings.map((f, i) => `${i + 1}. [${f.severity}] ${f.criterion}\n   Evidence: ${f.evidence}\n   Required fix: ${f.fix}`).join('\n')}`

const reviewPrompt = (id, round) => `Adversarial fresh-context review of work package ${id} (review round ${round}) in ${ROOT}. You did not write this code; judge only what is on disk.
1. Read ${specPath(id)} fully: goal, owned files, acceptance criteria.
2. Run this package's own test files first, then the full suite once: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -20. Record the summary and total count. Other reviewers and implementers work on other packages concurrently: if a failure is in a file outside this package, wait 60 seconds and re-run; report it only if it reproduces twice, and then as low severity.
3. Check EVERY acceptance criterion against the code and tests with file:line evidence; verify field names, enum members, signatures, numeric tolerances literally as written.
4. Ownership: cd ${POSIX} && git status --short && git diff --stat. Any modification to a COMMITTED test file of another package is a violation: diff it (git diff -- <file>) and decide whether it weakened an assertion to hide a defect in this package (then it is a high failure of this package).
5. Mutation spot-check: break one core behaviour of this package (flip a sign, change a constant), run ONLY this package's test files, confirm red, then restore by exact reverse edit (files may be uncommitted, so git checkout is NOT safe). Verify restoration with md5sum before/after and delete any __pycache__ you created. Keep the mutated window short.
6. Verdict 'fix' if any high or medium failure, any required test missing, or tests not green. One failure entry per criterion with a concrete fix. Do not fix anything yourself.`

const refutePrompt = (id, f) => `Verify a review finding for work package ${id} in ${ROOT}. Reported failure: ${JSON.stringify(f)}.
Read ${specPath(id)} for the criterion's exact wording, read the cited files, run the relevant package tests (cd ${POSIX} && ${TEST_CMD} <path> 2>&1 | tail -15). REAL means the code truly violates the criterion as written; REFUTED means the reviewer misread the criterion, the code, or the output. Refute only with concrete evidence that the criterion is satisfied.`

async function reviewRound(id, round) {
  const rev = await agent(reviewPrompt(id, round), { label: `review:${id}:r${round}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })
  if (!rev) return { verdict: 'fix', confirmed: [{ criterion: 'reviewer crashed', severity: 'high', evidence: 'no result', fix: 're-run' }], low: [], rev: null }
  const serious = rev.failures.filter(f => f.severity !== 'low')
  const votes = await parallel(serious.map(f => () =>
    agent(refutePrompt(id, f), { label: `verify:${id}:r${round}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'medium' })
      .then(v => ({ f, refuted: !!(v && v.refuted) }))))
  const confirmed = votes.filter(Boolean).filter(v => !v.refuted).map(v => v.f)
  if (rev.verdict === 'fix' && !rev.failures.length) {
    // reviewer said fix (e.g. tests not green) but listed no failure entry: keep the verdict, give the fixer something concrete
    log(`${id}: review round ${round} verdict 'fix' with zero failure entries (${rev.pytest_summary}); kept as fix`)
    confirmed.push({ criterion: 'reviewer verdict fix without failure entries', severity: 'medium', evidence: rev.pytest_summary || 'no pytest summary reported', fix: 'make the full suite green and satisfy every acceptance criterion in the spec' })
  }
  return { verdict: confirmed.length ? 'fix' : 'accept', confirmed, low: rev.failures.filter(f => f.severity === 'low'), rev }
}

const statusOf = res => (res && res.engine_status) || 'ok'  // null (agent crashed) = unknown: keep reviewing

// One implement/fix attempt on one engine. kind: 'impl' | 'fix'.
async function runOn(id, eng, kind, round, findings, fallback) {
  const sfx = fallback ? ':fallback' : ''
  const ph = kind === 'impl' ? 'Implement' : 'Fix'
  if (eng === 'claude_subagent') {
    return kind === 'impl'
      ? agent(implPrompt(id), { label: `impl:${id}${sfx}`, phase: ph, schema: IMPL_SCHEMA, effort: 'high' })
      : agent(fixPrompt(id, round, findings), { label: `fix:${id}:r${round}${sfx}`, phase: ph, schema: IMPL_SCHEMA, effort: 'high' })
  }
  // fallback runs get their own marker/log per engine (run-WP-fb-<engine>) so every dead run stays inspectable
  const tag = [kind === 'impl' ? '' : `fix${round}`, fallback ? 'fb-' + eng : ''].filter(Boolean).join('-')
  // the runner writes the fix file itself, after the probe: an exhausted engine costs one cheap agent call
  const extraFile = kind === 'fix' ? `${REPORTS}/fix-${id}-round${round}.md` : ''
  const label = kind === 'impl' ? `impl:${id}:${eng}${sfx}` : `fix:${id}:r${round}:${eng}${sfx}`
  return agent(runnerPrompt(id, eng, tag, extraFile, kind === 'fix' ? fixPrompt(id, round, findings) : ''), { label, phase: ph, schema: RUNNER_SCHEMA, effort: 'low' })
}

// Walk the package's engine chain. An external engine's runner probes it first (`jbr probe`) and
// reports 'quota' without spawning when it is EXHAUSTED/UNAVAILABLE. A quota/crash run did no work:
// it is never reviewed and never counts as a fix round. Every step starts again at the top of the
// chain, so a recovered engine is used again. Returns { res, tried, worked }; worked=false: no engine ran.
async function step(id, kind, round, findings) {
  const what = kind === 'impl' ? 'implement' : `fix round ${round}`
  const tried = []
  for (const [i, eng] of CHAINS[id].entries()) {
    const fallback = i > 0
    if (fallback) log(`${id}: ${what}: falling back to ${eng}`)
    const res = await runOn(id, eng, kind, round, findings, fallback)
    const st = statusOf(res)
    const hint = (res && res.reset_hint) || ''
    const t = { engine: eng, status: st, reset_hint: hint }
    if (st !== 'ok' && res && res.probe) t.probe = res.probe  // stopped by the availability gate, never spawned
    tried.push(t)
    if (st === 'ok') return { res, tried, worked: true }
    log(`${id}: ${what} on ${eng} -> ${st}${t.probe ? ' [probe: ' + t.probe + ']' : hint ? ' (resets in ' + hint + ')' : ''}: engine did no work; not reviewed, fix round not consumed`)
  }
  return { res: null, tried, worked: false }
}

const triedText = tried => tried.map(t => `${t.engine}=${t.status}`).join(', ')

async function build(id) {
  log(`${id}: implementing; engine chain ${CHAINS[id].join(' > ')}`)
  const s = await step(id, 'impl', 0, [])
  const impl = s.res
  const history = [{ stage: 'impl', engines: s.tried, summary: impl ? impl.pytest_summary : (s.worked ? 'implementer crashed' : 'no engine did any work'), unsatisfied: impl ? impl.unsatisfied : [] }]
  const row = (final, round, r, extra) => ({ wp: id, engine: engineOf(id), chain: CHAINS[id], final, rounds: round, history,
    unresolved: final === 'accept' ? [] : (r ? r.confirmed : []), low_open: r ? r.low : [],
    mutation: r && r.rev ? r.rev.mutation_check : '', ownership: r && r.rev ? r.rev.ownership_violations : [], ...extra })
  if (!s.worked) {
    log(`${id}: BLOCKED: no engine could implement (${triedText(s.tried)})`)
    return row('blocked', 0, null, { blocked: { stage: 'impl', tried: s.tried } })
  }
  let round = 1
  let r = await reviewRound(id, round)
  history.push({ stage: `review${round}`, verdict: r.verdict, confirmed: r.confirmed.length, low: r.low.length, tests: r.rev ? r.rev.test_count : 0 })
  while (r.verdict === 'fix' && round <= MAX_FIX_ROUNDS) {
    log(`${id}: review round ${round} -> ${r.confirmed.length} confirmed defects, fixing`)
    const f = await step(id, 'fix', round, r.confirmed.concat(r.low))
    history.push({ stage: `fix${round}`, engines: f.tried })
    if (!f.worked) {
      // the last review still describes the code on disk; re-reviewing a dead run would only burn a round
      log(`${id}: BLOCKED in fix round ${round}: no engine could run it (${triedText(f.tried)})`)
      return row('blocked', round, r, { blocked: { stage: `fix${round}`, tried: f.tried } })
    }
    round += 1
    r = await reviewRound(id, round)
    history.push({ stage: `review${round}`, verdict: r.verdict, confirmed: r.confirmed.length, low: r.low.length, tests: r.rev ? r.rev.test_count : 0 })
  }
  log(`${id}: ${r.verdict === 'accept' ? 'ACCEPTED' : 'UNRESOLVED after ' + MAX_FIX_ROUNDS + ' fix rounds'}`)
  return row(r.verdict, round, r, {})
}

// DAG scheduler: each package starts the moment all its deps have finished (accepted or not).
const P = {}
const start = id => {
  if (P[id]) return P[id]
  P[id] = (async () => {
    await Promise.all(PKGS[id].filter(d => !DONE.has(d)).map(d => start(d)))
    return build(id)
  })()
  return P[id]
}
const results = await Promise.all(Object.keys(PKGS).filter(id => !DONE.has(id)).map(start))
return results
