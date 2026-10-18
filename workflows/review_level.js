// Claude Code Workflow script: fresh-context review of one build level of work packages.
// One reviewer per package (read-back vs acceptance criteria, run tests, mutation spot-check),
// then one verifier per serious finding that tries to refute it. Output feeds
// `python -m jbr compose-fix <output.json> <round>`.
//
// args = {
//   root: 'C:\\path\\to\\project',      // required
//   posixRoot: '/c/path/to/project',    // default: derived from root
//   ids: ['WP01', 'WP02'],              // required (args must be this object; a bare array has no root)
//   promptsDir: 'ops/prompts',          // absolute, or relative to root
//   reportsDir: '<jbr paths .reports_dir>',  // where the engine transcripts are (optional: without it reviewers skip them)
//   testCmd: 'python -m pytest -q',
//   logTag: 'q0',                       // optional: engine transcript is <reportsDir>/<logPrefix>-<id>-<logTag>.log
//   logPrefix: 'run',                   // engine transcript is <reportsDir>/<logPrefix>-<id>.log
//   engineStatus: { WP02: 'quota' },    // optional, from `jbr wait`/`jbr status` (OK|QUOTA|CRASH): a package whose
//                                       // last engine run did no work is NOT reviewed (verdict 'blocked')
//   jbr: '/path/to/jev-build-router',   // needed for preReview
//   jbrArgs: '--packages <plan>/work_packages.json',  // global jbr options for prereview (optional)
//   preReview: false                    // true | { effort: 'low', budgetMin: 45 }: `jbr prereview` (open-code-review on local
//                                       // Ollama) first; its findings are unverified hints for the reviewer. Needs jbr +
//                                       // reportsDir. Skipped / failed / timed out: the review runs without it (default off)
// }
export const meta = {
  name: 'jbr-review-level',
  description: 'Fresh-context review of one build level of work packages: read-back vs acceptance criteria, run tests, mutation spot-check, then adversarially verify each failure',
  phases: [
    { title: 'Review', detail: 'one reviewer per work package' },
    { title: 'Verify', detail: 'refute each reported failure' },
  ],
}

const A = args || {}
const ids = Array.isArray(A.ids) ? A.ids : []
if (!A.root) throw new Error('args.root (project root) is required')
if (!ids.length) throw new Error('pass work package ids via args.ids, e.g. { root, ids: ["WP01"] }')

const ROOT = A.root
const SEP = ROOT.includes('\\') ? '\\' : '/'
const toPosix = p => p.replace(/^([A-Za-z]):[\\/]/, (m, d) => '/' + d.toLowerCase() + '/').replace(/\\/g, '/')
const POSIX = A.posixRoot || toPosix(ROOT)
const join = (...parts) => [ROOT, ...parts].join(SEP).replace(/[\\/]+/g, SEP)
const isAbs = p => /^([A-Za-z]:)?[\\/]/.test(p)
const under = (dir, ...rest) => isAbs(dir) ? [dir, ...rest].join('/').replace(/[\\/]+/g, '/') : join(dir, ...rest)
const PROMPTS = A.promptsDir || 'ops/prompts'
const REPORTS = A.reportsDir || ''
const TEST_CMD = A.testCmd || 'python -m pytest -q'
const LOG_PREFIX = A.logPrefix || 'run'
const LOG_TAG = A.logTag ? '-' + A.logTag : ''   // queue runs are tagged: run-<WP>-q<round>.log
const ENGINE_STATUS = A.engineStatus || {}
const JBR = A.jbr || ''
const JBR_ARGS = A.jbrArgs ? ' ' + A.jbrArgs : ''   // e.g. '--packages <plan>/work_packages.json' (owned files)
const PRE = A.preReview ? Object.assign({ effort: 'low', budgetMin: 45 }, A.preReview === true ? {} : A.preReview) : null
const PRE_WAITS = PRE ? Math.ceil(PRE.budgetMin / 8.5) + 1 : 0   // jbr enforces the budget, so the marker always comes
if (PRE && (!JBR || !REPORTS)) throw new Error('args.preReview needs args.jbr and args.reportsDir (`python -m jbr --project <root> paths` .reports_dir)')
const deadRun = id => ['quota', 'crash'].includes(String(ENGINE_STATUS[id] || 'ok').toLowerCase())

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    wp: { type: 'string' },
    pytest_summary: { type: 'string' },
    test_count: { type: 'number' },
    passed_criteria: { type: 'array', items: { type: 'string' } },
    failures: { type: 'array', items: { type: 'object', properties: {
      criterion: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] },
      evidence: { type: 'string' }, fix: { type: 'string' } }, required: ['criterion', 'severity', 'evidence', 'fix'] } },
    mutation_check: { type: 'string' },
    ownership_violations: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', enum: ['accept', 'fix'] },
  },
  required: ['wp', 'pytest_summary', 'test_count', 'passed_criteria', 'failures', 'mutation_check', 'ownership_violations', 'verdict'],
}
const VERDICT_SCHEMA = { type: 'object', properties: {
  refuted: { type: 'boolean' }, reason: { type: 'string' } }, required: ['refuted', 'reason'] }
