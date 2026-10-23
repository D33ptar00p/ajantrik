'use strict'
// Factory Studio. Edits .factory/config/ through the local API; nothing here talks to anything else.
// The working copy of factory.yaml (S.cfg) changes locally until Save. Agents and skills are files of
// their own and save from their editors.

const TOKEN = document.querySelector('meta[name="factory-token"]').content
const TOOLS = ['Read', 'Grep', 'Glob', 'Bash', 'Edit', 'Write', 'WebFetch', 'WebSearch']
const MODELS = ['', 'opus', 'sonnet', 'haiku']
const STAGE_KINDS = ['agent', 'approval', 'gate']
const KIND_HELP = {
  agent: 'One agent. What it does comes from the agent (its role); its phase is only the board column it shows in.',
  approval: 'The job waits here until a human approves; meanwhile it shows under Needs human on the board.',
  gate: 'Runs the guardrails you pick (Guardrails tab): secret scan, clean tree and your own checks. On failure the job goes back along its failure edge, or to the nearest builder before it, and uses up a round.',
}
// Phases are only board columns (loop.phases, in order); a phase's colour comes from its place in the list.
const PHASE_HUES = [210, 140, 28, 280, 350, 180, 55, 250, 100, 310]
const ROLES = ['plain', 'builder', 'reviewer', 'integrator']
const ROLE_HELP = {
  plain: 'Writes one file in the job folder.',
  builder: 'Owns code: implements its area, writes build-<id>.md, and gets gate and review findings back.',
  reviewer: 'Reviews the branch every round (PASS or CHANGES). CHANGES goes back along its failure edge.',
  integrator: 'Decides READY or ESCALATE before delivery and drafts the PR text. At most one.',
}
// Fields that belong to one role only; a role change drops the others (the server rejects them).
const ROLE_FIELDS = {
  plain: ['output', 'rerun_on_rework', 'read_builders'],
  builder: ['area_globs', 'description'],
  reviewer: ['chip', 'path_globs', 'exclude_globs', 'diff_patterns', 'scoped_patterns', 'metrics', 'builder', 'on_fail'],
  integrator: ['output'],
}
// Mirrors graph.role_for_agent / role_of: a stage's `role`, else what its agent's name says.
function roleForAgent(agent) {
  for (const r of ['builder', 'reviewer', 'integrator']) if (agent === r || (agent || '').startsWith(`${r}-`)) return r
  return 'plain'
}
function roleOf(s) {
  if (s.kind !== 'agent') return ''
  return s.role || roleForAgent(s.agent)
}
const REF = /^(env|keyring):[\w./-]+$/
const NAME = /^[a-z0-9][a-z0-9_-]*$/

const S = {
  server: null,     // last GET /api/state
  cfg: {},          // working copy of factory.yaml
  saved: '',        // JSON of cfg as last loaded/saved, for the dirty flag
  tab: 'board',
  pid: null,        // the open project; null on the projects page
  projects: [],     // last GET /api/projects
  agent: null,      // selected agent name
  skill: null,
  yamlText: null,   // Raw YAML tab draft
  issues: { errors: [], warnings: [] },
}

// ---- tiny DOM helpers ---------------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue
    if (k === 'class') el.className = v
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v)
    else if (k === 'value') el.value = v
    else if (k === 'checked') el.checked = !!v
    else if (k === 'html') el.innerHTML = v
    else el.setAttribute(k, v === true ? '' : v)
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)))
  }
  return el
}
const $ = (id) => document.getElementById(id)
const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)))
let uid = 0
const nextId = (p) => `${p}-${++uid}`

function toast(msg, bad = false) {
  const t = $('toast')
  t.textContent = msg
  t.className = 'toast show' + (bad ? ' bad' : '')
  clearTimeout(toast.timer)
  toast.timer = setTimeout(() => { t.className = 'toast' }, bad ? 6000 : 2600)
}

async function api(method, path, body) {
  const opts = { method, headers: { 'X-Factory-Token': TOKEN }, credentials: 'same-origin' }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  // project routes live under /api/p/<project>/; the projects list and presets are global
  const url = /^(projects|presets|workspace)\b/.test(path) ? `/api/${path}` : `/api/p/${encodeURIComponent(S.pid)}/${path}`
  const res = await fetch(url, opts)
  let data = null
  try { data = await res.json() } catch { data = null }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${method} ${path}: HTTP ${res.status}`)
    err.status = res.status
    throw err
  }
  return data
}

// ---- config paths: read through to the effective (defaulted) config, write into the working copy --
function getIn(obj, path) {
  let cur = obj
  for (const p of path) {
    if (cur === null || cur === undefined) return undefined
    cur = cur[p]
  }
  return cur
}
function val(path) {
  const v = getIn(S.cfg, path)
  return v !== undefined ? v : getIn(S.server && S.server.effective, path)
}
function setIn(path, value) {
  let cur = S.cfg
  for (let i = 0; i < path.length - 1; i++) {
    const p = path[i]
    if (cur[p] === undefined || cur[p] === null) {
      const eff = getIn(S.server && S.server.effective, path.slice(0, i + 1))
      cur[p] = Array.isArray(eff) ? stripNulls(clone(eff)) : (typeof path[i + 1] === 'number' ? [] : {})
    }
    cur = cur[p]
  }
  const last = path[path.length - 1]
  if (value === undefined) {
    if (Array.isArray(cur)) cur.splice(last, 1)
    else delete cur[last]
  } else cur[last] = value
  markDirty()
}
// The working list for an array path, materialised from the effective config on first edit.
function list(path) {
  const cur = getIn(S.cfg, path)
  if (Array.isArray(cur)) return cur
  const eff = getIn(S.server && S.server.effective, path)
  setIn(path, Array.isArray(eff) ? stripNulls(clone(eff)) : [])
  return getIn(S.cfg, path)
}
function stripNulls(x) {
  if (Array.isArray(x)) return x.map(stripNulls)
  if (x && typeof x === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(x)) if (v !== null) out[k] = stripNulls(v)
    return out
  }
  return x
}

function markDirty() {
  const dirty = JSON.stringify(S.cfg) !== S.saved
  $('dirty').hidden = !dirty
  $('save').disabled = !dirty
  $('revert').disabled = !dirty
}

// ---- form controls -------------------------------------------------------------------------------
function field(label, path, opt = {}) {
  const id = nextId('f')
  // opt.cur overrides the read-through value (the loop inspector reads stages from the working copy)
  const cur = 'cur' in opt ? opt.cur : val(path)
  let input
  const commit = (v) => {
    if (opt.validate) {
      const err = opt.validate(v)
      input.setCustomValidity(err || '')
      input.reportValidity()
      if (err) return
    }
    setIn(path, v)
    if (opt.rerender) render()
  }
  if (opt.type === 'select') {
    input = h('select', { id, onchange: (e) => commit(opt.parse ? opt.parse(e.target.value) : e.target.value) },
      opt.options.map((o) => {
        const [v, l] = Array.isArray(o) ? o : [o, o === '' ? '(none)' : o]
        return h('option', { value: v, selected: String(cur ?? '') === String(v) }, l)
      }))
  } else if (opt.type === 'bool') {
    input = h('input', { id, type: 'checkbox', checked: !!cur, onchange: (e) => commit(e.target.checked) })
    return h('div', { class: 'field check' }, input, h('label', { for: id }, label),
      opt.help ? h('div', { class: 'help' }, opt.help) : null)
  } else if (opt.type === 'textarea') {
    input = h('textarea', { id, rows: opt.rows || 4, class: opt.mono ? 'mono' : '', placeholder: opt.placeholder || '',
      onchange: (e) => commit(e.target.value) }, cur ?? '')
  } else if (opt.type === 'list') {
    // one item per line
    input = h('textarea', { id, rows: opt.rows || 3, class: 'mono', placeholder: opt.placeholder || 'one per line',
      onchange: (e) => commit(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)) },
    (cur || []).join('\n'))
  } else if (opt.type === 'number') {
    input = h('input', { id, type: 'number', value: cur ?? '', min: opt.min, max: opt.max, step: opt.step || 'any',
      onchange: (e) => commit(e.target.value === '' ? undefined : Number(e.target.value)) })
  } else {
    input = h('input', { id, type: 'text', value: cur ?? '', placeholder: opt.placeholder || '', spellcheck: 'false',
      autocomplete: 'off',
      onchange: (e) => commit(e.target.value === '' && opt.emptyUndefined !== false ? (opt.emptyNull ? null : undefined) : e.target.value) })
  }
  return h('div', { class: 'field' + (opt.wide ? ' wide' : '') }, h('label', { for: id }, label), input,
    opt.help ? h('div', { class: 'help' }, opt.help) : null)
}
const refCheck = (v) => (v && !REF.test(v) ? 'Use a reference like env:JIRA_TOKEN or keyring:factory/jira, never the secret itself.' : '')
function refField(label, path, help) {
  return field(label, path, { validate: refCheck, placeholder: 'env:NAME or keyring:service/name',
    help: help || 'A reference, never the value. The factory reads it at run time.' })
}

function section(title, ...kids) {
  return h('section', { class: 'card' }, h('h2', {}, title), ...kids)
}
function grid(...kids) { return h('div', { class: 'grid' }, ...kids) }

function moveBtns(arr, i, rerender = render) {
  const mv = (d) => () => {
    const j = i + d
    if (j < 0 || j >= arr.length) return
    ;[arr[i], arr[j]] = [arr[j], arr[i]]
    markDirty()
    rerender()
  }
  return h('span', { class: 'move' },
    h('button', { type: 'button', class: 'icon', title: 'Move up', 'aria-label': 'Move up', disabled: i === 0, onclick: mv(-1) }, '↑'),
    h('button', { type: 'button', class: 'icon', title: 'Move down', 'aria-label': 'Move down', disabled: i === arr.length - 1, onclick: mv(1) }, '↓'))
}
function removeBtn(arr, i, what, rerender = render) {
  return h('button', { type: 'button', class: 'icon danger', title: `Remove ${what}`, 'aria-label': `Remove ${what}`,
    onclick: () => { arr.splice(i, 1); markDirty(); rerender() } }, '✕')
}

// Drag to reorder (the arrow buttons do the same from the keyboard).
function draggable(el, arr, i) {
  el.draggable = true
  el.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(i)); el.classList.add('dragging') })
  el.addEventListener('dragend', () => el.classList.remove('dragging'))
  el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('over') })
  el.addEventListener('dragleave', () => el.classList.remove('over'))
  el.addEventListener('drop', (e) => {
    e.preventDefault()
    el.classList.remove('over')
    const from = Number(e.dataTransfer.getData('text/plain'))
    if (Number.isNaN(from) || from === i) return
    const [x] = arr.splice(from, 1)
    arr.splice(i, 0, x)
    markDirty()
    render()
  })
  return el
}

// ---- markdown editor: one pane, toggled between the source (default) and a preview -------------
function mdEditor(id, label, d) {
  const ta = h('textarea', { id, class: 'mono body', rows: 24, oninput: (e) => { d.body = e.target.value } }, d.body)
  const preview = h('div', { class: 'md preview', 'aria-label': `${label} preview`, hidden: true })
  const tabs = {}
  const show = (mode) => {
    const md = mode === 'markdown'
    if (!md) preview.innerHTML = markdown(d.body)
    ta.hidden = !md
    preview.hidden = md
    for (const [k, b] of Object.entries(tabs)) {
      b.classList.toggle('on', k === mode)
      b.setAttribute('aria-pressed', k === mode ? 'true' : 'false')
    }
  }
  tabs.markdown = h('button', { type: 'button', onclick: () => { show('markdown'); ta.focus() } }, 'Markdown')
  tabs.preview = h('button', { type: 'button', onclick: () => show('preview') }, 'Preview')
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': `${label} view` }, tabs.markdown, tabs.preview)
  show('markdown')
  return h('div', { class: 'field wide md-editor' },
    h('div', { class: 'md-head' }, h('label', { for: id }, label), toggle), ta, preview)
}

// ---- markdown preview (headings, lists, code, emphasis; everything escaped first) ----------------
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
}
function markdown(src) {
  const lines = String(src || '').split('\n')
  let out = '', i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (/^```/.test(line)) {
      const body = []
      i++
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++])
      i++
      out += `<pre><code>${esc(body.join('\n'))}</code></pre>`
      continue
    }
    const hm = line.match(/^(#{1,4})\s+(.*)$/)
    if (hm) { out += `<h${hm[1].length + 2}>${inline(hm[2])}</h${hm[1].length + 2}>`; i++; continue }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line)
      const items = []
      while (i < lines.length) {
        if (/^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ''))
        // an indented line under an item continues it (a wrapped bullet)
        else if (items.length && /^\s+\S/.test(lines[i])) items[items.length - 1] += ' ' + lines[i++].trim()
        else break
      }
      out += (ordered ? '<ol>' : '<ul>') + items.map((x) => `<li>${inline(x)}</li>`).join('') + (ordered ? '</ol>' : '</ul>')
      continue
    }
    if (/^\|/.test(line)) {
      const rows = []
      while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++])
      const cells = (r) => r.replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
      const body = rows.filter((r) => !/^\|[\s|:-]+\|$/.test(r))
      out += '<table>' + body.map((r, k) => `<tr>${cells(r).map((c) => (k ? `<td>${inline(c)}</td>` : `<th>${inline(c)}</th>`)).join('')}</tr>`).join('') + '</table>'
      continue
    }
    if (!line.trim()) { i++; continue }
    const para = []
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s|\|)/.test(lines[i])) para.push(lines[i++])
    out += `<p>${inline(para.join(' '))}</p>`
  }
  return out
}

