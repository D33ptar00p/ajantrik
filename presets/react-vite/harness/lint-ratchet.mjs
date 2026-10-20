#!/usr/bin/env node
// Lint ratchet for the factory gate: fail only on lint problems a branch adds.
//
//   node .factory/harness/lint-ratchet.mjs <merge-base> <file>...      (run from the worktree root)
//
// Lints each changed file as it is on HEAD and as it was at <merge-base> (via stdin, so the
// base copy is never written to disk), with --max-warnings 0 semantics: errors and warnings both
// count. A file fails when, for any rule, HEAD has more problems than the base had. New files
// compare against zero. Prints each new problem with its location; exit 1 if any.
import { execFileSync, spawnSync } from 'node:child_process'
import path from 'node:path'

const [mergeBase, ...files] = process.argv.slice(2)
if (!mergeBase) {
  console.error('usage: lint-ratchet.mjs <merge-base> <file>...')
  process.exit(2)
}
const eslint = path.resolve('node_modules/.bin/eslint')

function lint(args, input) {
  const r = spawnSync(eslint, ['--format', 'json', '--no-error-on-unmatched-pattern', ...args], {
    input, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
  })
  if (r.status === 2 || !r.stdout) throw new Error(`eslint crashed: ${r.stderr}`)
  return JSON.parse(r.stdout)
}

function countByRule(messages) {
  const counts = new Map()
  for (const m of messages) counts.set(m.ruleId || 'fatal', (counts.get(m.ruleId || 'fatal') || 0) + 1)
  return counts
}

let added = 0
for (const file of files) {
  const head = lint([file])[0]?.messages || []
  let baseSource = null
  try {
    baseSource = execFileSync('git', ['show', `${mergeBase}:${file}`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] })
  } catch { /* new file */ }
  const base = baseSource === null ? [] : lint(['--stdin', '--stdin-filename', file], baseSource)[0]?.messages || []
  const baseCounts = countByRule(base)
  const headCounts = countByRule(head)
  for (const [rule, n] of headCounts) {
    const before = baseCounts.get(rule) || 0
    if (n <= before) continue
    added += n - before
    console.log(`${file}: ${rule} ${before} → ${n}`)
    for (const m of head.filter((m) => (m.ruleId || 'fatal') === rule)) {
      console.log(`  ${m.line}:${m.column}  ${m.severity === 2 ? 'error' : 'warning'}  ${m.message}`)
    }
  }
  const pre = base.length
  if (pre) console.log(`${file}: ${pre} existing problem(s) on the base, not counted`)
}
console.log(added ? `lint: ${added} new problem(s)` : 'lint: no new problems')
process.exit(added ? 1 : 0)