const PRE_SCHEMA = { type: 'object', properties: {
  status: { type: 'string', enum: ['ok', 'skipped', 'failed', 'timeout'] }, reason: { type: 'string' }, findings: { type: 'number' } },
  required: ['status', 'findings'] }

const PRE_TAG = 'pre' + (A.logTag ? '-' + A.logTag : '')
const preStem = id => `prereview-${id}-${PRE_TAG}`
const preReviewPrompt = id => `You run the cheap pre-reviewer for work package ${id}. You do NOT review code yourself and you edit nothing.
1. cd ${POSIX} && PYTHONPATH="${JBR}" python -m jbr --reports "${REPORTS}" --project "${POSIX}" --prompts "${PROMPTS}"${JBR_ARGS} prereview ${id} --tag ${PRE_TAG} --effort ${PRE.effort} --budget-min ${PRE.budgetMin} --detach
   (returns at once and prints "spawned pid=... marker=${REPORTS}/${preStem(id)}.done"). If it does not print "spawned pid=", STOP: return status 'failed', findings 0, reason = its output. Do not wait.
2. Wait for the marker with repeated Bash calls, each: for i in $(seq 1 17); do [ -f ${REPORTS}/${preStem(id)}.done ] && break; sleep 30; done; cat ${REPORTS}/${preStem(id)}.done 2>/dev/null || echo STILL-RUNNING
   Repeat until it prints a line (up to ${PRE_WAITS} times). Still missing after that: return status 'timeout', findings 0.
3. cat ${REPORTS}/${preStem(id)}.json and return its "status" lowercased, its "reason", and the number of entries in its "findings" list.`

// pre: { findings } when the pre-reviewer left findings, else null (the prompt is then exactly the plain review)
const preHint = (id, pre) => pre ? `
0. Pre-review hints: a cheap local reviewer (open-code-review on a local model; it reads the diff, not the tests, and only skims the spec) left ${pre.findings} line-level findings in ${REPORTS}/${preStem(id)}.json ("findings": path, start_line, end_line, content, suggestion_code). They are UNVERIFIED and may be wrong: check each quickly at the cited lines, report each real one as a failure whose criterion starts with "pre-review: " (your own severity), drop the rest and any finding on a file this package does not own. Then spend your effort on what it does not cover: every acceptance criterion, the tests, ownership and the mutation check.` : ''

// Cheap first pass (args.preReview): never blocks or fails the review; anything but 'ok' with findings = no hints.
async function preReview(id) {
  if (!PRE) return null
  const r = await agent(preReviewPrompt(id), { label: `prereview:${id}`, phase: 'Review', schema: PRE_SCHEMA, effort: 'low' })
  const status = r && typeof r === 'object' && r.status ? r.status : 'failed'
  const findings = r && Number.isFinite(r.findings) ? r.findings : 0
  if (status !== 'ok') log(`${id}: pre-review ${status}${r && r.reason ? ' (' + r.reason + ')' : ''}: reviewing without it`)
  return { status, findings }
}
const preRow = (pre, confirmed) => pre ? { prereview: { status: pre.status, findings: pre.findings,
  confirmed: confirmed.filter(f => /^pre-review:/i.test(f.criterion || '')).length } } : {}