// ---- tabs ----------------------------------------------------------------------------------------
// Line icons (24px grid, stroked), drawn inline so the page needs nothing from outside.
const ICONS = {
  projects: ['M3 3h7v7H3z', 'M14 3h7v7h-7z', 'M3 14h7v7H3z', 'M14 14h7v7h-7z'],
  board: ['M3 4h5v16H3z', 'M10 4h5v10h-5z', 'M17 4h4v7h-4z'],
  backlog: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
  loop: ['M21 12a9 9 0 1 1-2.64-6.36', 'M21 3v6h-6'],
  agents: ['M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4 21v-1a7 7 0 0 1 16 0v1'],
  skills: ['M4 19.5A2.5 2.5 0 0 1 6.5 17H20', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z'],
  guardrails: ['M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', 'M9 12l2 2 4-4'],
  connections: ['M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71',
    'M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'],
  delivery: ['M22 2L11 13', 'M22 2l-7 20-4-9-9-4 20-7z'],
  runtime: ['M4 4h16v12H4z', 'M8 20h8', 'M12 16v4', 'M8 9l2 2-2 2', 'M13 13h3'],
  yaml: ['M16 18l6-6-6-6', 'M8 6l-6 6 6 6'],
}
function icon(name) {
  const NS = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(NS, 'svg')
  for (const [k, v] of Object.entries({ viewBox: '0 0 24 24', width: '20', height: '20', fill: 'none',
    stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', class: 'ri' })) svg.setAttribute(k, v)
  for (const d of ICONS[name] || []) {
    const path = document.createElementNS(NS, 'path')
    path.setAttribute('d', d)
    svg.appendChild(path)
  }
  return svg
}

// [key, label, render, short rail label]
const TABS = [
  ['board', 'Board', null, 'Board'],
  ['backlog', 'Backlog', renderBacklog, 'Backlog'],
  ['loop', 'Loop', renderLoop, 'Loop'],
  ['agents', 'Agents', renderAgents, 'Agents'],
  ['skills', 'Skills', renderSkills, 'Skills'],
  ['guardrails', 'Guardrails', renderGuardrails, 'Guards'],
  ['connections', 'Connections', renderConnections, 'Connect'],
  ['delivery', 'Delivery', renderDelivery, 'Deliver'],
  ['runtime', 'Runtime', renderRuntime, 'Runtime'],
  ['yaml', 'Raw YAML', renderYaml, 'YAML'],
]

function selectTab(key) {
  S.tab = key
  try { localStorage.setItem(`studio.tab.${S.pid}`, key) } catch { /* private mode */ }
  history.replaceState(null, '', `#/p/${encodeURIComponent(S.pid)}/${key}`)
  render()
}

function renderTabs() {
  const nav = $('tabs')
  const home = h('button', { type: 'button', class: 'home-btn', title: 'All projects', 'aria-label': 'All projects',
    onclick: () => { location.hash = '#/' } }, icon('projects'), h('span', { class: 'rl' }, 'Projects'))
  nav.replaceChildren(home, h('hr', { class: 'rail-sep' }), ...TABS.map(([key, label, , short], idx) => h('button', {
    type: 'button', role: 'tab', id: `tab-${key}`, 'aria-selected': S.tab === key ? 'true' : 'false',
    'aria-controls': key === 'board' ? 'boardpane' : 'view', 'aria-label': label, title: label,
    tabindex: S.tab === key ? '0' : '-1', class: S.tab === key ? 'active' : '',
    onclick: () => selectTab(key),
    onkeydown: (e) => {
      const d = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0
      if (!d) return
      e.preventDefault()
      const next = TABS[(idx + d + TABS.length) % TABS.length][0]
      selectTab(next)
      $(`tab-${next}`).focus()
    },
  }, icon(key), h('span', { class: 'rl' }, short))))
}

function renderMessages() {
  const { errors, warnings } = S.issues
  $('messages').replaceChildren(
    ...errors.map((e) => h('div', { class: 'msg error' }, h('b', {}, 'Error '), e)),
    ...warnings.map((w) => h('div', { class: 'msg warn' }, h('b', {}, 'Warning '), w)))
}

// Every option is a full-screen view; the board stays loaded (hidden) so it never reloads.
function render() {
  renderTabs()
  renderMessages()
  const onBoard = S.tab === 'board'
  $('boardpane').hidden = !onBoard
  $('view').hidden = onBoard
  if (onBoard) { pollJobs(); return }
  const [, label, fn] = TABS.find(([k]) => k === S.tab) || TABS[2]
  $('view-title').textContent = label
  const view = $('view')
  const scroll = S.lastTab === S.tab ? view.scrollTop : 0
  $('main').replaceChildren(fn())
  $('main').setAttribute('aria-labelledby', `tab-${S.tab}`)
  view.scrollTop = scroll
  S.lastTab = S.tab
}

// ---- Board: new job dialog, and the jobs waiting on you ------------------------------------------
let jobsTimer = null
let lastJobs = []
function pollJobs() {
  clearInterval(jobsTimer)
  const tick = async () => {
    if (S.tab !== 'board') { clearInterval(jobsTimer); return }
    try {
      lastJobs = await api('GET', 'jobs')
      const n = waitingJobs(lastJobs).length
      $('waiting-n').textContent = n ? String(n) : ''
      $('waiting').classList.toggle('has', n > 0)
      if ($('dlg-waiting').open) renderJobs(lastJobs)
    } catch { /* the board shows its own connection status */ }
  }
  tick()
  jobsTimer = setInterval(tick, 4000)
}

function waitingJobs(jobs) {
  return jobs.filter((j) => /^awaiting-|^ready$|^needs-human$/.test(j.stage || ''))
}

function openNewJob() {
  const kind = val(['runtime', 'kind'])
  const interactive = kind === 'claude-code'
  const tickets = val(['connections', 'tickets', 'provider']) && val(['connections', 'tickets', 'provider']) !== 'none'
  const req = h('textarea', { id: 'nj-req', rows: 4, placeholder: 'What should the factory build?', required: true })
  const ticket = h('input', { id: 'nj-ticket', type: 'text', placeholder: tickets ? 'ABC-123' : 'none configured', autocomplete: 'off', disabled: !tickets })
  const base = h('input', { id: 'nj-base', type: 'text', placeholder: val(['repo', 'default_base']) || 'main', autocomplete: 'off' })
  const dlg = $('dlg-new')
  const submit = h('button', { type: 'submit', class: 'primary' }, interactive ? 'Copy /factory command' : 'Start job')
  const form = h('form', { method: 'dialog', class: 'dlg', onsubmit: async (e) => {
    e.preventDefault()
    if (!req.value.trim() && !ticket.value.trim()) { req.focus(); return }
    if (interactive) {
      const arg = ticket.value.trim() || `"${req.value.trim().replace(/"/g, '\\"')}"`
      const cmd = `/factory ${arg}${base.value.trim() ? ` --base ${base.value.trim()}` : ''}`
      try { await navigator.clipboard.writeText(cmd); toast(`Copied: ${cmd}`) } catch { toast(cmd) }
      dlg.close()
      return
    }
    submit.disabled = true
    try {
      const r = await api('POST', 'jobs', { request: req.value, ticket: ticket.value, base: base.value })
      toast(`Started: log in ${r.log}`)
      dlg.close()
    } catch (err) { toast(err.message, true) }
    submit.disabled = false
  } },
  h('header', { class: 'dlg-head' }, h('h2', {}, 'New job'),
    h('button', { type: 'button', class: 'ghost icon', 'aria-label': 'Close', onclick: () => dlg.close() }, '×')),
  h('div', { class: 'field' }, h('label', { for: 'nj-req' }, 'Request'), req,
    h('p', { class: 'help' }, 'Plain words, as you would write a ticket. With a ticket key, the ticket text is used.')),
  h('div', { class: 'grid' },
    h('div', { class: 'field' }, h('label', { for: 'nj-ticket' }, 'Ticket'), ticket),
    h('div', { class: 'field' }, h('label', { for: 'nj-base' }, 'Base branch'), base)),
  interactive ? h('p', { class: 'msg info' }, 'The runtime is interactive Claude Code: this copies the /factory command to paste into a Claude Code session in the repo. Pick claude-cli, codex-cli or api under Runtime to start jobs from here.') : null,
  h('div', { class: 'row end' }, h('button', { type: 'button', onclick: () => dlg.close() }, 'Cancel'), submit))
  dlg.replaceChildren(form)
  dlg.showModal()
  req.focus()
}

function openWaiting() {
  renderJobs(lastJobs)
  $('dlg-waiting').showModal()
}

function renderJobs(jobs) {
  const dlg = $('dlg-waiting')
  const waiting = waitingJobs(jobs)
  const head = h('header', { class: 'dlg-head' }, h('h2', {}, 'Waiting on you'),
    h('button', { type: 'button', class: 'ghost icon', 'aria-label': 'Close', onclick: () => dlg.close() }, '×'))
  if (!waiting.length) {
    dlg.replaceChildren(h('div', { class: 'dlg' }, head,
      h('p', { class: 'help' }, jobs.length ? `${jobs.length} jobs, none waiting on you.` : 'No jobs yet.')))
    return
  }
  const act = (label, fn, primary) => h('button', { type: 'button', class: primary ? 'primary small' : 'small', onclick: async (e) => {
    e.target.disabled = true
    try { const r = await fn(); toast(r && r.error ? r.error : `${label}: done`, !!(r && r.error)) } catch (err) { toast(err.message, true) }
    e.target.disabled = false
  } }, label)
  const rows = waiting.map((j) => {
    const stage = j.stage || ''
    const awaiting = stage.startsWith('awaiting-') ? stage.slice('awaiting-'.length) : null
    const isDelivery = stage === 'awaiting-deliver'  // delivery follows the last stage on its own
    const btns = []
    if (val(['runtime', 'kind']) === 'claude-code') {
      // The interactive session is the orchestrator: the slash command is the approval.
      const c = awaiting && !isDelivery ? `/factory go ${j.id}` : (isDelivery || stage === 'ready') ? `/factory deliver ${j.id}` : `/factory rework ${j.id} "<note>"`
      btns.push(act(`Copy ${c.split(' ').slice(0, 2).join(' ')}`, async () => { await navigator.clipboard.writeText(c); return null }, true))
    } else {
      if (awaiting && !isDelivery) {
        btns.push(act('Approve', () => api('POST', `jobs/${j.id}/approve`, { stage: awaiting })))
        btns.push(act('Go', () => api('POST', `jobs/${j.id}/go`, {}), true))
      }
      if (isDelivery) btns.push(act('Approve & deliver', () => api('POST', `jobs/${j.id}/deliver`, {}), true))
      if (stage === 'ready' && val(['delivery', 'mode']) !== 'none') btns.push(act('Deliver', () => api('POST', `jobs/${j.id}/deliver`, {}), true))
    }
    return h('tr', {}, h('td', {}, h('code', {}, j.id), h('div', { class: 'help' }, j.feature || '')),
      h('td', {}, stage), h('td', {}, j.note || ''), h('td', { class: 'acts' }, btns))
  })
  dlg.replaceChildren(h('div', { class: 'dlg wide' }, head, h('table', { class: 'jobtable' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Job'), h('th', {}, 'Stage'), h('th', {}, 'Note'), h('th', {}, ''))),
    h('tbody', {}, rows))))
}

// ---- Backlog: add, edit and delete items, and start one job from the queued items picked ---------
// The list lives in .factory/backlog.md. The server identifies an item by its index plus the raw line the
// page last saw, so a change made elsewhere comes back as a 409 and the list reloads.
const BACKLOG_INTERACTIVE = 'The runtime is interactive Claude Code. Pick claude-cli, codex-cli or api under Runtime to start jobs from here.'

function backlogState() {
  if (!S.backlog || S.backlog.pid !== S.pid) {
    S.backlog = { pid: S.pid, items: null, headless: false, jira: { connected: false, push: false, project: null },
      report: null, error: null, loading: false, sel: new Set(),
      edit: null, editLine: '', draft: { text: '', key: '', base: '' },
      editDraft: { text: '', key: '', base: '', state: 'queued' }, busy: false }
  }
  return S.backlog
}

async function loadBacklog() {
  const B = backlogState()
  if (B.loading) return
  B.loading = true
  try {
    const r = await api('GET', 'backlog')
    B.items = r.items
    B.headless = r.headless
    B.jira = r.jira || { connected: false, push: false, project: null }
    B.error = null
    B.sel = new Set()
    const open = B.edit === null ? null : B.items.find((it) => it.index === B.edit)
    if (!open || open.line !== B.editLine) B.edit = null
  } catch (err) { B.error = err.message }
  B.loading = false
  if (S.tab === 'backlog') render()
}

// One write: the response replaces the list. A failure keeps the drafts; a 409 also reloads.
async function backlogCall(method, path, body, done) {
  const B = backlogState()
  if (B.busy) return
  B.busy = true
  render()
  try {
    const r = await api(method, path, body)
    B.items = r.items
    B.sel = new Set()
    B.report = null
    if (done) done(r)
  } catch (err) {
    toast(err.message, true)
    if (err.status === 409) await loadBacklog()
  }
  B.busy = false
  if (S.tab === 'backlog') render()
}

function backlogInput(id, label, value, onInput, attrs) {
  return h('div', { class: 'field' }, h('label', { for: id }, label),
    h('input', { id, type: 'text', autocomplete: 'off', value, oninput: (e) => onInput(e.target.value), ...attrs }))
}

function renderBacklog() {
  const B = backlogState()
  if (!B.loading && ((B.items === null && !B.error) || S.lastTab !== 'backlog')) loadBacklog()
  const retry = h('button', { type: 'button', class: 'small', onclick: () => { B.error = null; loadBacklog(); render() } }, 'Retry')
  if (B.items === null) {
    return section('Backlog', B.error
      ? h('div', { class: 'msg error' }, `Could not load the backlog: ${B.error} `, retry)
      : h('p', { class: 'help' }, 'Loading backlog…'))
  }
  const add = h('form', { onsubmit: (e) => {
    e.preventDefault()
    backlogCall('POST', 'backlog', { ...B.draft }, () => { B.draft = { text: '', key: '', base: '' } })
  } },
  h('div', { class: 'grid' },
    backlogInput('bl-text', 'Request', B.draft.text, (v) => { B.draft.text = v }, { required: true, placeholder: 'What should the factory build?' }),
    backlogInput('bl-key', 'Ticket (optional)', B.draft.key, (v) => { B.draft.key = v }, { placeholder: 'ABC-123' }),
    backlogInput('bl-base', 'Base branch (optional)', B.draft.base, (v) => { B.draft.base = v }, { placeholder: val(['repo', 'default_base']) || 'main' })),
  h('div', { class: 'row end' }, h('button', { type: 'submit', class: 'primary', disabled: B.busy }, 'Add item')))

  const cell = (...kids) => h('td', {}, ...kids)
  const viewRow = (it) => h('tr', {},
    cell(h('input', { type: 'checkbox', 'aria-label': `Select item ${it.index + 1}`, checked: B.sel.has(it.index),
      disabled: it.state !== 'queued' || B.busy, onchange: (e) => {
        if (e.target.checked) B.sel.add(it.index)
        else B.sel.delete(it.index)
        render()
      } })),
    cell(h('span', { class: `bstate ${it.state}` }, it.state)),
    cell(it.key || ''), cell(it.base || ''), cell(it.text),
    h('td', { class: 'acts' },
      it.state !== 'done' ? h('button', { type: 'button', class: 'small', disabled: B.busy, onclick: () => {
        B.edit = it.index
        B.editLine = it.line
        B.editDraft = { text: it.text, key: it.key || '', base: it.base || '', state: it.state }
        render()
      } }, 'Edit') : null,
      h('button', { type: 'button', class: 'small', disabled: B.busy, onclick: () => {
        if (!confirm(`Delete this backlog item?\n\n${it.text}`)) return
        backlogCall('DELETE', `backlog/${it.index}`, { line: it.line }, () => { B.edit = null })
      } }, 'Delete')))
  const save = (it) => backlogCall('PUT', `backlog/${it.index}`, { line: B.editLine, ...B.editDraft }, () => { B.edit = null })
  const editRow = (it) => {
    const d = B.editDraft
    const box = (label, k) => h('input', { type: 'text', autocomplete: 'off', 'aria-label': label, value: d[k],
      oninput: (e) => { d[k] = e.target.value }, onkeydown: (e) => { if (e.key === 'Enter') save(it) } })
    return h('tr', {},
      cell(),
      cell(h('select', { 'aria-label': 'State', onchange: (e) => { d.state = e.target.value } },
        ['queued', 'blocked'].map((s) => h('option', { value: s, selected: d.state === s }, s)))),
      cell(box('Ticket', 'key')), cell(box('Base branch', 'base')), cell(box('Request', 'text')),
      h('td', { class: 'acts' },
        h('button', { type: 'button', class: 'small primary', disabled: B.busy, onclick: () => save(it) }, 'Save'),
        h('button', { type: 'button', class: 'small', onclick: () => { B.edit = null; render() } }, 'Cancel')))
  }
  const picked = B.items.filter((it) => B.sel.has(it.index))
  const start = h('button', { type: 'button', class: 'primary', disabled: !B.headless || !picked.length || B.busy, onclick: async () => {
    if (B.busy) return
    B.busy = true
    render()
    let reload = false
    try {
      const r = await api('POST', 'backlog/run', { items: picked.map(({ index, line }) => ({ index, line })) })
      toast(`Started: log in ${r.log}`)
      reload = true
    } catch (err) {
      toast(err.message, true)
      reload = err.status === 409
    }
    B.busy = false
    if (reload) await loadBacklog()
    else if (S.tab === 'backlog') render()
  } }, 'Start job')
  // Two-way: queued items without a ticket become Jira issues (rest mode only), then new tickets come in.
  const sync = B.jira.connected ? h('button', { type: 'button', disabled: B.busy, onclick: () => {
    const fresh = B.items.filter((it) => it.state === 'queued' && !it.key).length
    const ask = B.jira.push && fresh
      ? `Create ${fresh} Jira issue(s) in ${B.jira.project} for the queued items without a ticket, then pull new tickets from Jira?`
      : 'Pull new tickets from Jira into the backlog?'
    if (!confirm(ask)) return
    backlogCall('POST', 'backlog/sync', {}, (r) => { B.report = r.report })
  } }, B.busy ? 'Working…' : 'Sync with Jira') : null
  return section('Backlog',
    B.error ? h('div', { class: 'msg error' }, `Could not refresh the backlog: ${B.error} `, retry) : null,
    h('p', { class: 'help' }, 'One item per line in .factory/backlog.md. Tick queued items and press Start job: they become one job, their requests joined with "; ". They must share one ticket and one base branch. Editing a blocked item drops its reason.'),
    B.jira.connected ? h('p', { class: 'help' }, B.jira.push
      ? `Sync with Jira creates an issue in ${B.jira.project} for each queued item without a ticket and writes its key into the line, then pulls new tickets from Jira.`
      : 'Sync with Jira pulls new tickets only: creating issues needs the rest mode and a project under Connections.') : null,
    B.headless ? null : h('p', { class: 'msg info' }, BACKLOG_INTERACTIVE),
    B.report ? h('div', { class: 'msg info' }, h('b', {}, 'Sync: '),
      B.report.length ? h('ul', {}, B.report.map((ln) => h('li', {}, ln))) : 'nothing new.') : null,
    add,
    B.items.length ? h('table', { class: 'jobtable backlog' },
      h('thead', {}, h('tr', {}, ['', 'State', 'Ticket', 'Base', 'Request', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, B.items.map((it) => (it.index === B.edit ? editRow(it) : viewRow(it)))))
      : h('p', { class: 'help' }, 'No backlog items yet.'),
    h('div', { class: 'row end' }, sync, start))
}

// ---- Loop designer: a free-form graph of stages ---------------------------------------------------
// Each stage is a node you can drag anywhere (its position is saved as `pos`). The solid edge out of
// a node is its success edge (`next`); gates and reviewer agents also have a failure edge (`on_fail`)
// back into the round loop. A job starts at the start node and follows success edges; nodes off that
// path never run. After every edit the list is re-sorted into run order and `next` is written only
// where it differs from the list order, so the YAML stays as plain as the graph allows. Agents and
// gates sit in one of the four fixed phases (an agent's phase is also what it does); approvals have
// none. Delivery is shown as one Deliver block: a view of the `delivery` settings (the same ones as the
// Deliver tab) that always follows the last stage. It has no ports and is not a stage.
function stageLabel(s) { return s.title || s.id }
const NODE_W = 184
const NODE_H = 84
const KIND_LABEL = { agent: 'Agent', approval: 'Approval', gate: 'Guardrails' }
const RESERVED_IDS = ['ready', 'needs-human', 'abandoned', 'delivered', 'end', 'deliver']
const DELIVER_ID = 'deliver'
const hasDeliver = () => (val(['delivery', 'mode']) || 'none') !== 'none'
// The working copy, not val(): a removed pos must not come back from the server's effective config.
const deliverPos = () => getIn(S.cfg, ['delivery', 'pos']) || S.designer.auto[DELIVER_ID]
function deliverSummary() {
  return `${val(['delivery', 'mode'])}${val(['delivery', 'require_approval']) ? ' · after approval' : ''}`
}
const hasFail = (s) => s.kind === 'gate' || roleOf(s) === 'reviewer'
const defaultAgent = (s) => ({ builder: `builder-${s.id}`, reviewer: `reviewer-${s.id}` })[roleOf(s)] || null
// Where a failure goes when on_fail is unset: the nearest builder before the stage on the run path.
function failTarget(stages, order, s) {
  if (s.on_fail) return s.on_fail
  const i = order.indexOf(s.id)
  for (let j = i - 1; j >= 0; j--) {
    const b = stages.find((x) => x.id === order[j])
    if (b && roleOf(b) === 'builder') return b.id
  }
  return null
}
// The phase a stage shows in (schema.Loop): agents default to the first phase, guardrails blocks to guardrails, else review (or the
// last phase), approvals have none.
const phaseIds = () => (val(['loop', 'phases']) || []).map((p) => p.id)
function phaseOf(s) {
  if (s.kind === 'approval') return null
  if (s.phase) return s.phase
  const ids = phaseIds()
  if (s.kind === 'gate') return ['guardrails', 'review'].find((p) => ids.includes(p)) || ids[ids.length - 1] || null
  return ids[0] || null
}
const phaseHue = (pid) => { const i = phaseIds().indexOf(pid); return i < 0 ? null : PHASE_HUES[i % PHASE_HUES.length] }
const phaseTitleOf = (name) => name.charAt(0).toUpperCase() + name.slice(1).replace(/-/g, ' ')

// Success edges, mirroring core/graph.py. `next` is unset (the next stage in the list), `end`, one
// stage id, or a list of ids: a fan-out whose stages run at the same time and join again after.
function successorsOf(stages, sid) {
  const i = stages.findIndex((s) => s.id === sid)
  if (i < 0) return []
  const n = stages[i].next
  if (n === 'end') return []
  if (Array.isArray(n)) return n.filter((x) => x !== 'end').map(String)
  if (n) return [n]
  return i + 1 < stages.length ? [stages[i + 1].id] : []
}
const asNext = (ids) => (ids.length > 1 ? [...ids] : ids[0] || 'end')
const sameIds = (a, b) => a.length === b.length && a.every((x, i) => x === b[i])
// graph.groups: the run path as steps (a fan-out is one step). The server rejects a broken path; here
// the walk just stops where the rules break and says why in `.error`, so the designer still draws.
function groupsOf(stages, start) {
  const out = []
  out.error = null
  if (!stages.length) return out
  const byId = Object.fromEntries(stages.map((s) => [s.id, s]))
  const seen = new Set()
  let cur = [start || stages[0].id]
  while (cur.length) {
    const bad = cur.find((id) => !byId[id] || seen.has(id))
    if (bad) { out.error = byId[bad] ? `the success edges loop back to ${bad}` : `no stage ${bad}`; break }
    cur.forEach((id) => seen.add(id))
    out.push(cur)
    if (cur.length > 1) {
      const notAgent = cur.find((id) => byId[id].kind !== 'agent')
      if (notAgent) { out.error = `${notAgent} is a parallel branch: only agents run in parallel`; break }
      const joins = cur.map((id) => successorsOf(stages, id))
      if (joins.some((j) => !sameIds(j, joins[0]))) { out.error = `parallel stages ${cur.join(', ')} must all lead to the same next stage`; break }
      cur = joins[0]
    } else cur = successorsOf(stages, cur[0])
  }
  return out
}
const runOrderOf = (stages, start) => groupsOf(stages, start).flat()
const G = () => {
  const stages = list(['loop', 'stages'])
  const groups = groupsOf(stages, val(['loop', 'start']))
  return { stages, groups, order: groups.flat() }
}
// Would these success edges loop? (Going back is what a failure edge is for.)
function cyclic(succ) {
  const state = {}
  const visit = (id) => {
    if (state[id] === 1) return true
    if (state[id] === 2) return false
    state[id] = 1
    for (const n of succ[id] || []) if (visit(n)) return true
    state[id] = 2
    return false
  }
  return Object.keys(succ).some((id) => visit(id))
}
// Re-sort into run order, then drop `next` wherever the list order already says it. A fan-out always
// keeps its list.
function normalizeLoop() {
  const stages = list(['loop', 'stages'])
  const start = val(['loop', 'start'])
  const succ = Object.fromEntries(stages.map((s) => [s.id, successorsOf(stages, s.id)]))
  const order = runOrderOf(stages, start)
  const sorted = [...order.map((id) => stages.find((s) => s.id === id)), ...stages.filter((s) => !order.includes(s.id))]
  sorted.forEach((s, i) => {
    const implied = i + 1 < sorted.length ? sorted[i + 1].id : null
    const actual = succ[s.id]
    if (actual.length > 1) s.next = [...actual]
    else if ((actual[0] || null) === implied) delete s.next
    else s.next = actual[0] || 'end'
  })
  stages.splice(0, stages.length, ...sorted)
  if (S.cfg.loop) delete S.cfg.loop.start
  markDirty()
}
// Write every stage's successors explicitly (so moving one in the list re-routes nothing), then tidy.
function applySucc(stages, succ) {
  for (const s of stages) s.next = asNext(succ[s.id] || [])
  normalizeLoop()
}
function setNext(stages, sid, targets) {
  const succ = Object.fromEntries(stages.map((s) => [s.id, successorsOf(stages, s.id)]))
  succ[sid] = targets || []
  applySucc(stages, succ)
}

// Positions: a stage's saved `pos`, else an automatic one kept out of the config (so just looking
// at the loop never makes it "unsaved"). Moving any node saves every position.
// One column per phase, plan → build → review → integrate; an approval gets a column of its own just
// after the phase of the stage before it on the run path; the branches of a fan-out share the column
// of the first. Stages in one column stack top to bottom.
function autoLayout(all) {
  const D = S.designer
  const { stages, order, groups } = G()
  const place = (s, x, y) => { if (all) s.pos = { x, y }; else D.auto[s.id] = { x, y } }
  const slot = {}
  let last = -1
  for (const g of groups) {
    const s = stages.find((x) => x.id === g[0])
    if (s.kind === 'approval') { slot[s.id] = `${last + 0.5}`; continue }
    last = Math.max(0, phaseIds().indexOf(phaseOf(s)))
    for (const id of g) slot[id] = `${last}`
  }
  const slots = [...new Set(Object.values(slot))].sort((a, b) => a - b)
  const cols = slots.map(() => [])
  for (const id of order) cols[slots.indexOf(slot[id])].push(stages.find((x) => x.id === id))
  let deepest = 0
  cols.forEach((col, c) => col.forEach((s, r) => {
    deepest = Math.max(deepest, r + 1)
    if (!all && s.pos) return
    place(s, 40 + c * (NODE_W + 110), 50 + r * (NODE_H + 48))  // wide gaps: failure edges run in them
  }))
  stages.filter((s) => !order.includes(s.id)).forEach((s, j) => {
    if (!all && s.pos) return
    place(s, 40 + j * (NODE_W + 40), 50 + deepest * (NODE_H + 48) + 120)
  })
  // the Deliver block gets the column after the last one of the run path
  if (hasDeliver()) {
    const p = { x: 40 + slots.length * (NODE_W + 110), y: 50 }
    if (all) setIn(['delivery', 'pos'], p)
    else if (!getIn(S.cfg, ['delivery', 'pos'])) D.auto[DELIVER_ID] = p
  }
}
const posOf = (s) => s.pos || S.designer.auto[s.id]
function savePositions() {
  for (const s of list(['loop', 'stages'])) if (!s.pos && S.designer.auto[s.id]) s.pos = { ...S.designer.auto[s.id] }
  if (hasDeliver() && !getIn(S.cfg, ['delivery', 'pos']) && S.designer.auto[DELIVER_ID]) setIn(['delivery', 'pos'], { ...S.designer.auto[DELIVER_ID] })
}

function nodeSummary(s) {
  if (s.kind === 'agent') return `${roleOf(s)} · ${s.agent || defaultAgent(s) || '(no agent)'}`
  if (s.kind === 'gate') {
    const n = (s.steps || []).length
    return n ? `${n} guardrail${n === 1 ? '' : 's'}` : 'all guardrails'
  }
  if (s.kind === 'approval') return s.note || 'waits for a human'
  return ''
}

function renderLoop() {
  S.designer = S.designer || { x: 0, y: 0, k: 1, sel: null }
  const D = S.designer
  D.auto = {}
  D.width = D.width || Math.max(640, window.innerWidth - 470)  // canvas width: window minus rail and inspector
  autoLayout(false)

  const svgNS = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(svgNS, 'svg')
  svg.setAttribute('class', 'edges')
  svg.innerHTML = `<defs>
    <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah"/></marker>
    <marker id="arr-fail" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah-fail"/></marker>
  </defs>`
  const edgeLayer = document.createElementNS(svgNS, 'g')
  svg.appendChild(edgeLayer)
  const layer = h('div', { class: 'layer' })
  const canvas = h('div', { class: 'canvas', tabindex: '0', 'aria-label': 'Loop graph. Drag nodes to move them; drag from a port to connect.' }, h('div', { class: 'world' }, svg, layer))
  const world = canvas.firstChild
  const applyView = () => { world.style.transform = `translate(${D.x}px, ${D.y}px) scale(${D.k})` }
  applyView()
  const inspector = h('aside', { class: 'inspector' })
  const toWorld = (cx, cy) => {
    const r = canvas.getBoundingClientRect()
    return { x: (cx - r.left - D.x) / D.k, y: (cy - r.top - D.y) / D.k }
  }

  // ---- edges ----
  const path = (d, cls, title, onclick) => {
    const g = document.createElementNS(svgNS, 'g')
    g.setAttribute('class', `edge ${cls}`)
    const hit = document.createElementNS(svgNS, 'path')
    hit.setAttribute('d', d)
    hit.setAttribute('class', 'hit')
    const p = document.createElementNS(svgNS, 'path')
    p.setAttribute('d', d)
    p.setAttribute('class', 'line')
    p.setAttribute('marker-end', cls.includes('fail') ? 'url(#arr-fail)' : 'url(#arr)')
    const t = document.createElementNS(svgNS, 'title')
    t.textContent = title
    g.append(t, hit, p)
    if (onclick) g.addEventListener('pointerdown', (e) => { e.stopPropagation(); onclick() })
    return g
  }
  const drawEdges = () => {
    const { stages: st, order, groups } = G()
    const byId = Object.fromEntries(st.map((s) => [s.id, s]))
    edgeLayer.replaceChildren()
    // A dashed bracket around the stages of each fan-out: they run at the same time.
    for (const g of groups.filter((x) => x.length > 1)) {
      const ps = g.map((id) => byId[id] && posOf(byId[id])).filter(Boolean)
      if (!ps.length) continue
      const x = Math.min(...ps.map((p) => p.x)) - 10, y = Math.min(...ps.map((p) => p.y)) - 18
      const r = document.createElementNS(svgNS, 'rect')
      for (const [k, v] of Object.entries({ x, y, rx: 12, class: 'pgroup',
        width: Math.max(...ps.map((p) => p.x)) + NODE_W + 10 - x, height: Math.max(...ps.map((p) => p.y)) + NODE_H + 10 - y })) r.setAttribute(k, v)
      const t = document.createElementNS(svgNS, 'title')
      t.textContent = `${g.join(' ∥ ')} run at the same time`
      r.appendChild(t)
      edgeLayer.appendChild(r)
    }
    // Failure edges run below every node, each in a lane of its own, so none hides behind a node.
    const placed = st.filter((s) => posOf(s))
    const floor = placed.length ? Math.max(...placed.map((s) => posOf(s).y + NODE_H)) : 0
    let lane = 0
    for (const s of st) {
      const a = posOf(s)
      if (!a) continue
      const sel = D.sel && D.sel.type === 'edge' && D.sel.from === s.id
      const outs = successorsOf(st, s.id)
      for (const n of outs) {
        if (!byId[n] || !posOf(byId[n])) continue
        const b = posOf(byId[n])
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2
        let d
        if (x2 >= x1 + 30) {
          const dx = Math.max(40, (x2 - x1) / 2)
          d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
        } else {
          // target is behind: step out, run along the gap between the rows, step back in
          const ym = Math.abs(y2 - y1) > NODE_H ? (y1 + y2) / 2 : Math.min(y1, y2) - NODE_H / 2 - 36
          const r = 30
          d = `M${x1},${y1} C${x1 + r * 1.6},${y1} ${x1 + r * 1.6},${ym} ${x1},${ym} L${x2},${ym} C${x2 - r * 1.6},${ym} ${x2 - r * 1.6},${y2} ${x2},${y2}`
        }
        edgeLayer.appendChild(path(d, `next${sel && D.sel.kind === 'next' && D.sel.to === n ? ' sel' : ''}${order.includes(s.id) ? '' : ' off'}`,
          `${s.id} → ${n} (success${outs.length > 1 ? ', one branch of a fan-out' : ''}). Click to select, Delete to remove.`,
          () => { D.sel = { type: 'edge', kind: 'next', from: s.id, to: n }; refresh() }))
      }
      if (hasFail(s)) {
        const target = failTarget(st, order, s)
        const t = target && byId[target]
        if (t && posOf(t)) {
          // down out of the fail port, along the gap left of the node, below everything in a lane of its
          // own, up the gap left of the target and into its bottom
          const tp = posOf(t), k = lane % 4
          const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = tp.x + NODE_W / 2, y2 = tp.y + NODE_H
          const gx = a.x - 14 - k * 6, tx = tp.x - 14 - k * 6, low = floor + 34 + lane * 22
          const d = `M${x1},${y1} V${y1 + 12} H${gx} V${low} H${tx} V${y2 + 14} H${x2} V${y2}`
          lane++
          edgeLayer.appendChild(path(d, `fail${s.on_fail ? '' : ' implicit'}${sel && D.sel.kind === 'fail' ? ' sel' : ''}`,
            `${s.id} → ${target} on failure / CHANGES${s.on_fail ? '' : ' (default: the nearest builder before it)'}. Click to select.`,
            () => { D.sel = { type: 'edge', kind: 'fail', from: s.id }; refresh() }))
          const lbl = document.createElementNS(svgNS, 'text')
          lbl.setAttribute('x', Math.min(gx, tx) + 10)
          lbl.setAttribute('y', low - 5)
          lbl.setAttribute('class', 'elabel')
          lbl.textContent = `${s.kind === 'gate' ? '↺ fail' : '↺ CHANGES'}: ${s.id} → ${target}${s.on_fail ? '' : ' (default)'}`
          edgeLayer.appendChild(lbl)
        }
      }
    }
    // The Deliver block follows the last step of the run path: one edge from each stage of it.
    if (hasDeliver() && groups.length) {
      const b = deliverPos()
      for (const id of groups[groups.length - 1]) {
        const a = byId[id] && posOf(byId[id])
        if (!a) continue
        const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2
        let d
        if (x2 >= x1 + 30) {
          const dx = Math.max(40, (x2 - x1) / 2)
          d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
        } else {
          const ym = Math.abs(y2 - y1) > NODE_H ? (y1 + y2) / 2 : Math.min(y1, y2) - NODE_H / 2 - 36
          const r = 30
          d = `M${x1},${y1} C${x1 + r * 1.6},${y1} ${x1 + r * 1.6},${ym} ${x1},${ym} L${x2},${ym} C${x2 - r * 1.6},${ym} ${x2 - r * 1.6},${y2} ${x2},${y2}`
        }
        edgeLayer.appendChild(path(d, 'next deliver', `${id} → deliver (delivery always follows the last stage)`))
      }
    }
    if (D.temp) {
      const p = document.createElementNS(svgNS, 'path')
      const { x1, y1, x2, y2, fail } = D.temp
      p.setAttribute('d', fail ? `M${x1},${y1} C${x1},${y1 + 60} ${x2},${y2 + 60} ${x2},${y2}` : `M${x1},${y1} C${x1 + 60},${y1} ${x2 - 60},${y2} ${x2},${y2}`)
      p.setAttribute('class', `temp${fail ? ' fail' : ''}`)
      edgeLayer.appendChild(p)
    }
  }

  // ---- nodes ----
  const nodeAt = (cx, cy) => {
    const el = document.elementFromPoint(cx, cy)
    const n = el && el.closest && el.closest('.gnode')
    return n ? n.dataset.id : null
  }
  const connectDrag = (e, s, fail) => {
    e.stopPropagation()
    e.preventDefault()
    const p = posOf(s)
    const start = fail ? { x: p.x + NODE_W / 2, y: p.y + NODE_H } : { x: p.x + NODE_W, y: p.y + NODE_H / 2 }
    canvas.setPointerCapture(e.pointerId)
    const move = (ev) => { const w = toWorld(ev.clientX, ev.clientY); D.temp = { x1: start.x, y1: start.y, x2: w.x, y2: w.y, fail }; drawEdges() }
    const up = (ev) => {
      canvas.removeEventListener('pointermove', move)
      D.temp = null
      const target = nodeAt(ev.clientX, ev.clientY)
      if (target === DELIVER_ID) { toast('Delivery always follows the last stage; it takes no edges.'); drawEdges() }
      else if (target && target !== s.id) connect(s.id, target, fail, ev.shiftKey || ev.altKey)
      else drawEdges()
    }
    canvas.addEventListener('pointermove', move)
    canvas.addEventListener('pointerup', up, { once: true })
  }
  // A success edge from a stage that already has one adds a branch (a fan-out); with Shift or Alt held
  // it replaces the stage's edges instead.
  const connect = (from, to, fail, replace = false) => {
    const { stages: st, order } = G()
    const src = st.find((x) => x.id === from)
    if (fail) {
      if (!order.includes(from) || !order.includes(to) || order.indexOf(to) >= order.indexOf(from)) {
        toast('A failure edge goes back to an earlier stage on the path (usually a builder).', true)
        drawEdges()
        return
      }
      src.on_fail = to
      markDirty()
    } else {
      const byId = Object.fromEntries(st.map((x) => [x.id, x]))
      const succ = Object.fromEntries(st.map((x) => [x.id, successorsOf(st, x.id)]))
      const cur = succ[from]
      let note = ''
      if (replace || !cur.length) succ[from] = [to]
      else {
        if (cur.includes(to)) { toast(`${from} already leads to ${to}.`); drawEdges(); return }
        const branches = [...cur, to]
        const notAgent = branches.find((id) => byId[id].kind !== 'agent')
        if (notAgent) {
          toast(`Only agents run in parallel, and ${notAgent} is not one. Hold Shift while dropping to re-point the edge instead.`, true)
          drawEdges()
          return
        }
        // the branches must join again: the new one leads where its siblings lead
        const join = succ[cur[0]]
        if (!sameIds(succ[to], join)) {
          succ[to] = [...join]
          note = ` ${to} now leads to ${join.join(' ∥ ') || 'the end'}, like ${cur.join(' and ')}, so the branches join again.`
        }
        succ[from] = branches
      }
      if (cyclic(succ)) { toast('That would make the success path loop. Going back is what a failure edge (from a guardrails block or reviewer) is for.', true); drawEdges(); return }
      applySucc(st, succ)
      if (succ[from].length > 1) toast(`${succ[from].join(' ∥ ')} now run at the same time.${note}`)
    }
    D.sel = { type: 'edge', kind: fail ? 'fail' : 'next', from, to }
    refresh()
  }
  // Dragging a node: `place({x, y})` runs on every snapped move, `done(moved)` on release.
  const dragNode = (e, el, p0, place, done) => {
    const w0 = toWorld(e.clientX, e.clientY)
    let moved = false
    el.setPointerCapture(e.pointerId)
    const move = (ev) => {
      const w = toWorld(ev.clientX, ev.clientY)
      const nx = Math.round((p0.x + w.x - w0.x) / 8) * 8, ny = Math.round((p0.y + w.y - w0.y) / 8) * 8
      if (!moved && Math.abs(nx - p0.x) + Math.abs(ny - p0.y) < 4) return
      if (!moved) savePositions()
      moved = true
      place({ x: nx, y: ny })
      el.style.left = `${nx}px`
      el.style.top = `${ny}px`
      drawEdges()
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', () => {
      el.removeEventListener('pointermove', move)
      done(moved)
    }, { once: true })
  }
  const drawNodes = () => {
    const { stages: st, order, groups } = G()
    const phases = val(['loop', 'phases']) || []
    const startId = order[0]
    const groupOf = Object.fromEntries(groups.filter((g) => g.length > 1).flatMap((g) => g.map((id) => [id, g])))
    layer.replaceChildren(...st.map((s) => {
      const off = !order.includes(s.id)
      const sel = D.sel && D.sel.type === 'node' && D.sel.id === s.id
      const pid = phaseOf(s)
      const ph = pid && (phases.find((p) => p.id === pid) || { id: pid, title: pid })
      const hue = pid ? phaseHue(pid) : null
      const el = h('div', {
        class: `gnode k-${s.kind}${s.kind === 'agent' ? ` r-${roleOf(s)}` : ''}${sel ? ' sel' : ''}${off ? ' off' : ''}`, tabindex: '0', role: 'button',
        'data-id': s.id, 'aria-label': `${KIND_LABEL[s.kind] || s.kind} stage ${stageLabel(s)}${ph ? `, phase ${ph.title || ph.id}` : ''}${off ? ', not connected' : ''}`,
        style: `left:${posOf(s).x}px;top:${posOf(s).y}px;width:${NODE_W}px;height:${NODE_H}px${hue === null ? '' : `;--ph:${hue}`}`,
      },
      s.id === startId ? h('span', { class: 'startpill' }, '▶ start') : null,
      off ? h('span', { class: 'offpill' }, 'not connected') : null,
      h('span', { class: 'phpill', title: ph ? `Phase ${ph.id}` : 'Waiting jobs show under Needs human' },
        ph ? ph.title || ph.id : 'needs human'),
      h('div', { class: 'gk' }, KIND_LABEL[s.kind] || s.kind,
        groupOf[s.id] ? h('span', { class: 'parbadge', title: `Runs at the same time as ${groupOf[s.id].filter((x) => x !== s.id).join(', ')}` }, ' ∥ parallel') : null),
      h('div', { class: 'gt' }, stageLabel(s)),
      h('div', { class: 'gs' }, nodeSummary(s)),
      h('span', { class: 'port in', title: 'Drop a connection here' }),
      h('span', { class: 'port out', title: 'Drag to the next stage (success)' }),
      hasFail(s) ? h('span', { class: 'port failp', title: 'Drag to where a failure or CHANGES goes back to' }) : null)
      el.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return
        if (e.target.classList.contains('out')) return connectDrag(e, s, false)
        if (e.target.classList.contains('failp')) return connectDrag(e, s, true)
        e.stopPropagation()
        dragNode(e, el, { ...posOf(s) }, (p) => { s.pos = p }, (moved) => {
          if (moved) markDirty()
          D.sel = { type: 'node', id: s.id }
          refresh()
        })
      })
      el.addEventListener('keydown', (e) => {
        const step = e.shiftKey ? 40 : 8
        const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key]
        if (mv) { e.preventDefault(); savePositions(); s.pos = { x: s.pos.x + mv[0], y: s.pos.y + mv[1] }; markDirty(); D.sel = { type: 'node', id: s.id }; refresh(); layer.querySelector(`[data-id="${s.id}"]`)?.focus() }
        if (e.key === 'Enter') { D.sel = { type: 'node', id: s.id }; refresh() }
      })
      return el
    }), ...(hasDeliver() ? [deliverNode()] : []))
  }
  // The Deliver block: no ports, not a stage. Its position is `delivery.pos`.
  const deliverNode = () => {
    const p = deliverPos()
    const sum = deliverSummary()
    const el = h('div', {
      class: `gnode k-deliver${D.sel && D.sel.type === 'deliver' ? ' sel' : ''}`, tabindex: '0', role: 'button',
      'data-id': DELIVER_ID, 'aria-label': `Deliver block, ${sum}`,
      style: `left:${p.x}px;top:${p.y}px;width:${NODE_W}px;height:${NODE_H}px`,
    },
    h('div', { class: 'gk' }, 'Deliver'),
    h('div', { class: 'gt' }, 'Deliver'),
    h('div', { class: 'gs' }, sum))
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return
      e.stopPropagation()
      dragNode(e, el, { ...deliverPos() }, (np) => setIn(['delivery', 'pos'], np), () => {
        D.sel = { type: 'deliver' }
        refresh()
      })
    })
    el.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 40 : 8
      const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key]
      if (mv) {
        e.preventDefault()
        savePositions()
        const q = deliverPos()
        setIn(['delivery', 'pos'], { x: q.x + mv[0], y: q.y + mv[1] })
        D.sel = { type: 'deliver' }
        refresh()
        layer.querySelector(`[data-id="${DELIVER_ID}"]`)?.focus()
      }
      if (e.key === 'Enter') { D.sel = { type: 'deliver' }; refresh() }
    })
    return el
  }

  // ---- inspector ----
  const renameStage = (oldId, newId) => {
    const st = list(['loop', 'stages'])
    for (const x of st) {
      if (x.id === oldId) {
        // keep the agent file it used: a builder's or reviewer's default agent name follows the id
        if (!x.agent && defaultAgent(x)) x.agent = defaultAgent(x)
        x.id = newId
      }
      if (x.next === oldId) x.next = newId
      if (Array.isArray(x.next)) x.next = x.next.map((n) => (n === oldId ? newId : n))
      if (x.on_fail === oldId) x.on_fail = newId
      if (x.builder === oldId) x.builder = newId
    }
    if ((val(['gate', 'steps']) || []).some((g) => g.builder === oldId)) {
      for (const g of list(['gate', 'steps'])) if (g.builder === oldId) g.builder = newId
    }
    if (S.cfg.loop && S.cfg.loop.start === oldId) S.cfg.loop.start = newId
    D.sel = { type: 'node', id: newId }
    markDirty()
  }
  const deleteStage = (sid) => {
    const st = list(['loop', 'stages'])
    const succ = Object.fromEntries(st.map((x) => [x.id, successorsOf(st, x.id)]))
    const through = succ[sid]
    for (const x of st) {
      const outs = succ[x.id]
      // a branch of a fan-out just drops out of it; a single edge into it is re-routed past it
      const nxt = !outs.includes(sid) ? outs : outs.length > 1 ? outs.filter((n) => n !== sid)
        : through.filter((n) => n !== x.id)
      x.next = asNext([...new Set(nxt)])
      if (x.on_fail === sid) delete x.on_fail
      if (x.builder === sid) delete x.builder
    }
    if ((val(['gate', 'steps']) || []).some((g) => g.builder === sid)) {
      for (const g of list(['gate', 'steps'])) if (g.builder === sid) delete g.builder
    }
    const order = runOrderOf(st, val(['loop', 'start']))
    const newStart = order[0] === sid ? through[0] : order[0]
    st.splice(st.findIndex((x) => x.id === sid), 1)
    if (newStart) { S.cfg.loop = S.cfg.loop || {}; S.cfg.loop.start = newStart }
    normalizeLoop()
    D.sel = null
    refresh()
  }
  // Drop the fields the stage's role does not have; the server rejects them.
  const stripForRole = (s) => {
    const role = roleOf(s)
    const keep = new Set(ROLE_FIELDS[role] || [])
    for (const r of Object.keys(ROLE_FIELDS)) for (const f of ROLE_FIELDS[r]) if (!keep.has(f)) delete s[f]
    if ((role === 'plain' || role === 'integrator') && !s.agent) {
      const names = (S.server.agents || []).map((a) => a.name)
      s.agent = role === 'integrator' && names.includes('integrator') ? 'integrator' : names[0] || ''
    }
    markDirty()
  }
  // 'auto': no `role` key, so the role follows the agent's name.
  const setRole = (s, role) => {
    if (role === 'auto') delete s.role
    else s.role = role
    stripForRole(s)
  }
  // Phases are board columns only: tags in board order, added by name, removable once nothing uses them.
  const movePhase = (i, j) => {
    const arr = list(['loop', 'phases'])
    if (j < 0 || j >= arr.length) return
    ;[arr[i], arr[j]] = [arr[j], arr[i]]
    markDirty()
    render()
  }
  const addPhase = (input) => {
    const name = input.value.trim()
    const id = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    const fail = (msg) => { input.setCustomValidity(msg); input.reportValidity(); input.setCustomValidity('') }
    if (!id) return fail('Type a name for the phase')
    if (!/^[a-z]/.test(id)) return fail('A phase name starts with a letter')
    if (phaseIds().includes(id)) return fail(`There is already a phase ${id}`)
    list(['loop', 'phases']).push(name === phaseTitleOf(id) ? { id } : { id, title: name })
    markDirty()
    render()
  }
  const phasesEditor = () => {
    const phases = val(['loop', 'phases']) || []
    const { stages: st } = G()
    const input = h('input', { type: 'text', placeholder: 'New phase, e.g. QA', 'aria-label': 'New phase name', autocomplete: 'off',
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); addPhase(e.target) } } })
    return h('div', { class: 'phases' },
      h('h3', {}, 'Phases'),
      h('p', { class: 'help' }, 'The board columns, in this order, followed by Done and Needs human. A phase is only where a stage shows on the board: what an agent does comes from the agent.'),
      h('ul', { class: 'phtags' }, phases.map((p, i) => {
        const users = st.filter((x) => phaseOf(x) === p.id).map((x) => x.id)
        const why = phases.length === 1 ? 'The loop needs at least one phase' : users.length ? `In use by ${users.join(', ')}` : `Remove phase ${p.id}`
        const btn = (label, text, disabled, fn, cls = '') => h('button', { type: 'button', class: `phbtn ${cls}`, title: label, 'aria-label': label, disabled, onclick: fn }, text)
        return h('li', { class: 'phtag', style: `--ph:${phaseHue(p.id)}`, title: `${p.id}${users.length ? `: ${users.join(', ')}` : ''}` },
          btn(`Move ${p.title || p.id} left`, '←', i === 0, () => movePhase(i, i - 1)),
          h('span', { class: 'phdot', 'aria-hidden': 'true' }), h('span', { class: 'phname' }, p.title || p.id),
          btn(`Move ${p.title || p.id} right`, '→', i === phases.length - 1, () => movePhase(i, i + 1)),
          btn(why, '✕', phases.length === 1 || users.length > 0, () => { list(['loop', 'phases']).splice(i, 1); markDirty(); render() }, 'danger'))
      })),
      h('div', { class: 'row phadd' }, input, h('button', { type: 'button', class: 'small', onclick: () => addPhase(input) }, '+ Phase')))
  }
  const renderInspector = () => {
    const { stages: st, order, groups } = G()
    const phases = val(['loop', 'phases']) || []
    if (D.sel && D.sel.type === 'deliver' && !hasDeliver()) D.sel = null  // None was chosen: the block is gone
    if (D.sel && D.sel.type === 'deliver') {
      const { core, sections } = deliverySettings()
      inspector.replaceChildren(
        h('h3', {}, h('span', { class: 'kpill k-deliver' }, 'Deliver')),
        h('p', { class: 'help' }, 'Runs after the last stage. The same settings as the Deliver tab.'),
        ...core, ...sections,
        h('div', { class: 'row' }, h('button', { type: 'button', class: 'danger', onclick: () => {
          if (confirm('Remove the Deliver block? Delivery is turned off; its settings are kept.')) removeDeliver()
        } }, 'Remove deliver block')))
      return
    }
    if (D.sel && D.sel.type === 'edge') {
      const s = st.find((x) => x.id === D.sel.from)
      if (!s) { D.sel = null; return renderInspector() }
      const isFail = D.sel.kind === 'fail'
      const outs = successorsOf(st, s.id)
      const to = D.sel.to && outs.includes(D.sel.to) ? D.sel.to : outs[0]
      const target = isFail ? (failTarget(st, order, s) || '(a human)') : (to || 'end')
      const fan = !isFail && outs.length > 1
      inspector.replaceChildren(h('h3', {}, isFail ? 'Failure edge' : fan ? 'Success edge (fan-out branch)' : 'Success edge'),
        h('p', {}, h('code', {}, s.id), ' → ', h('code', {}, target)),
        h('p', { class: 'help' }, isFail ? 'Where the job goes back to when the gate fails or a reviewer asks for CHANGES. Each pass uses a round.'
          : fan ? `${s.id} fans out: ${outs.join(' ∥ ')} run at the same time, then join again.` : 'Where the job goes when this stage succeeds.'),
        h('div', { class: 'row' }, h('button', { type: 'button', class: 'danger', disabled: isFail && !s.on_fail, onclick: () => {
          if (isFail) { delete s.on_fail; markDirty() } else setNext(st, s.id, outs.filter((n) => n !== to))
          D.sel = null
          refresh()
        } }, isFail ? 'Reset to the nearest builder' : fan ? 'Remove this branch' : 'Remove edge (stage ends the job)')))
      return
    }
    const s = D.sel && D.sel.type === 'node' && st.find((x) => x.id === D.sel.id)
    if (!s) {
      const mode = val(['delivery', 'mode']) || 'none'
      const tail = mode === 'none' ? '' : ` → deliver (${mode}${val(['delivery', 'require_approval']) ? ', after approval' : ''})`
      inspector.replaceChildren(h('h3', {}, 'Loop'),
        grid(field('Max rounds', ['loop', 'max_rounds'], { type: 'number', min: 1, max: 10, help: 'A round is one gate run plus, if it passes, one review.' })),
        h('div', { class: 'legend' },
          h('p', {}, h('b', {}, 'Move'), ': drag a node. ', h('b', {}, 'Pan'), ': drag the background. ', h('b', {}, 'Zoom'), ': scroll.'),
          h('p', {}, h('span', { class: 'lg next' }), 'Success edge: drag from the right port to another node. From a stage that already has one, this adds a branch: the branches run at the same time and must lead to the same next stage. Hold Shift (or Alt) while dropping to re-point the edge instead.'),
          h('p', {}, h('span', { class: 'lg fail' }), 'Failure edge (guardrails blocks, reviewer agents): drag from the bottom port back to an earlier node.'),
          h('p', {}, 'Select a node or edge to edit it; Delete removes it.'),
          h('p', { class: 'help' }, `Run order: ${groups.map((g) => (g.length > 1 ? `(${g.join(' ∥ ')})` : g[0])).join(' → ') || '(empty)'}${tail}`),
          groups.error ? h('p', { class: 'msg error' }, `The path stops here: ${groups.error}.`) : '',
          h('p', { class: 'help' }, mode === 'none'
            ? 'Delivery is off: a job ends ready after the last stage. Add a Deliver block to turn it on.'
            : 'Delivery shows as the Deliver block after the last stage; select it (or use the Deliver tab) to set it up.')),
        phasesEditor())
      return
    }
    const i = st.indexOf(s)
    const P = ['loop', 'stages', i]
    const idInput = h('input', { type: 'text', value: s.id, spellcheck: 'false', onchange: (e) => {
      const v = e.target.value.trim()
      if (!/^[a-z][a-z0-9-]*$/.test(v) || RESERVED_IDS.includes(v)) { e.target.setCustomValidity('Lower-case kebab, not a reserved job state'); e.target.reportValidity(); return }
      if (st.some((x) => x !== s && x.id === v)) { e.target.setCustomValidity('Another stage has this id'); e.target.reportValidity(); return }
      renameStage(s.id, v)
      refresh()
    } })
    // Stage fields read the working copy (`cur`): the server's effective config lags behind role and id changes.
    const fields = [
      h('div', { class: 'field' }, h('label', {}, 'Id'), idInput, h('div', { class: 'help' }, 'Also the job stage name.')),
      field('Title', [...P, 'title'], { cur: s.title || '', rerender: true }),
    ]
    const role = roleOf(s)
    const pid = phaseOf(s)
    if (s.kind !== 'approval') {
      const sid = nextId('phase')
      fields.push(h('div', { class: 'field' }, h('label', { for: sid }, 'Phase'),
        h('select', { id: sid, onchange: (e) => { s.phase = e.target.value; markDirty(); refresh() } },
          phases.map((p) => h('option', { value: p.id, selected: p.id === pid }, p.title || p.id))),
        h('div', { class: 'help' }, 'The board column it shows in; it does not change what the stage does.')))
    }
    if (s.kind === 'agent') {
      const rid = nextId('role'), aid = nextId('agent')
      const others = st.some((x) => x !== s && roleOf(x) === 'integrator')
      const auto = roleForAgent(s.agent || '')
      fields.push(h('div', { class: 'field' }, h('label', { for: aid }, 'Agent'),
        h('select', { id: aid, onchange: (e) => {
          if (e.target.value) s.agent = e.target.value
          else delete s.agent
          stripForRole(s)  // with Acts as Auto, the role follows the new name
          refresh()
        } }, agentOptions(defaultAgent(s), s.agent).map(([v, l]) => h('option', { value: v, selected: (s.agent || '') === v }, l)))))
      fields.push(h('div', { class: 'field' }, h('label', { for: rid }, 'Acts as'),
        h('select', { id: rid, onchange: (e) => { setRole(s, e.target.value); refresh() } },
          h('option', { value: 'auto', selected: !s.role, disabled: auto === 'integrator' && others && roleOf(s) !== 'integrator' }, `Auto (from agent: ${auto})`),
          ROLES.map((r) => h('option', { value: r, selected: s.role === r, disabled: r === 'integrator' && others }, r))),
        h('div', { class: 'help' }, ROLE_HELP[role])))
      if (role === 'plain' || role === 'integrator') {
        fields.push(field('Output file', [...P, 'output'], { cur: s.output || '', placeholder: role === 'integrator' ? 'decision.md' : `${s.id}.md` }))
      }
      if (role === 'plain') {
        fields.push(field('Re-run on rework', [...P, 'rerun_on_rework'], { type: 'select', cur: s.rerun_on_rework || 'never', options: ['never', 'scope', 'always'] }))
        fields.push(field('Read the Builders: line', [...P, 'read_builders'], { type: 'bool', cur: !!s.read_builders, help: 'The output names which builders the job needs.' }))
      }
      if (role === 'builder') {
        fields.push(field('Description', [...P, 'description'], { type: 'textarea', rows: 2, cur: s.description || '', help: 'What it builds, shown to the architect.' }))
        fields.push(field('Area (globs)', [...P, 'area_globs'], { type: 'list', cur: s.area_globs || [], help: 'Paths it owns; findings there come back to it.' }))
      }
      if (role === 'reviewer') {
        const bids = st.filter((x) => roleOf(x) === 'builder').map((x) => x.id)
        fields.push(field('Chip', [...P, 'chip'], { cur: s.chip || '', placeholder: s.id, help: 'Dashboard label' }))
        fields.push(field('Findings go to builder', [...P, 'builder'], { type: 'select', cur: s.builder || '', parse: (v) => v || undefined,
          options: [['', '(every builder)'], ...bids.map((x) => [x, x]), ...(s.builder && !bids.includes(s.builder) ? [[s.builder, `${s.builder} (missing)`]] : [])] }))
        fields.push(h('p', { class: 'help' }, 'Routing: with none of the triggers below it always runs; with any, it runs only when one matches.'))
        fields.push(field('Changed path globs', [...P, 'path_globs'], { type: 'list', cur: s.path_globs || [] }))
        fields.push(field('Excluded path globs', [...P, 'exclude_globs'], { type: 'list', cur: s.exclude_globs || [], help: 'Changed paths that never trigger it.' }))
        fields.push(field('Added-line patterns (regex)', [...P, 'diff_patterns'], { type: 'list', cur: s.diff_patterns || [] }))
        fields.push(scopedEditor([...P, 'scoped_patterns'], s.scoped_patterns || []))
        fields.push(metricEditor([...P, 'metrics'], s.metrics || []))
      }
      fields.push(field('Extra prompt', [...P, 'prompt'], { type: 'textarea', rows: 3, cur: s.prompt || '', placeholder: 'Appended to the agent prompt for this stage' }))
    }
    if (s.kind === 'gate') {
      // Ticked guardrails go into `steps`; Run all is an empty list (every guardrail, built-ins included).
      const choices = guardrailChoices()
      const chosen = s.steps || []
      const all = !chosen.length
      const aid = nextId('gall')
      fields.push(h('fieldset', { class: 'field wide guardpick' }, h('legend', {}, 'Guardrails'),
        h('div', { class: 'chk' }, h('input', { type: 'checkbox', id: aid, checked: all, onchange: (e) => {
          if (e.target.checked) delete s.steps
          else s.steps = choices.filter((c) => !c.off).map((c) => c.key)
          markDirty()
          refresh()
        } }), h('label', { for: aid }, h('b', {}, 'Run all'), ' (and any added later)')),
        choices.map((c) => {
          const cid = nextId('gs')
          return h('div', { class: 'grow' },
            h('div', { class: 'chk' }, h('input', { type: 'checkbox', id: cid, checked: all ? !c.off : chosen.includes(c.key), disabled: all || (c.off && !chosen.includes(c.key)),
              onchange: (e) => {
                const keys = choices.map((x) => x.key)
                const nxt = e.target.checked ? keys.filter((x) => x === c.key || chosen.includes(x)) : chosen.filter((x) => x !== c.key)
                if (!nxt.length) { toast('A guardrails block runs at least one guardrail; tick Run all to run every one.', true); e.target.checked = true; return }
                s.steps = nxt
                markDirty()
                refresh()
              } }),
            h('label', { for: cid }, c.title, ' ', h('code', {}, c.key), c.off ? h('span', { class: 'help inline' }, ` (${c.why})`) : null)))
        }),
        h('div', { class: 'help' }, all ? 'Runs every guardrail that is on.' : `Runs ${chosen.length} of ${choices.length} guardrails.`)))
    }
    if (s.kind === 'approval') {
      fields.push(field('Card note', [...P, 'note'], { cur: s.note || '', placeholder: 'approve the spec and design' }))
      fields.push(h('p', { class: 'help' }, 'Approvals have no phase: while a job waits here it shows under Needs human on the board, with this note.'))
    }
    let failNote = null
    if (hasFail(s)) {
      const ft = failTarget(st, order, s)
      failNote = s.on_fail ? `On failure it goes back to ${s.on_fail}.`
        : ft ? `On failure it goes back to ${ft}, the nearest builder before it. Drag from the bottom port to change.`
          : 'No builder before it: failure stops for a human.'
    }
    const where = s.kind === 'gate' ? 'What each guardrail runs is edited in the Guardrails tab.' : null
    inspector.replaceChildren(
      h('h3', {}, h('span', { class: `kpill k-${s.kind}` }, s.kind === 'agent' && role !== 'plain' ? role : KIND_LABEL[s.kind] || s.kind), ' ', stageLabel(s)),
      h('p', { class: 'help' }, KIND_HELP[s.kind] || ''),
      where ? h('p', { class: 'help' }, where) : '',  // replaceChildren would print a null
      failNote ? h('p', { class: 'help' }, failNote) : '',
      h('div', { class: 'stack' }, fields),
      h('div', { class: 'row' },
        h('button', { type: 'button', disabled: order[0] === s.id, onclick: () => { S.cfg.loop = S.cfg.loop || {}; S.cfg.loop.start = s.id; normalizeLoop(); refresh() } }, 'Make start'),
        h('button', { type: 'button', class: 'danger', onclick: () => { if (confirm(`Remove stage ${s.id}? Edges into it are re-routed to what came after it.`)) deleteStage(s.id) } }, 'Delete stage')))
  }

  // ---- palette, toolbar ----
  const addNode = (kind) => {
    savePositions()
    const st = list(['loop', 'stages'])
    // freeze the edges, so the new node (appended to the list) is not wired in by list order
    const succ = Object.fromEntries(st.map((x) => [x.id, successorsOf(st, x.id)]))
    for (const x of st) x.next = asNext(succ[x.id])
    const base = kind === 'gate' ? 'guardrails' : kind
    let id = base
    let n = 2
    while (st.some((x) => x.id === id)) id = `${base}-${n++}`
    const r = canvas.getBoundingClientRect()
    const c = toWorld(r.left + r.width / 2, r.top + r.height / 2)
    const node = { id, kind, title: '', next: 'end', pos: { x: Math.round((c.x - NODE_W / 2) / 8) * 8, y: Math.round((c.y - NODE_H / 2) / 8) * 8 } }
    if (kind !== 'approval') node.phase = phaseOf(node)  // agents: the first phase; guardrails blocks: guardrails, review or the last
    if (kind === 'agent') node.agent = (S.server.agents || [])[0]?.name || ''
    st.push(node)
    normalizeLoop()
    D.sel = { type: 'node', id }
    refresh()
    toast(kind === 'gate' ? `Added ${id}. Pick the guardrails this gate runs, then drag from a port to connect it.` : `Added ${id}. Drag from a port to connect it.`)
  }
  // The Deliver block is a view of `delivery`: adding it turns delivery on (push), removing it turns it off.
  const addDeliver = () => {
    setDeliveryMode('push')
    D.sel = { type: 'deliver' }
    render()  // not refresh(): the palette's disabled state has to update
    toast('Added the Deliver block (push). Pick the mode in the inspector.')
  }
  const removeDeliver = () => {
    setDeliveryMode('none')
    D.sel = null
    render()
  }
  const palette = h('div', { class: 'palette', role: 'toolbar', 'aria-label': 'Add a stage' },
    h('span', { class: 'help' }, 'Add'),
    STAGE_KINDS.map((k) => h('button', { type: 'button', class: `small k-${k}`, title: KIND_HELP[k], onclick: () => addNode(k) }, `+ ${KIND_LABEL[k]}`)),
    h('button', { type: 'button', class: 'small k-deliver', disabled: hasDeliver(),
      title: hasDeliver() ? 'Delivery is already in the loop' : 'Deliver the finished job after the last stage', onclick: addDeliver }, '+ Deliver'))
  const fit = () => {
    const st = G().stages.filter((s) => posOf(s))
    const dp = hasDeliver() ? deliverPos() : null
    if (!st.length && !dp) return
    const r = canvas.getBoundingClientRect()
    const xs = [...st.map((s) => posOf(s).x), ...(dp ? [dp.x] : [])], ys = [...st.map((s) => posOf(s).y), ...(dp ? [dp.y] : [])]
    const minX = Math.min(...xs) - 40, minY = Math.min(...ys) - 50
    const maxX = Math.max(...xs) + NODE_W + 40, maxY = Math.max(...ys) + NODE_H + 60 + 22 * st.filter(hasFail).length  // room for the failure lanes
    D.k = Math.min(1.25, Math.max(0.3, Math.min(r.width / (maxX - minX), r.height / (maxY - minY))))
    D.x = (r.width - (maxX - minX) * D.k) / 2 - minX * D.k
    D.y = (r.height - (maxY - minY) * D.k) / 2 - minY * D.k
    applyView()
  }
  const zoom = (f, cx, cy) => {
    const r = canvas.getBoundingClientRect()
    const px = cx ?? r.left + r.width / 2, py = cy ?? r.top + r.height / 2
    const w = toWorld(px, py)
    D.k = Math.min(2, Math.max(0.25, D.k * f))
    D.x = px - r.left - w.x * D.k
    D.y = py - r.top - w.y * D.k
    applyView()
  }
  const tools = h('div', { class: 'gtools' },
    h('button', { type: 'button', class: 'small', title: 'Zoom out', 'aria-label': 'Zoom out', onclick: () => zoom(1 / 1.2) }, '−'),
    h('button', { type: 'button', class: 'small', title: 'Zoom in', 'aria-label': 'Zoom in', onclick: () => zoom(1.2) }, '+'),
    h('button', { type: 'button', class: 'small', onclick: fit }, 'Fit'),
    h('button', { type: 'button', class: 'small', onclick: () => { D.width = canvas.getBoundingClientRect().width; autoLayout(true); markDirty(); refresh(); fit() } }, 'Tidy up'))

  // ---- canvas interactions: pan, zoom, select, delete ----
  canvas.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.gnode') || e.button !== 0) return
    const x0 = e.clientX - D.x, y0 = e.clientY - D.y
    let moved = false
    canvas.setPointerCapture(e.pointerId)
    canvas.classList.add('panning')
    const move = (ev) => { moved = true; D.x = ev.clientX - x0; D.y = ev.clientY - y0; applyView() }
    canvas.addEventListener('pointermove', move)
    canvas.addEventListener('pointerup', () => {
      canvas.removeEventListener('pointermove', move)
      canvas.classList.remove('panning')
      if (!moved && D.sel) { D.sel = null; refresh() }
    }, { once: true })
  })
  canvas.addEventListener('wheel', (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX, e.clientY) }, { passive: false })
  canvas.addEventListener('keydown', (e) => {
    if ((e.key === 'Delete' || e.key === 'Backspace') && D.sel) {
      e.preventDefault()
      const st = G().stages
      if (D.sel.type === 'deliver') { if (confirm('Remove the Deliver block? Delivery is turned off; its settings are kept.')) removeDeliver() }
      else if (D.sel.type === 'node') { if (confirm(`Remove stage ${D.sel.id}?`)) deleteStage(D.sel.id) }
      else if (D.sel.kind === 'fail') { const s = st.find((x) => x.id === D.sel.from); if (s) { delete s.on_fail; markDirty() } D.sel = null; refresh() }
      else {
        // removes just this edge; a fan-out keeps its other branches
        const outs = successorsOf(st, D.sel.from)
        setNext(st, D.sel.from, outs.filter((n) => n !== (D.sel.to || outs[0])))
        D.sel = null
        refresh()
      }
    }
    if (e.key === 'Escape') { D.sel = null; refresh() }
  })

  function refresh() { drawNodes(); drawEdges(); renderInspector() }
  refresh()
  const wrap = h('div', { class: 'designer' },
    h('div', { class: 'gbar' }, palette, h('span', { class: 'spacer' }), tools),
    h('div', { class: 'gbody' }, canvas, inspector))
  if (!D.fitted) requestAnimationFrame(() => { if (canvas.isConnected) { fit(); D.fitted = true } })
  return wrap
}

