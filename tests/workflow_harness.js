// Test harness: run a Workflow script (workflows/*.js) under node with stubbed Workflow globals.
//   node tests/workflow_harness.js <script.js> '<args json>' '<stubs json>'
// stubs = { reviews: { WP01: <REVIEW_SCHEMA object> }, refuted: true|false }
// Prints one JSON line: { result, calls } or { error }. Exit 1 on error.
const fs = require('fs')
const [, , script, argsJson, stubsJson] = process.argv
const src = fs.readFileSync(script, 'utf8').replace(/^export const meta/m, 'const meta')
const args = JSON.parse(argsJson || '{}')
const stubs = JSON.parse(stubsJson || '{}')
const calls = []
const acceptReview = id => ({ wp: id, pytest_summary: '5 passed', test_count: 5, passed_criteria: ['a'], failures: [], mutation_check: 'caught', ownership_violations: [], verdict: 'accept' })
const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || ''
  calls.push(label)
  const id = label.split(':')[1]
  if (label.startsWith('impl:') || label.startsWith('fix:')) return { wp: id, pytest_summary: '5 passed', files_created_or_changed: [], unsatisfied: [], notes: '' }
  if (label.startsWith('review:')) return (stubs.reviews && stubs.reviews[id]) || acceptReview(id)
  if (label.startsWith('verify:')) return { refuted: !!stubs.refuted, reason: 'stub' }
  return 'ok'
}
const parallel = fns => Promise.all(fns.map(f => f()))
const log = () => {}
const phase = () => {}
const pipeline = (ids, first, second) => Promise.all(ids.map(async id => second(await first(id), id)))
const run = new Function('args', 'agent', 'parallel', 'log', 'phase', 'pipeline', `return (async () => {\n${src}\n})()`)
run(args, agent, parallel, log, phase, pipeline).then(
  result => { process.stdout.write(JSON.stringify({ result, calls }) + '\n') },
  e => { process.stdout.write(JSON.stringify({ error: String(e && e.message || e), calls }) + '\n'); process.exit(1) })
