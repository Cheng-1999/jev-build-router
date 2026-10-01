// Test harness: run a Workflow script (workflows/*.js) under node with stubbed Workflow globals.
//   node tests/workflow_harness.js <script.js> '<args json>' '<stubs json>'
// stubs = { reviews: { WP01: <REVIEW_SCHEMA object> | [<one per review call>, ...] }, refuted: true|false,
//           runs: { '<exact agent label>': <result> | [<one per call>, ...] } }   // e.g. a mocked engine_status
// A stub array is consumed one entry per call; its last entry repeats.
// Prints one JSON line: { result, calls, logs, required } or { error, calls, logs }. Exit 1 on error.
// required: { label: schema.required } of each agent call, to check which schema a step used.
const fs = require('fs')
const [, , script, argsJson, stubsJson] = process.argv
const src = fs.readFileSync(script, 'utf8').replace(/^export const meta/m, 'const meta')
const args = JSON.parse(argsJson || '{}')
const stubs = JSON.parse(stubsJson || '{}')
const calls = []
const logs = []
const required = {}
const next = v => (Array.isArray(v) ? (v.length > 1 ? v.shift() : v[0]) : v)
const acceptReview = id => ({ wp: id, pytest_summary: '5 passed', test_count: 5, passed_criteria: ['a'], failures: [], mutation_check: 'caught', ownership_violations: [], verdict: 'accept' })
const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || ''
  calls.push(label)
  if (opts && opts.schema) required[label] = opts.schema.required
  const id = label.split(':')[1]
  if (stubs.runs && label in stubs.runs) return next(stubs.runs[label])
  if (label.startsWith('impl:') || label.startsWith('fix:')) return { wp: id, pytest_summary: '5 passed', files_created_or_changed: [], unsatisfied: [], notes: '' }
  if (label.startsWith('review:')) return (stubs.reviews && stubs.reviews[id] && next(stubs.reviews[id])) || acceptReview(id)
  if (label.startsWith('verify:')) return { refuted: !!stubs.refuted, reason: 'stub' }
  return 'ok'
}
const parallel = fns => Promise.all(fns.map(f => f()))
const log = msg => { logs.push(String(msg)) }
const phase = () => {}
const pipeline = (ids, first, second) => Promise.all(ids.map(async id => second(await first(id), id)))
const run = new Function('args', 'agent', 'parallel', 'log', 'phase', 'pipeline', `return (async () => {\n${src}\n})()`)
run(args, agent, parallel, log, phase, pipeline).then(
  result => { process.stdout.write(JSON.stringify({ result, calls, logs, required }) + '\n') },
  e => { process.stdout.write(JSON.stringify({ error: String(e && e.message || e), calls, logs }) + '\n'); process.exit(1) })