// ---- Agents --------------------------------------------------------------------------------------
function renderAgents() {
  const agents = S.server.agents || []
  const used = new Set()
  for (const s of val(['loop', 'stages']) || []) {
    const a = s.agent || (s.kind === 'agent' && defaultAgent(s))
    if (a) used.add(a)
  }
  if (!S.agent || !agents.some((a) => a.name === S.agent)) S.agent = agents[0] ? agents[0].name : null
  const lib = S.server.library.agents || []
  const libSel = h('select', { id: 'lib-agent', 'aria-label': 'Library agent' }, lib.map((a) => h('option', { value: a.name, title: a.description }, a.name)))
  const listEl = h('ul', { class: 'picker', role: 'listbox', 'aria-label': 'Agents' }, agents.map((a) => h('li', {},
    h('button', { type: 'button', role: 'option', 'aria-selected': a.name === S.agent ? 'true' : 'false', class: a.name === S.agent ? 'sel' : '',
      onclick: () => { S.agent = a.name; render() } }, a.name, used.has(a.name) ? null : h('span', { class: 'tag' }, 'unused')))))
  const side = h('aside', { class: 'side' }, listEl,
    h('div', { class: 'row' }, h('button', { type: 'button', onclick: newAgent }, 'New agent')),
    lib.length ? h('div', { class: 'row lib' }, libSel, h('button', { type: 'button', onclick: async () => {
      const name = prompt('Name for the new agent', libSel.value)
      if (!name) return
      if (!NAME.test(name)) return toast('Names are lower-case letters, digits, - and _', true)
      try { applyEdit(await api('POST', 'agents/from-library', { library: libSel.value, name })); S.agent = name; render(); toast(`Added ${name}`) } catch (e) { toast(e.message, true) }
    } }, 'Clone from library')) : null)
  const a = agents.find((x) => x.name === S.agent)
  return h('div', { class: 'split' }, side, a ? agentEditor(a) : h('div', { class: 'card' }, h('p', {}, 'No agents yet.')))
}