const reviewPrompt = (id, pre) => `Adversarial fresh-context review of work package ${id} in the repo ${ROOT}. You did not write this code; do not infer the author's intent, judge only what is on disk.${preHint(id, pre)}
1. Read ${under(PROMPTS, id + '.md')} fully: it holds the goal, owned files and the acceptance criteria.
2. ${REPORTS ? `Read ${under(REPORTS, LOG_PREFIX + '-' + id + LOG_TAG + '.log')} if it exists (the engineer's transcript; treat its claims as unverified).` : 'No engine transcript was provided; judge the code and tests only.'}
3. Run the tests from the repo root with Bash: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -20 . Record the summary line and total test count. If the package spec names specific test ids, run them explicitly too.
4. Check EVERY acceptance criterion against the code and test output; cite file:line evidence for each pass and each failure. Verify field names, enum members, signatures and numeric tolerances literally as written in the criteria.
5. Ownership: list any file the engineer modified that is not in the package's owned-file list (cd ${POSIX} && git status --short && git diff --stat) unless it is a pure export append.
6. Mutation spot-check: temporarily break one core behaviour of this package (e.g. flip a sign, change a formula constant), run the relevant tests, confirm they go red, then RESTORE the file exactly (git checkout -- <file> is NOT safe for untracked files; re-edit it back and re-run tests to confirm green). Report what you mutated and whether the tests caught it.
7. Verdict 'fix' if any high or medium failure, any missing test that the criteria require, or tests not green. Report failures as one entry per criterion with a concrete fix instruction the engineer can apply.
Do not fix anything yourself.`

const refutePrompt = (id, f) => `Verify a review finding for work package ${id} in ${ROOT}. A reviewer reported this failure: ${JSON.stringify(f)}.
Read ${under(PROMPTS, id + '.md')} for the acceptance criterion's exact wording, read the cited files, and run the relevant tests (cd ${POSIX} && ${TEST_CMD} <path> 2>&1 | tail -15). Decide whether the finding is REAL (the code truly violates the criterion as written) or REFUTED (the reviewer misread the criterion, the code, or the test output). Default to refuted only when you can show concrete evidence that the criterion is satisfied.`

// reviewing an engine run that did no work only burns a fix round: report it blocked instead
const blocked = ids.filter(deadRun).map(id => {
  log(`${id}: engine status ${ENGINE_STATUS[id]}: no engine work on disk, not reviewed; re-run the package on another engine (same round)`)
  return { wp: id, verdict: 'blocked', blocked_reason: `engine ${String(ENGINE_STATUS[id]).toLowerCase()}`, pytest_summary: '', test_count: 0,
    confirmed_failures: [], refuted_findings: [], low_findings: [], ownership_violations: [], mutation_check: '', passed_count: 0 }
})
const live = ids.filter(id => !deadRun(id))

phase('Review')
// without preReview the stage-1 result stays the bare review (null when the reviewer crashed), exactly as before
const results = !live.length ? [] : await pipeline(live,
  async id => {
    const pre = await preReview(id)
    const hints = pre && pre.status === 'ok' && pre.findings > 0 ? pre : null
    const rev = await agent(reviewPrompt(id, hints), { label: `review:${id}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' })
    return PRE ? { rev, pre } : rev
  },
  async (out, id) => {
    const { rev, pre } = PRE ? (out || {}) : { rev: out, pre: null }
    if (!rev) return { wp: id, verdict: 'fix', confirmed_failures: [{ criterion: 'reviewer crashed', severity: 'high', evidence: 'no review result', fix: 're-run review' }], refuted_findings: [], low_findings: [], pytest_summary: '', test_count: 0, ownership_violations: [], mutation_check: '', passed_count: 0, ...preRow(pre, []) }
    const serious = rev.failures.filter(f => f.severity !== 'low')
    const votes = await parallel(serious.map(f => () =>
      agent(refutePrompt(id, f), { label: `verify:${id}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' })
        .then(v => ({ f, refuted: !!(v && v.refuted), reason: v ? v.reason : 'verifier crashed' }))))
    const confirmed = votes.filter(Boolean).filter(v => !v.refuted).map(v => v.f)
    const refuted = votes.filter(Boolean).filter(v => v.refuted).map(v => ({ ...v.f, refute_reason: v.reason }))
    const low = rev.failures.filter(f => f.severity === 'low')
    if (rev.verdict === 'fix' && !rev.failures.length) {
      // reviewer said fix (e.g. tests not green) but listed no failure entry: keep the verdict, give compose-fix something concrete
      log(`${id}: verdict 'fix' with zero failure entries (${rev.pytest_summary}); kept as fix`)
      confirmed.push({ criterion: 'reviewer verdict fix without failure entries', severity: 'medium', evidence: rev.pytest_summary || 'no pytest summary reported', fix: 'make the full suite green and satisfy every acceptance criterion in the spec' })
    }
    return { wp: id, verdict: confirmed.length ? 'fix' : 'accept', pytest_summary: rev.pytest_summary, test_count: rev.test_count,
      confirmed_failures: confirmed, refuted_findings: refuted, low_findings: low,
      ownership_violations: rev.ownership_violations, mutation_check: rev.mutation_check, passed_count: rev.passed_criteria.length,
      ...preRow(pre, confirmed) }
  })
return blocked.concat(results.filter(Boolean))
