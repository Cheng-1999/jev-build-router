// Claude Code Workflow script: fresh-context review of one build level of work packages.
// One reviewer per package (read-back vs acceptance criteria, run tests, mutation spot-check),
// then one verifier per serious finding that tries to refute it. Output feeds
// `python -m jbr compose-fix <output.json> <round>`.
//
// args = {
//   root: 'C:\\path\\to\\project',      // required
//   posixRoot: '/c/path/to/project',    // default: derived from root
//   ids: ['WP01', 'WP02'],              // required (a bare array is accepted too)
//   promptsDir: 'ops/prompts', reportsDir: 'ops/reports',
//   testCmd: 'python -m pytest -q',
//   logPrefix: 'run'                    // engine transcript is <reportsDir>/<logPrefix>-<id>.log
// }
export const meta = {
  name: 'jbr-review-level',
  description: 'Fresh-context review of one build level of work packages: read-back vs acceptance criteria, run tests, mutation spot-check, then adversarially verify each failure',
  phases: [
    { title: 'Review', detail: 'one reviewer per work package' },
    { title: 'Verify', detail: 'refute each reported failure' },
  ],
}

const A = Array.isArray(args) ? { ids: args } : (args || {})
const ids = A.ids || []
if (!A.root) throw new Error('args.root (project root) is required')
if (!ids.length) throw new Error('pass work package ids via args.ids, e.g. { root, ids: ["WP01"] }')

const ROOT = A.root
const SEP = ROOT.includes('\\') ? '\\' : '/'
const toPosix = p => p.replace(/^([A-Za-z]):[\\/]/, (m, d) => '/' + d.toLowerCase() + '/').replace(/\\/g, '/')
const POSIX = A.posixRoot || toPosix(ROOT)
const join = (...parts) => [ROOT, ...parts].join(SEP).replace(/[\\/]+/g, SEP)
const PROMPTS = A.promptsDir || 'ops/prompts'
const REPORTS = A.reportsDir || 'ops/reports'
const TEST_CMD = A.testCmd || 'python -m pytest -q'
const LOG_PREFIX = A.logPrefix || 'run'

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

const reviewPrompt = id => `Adversarial fresh-context review of work package ${id} in the repo ${ROOT}. You did not write this code; do not infer the author's intent, judge only what is on disk.
1. Read ${join(PROMPTS, id + '.md')} fully: it holds the goal, owned files and the acceptance criteria.
2. Read ${join(REPORTS, LOG_PREFIX + '-' + id + '.log')} if it exists (the engineer's transcript; treat its claims as unverified).
3. Run the tests from the repo root with Bash: cd ${POSIX} && ${TEST_CMD} 2>&1 | tail -20 . Record the summary line and total test count. If the package spec names specific test ids, run them explicitly too.
4. Check EVERY acceptance criterion against the code and test output; cite file:line evidence for each pass and each failure. Verify field names, enum members, signatures and numeric tolerances literally as written in the criteria.
5. Ownership: list any file the engineer modified that is not in the package's owned-file list (cd ${POSIX} && git status --short && git diff --stat) unless it is a pure export append.
6. Mutation spot-check: temporarily break one core behaviour of this package (e.g. flip a sign, change a formula constant), run the relevant tests, confirm they go red, then RESTORE the file exactly (git checkout -- <file> is NOT safe for untracked files; re-edit it back and re-run tests to confirm green). Report what you mutated and whether the tests caught it.
7. Verdict 'fix' if any high or medium failure, any missing test that the criteria require, or tests not green. Report failures as one entry per criterion with a concrete fix instruction the engineer can apply.
Do not fix anything yourself.`

const refutePrompt = (id, f) => `Verify a review finding for work package ${id} in ${ROOT}. A reviewer reported this failure: ${JSON.stringify(f)}.
Read ${join(PROMPTS, id + '.md')} for the acceptance criterion's exact wording, read the cited files, and run the relevant tests (cd ${POSIX} && ${TEST_CMD} <path> 2>&1 | tail -15). Decide whether the finding is REAL (the code truly violates the criterion as written) or REFUTED (the reviewer misread the criterion, the code, or the test output). Default to refuted only when you can show concrete evidence that the criterion is satisfied.`

phase('Review')
const results = await pipeline(ids,
  id => agent(reviewPrompt(id), { label: `review:${id}`, phase: 'Review', schema: REVIEW_SCHEMA, effort: 'high' }),
  async (rev, id) => {
    if (!rev) return { wp: id, verdict: 'fix', confirmed_failures: [{ criterion: 'reviewer crashed', severity: 'high', evidence: 'no review result', fix: 're-run review' }], refuted_findings: [], low_findings: [], pytest_summary: '', test_count: 0, ownership_violations: [], mutation_check: '', passed_count: 0 }
    const serious = rev.failures.filter(f => f.severity !== 'low')
    const votes = await parallel(serious.map(f => () =>
      agent(refutePrompt(id, f), { label: `verify:${id}`, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' })
        .then(v => ({ f, refuted: !!(v && v.refuted), reason: v ? v.reason : 'verifier crashed' }))))
    const confirmed = votes.filter(Boolean).filter(v => !v.refuted).map(v => v.f)
    const refuted = votes.filter(Boolean).filter(v => v.refuted).map(v => ({ ...v.f, refute_reason: v.reason }))
    const low = rev.failures.filter(f => f.severity === 'low')
    return { wp: id, verdict: confirmed.length ? 'fix' : 'accept', pytest_summary: rev.pytest_summary, test_count: rev.test_count,
      confirmed_failures: confirmed, refuted_findings: refuted, low_findings: low,
      ownership_violations: rev.ownership_violations, mutation_check: rev.mutation_check, passed_count: rev.passed_criteria.length }
  })
return results.filter(Boolean)