function newAgent() {
  const name = prompt('Agent name (lower-case, e.g. reviewer-docs)')
  if (!name) return
  if (!NAME.test(name)) return toast('Names are lower-case letters, digits, - and _', true)
  if ((S.server.agents || []).some((a) => a.name === name)) return toast(`${name} exists`, true)
  api('PUT', `agents/${name}`, { name, description: '', tools: ['Read', 'Grep', 'Glob', 'Bash'], model: 'sonnet', skills: [], mcp: [], body: `You are ${name}.\n` })
    .then((r) => { applyEdit(r); S.agent = name; render(); toast(`Created ${name}`) })
    .catch((e) => toast(e.message, true))
}

function applyEdit(r) {
  if (!r) return
  if (r.agents) S.server.agents = r.agents
  if (r.skills) S.server.skills = r.skills
  if (r.errors) S.server.errors = r.errors
  if (r.warnings) S.server.warnings = r.warnings
  S.issues = { errors: r.errors || [], warnings: r.warnings || [] }
  if (r.render && r.render.ok === false) toast(`Saved, but render failed: ${r.render.error}`, true)
}

function agentEditor(a) {
  const d = clone(a)
  const skills = (S.server.skills || []).map((s) => s.name)
  const id = (k) => `ag-${k}`
  const modelCustom = d.model && !MODELS.includes(d.model)
  const modelInput = h('input', { id: id('model-custom'), type: 'text', value: modelCustom ? d.model : '', placeholder: 'model id', hidden: !modelCustom, oninput: (e) => { d.model = e.target.value || null } })
  const save = async () => {
    try {
      const r = await api('PUT', `agents/${a.name}`, { ...d, old_name: a.name })
      applyEdit(r); S.agent = d.name; render(); toast(`Saved ${d.name}`)
    } catch (e) { toast(e.message, true) }
  }
  return h('div', { class: 'card editor' },
    h('div', { class: 'ed-head' }, h('h2', {}, a.name), h('span', { class: 'spacer' }),
      h('button', { type: 'button', onclick: () => {
        const n = prompt('Rename to', a.name)
        if (!n || n === a.name) return
        if (!NAME.test(n)) return toast('Names are lower-case letters, digits, - and _', true)
        d.name = n; save()
      } }, 'Rename'),
      h('button', { type: 'button', class: 'danger', onclick: async () => {
        if (!confirm(`Delete agent ${a.name}? Loop stages that use it will fail validation.`)) return
        try { applyEdit(await api('DELETE', `agents/${a.name}`)); S.agent = null; render(); toast(`Deleted ${a.name}`) } catch (e) { toast(e.message, true) }
      } }, 'Delete'),
      h('button', { type: 'button', class: 'primary', onclick: save }, 'Save agent')),
    h('p', { class: 'help' }, 'Agent files save on their own (not with the Save button at the top).'),
    grid(
      h('div', { class: 'field wide' }, h('label', { for: id('desc') }, 'Description'),
        h('textarea', { id: id('desc'), rows: 2, oninput: (e) => { d.description = e.target.value } }, d.description)),
      h('div', { class: 'field' }, h('label', { for: id('model') }, 'Model'),
        h('select', { id: id('model'), onchange: (e) => {
          if (e.target.value === 'custom') { modelInput.hidden = false; modelInput.focus(); d.model = modelInput.value || null } else { modelInput.hidden = true; d.model = e.target.value || null }
        } }, [...MODELS, 'custom'].map((m) => h('option', { value: m, selected: modelCustom ? m === 'custom' : (d.model || '') === m }, m || '(runtime default)'))),
        modelInput, h('div', { class: 'help' }, 'Aliases map to real model ids per runtime (Runtime tab).')),
      h('fieldset', { class: 'field wide' }, h('legend', {}, 'Tools'),
        h('div', { class: 'checks' }, TOOLS.map((t) => {
          const cid = id('tool-' + t)
          return h('span', { class: 'chk' }, h('input', { type: 'checkbox', id: cid, checked: d.tools.includes(t), onchange: (e) => {
            d.tools = e.target.checked ? [...d.tools, t] : d.tools.filter((x) => x !== t)
          } }), h('label', { for: cid }, t))
        }))),
      h('fieldset', { class: 'field wide' }, h('legend', {}, 'Skills (preloaded into the agent)'),
        skills.length ? h('div', { class: 'checks' }, skills.map((s) => {
          const cid = id('skill-' + s)
          return h('span', { class: 'chk' }, h('input', { type: 'checkbox', id: cid, checked: d.skills.includes(s), onchange: (e) => {
            d.skills = e.target.checked ? [...d.skills, s] : d.skills.filter((x) => x !== s)
          } }), h('label', { for: cid }, s))
        })) : h('p', { class: 'help' }, 'No skills yet (Skills tab).')),
      h('div', { class: 'field wide' }, h('label', { for: id('mcp') }, 'MCP tools (one per line, e.g. mcp__claude_ai_Atlassian)'),
        h('textarea', { id: id('mcp'), class: 'mono', rows: 2, oninput: (e) => { d.mcp = e.target.value.split('\n').map((x) => x.trim()).filter(Boolean) } }, (d.mcp || []).join('\n')))),
    mdEditor(id('body'), 'Instructions', d))
}

