// Claude Code Workflow script: dependency-aware parallel build of work packages.
// Implement (Claude subagent or external engine via jbr), fresh-context review, adversarial
// refutation, fix loop, per package. Everything project-specific comes from `args`:
//
// args = {
//   root: 'C:\\path\\to\\project',            // Windows or POSIX path (required)
//   posixRoot: '/c/path/to/project',          // path usable from Git Bash (default: derived from root)
//   jbr: '/c/path/to/jev-build-router',       // POSIX path of this repo (required when any engine is external)
//   packages: [{ id: 'WP04', depends_on: ['WP03'] }, ...],   // required
//   done: ['WP01', 'WP02'],                   // already finished (deps satisfied)
//   routing: { WP04: 'claude_subagent', WP05: 'agy_gemini_pro' },  // from ops/routing.json (default claude_subagent)
//   engineArgs: { agy_gemini_pro: '--timeout 55m' },   // extra `python -m jbr spawn` args per engine (optional)
//   fallbackEngine: 'claude_subagent',        // takes over an implement/fix step whose engine hit quota or crashed (default claude_subagent)
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
const RUN_PREFIX = 'run'
const engineOf = id => ROUTING[id] || 'claude_subagent'
const FALLBACK = A.fallbackEngine || 'claude_subagent'
// engines that reported quota in this run: later steps (any package) skip straight to the fallback
const EXHAUSTED = new Set()
const external = id => engineOf(id) !== 'claude_subagent'
if (Object.keys(PKGS).some(external) && !JBR) throw new Error('args.jbr (POSIX path of jev-build-router) is required when a package is routed to an external engine')
// every dependency must be a package in this run or already done; a silent drop would start a package with its dep unbuilt
for (const [id, deps] of Object.entries(PKGS)) for (const d of deps) if (!DONE.has(d) && !PKGS[d]) throw new Error(`unknown dependency ${d} of ${id}: not in args.packages or args.done`)

const specPath = id => join(PROMPTS, `${id}.md`)
const runStem = (id, tag) => `${RUN_PREFIX}-${id}${tag ? '-' + tag : ''}`

const runnerPrompt = (id, eng, tag, extraFile) => {
  const stem = runStem(id, tag)
  const extra = extraFile ? ` --extra-file "${extraFile}"` : ''
  const engArgs = ENGINE_ARGS[eng] ? ' ' + ENGINE_ARGS[eng] : ''
  return `You are the runner for work package ${id} on the external engine "${eng}". You do NOT write code yourself; you launch the engine, wait for it, and report.
1. cd ${POSIX} && PYTHONPATH="${JBR}" python -m jbr --project "${POSIX}" spawn ${id} --engine ${eng}${tag ? ' --tag ' + tag : ''}${extra}${engArgs}
   (returns immediately and prints the marker path ${REPORTS}/${stem}.done)
2. Wait for the marker with repeated Bash calls, each: cd ${POSIX} && for i in $(seq 1 17); do [ -f ${REPORTS}/${stem}.done ] && break; sleep 30; done; cat ${REPORTS}/${stem}.done 2>/dev/null || echo STILL-RUNNING
   Repeat until it prints an exit code (up to 8 times, ~70 minutes). The marker reads "<exit> <minutes>min <STATUS> [reset-hint]": STATUS QUOTA = the engine hit its quota (429 / RESOURCE_EXHAUSTED / fast exit with no work) and did nothing; CRASH = it died without output; OK = it ran. A marker with only two tokens: exit -1 is CRASH, anything else OK. No marker after 8 waits and no agy/codex process running: CRASH.
3. Only if STATUS is OK: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -5 ; and read the last 60 lines of ${REPORTS}/${stem}.log. Otherwise do NOT run tests; read the last 20 lines of the log for the notes.
Return: engine_status ('ok' | 'quota' | 'crash', from STATUS), reset_hint (the text after STATUS, else ''), the engine's final test summary (or 'no engine work: <status>'), the files it says it created/changed, any acceptance items it reported as unsatisfied, and the exit code in notes.`
}

const IMPL_SCHEMA = { type: 'object', properties: {
  wp: { type: 'string' }, pytest_summary: { type: 'string' },
  files_created_or_changed: { type: 'array', items: { type: 'string' } },
  unsatisfied: { type: 'array', items: { type: 'string' } },
  notes: { type: 'string' } }, required: ['wp', 'pytest_summary', 'files_created_or_changed', 'unsatisfied', 'notes'] }
// external engine runs also report whether the engine did any work at all
const RUNNER_SCHEMA = { type: 'object', properties: { ...IMPL_SCHEMA.properties,
  engine_status: { type: 'string', enum: ['ok', 'quota', 'crash'] }, reset_hint: { type: 'string' } },
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
  // fallback runs get their own marker/log so the dead primary run stays inspectable
  const tag = [kind === 'impl' ? '' : `fix${round}`, fallback ? 'fb' : ''].filter(Boolean).join('-')
  let extraFile = ''
  if (kind === 'fix') {
    extraFile = `${REPORTS}/fix-${id}-round${round}.md`
    await agent(`Write the following text verbatim to the file ${join(extraFile)} (create it; overwrite if present) and return 'ok':\n\n${fixPrompt(id, round, findings)}`, { label: `fixfile:${id}:r${round}${sfx}`, phase: ph, effort: 'low' })
  }
  const label = kind === 'impl' ? `impl:${id}:${eng}${sfx}` : `fix:${id}:r${round}:${eng}${sfx}`
  return agent(runnerPrompt(id, eng, tag, extraFile), { label, phase: ph, schema: RUNNER_SCHEMA, effort: 'low' })
}

// Routed engine first, then FALLBACK. A quota/crash run did no work: it is never reviewed and never
// counts as a fix round. Returns { res, tried, worked }; worked=false means no engine could run the step.
async function step(id, kind, round, findings) {
  const what = kind === 'impl' ? 'implement' : `fix round ${round}`
  const tried = []
  for (const eng of [...new Set([engineOf(id), FALLBACK])]) {
    const fallback = eng !== engineOf(id)
    if (EXHAUSTED.has(eng)) {
      log(`${id}: ${what}: skipping ${eng} (quota exhausted earlier in this run)`)
      tried.push({ engine: eng, status: 'quota', skipped: true })
      continue
    }
    if (fallback) log(`${id}: ${what}: falling back to ${eng}`)
    const res = await runOn(id, eng, kind, round, findings, fallback)
    const st = statusOf(res)
    const hint = (res && res.reset_hint) || ''
    tried.push({ engine: eng, status: st, reset_hint: hint })
    if (st === 'ok') return { res, tried, worked: true }
    if (st === 'quota') EXHAUSTED.add(eng)
    log(`${id}: ${what} on ${eng} -> ${st}${hint ? ' (resets in ' + hint + ')' : ''}: engine did no work; not reviewed, fix round not consumed`)
  }
  return { res: null, tried, worked: false }
}

const triedText = tried => tried.map(t => `${t.engine}=${t.status}`).join(', ')

async function build(id) {
  log(`${id}: implementing on ${engineOf(id)}`)
  const s = await step(id, 'impl', 0, [])
  const impl = s.res
  const history = [{ stage: 'impl', engines: s.tried, summary: impl ? impl.pytest_summary : (s.worked ? 'implementer crashed' : 'no engine did any work'), unsatisfied: impl ? impl.unsatisfied : [] }]
  const row = (final, round, r, extra) => ({ wp: id, engine: engineOf(id), final, rounds: round, history,
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