// ---- Skills --------------------------------------------------------------------------------------
function renderSkills() {
  const skills = S.server.skills || []
  if (!S.skill || !skills.some((s) => s.name === S.skill)) S.skill = skills[0] ? skills[0].name : null
  const usedBy = {}
  for (const a of S.server.agents || []) for (const s of a.skills || []) (usedBy[s] = usedBy[s] || []).push(a.name)
  const lib = S.server.library.skills || []
  const libSel = h('select', { id: 'lib-skill', 'aria-label': 'Library skill' }, lib.map((s) => h('option', { value: s.name, title: s.description }, s.name)))
  const side = h('aside', { class: 'side' },
    h('ul', { class: 'picker', role: 'listbox', 'aria-label': 'Skills' }, skills.map((s) => h('li', {},
      h('button', { type: 'button', role: 'option', 'aria-selected': s.name === S.skill ? 'true' : 'false', class: s.name === S.skill ? 'sel' : '', onclick: () => { S.skill = s.name; render() } },
        s.name, usedBy[s.name] ? null : h('span', { class: 'tag' }, 'unused'))))),
    h('div', { class: 'row' }, h('button', { type: 'button', onclick: () => {
      const name = prompt('Skill name (lower-case, e.g. api-conventions)')
      if (!name) return
      if (!NAME.test(name)) return toast('Names are lower-case letters, digits, - and _', true)
      api('PUT', `skills/${name}`, { name, description: '', body: `# ${name}\n\n## Rules\n\n- \n` })
        .then((r) => { applyEdit(r); S.skill = name; render(); toast(`Created ${name}`) }).catch((e) => toast(e.message, true))
    } }, 'New skill')),
    lib.length ? h('div', { class: 'row lib' }, libSel, h('button', { type: 'button', onclick: async () => {
      const name = prompt('Name for the new skill', libSel.value)
      if (!name) return
      if (!NAME.test(name)) return toast('Names are lower-case letters, digits, - and _', true)
      try { applyEdit(await api('POST', 'skills/from-library', { library: libSel.value, name })); S.skill = name; render(); toast(`Added ${name}`) } catch (e) { toast(e.message, true) }
    } }, 'Clone from library')) : null)
  const s = skills.find((x) => x.name === S.skill)
  return h('div', { class: 'split' }, side, s ? skillEditor(s, usedBy[s.name] || []) : h('div', { class: 'card' }, h('p', {}, 'No skills yet.')))
}

function skillEditor(s, users) {
  const d = clone(s)
  const save = async () => {
    try { applyEdit(await api('PUT', `skills/${s.name}`, { ...d, old_name: s.name })); S.skill = d.name; render(); toast(`Saved ${d.name}`) } catch (e) { toast(e.message, true) }
  }
  return h('div', { class: 'card editor' },
    h('div', { class: 'ed-head' }, h('h2', {}, s.name), h('span', { class: 'spacer' }),
      h('button', { type: 'button', onclick: () => {
        const n = prompt('Rename to', s.name)
        if (!n || n === s.name) return
        if (!NAME.test(n)) return toast('Names are lower-case letters, digits, - and _', true)
        d.name = n; save()
      } }, 'Rename'),
      h('button', { type: 'button', class: 'danger', onclick: async () => {
        if (!confirm(`Delete skill ${s.name}?${users.length ? ` Used by ${users.join(', ')}.` : ''}`)) return
        try { applyEdit(await api('DELETE', `skills/${s.name}`)); S.skill = null; render(); toast(`Deleted ${s.name}`) } catch (e) { toast(e.message, true) }
      } }, 'Delete'),
      h('button', { type: 'button', class: 'primary', onclick: save }, 'Save skill')),
    h('p', { class: 'help' }, users.length ? `Preloaded by: ${users.join(', ')}. Attach skills to agents in the Agents tab.` : 'No agent preloads this skill yet. Attach it in the Agents tab.'),
    h('div', { class: 'field wide' }, h('label', { for: 'sk-desc' }, 'Description (when it applies)'),
      h('textarea', { id: 'sk-desc', rows: 2, oninput: (e) => { d.description = e.target.value } }, d.description)),
    mdEditor('sk-body', 'Skill', d))
}

// ---- Guardrails ----------------------------------------------------------------------------------
// The deterministic checks a gate block runs. Two are built in (switched on or off, their command is
// fixed); the rest are gate.steps, copied from the library or written by hand, commands in full view.
const guardLib = () => (S.server.library && S.server.library.guardrails) || []
const guardBuiltins = () => (S.server.library && S.server.library.builtin_guardrails) || []
function guardTitle(step) {
  const lib = guardLib().find((g) => g.step.key === step.key)
  return lib ? lib.title : step.chip || step.key
}
const codeBlock = (text) => h('pre', { class: 'code' }, h('code', {}, text || ''))
// Every guardrail a gate block can pick: the built-ins first, then gate.steps.
function guardrailChoices() {
  return [
    ...guardBuiltins().map((b) => ({ key: b.key, title: b.title, cmd: b.cmd, off: val(['gate', b.flag]) === false, why: 'off in Guardrails' })),
    ...(val(['gate', 'steps']) || []).map((g) => ({ key: g.key, title: guardTitle(g), cmd: g.cmd, off: g.mode === 'off', why: 'mode off' })),
  ]
}

function renderGuardrails() {
  const steps = val(['gate', 'steps']) || []
  const setup = val(['gate', 'setup']) || []
  const stepKeys = steps.map((s) => s.key)
  const builders = (val(['loop', 'stages']) || []).filter((s) => roleOf(s) === 'builder').map((s) => s.id)
  const moveStep = (i, j) => {
    const arr = list(['gate', 'steps'])
    if (j < 0 || j >= arr.length) return
    ;[arr[i], arr[j]] = [arr[j], arr[i]]
    markDirty()
    render()
  }
  const iconBtn = (label, text, disabled, fn, cls = 'icon') => h('button', { type: 'button', class: cls, title: label, 'aria-label': label, disabled, onclick: fn }, text)
  const builtin = guardBuiltins().map((b) => h('div', { class: 'guard' },
    h('div', { class: 'item-head' }, h('strong', {}, b.title), h('span', { class: 'badge' }, b.key), h('span', { class: 'spacer' }),
      field('On', ['gate', b.flag], { type: 'bool', rerender: true })),
    h('p', { class: 'help' }, b.description),
    codeBlock(b.cmd)))
  const cards = steps.map((st, i) => {
    const P = ['gate', 'steps', i]
    const mode = st.mode || 'gate'
    const lines = String(st.cmd || '').split('\n').length
    return h('li', { class: 'guard item' },
      h('div', { class: 'item-head' }, h('strong', {}, guardTitle(st)), h('span', { class: 'badge' }, st.chip || st.key),
        h('span', { class: `mode m-${mode}`, title: { gate: 'Fails the gate', report: 'Runs, never fails the gate', off: 'Does not run' }[mode] }, mode),
        h('span', { class: 'spacer' }),
        iconBtn('Move up', '↑', i === 0, () => moveStep(i, i - 1)),
        iconBtn('Move down', '↓', i === steps.length - 1, () => moveStep(i, i + 1)),
        iconBtn(`Remove ${st.key}`, '✕', false, () => {
          if (!confirm(`Remove the guardrail ${st.key}? Guardrails blocks that pick it will fail validation.`)) return
          list(['gate', 'steps']).splice(i, 1)
          markDirty()
          render()
        }, 'icon danger')),
      field('Command (bash)', [...P, 'cmd'], { type: 'textarea', mono: true, wide: true, rows: Math.min(18, Math.max(2, lines + 1)) }),
      envRows([...P, 'env'], st),
      grid(
        field('Key', [...P, 'key'], { help: 'gate.json key; gate blocks pick it by this' }),
        field('Chip', [...P, 'chip'], { emptyNull: true, help: 'Board label' }),
        field('Mode', [...P, 'mode'], { type: 'select', options: ['gate', 'report', 'off'], rerender: true, help: 'report: runs, never fails the gate' })),
      h('details', { class: 'more' }, h('summary', {}, 'More'),
        grid(
          field('Only when changed (globs)', [...P, 'when_changed'], { type: 'list' }),
          field('Requires guardrails', [...P, 'requires'], { type: 'list', placeholder: stepKeys.filter((k) => k !== st.key).join('\n') || 'build' }),
          field('Timeout (minutes)', [...P, 'timeout_min'], { type: 'number', min: 1 }),
          field('Hold the port lock', [...P, 'lock'], { type: 'bool', help: 'Serialise app servers across jobs' }),
          field('JUnit XML', [...P, 'junit'], { emptyNull: true, placeholder: 'junit.xml', help: 'Relative to the round folder' }),
          field('Cobertura XML', [...P, 'coverage', 'cobertura'], { emptyNull: true, placeholder: 'coverage.xml' }),
          field('Coverage summary JSON', [...P, 'coverage', 'json_summary'], { emptyNull: true, placeholder: 'coverage/coverage-summary.json' }),
          field('Metrics JSON', [...P, 'metrics_file'], { emptyNull: true, help: 'Numbers merged into gate.json' }),
          field('If only this fails', [...P, 'external_failure'], { type: 'select', options: [['fail', 'use up a round'], ['needs-human', 'needs human, keep the round']] }),
          field('Failures go to builder', [...P, 'builder'], { type: 'select', parse: (v) => v || undefined,
            options: [['', '(every builder)'], ...builders.map((b) => [b, b]), ...(st.builder && !builders.includes(st.builder) ? [[st.builder, `${st.builder} (missing)`]] : [])] }))))
  })
  const addStep = (step) => {
    const arr = list(['gate', 'steps'])
    let key = step.key
    for (let n = 2; arr.some((x) => x.key === key); n++) key = `${step.key}-${n}`
    arr.push({ ...clone(step), key })
    markDirty()
    render()
    toast(`Added ${key}. Guardrails blocks that run all of them pick it up; others choose it in the Loop.`)
  }
  const picker = S.guardPicker ? h('div', { class: 'card picker-lib' },
    h('div', { class: 'item-head' }, h('h2', {}, 'Library'), h('span', { class: 'spacer' }),
      h('button', { type: 'button', class: 'small', onclick: () => { S.guardPicker = false; render() } }, 'Close')),
    guardLib().length ? h('ul', { class: 'items' }, guardLib().map((g) => {
      const added = stepKeys.includes(g.step.key)
      return h('li', { class: 'guard item' },
        h('div', { class: 'item-head' }, h('strong', {}, g.title), h('span', { class: 'badge' }, g.step.chip || g.step.key),
          g.step.mode && g.step.mode !== 'gate' ? h('span', { class: `mode m-${g.step.mode}` }, g.step.mode) : null, h('span', { class: 'spacer' }),
          h('button', { type: 'button', class: added ? 'small' : 'small primary', onclick: () => addStep(g.step) }, added ? 'Added · add again' : 'Add')),
        h('p', { class: 'help' }, g.description),
        h('details', { class: 'more' }, h('summary', {}, 'Show code'), codeBlock(g.step.cmd),
          g.step.env && Object.keys(g.step.env).length ? codeBlock(Object.entries(g.step.env).map(([k, v]) => `${k}=${v}`).join('\n')) : null))
    })) : h('p', { class: 'help' }, 'The library has no guardrails.')) : null
  return h('div', {},
    section('Guardrails',
      h('p', { class: 'help' }, 'Guardrails are deterministic checks on a job\'s branch. Guardrails blocks in the Loop run them, each picking which ones (or all). A guardrail is bash run in the worktree with WORKTREE, BASE, MERGE_BASE, OUT, REPO, FACTORY_DIR and CHANGED (a file listing changed paths) set.')),
    section('Built in', ...builtin),
    section('Your guardrails',
      steps.length ? h('ol', { class: 'items' }, cards) : h('p', { class: 'help' }, 'None yet. Add one from the library or write your own.'),
      h('div', { class: 'row' },
        h('button', { type: 'button', class: 'primary', onclick: () => { S.guardPicker = !S.guardPicker; render() } }, '+ From library'),
        h('button', { type: 'button', onclick: () => {
          let n = steps.length + 1
          while (stepKeys.includes(`check-${n}`)) n++
          list(['gate', 'steps']).push({ key: `check-${n}`, cmd: '' })
          markDirty()
          render()
        } }, '+ Custom guardrail'))),
    picker,
    section('Run settings',
      grid(field('Timeout for a guardrails run (minutes)', ['gate', 'timeout_min'], { type: 'number', min: 1 })),
      h('h3', {}, 'Setup checks'),
      h('p', { class: 'help' }, 'Run before any guardrail; a failure stops the gate with the error shown (for example: node_modules missing).'),
      h('ol', { class: 'items' }, setup.map((c, i) => {
        const P = ['gate', 'setup', i]
        return h('li', { class: 'item' }, h('div', { class: 'item-head' }, h('strong', {}, c.error || c.test || `check ${i + 1}`), h('span', { class: 'spacer' }),
          moveBtns(list(['gate', 'setup']), i), removeBtn(list(['gate', 'setup']), i, 'check')),
        grid(field('Test (bash)', [...P, 'test'], { wide: true, placeholder: '[[ -d node_modules ]]' }), field('Error', [...P, 'error'], { wide: true })))
      })),
      h('button', { type: 'button', onclick: () => { list(['gate', 'setup']).push({ test: '', error: '' }); markDirty(); render() } }, 'Add check')))
}

// A guardrail's environment as KEY = value rows (no secrets: the values are in the config). TOPICS gets
// one "name: word, phrase" line per topic and PATTERNS one regex per line.
function envRows(path, step) {
  const cur = val(path) || {}
  const put = (k, v) => { setIn(path, { ...(val(path) || {}), [k]: v }) }
  const rows = Object.entries(cur).map(([k, v]) => {
    const id = nextId('env')
    let input
    if (k === 'TOPICS') {
      input = h('textarea', { id, class: 'mono', rows: Math.max(2, String(v).split(';').length + 1), placeholder: 'codenames: project falcon, skunkworks',
        onchange: (e) => put(k, e.target.value.split('\n').map((x) => x.trim()).filter(Boolean).join('; ')) },
      String(v).split(';').map((x) => x.trim()).filter(Boolean).join('\n'))
    } else if (k === 'PATTERNS' || String(v).includes('\n')) {
      input = h('textarea', { id, class: 'mono', rows: Math.max(2, String(v).split('\n').length + 1), onchange: (e) => put(k, e.target.value.replace(/\s+$/, '')) }, v)
    } else {
      input = h('input', { id, type: 'text', class: 'mono', value: v, spellcheck: 'false', autocomplete: 'off', onchange: (e) => put(k, e.target.value) })
    }
    const help = { TOPICS: 'One topic per line: name: word, phrase. Matched case-insensitively on word boundaries; matches are never logged.',
      PATTERNS: 'One Python regex per line, searched in added lines; (?i) for case-insensitive.', PATHS: 'Space-separated globs; empty checks every file.' }[k]
    return h('div', { class: 'envrow' }, h('label', { for: id, class: 'mono' }, k), input,
      h('button', { type: 'button', class: 'icon danger', title: `Remove ${k}`, 'aria-label': `Remove ${k}`, onclick: () => {
        const next = { ...(val(path) || {}) }
        delete next[k]
        setIn(path, next)
        render()
      } }, '✕'),
      help ? h('div', { class: 'help' }, help) : null)
  })
  const add = h('input', { type: 'text', class: 'mono', placeholder: 'NEW_VARIABLE', 'aria-label': `New environment variable for ${step.key}`, autocomplete: 'off',
    onkeydown: (e) => {
      if (e.key !== 'Enter') return
      e.preventDefault()
      const k = e.target.value.trim()
      if (!/^[A-Za-z_]\w*$/.test(k)) { e.target.setCustomValidity('Letters, digits and _'); e.target.reportValidity(); e.target.setCustomValidity(''); return }
      put(k, '')
      render()
    } })
  return h('div', { class: 'field wide env' }, h('label', {}, 'Environment (no secrets)'), ...rows,
    h('div', { class: 'envrow add' }, add, h('span', { class: 'help' }, 'Enter adds a variable')))
}

// ---- stage editors used by the loop inspector (builders and reviewers are agent stages) -----------
// `def`: the default agent name ('' then means "use it"); without one an agent must be chosen.
function agentOptions(def, cur) {
  const names = (S.server.agents || []).map((a) => a.name)
  const opts = [['', def ? `(default: ${def})` : '(choose an agent)'], ...names.map((n) => [n, n])]
  if (cur && !names.includes(cur)) opts.push([cur, `${cur} (missing)`])
  return opts
}

function scopedEditor(path, cur = val(path) || []) {
  const id = nextId('sc')
  const text = cur.map((s) => `${(s.paths || []).join(',')} :: ${s.pattern}`).join('\n')
  return h('div', { class: 'field wide' }, h('label', { for: id }, 'Scoped patterns (globs, comma separated :: regex)'),
    h('textarea', { id, class: 'mono', rows: 2, placeholder: 'src/**/*.py :: @Property', onchange: (e) => {
      const out = []
      for (const line of e.target.value.split('\n')) {
        const [g, ...rest] = line.split('::')
        if (!rest.length) continue
        out.push({ paths: g.split(',').map((x) => x.trim()).filter(Boolean), pattern: rest.join('::').trim() })
      }
      setIn(path, out)
    } }, text))
}

function metricEditor(path, cur = val(path) || []) {
  const id = nextId('mt')
  return h('div', { class: 'field wide' }, h('label', { for: id }, 'Gate metrics (key > value per line)'),
    h('textarea', { id, class: 'mono', rows: 2, placeholder: 'data_mb_delta > 1', onchange: (e) => {
      const out = []
      for (const line of e.target.value.split('\n')) {
        const m = line.match(/^\s*([\w.-]+)\s*>\s*(-?[\d.]+)\s*$/)
        if (m) out.push({ key: m[1], gt: Number(m[2]) })
      }
      setIn(path, out)
    } }, cur.map((m) => `${m.key} > ${m.gt}`).join('\n')))
}

// ---- Connections ---------------------------------------------------------------------------------
// ---- Connection checks ----------------------------------------------------------------------------
// The last `factory doctor --only <x>` result per project stays on screen as a badge. git and tickets
// check themselves when the Connections tab opens and again after each save; the runtime probe only
// runs when asked (it may call a paid API).
const CONN_LABELS = { checking: 'Checking…', ok: 'Connected', error: 'Not connected' }

function connStatuses() {
  if (!S.conn || S.conn.pid !== S.pid) S.conn = { pid: S.pid, results: {} }
  return S.conn
}

async function checkConnection(only) {
  const box = connStatuses()
  const tab = S.tab
  box.results[only] = { state: 'checking', rows: [], error: null }
  render()
  let st
  try {
    const r = await api('POST', 'doctor', { only })
    const rows = Array.isArray(r.data) ? r.data : []
    st = { state: rows.length && rows.every((x) => x.ok) ? 'ok' : 'error', rows, error: rows.length ? null : (r.error || 'No result') }
  } catch (e) { st = { state: 'error', rows: [], error: e.message } }
  box.results[only] = st
  if (S.conn === box && S.tab === tab) render()
}

function autoCheck(only) {
  const box = connStatuses()
  if (box.results[only] || !$('save').disabled) return  // the check reads the saved config
  box.results[only] = { state: 'checking', rows: [], error: null }
  setTimeout(() => checkConnection(only))  // not from inside the render that called us
}

function doctorButton(only, label) {
  const st = connStatuses().results[only]
  const badge = st ? h('span', { class: `cstate ${st.state}` },
    only === 'runtime' && st.state !== 'checking' ? (st.state === 'ok' ? 'Working' : 'Failing') : CONN_LABELS[st.state]) : null
  const btn = h('button', { type: 'button', disabled: !!st && st.state === 'checking', onclick: () => checkConnection(only) }, label)
  const rows = st && st.state !== 'checking'
    ? [...st.rows.map((x) => h('div', { class: `msg ${x.ok ? 'ok' : 'error'}` }, h('b', {}, (x.ok ? 'OK ' : 'Failed ') + x.name + ' '), x.detail || '')),
        st.error ? h('div', { class: 'msg error' }, st.error) : null]
    : []
  return h('div', {},
    h('div', { class: 'row' }, btn, badge, h('span', { class: 'help' }, $('save').disabled ? '' : 'Save first: the test uses the saved config.')),
    h('div', { class: 'doctor', 'aria-live': 'polite' }, rows))
}

function renderConnections() {
  const gp = val(['connections', 'git', 'provider'])
  const tp = val(['connections', 'tickets', 'provider'])
  if (gp && gp !== 'none') autoCheck('git')
  if (tp === 'jira') autoCheck('tickets')
  const P = ['connections', 'tickets']
  return h('div', {},
    h('div', { class: 'msg info' }, 'Credentials are referenced, never stored: write env:JIRA_TOKEN (read from the environment) or keyring:factory/jira (read from the OS keyring). A value that is not a reference is rejected.'),
    section('Git host',
      grid(
        field('Provider', ['connections', 'git', 'provider'], { type: 'select', options: ['github', 'gitlab', 'bitbucket', 'none'], rerender: true,
          help: 'github and gitlab use the gh / glab CLI and its login; bitbucket uses its REST API.' }),
        field('Remote', ['connections', 'git', 'remote']),
        gp === 'github' || gp === 'gitlab' ? field('Host', ['connections', 'git', 'host'], { emptyNull: true, placeholder: gp === 'github' ? 'github.example.com (Enterprise only)' : 'gitlab.example.com (self-hosted only)' }) : null,
        gp !== 'none' ? refField('Token', ['connections', 'git', 'token_ref'], gp === 'bitbucket' ? 'App password or access token reference.' : 'Optional: gh / glab use their own login when unset.') : null,
        gp === 'bitbucket' ? field('Workspace', ['connections', 'git', 'bitbucket_workspace'], { emptyNull: true }) : null,
        gp === 'bitbucket' ? field('Repository slug', ['connections', 'git', 'bitbucket_repo'], { emptyNull: true }) : null),
      doctorButton('git', 'Test git connection')),
    section('Tickets',
      grid(
        field('Provider', [...P, 'provider'], { type: 'select', options: ['none', 'jira'], rerender: true }),
        tp === 'jira' ? field('Mode', [...P, 'mode'], { type: 'select', options: [['rest', 'rest: the factory calls Jira'], ['mcp', 'mcp: the Claude Code session\'s Atlassian MCP']], rerender: true }) : null),
      tp === 'jira' ? grid(
        field('Site', [...P, 'site'], { emptyNull: true, placeholder: 'https://your-org.atlassian.net' }),
        field('Project key', [...P, 'project'], { emptyNull: true, placeholder: 'ABC' }),
        field('Label', [...P, 'label'], { help: 'Tickets with this label are queued by sync.' }),
        field('JQL', [...P, 'jql'], { emptyNull: true, wide: true, placeholder: 'project = ABC AND labels = factory AND statusCategory = "To Do" ORDER BY rank ASC' }),
        val([...P, 'mode']) === 'rest' ? refField('Account email', [...P, 'email_ref'], 'Reference to the Jira account email (env:JIRA_EMAIL).') : null,
        val([...P, 'mode']) === 'rest' ? refField('API token', [...P, 'token_ref']) : null,
        field('Epic branch glob', [...P, 'epic_branch_glob'], { help: '{epic} is the epic key; matched against remote branches to pick the base.' }),
        field('Epic with no branch', [...P, 'no_epic_branch'], { type: 'select', options: [['block', 'block the ticket'], ['fallback', 'fall back to the default base']] }),
        field('Write back', [...P, 'write_back'], { type: 'select', options: [['none', 'none'], ['draft', 'draft: write jira-comment.md for a human'], ['comment', 'comment on delivery'], ['transition', 'comment and transition on delivery']], rerender: true }),
        val([...P, 'write_back']) === 'transition' ? field('Transition to', [...P, 'transition_to'], { emptyNull: true, placeholder: 'In Review' }) : null,
        val([...P, 'mode']) === 'rest' ? field('New issue type', [...P, 'create_type'], { placeholder: 'Task', help: 'Issue type for backlog items the Backlog tab\'s Sync with Jira creates.' }) : null,
        field('Ticket key pattern', [...P, 'key_pattern'])) : null,
      tp === 'jira' ? doctorButton('tickets', 'Test Jira connection') : null))
}

// ---- Delivery ------------------------------------------------------------------------------------
const MODES = [
  ['none', 'None', 'Stop at ready. The card shows the push and PR commands for you to run.'],
  ['push', 'Push branch', 'Push the job branch to the remote. You open the PR.'],
  ['pr', 'Pull request', 'Push the branch and open a PR / MR on the git host.'],
  ['direct', 'Direct to base', 'Put the commits straight onto the base branch, with no PR.'],
]
// Mode changes go through here so choosing None, from the Deliver tab or the loop designer's Deliver
// block, also drops the block's layout position (the other delivery settings are kept).
function setDeliveryMode(v) {
  setIn(['delivery', 'mode'], v)
  if (v === 'none') setIn(['delivery', 'pos'], undefined)
}
// The delivery settings, shared by the Deliver tab and the Deliver block's inspector in the loop designer:
// `core` is the mode, approval and warnings; `sections` the Pull request / Direct to base cards.
function deliverySettings() {
  const mode = val(['delivery', 'mode'])
  const warns = (S.issues.warnings || []).filter((w) => /deliver|direct|approval/i.test(w))
  const core = [
    h('fieldset', { class: 'radios' }, h('legend', {}, 'Mode'), MODES.map(([v, l, help]) => {
      const id = `dm-${v}`
      return h('div', { class: 'radio' + (mode === v ? ' sel' : '') },
        h('input', { type: 'radio', name: 'delivery-mode', id, value: v, checked: mode === v, onchange: () => { setDeliveryMode(v); render() } }),
        h('label', { for: id }, h('strong', {}, l), h('span', { class: 'help' }, help)))
    })),
    mode !== 'none' ? field('Require a human approval before delivering', ['delivery', 'require_approval'], { type: 'bool', rerender: true,
      help: 'The job waits in an approval column; approve from the studio, `factory approve`, or /factory deliver.' }) : null,
    ...warns.map((w) => h('div', { class: 'msg warn' }, w)),
    mode === 'direct' && val(['delivery', 'require_approval']) === false && !warns.length ? h('div', { class: 'msg warn' }, 'Commits will land on the base branch with no human in the loop. Save to see the server\'s check.') : null,
  ]
  const sections = [
    mode === 'pr' ? section('Pull request',
      grid(field('Open as draft', ['delivery', 'pr', 'draft'], { type: 'bool' }),
        field('Reviewers', ['delivery', 'pr', 'reviewers'], { type: 'list' }),
        field('Labels', ['delivery', 'pr', 'labels'], { type: 'list' })),
      h('p', { class: 'help' }, `Git host: ${val(['connections', 'git', 'provider'])} (Connections tab).`)) : null,
    mode === 'direct' ? section('Direct to base',
      grid(field('Strategy', ['delivery', 'direct', 'strategy'], { type: 'select', options: [['ff-only', 'fast-forward only'], ['squash', 'squash into one commit']] }),
        field('Push the base branch', ['delivery', 'direct', 'push'], { type: 'bool', help: 'Off: update the local base branch only (refused when it is checked out).' }))) : null,
  ]
  return { core: core.filter(Boolean), sections: sections.filter(Boolean) }  // replaceChildren would print a null
}
function renderDelivery() {
  const { core, sections } = deliverySettings()
  return h('div', {},
    section('How a finished job leaves the factory',
      h('p', { class: 'help' }, 'Delivery runs on its own after the last stage of the loop. The loop designer shows it as a single Deliver block with these same settings.'),
      ...core),
    ...sections)
}

// ---- Runtime -------------------------------------------------------------------------------------
const KINDS = [
  ['claude-code', 'Claude Code (interactive)', 'Type /factory in a Claude Code session; the session is the orchestrator. Hooks guard every tool call.'],
  ['claude-cli', 'Claude Code CLI (headless)', '`factory run` drives the loop and runs each agent with `claude -p`. Hooks still apply.'],
  ['codex-cli', 'Codex CLI (headless)', '`factory run` runs each agent with `codex exec`. The Codex sandbox is the boundary.'],
  ['api', 'LLM API (headless)', '`factory run` calls a model directly through LiteLLM with the factory\'s own guarded tools.'],
]
function renderRuntime() {
  const kind = val(['runtime', 'kind'])
  const provider = val(['runtime', 'api', 'provider'])
  const agents = (S.server.agents || []).map((a) => a.name)
  return h('div', {},
    section('How agents run',
      h('fieldset', { class: 'radios' }, h('legend', {}, 'Runtime'), KINDS.map(([v, l, help]) => {
        const id = `rk-${v}`
        return h('div', { class: 'radio' + (kind === v ? ' sel' : '') },
          h('input', { type: 'radio', name: 'runtime-kind', id, checked: kind === v, onchange: () => { setIn(['runtime', 'kind'], v); render() } }),
          h('label', { for: id }, h('strong', {}, l), h('span', { class: 'help' }, help)))
      })),
      grid(field('Default model', ['runtime', 'default_model'], { help: 'Alias or id for agents that name no model.' }),
        field('Parallel agents', ['runtime', 'concurrency'], { type: 'number', min: 1, max: 16, help: 'Headless runtimes.' }))),
    kind === 'api' ? section('LLM API',
      grid(
        field('Provider', ['runtime', 'api', 'provider'], { type: 'select', options: ['anthropic', 'openai', 'bedrock', 'ollama', 'litellm', 'openai-compatible'], rerender: true }),
        ['ollama', 'litellm', 'openai-compatible'].includes(provider) || val(['runtime', 'api', 'base_url'])
          ? field('Base URL', ['runtime', 'api', 'base_url'], { emptyNull: true, placeholder: provider === 'ollama' ? 'http://localhost:11434' : provider === 'litellm' ? 'http://localhost:4000' : 'https://...' }) : null,
        provider !== 'ollama' && provider !== 'bedrock' ? refField('API key', ['runtime', 'api', 'api_key_ref']) : null,
        field('Temperature', ['runtime', 'api', 'temperature'], { type: 'number', min: 0, max: 2, step: 0.1 }),
        field('Max turns per agent', ['runtime', 'api', 'max_turns'], { type: 'number', min: 1 }),
        field('Tool output limit (chars)', ['runtime', 'api', 'tool_output_limit'], { type: 'number', min: 1000 }),
        field('Command timeout (s)', ['runtime', 'api', 'command_timeout_s'], { type: 'number', min: 10 })),
      provider === 'bedrock' ? h('p', { class: 'help' }, 'Bedrock uses the standard AWS credential chain (profile, SSO or role).') : null) : null,
    kind === 'claude-cli' ? section('Claude Code CLI',
      grid(field('Binary', ['runtime', 'claude_cli', 'binary']),
        field('Permission mode', ['runtime', 'claude_cli', 'permission_mode'], { type: 'select', options: ['default', 'acceptEdits', 'bypassPermissions'] }),
        field('Extra arguments', ['runtime', 'claude_cli', 'extra_args'], { type: 'list', wide: true }))) : null,
    kind === 'codex-cli' ? section('Codex CLI',
      grid(field('Binary', ['runtime', 'codex_cli', 'binary']),
        field('Sandbox', ['runtime', 'codex_cli', 'sandbox'], { type: 'select', options: ['read-only', 'workspace-write', 'danger-full-access'] }),
        field('Extra arguments', ['runtime', 'codex_cli', 'extra_args'], { type: 'list', wide: true }))) : null,
    section('Model aliases',
      h('p', { class: 'help' }, 'Agent files name a model alias (opus, sonnet, haiku). Map each to a model id for this runtime, e.g. opus to llama3.2 for Ollama. Unmapped aliases pass through.'),
      mapEditor(['runtime', 'models'], 'alias', 'model id', ['opus', 'sonnet', 'haiku'])),
    section('Per-agent overrides', mapEditor(['runtime', 'per_agent'], 'agent', 'model', agents)),
    section('Check', doctorButton('runtime', 'Probe runtime')))
}

function mapEditor(path, kLabel, vLabel, suggestions) {
  const cur = val(path) || {}
  const entries = Object.entries(cur)
  const listId = nextId('dl')
  const rows = entries.map(([k, v], i) => {
    const kid = nextId('mk'), vid = nextId('mv')
    return h('div', { class: 'maprow' },
      h('label', { for: kid, class: 'sr' }, kLabel), h('input', { id: kid, type: 'text', value: k, list: listId, onchange: (e) => {
        const o = {}
        entries.forEach(([kk, vv], j) => { o[j === i ? e.target.value : kk] = vv })
        setIn(path, o); render()
      } }),
      h('span', { 'aria-hidden': 'true' }, '→'),
      h('label', { for: vid, class: 'sr' }, vLabel), h('input', { id: vid, type: 'text', value: v, onchange: (e) => setIn([...path, k], e.target.value) }),
      h('button', { type: 'button', class: 'icon danger', 'aria-label': `Remove ${k}`, onclick: () => { const o = { ...cur }; delete o[k]; setIn(path, o); render() } }, '✕'))
  })
  return h('div', { class: 'map' }, h('datalist', { id: listId }, suggestions.map((s) => h('option', { value: s }))), rows,
    h('button', { type: 'button', onclick: () => {
      const free = suggestions.find((s) => !(s in cur)) || `new${entries.length + 1}`
      setIn(path, { ...cur, [free]: '' }); render()
    } }, `Add ${kLabel}`))
}

// ---- Raw YAML ------------------------------------------------------------------------------------
function renderYaml() {
  const ta = h('textarea', { id: 'yaml', class: 'mono yaml', rows: 40, spellcheck: 'false', 'aria-label': 'factory.yaml' }, S.yamlText ?? '')
  if (S.yamlText === null) {
    ta.value = 'Loading...'
    api('POST', 'yaml/dump', { config: S.cfg }).then((r) => { S.yamlText = r.text; ta.value = r.text }).catch((e) => toast(e.message, true))
  }
  ta.addEventListener('input', () => { S.yamlText = ta.value })
  return section('factory.yaml',
    h('p', { class: 'help' }, 'The working copy as YAML. Apply copies your text into the working copy; Save writes it.'),
    ta,
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'primary', onclick: async () => {
        try { const r = await api('POST', 'yaml/parse', { text: ta.value }); S.cfg = r.config; markDirty(); toast('Applied to the working copy; Save to write it') } catch (e) { toast(e.message, true) }
      } }, 'Apply'),
      h('button', { type: 'button', onclick: () => { S.yamlText = null; render() } }, 'Reset from working copy')))
}

// ---- load and save -------------------------------------------------------------------------------
async function load() {
  const st = await api('GET', 'state')
  S.server = st
  S.cfg = clone(st.config) || {}
  S.saved = JSON.stringify(S.cfg)
  S.yamlText = null
  S.issues = { errors: st.errors || [], warnings: st.warnings || [] }
  const repo = (st.effective && st.effective.repo.name) || ''
  const proj = S.projects.find((x) => x.id === S.pid)
  $('title').textContent = (proj && proj.name) || repo
  document.title = repo ? `${repo} · Ajantrik` : 'Ajantrik · Software Factory'
  $('root').textContent = st.root
  markDirty()
  render()
}

async function save() {
  $('save').disabled = true
  try {
    const r = await api('PUT', 'config', { config: S.cfg })
    if (!r.saved) {
      S.issues = { errors: r.errors || ['not saved'], warnings: r.warnings || [] }
      renderMessages()
      markDirty()
      toast('Not saved: fix the errors above', true)
      return
    }
    const cur = S.tab
    S.conn = null  // the saved config changed: check the connections again
    await load()
    S.tab = cur
    S.issues = { errors: r.errors || [], warnings: r.warnings || [] }
    render()
    if (r.render && r.render.ok === false) toast(`Saved, but render failed: ${r.render.error}`, true)
    else toast('Saved and rendered')
  } catch (e) { toast(e.message, true); markDirty() }
}

$('save').addEventListener('click', save)
$('revert').addEventListener('click', () => {
  if (!confirm('Discard unsaved changes?')) return
  S.cfg = JSON.parse(S.saved)
  S.yamlText = null
  S.issues = { errors: S.server.errors || [], warnings: S.server.warnings || [] }
  markDirty()
  render()
})
window.addEventListener('beforeunload', (e) => { if (!$('save').disabled) { e.preventDefault(); e.returnValue = '' } })
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); if (!$('save').disabled) save() }
})
$('new-job').addEventListener('click', openNewJob)
$('waiting').addEventListener('click', openWaiting)

// ---- projects: the home page, and switching between projects --------------------------------------
let projectsTimer = null
function ago(iso) {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
const RUNTIME_LABEL = { 'claude-code': 'Claude Code (interactive)', 'claude-cli': 'Claude Code CLI', 'codex-cli': 'Codex CLI', api: 'LLM API' }

function showChrome(project) {
  $('home').hidden = !!project
  $('layout').hidden = !project
  for (const id of ['save', 'revert']) $(id).hidden = !project
  if (!project) $('dirty').hidden = true
  $('crumb-sep').hidden = !project
  if (!project) { $('title').textContent = ''; $('root').textContent = 'Every repo this machine runs a factory for' }
}

async function showHome() {
  if (S.pid && !$('save').disabled && !confirm('Leave this project with unsaved changes?')) {
    history.replaceState(null, '', `#/p/${encodeURIComponent(S.pid)}/${S.tab}`)
    return
  }
  S.pid = null
  clearInterval(jobsTimer)
  showChrome(false)
  document.title = 'Projects · Ajantrik'
  $('board-frame').removeAttribute('src')
  renderHome()
  clearInterval(projectsTimer)
  const tick = async () => {
    if (S.pid) { clearInterval(projectsTimer); return }
    try { S.projects = await api('GET', 'projects'); renderHome() } catch (e) { toast(e.message, true) }
  }
  await tick()
  projectsTimer = setInterval(tick, 5000)
}

function renderHome() {
  const tiles = S.projects.map((p) => {
    const jobs = p.jobs || { active: [], waiting: [] }
    const bl = p.backlog || { queued: 0, blocked: 0, next: [] }
    const state = !p.ok ? 'bad' : jobs.active.length ? 'busy' : jobs.waiting.length ? 'wait' : 'idle'
    const stateText = { bad: 'Needs setup', busy: `${jobs.active.length} running`, wait: `${jobs.waiting.length} waiting on you`, idle: 'Idle' }[state]
    const open = () => { location.hash = `#/p/${encodeURIComponent(p.id)}` }
    return h('article', { class: `ptile s-${state}`, tabindex: '0', role: 'link', 'aria-label': `Open ${p.name}`,
      onclick: (e) => { if (!e.target.closest('button')) open() }, onkeydown: (e) => { if (e.key === 'Enter') open() } },
    h('header', {},
      h('div', {}, h('h3', {}, p.name), h('div', { class: 'proot' }, p.root)),
      h('span', { class: `pstate s-${state}` }, stateText)),
    h('div', { class: 'pmeta' },
      h('span', { class: 'pill' }, RUNTIME_LABEL[p.runtime] || p.runtime || '?'),
      h('span', { class: 'pill' }, `base ${p.base || '?'}`),
      h('span', { class: 'pill' }, `deliver: ${p.delivery || 'none'}`),
      p.tickets && p.tickets !== 'none' ? h('span', { class: 'pill' }, p.tickets) : null),
    (p.errors || []).length ? h('p', { class: 'msg error' }, p.errors.join('; ')) : null,
    h('div', { class: 'pstats' },
      h('div', {}, h('b', {}, String(jobs.active.length)), h('span', {}, 'running')),
      h('div', {}, h('b', {}, String(jobs.waiting.length)), h('span', {}, 'waiting on you')),
      h('div', {}, h('b', {}, String(bl.queued)), h('span', {}, 'in backlog')),
      h('div', {}, h('b', {}, String(jobs.delivered || 0)), h('span', {}, 'delivered'))),
    jobs.active.length || jobs.waiting.length ? h('ul', { class: 'pjobs' },
      [...jobs.active.map((j) => ['run', j]), ...jobs.waiting.map((j) => ['wait', j])].slice(0, 4).map(([k, j]) =>
        h('li', { class: k }, h('code', {}, j.id.split('-')[0]), ' ', h('span', { class: 'pf' }, j.feature || ''),
          h('span', { class: 'ps' }, k === 'run' ? `${j.stage}${j.agent ? ` · ${j.agent}` : ''}${j.round ? ` · round ${j.round}` : ''}` : j.stage)))) : null,
    bl.next && bl.next.length ? h('div', { class: 'pnext' }, h('span', { class: 'help' }, 'Next in backlog: '), bl.next[0]) : null,
    h('footer', {},
      h('span', { class: 'help' }, jobs.last_updated ? `Last activity ${ago(jobs.last_updated)}` : `${jobs.total || 0} jobs`),
      h('span', { class: 'spacer' }),
      h('button', { type: 'button', class: 'small ghost', onclick: async () => {
        if (!confirm(`Forget ${p.name}? Its repo and .factory/ files stay as they are; you can add it again.`)) return
        try { await api('DELETE', `projects/${encodeURIComponent(p.id)}`); S.projects = S.projects.filter((x) => x.id !== p.id); renderHome() } catch (e) { toast(e.message, true) }
      } }, 'Remove'),
      h('button', { type: 'button', class: 'small primary', onclick: open }, 'Open')))
  })
  $('home').replaceChildren(h('div', { class: 'home-inner' },
    h('div', { class: 'home-head' }, h('h2', {}, 'Projects'), h('span', { class: 'spacer' }),
      h('button', { type: 'button', class: 'primary', onclick: openNewProject }, '+ New project')),
    tiles.length ? h('div', { class: 'ptiles' }, tiles)
      : h('div', { class: 'empty-home' }, h('p', {}, 'No projects yet. A project is one git repository with its own factory: loop, agents, skills and gate.'),
        h('button', { type: 'button', class: 'primary', onclick: openNewProject }, '+ New project'))))
}

async function openNewProject() {
  const dlg = $('dlg-project')
  const presets = await api('GET', 'presets').catch(() => [{ name: 'blank', title: 'Blank' }])
  const path = h('input', { id: 'np-path', type: 'text', placeholder: '/home/you/repos/my-app', autocomplete: 'off', spellcheck: 'false', required: true })
  const name = h('input', { id: 'np-name', type: 'text', placeholder: 'defaults to the folder name', autocomplete: 'off' })
  const info = h('div', { class: 'np-info', 'aria-live': 'polite' })
  const preset = h('select', { id: 'np-preset' }, presets.map((p) => h('option', { value: p.name, selected: p.name === 'blank' }, `${p.title} (${p.name})`)))
  const runtimes = ['claude-code', 'claude-cli', 'codex-cli', 'api']
  const RT_HELP = { 'claude-code': 'You run /factory in Claude Code; the session orchestrates.', 'claude-cli': 'Ajantrik drives `claude -p` headless.', 'codex-cli': 'Ajantrik drives `codex exec` headless.', api: 'Ajantrik calls a model API itself: Anthropic, OpenAI, Bedrock, Ollama or a LiteLLM proxy.' }
  let runtime = 'claude-code'
  const apiBox = h('div', { class: 'grid', hidden: true },
    h('div', { class: 'field' }, h('label', { for: 'np-prov' }, 'Provider'),
      h('select', { id: 'np-prov' }, ['anthropic', 'openai', 'bedrock', 'ollama', 'litellm', 'openai-compatible'].map((x) => h('option', { value: x }, x)))),
    h('div', { class: 'field' }, h('label', { for: 'np-model' }, 'Model'), h('input', { id: 'np-model', type: 'text', placeholder: 'e.g. claude-sonnet-5-5 or llama3.2' })),
    h('div', { class: 'field' }, h('label', { for: 'np-base' }, 'Base URL'), h('input', { id: 'np-base', type: 'text', placeholder: 'only for Ollama, a proxy or a compatible server' })),
    h('div', { class: 'field' }, h('label', { for: 'np-key' }, 'API key reference'), h('input', { id: 'np-key', type: 'text', placeholder: 'env:ANTHROPIC_API_KEY' })))
  const rtRadios = h('fieldset', { class: 'radios' }, h('legend', {}, 'Coding runtime'),
    runtimes.map((r) => h('div', { class: `radio${r === runtime ? ' sel' : ''}` },
      h('input', { type: 'radio', name: 'np-rt', id: `np-rt-${r}`, value: r, checked: r === runtime, onchange: () => {
        runtime = r
        rtRadios.querySelectorAll('.radio').forEach((el) => el.classList.toggle('sel', el.contains($(`np-rt-${r}`))))
        apiBox.hidden = r !== 'api'
      } }),
      h('label', { for: `np-rt-${r}` }, h('b', {}, RUNTIME_LABEL[r]), h('span', { class: 'help' }, RT_HELP[r])))))
  let inspected = null
  const check = async () => {
    if (!path.value.trim()) { info.replaceChildren(); return }
    try {
      inspected = await api('POST', 'projects/inspect', { path: path.value.trim() })
      const i = inspected
      if (!i.exists) info.replaceChildren(h('p', { class: 'msg error' }, 'No such folder.'))
      else if (!i.git) info.replaceChildren(h('p', { class: 'msg error' }, `${i.root} is not the root of a git repository.`))
      else if (i.registered) info.replaceChildren(h('p', { class: 'msg warn' }, 'Already a project here; adding it again just opens it.'))
      else if (i.configured) info.replaceChildren(h('p', { class: 'msg ok' }, `This repo already has a factory (runtime ${i.runtime || '?'}). It is added as it is; runtime and preset below are ignored.`))
      else info.replaceChildren(h('p', { class: 'msg ok' }, `Git repo found. Ajantrik will write .factory/ and .claude/ into ${i.root} from the preset below.`))
      if (i.exists && !name.value) name.placeholder = i.name
      preset.disabled = !!i.configured
    } catch (e) { info.replaceChildren(h('p', { class: 'msg error' }, e.message)) }
  }
  path.addEventListener('change', check)
  path.addEventListener('blur', check)
  const submit = h('button', { type: 'submit', class: 'primary' }, 'Create project')
  const form = h('form', { method: 'dialog', class: 'dlg', onsubmit: async (e) => {
    e.preventDefault()
    const key = $('np-key').value.trim()
    if (runtime === 'api' && key && !REF.test(key)) { toast('The API key must be a reference like env:NAME, never the key itself.', true); return }
    submit.disabled = true
    submit.textContent = 'Setting up...'
    try {
      const r = await api('POST', 'projects', { path: path.value.trim(), name: name.value.trim() || null, runtime, preset: preset.value,
        api: runtime === 'api' ? { provider: $('np-prov').value, model: $('np-model').value.trim(), base_url: $('np-base').value.trim(), api_key_ref: key } : null })
      dlg.close()
      toast(r.set_up ? `Set up ${r.project.name}: ${r.written} files written into the repo` : `Added ${r.project.name}`)
      location.hash = `#/p/${encodeURIComponent(r.project.id)}`
    } catch (err) { toast(err.message, true) }
    submit.disabled = false
    submit.textContent = 'Create project'
  } },
  h('header', { class: 'dlg-head' }, h('h2', {}, 'New project'),
    h('button', { type: 'button', class: 'ghost icon', 'aria-label': 'Close', onclick: () => dlg.close() }, '×')),
  h('p', { class: 'help' }, 'A project is one git repository. Ajantrik copies the factory files into it (.factory/ and .claude/) and keeps its loop, agents, skills and gate there.'),
  h('div', { class: 'field' }, h('label', { for: 'np-path' }, 'Repository path'), path),
  info,
  h('div', { class: 'grid' },
    h('div', { class: 'field' }, h('label', { for: 'np-name' }, 'Name'), name),
    h('div', { class: 'field' }, h('label', { for: 'np-preset' }, 'Start from'), preset)),
  rtRadios, apiBox,
  h('div', { class: 'row end' }, h('button', { type: 'button', onclick: () => dlg.close() }, 'Cancel'), submit))
  dlg.replaceChildren(form)
  dlg.showModal()
  path.focus()
}

async function openProject(pid, tab) {
  if (!S.projects.length) { try { S.projects = await api('GET', 'projects') } catch { /* names only */ } }
  if (S.pid !== pid) {
    if (S.pid && !$('save').disabled && !confirm('Leave this project with unsaved changes?')) {
      history.replaceState(null, '', `#/p/${encodeURIComponent(S.pid)}/${S.tab}`)
      return
    }
    clearInterval(projectsTimer)
    S.pid = pid
    S.designer = null
    S.agent = null
    S.skill = null
    S.lastTab = null
    showChrome(true)
    $('board-frame').src = `/p/${encodeURIComponent(pid)}/board/dashboard.html`
    try { await load() } catch (e) {
      toast(e.message, true)
      location.hash = '#/'
      return
    }
  }
  let saved = null
  try { saved = localStorage.getItem(`studio.tab.${pid}`) } catch { /* private mode */ }
  // the Guardrails tab used to be called gate
  if (tab === 'gate') tab = 'guardrails'
  if (saved === 'gate') saved = 'guardrails'
  S.tab = TABS.some(([k]) => k === tab) ? tab : (TABS.some(([k]) => k === saved) ? saved : 'board')
  render()
}

function route() {
  const m = location.hash.match(/^#\/p\/([^/]+)(?:\/([a-z]+))?/)
  if (m) openProject(decodeURIComponent(m[1]), m[2])
  else showHome()
}
window.addEventListener('hashchange', route)

// ---- Board actions: the board iframe has no token, so it asks this page to act for it -----------------------
const BOARD_ACTIONS = { go: 'Go', approve: 'Approve', deliver: 'Deliver' }
window.addEventListener('message', async (event) => {
  if (!(event.origin === location.origin && event.source === $('board-frame').contentWindow)) return
  const { type, job, action, stage } = event.data || {}
  if (type !== 'factory-board-action' || typeof action !== 'string' || !Object.hasOwn(BOARD_ACTIONS, action)) return
  if (!S.pid || typeof job !== 'string' || !/^[0-9]{3}-[A-Za-z0-9_-]+$/.test(job)) return
  if (action === 'approve' && !(typeof stage === 'string' && /^[a-z][a-z0-9-]*$/.test(stage))) return
  let ok = false
  try {
    const r = await api('POST', `jobs/${job}/${action}`, action === 'approve' ? { stage } : {})
    ok = !(r && r.error)
    toast(r && r.error ? r.error : `${BOARD_ACTIONS[action]}: done`, !ok)
  } catch (err) {
    toast(err.message, true)
  } finally {
    event.source.postMessage({ type: 'factory-board-result', job, action, ok }, location.origin)
  }
})
;(async () => {
  try {
    const ws = await api('GET', 'workspace')
    if (!location.hash && ws.default) location.hash = `#/p/${encodeURIComponent(ws.default)}`
  } catch { /* fall through to the projects page */ }
  route()
})()
