#!/usr/bin/env node
// Staging screenshot harness for the accounts-portal factory.
//
//   .factory/harness/with-app.sh <checkout> -- node .factory/harness/shoot.mjs <shots.json>
//
// Logs in to the staging Auth0 tenant as the e2e user, then for each shot opens a route at a
// viewport, runs its steps and saves PNGs. The session is READ-ONLY: every POST/PUT/PATCH/DELETE
// is aborted except Auth0's own login/token calls and GraphQL queries (never mutations), so a
// stray click cannot create a checkout session, invite a member, revoke a key or upload a file.
//
// shots.json:
// {
//   "outDir": "/abs/path/shots",                  // required; PNGs and <name>.report.json land here
//   "shots": [{
//     "name": "seats-tab-typical-after",          // PNG stem: <view>-<state>-<before|after|mockup>
//     "path": "/dashboard",                         // app route
//     "viewport": "desktop",                        // desktop 1440x900 | mobile 390x844 | {"width":..,"height":..}
//     "fullPage": false,
//     "steps": [                                    // Playwright selectors: text=..., css, [data-cy=...]
//       {"click": "text=Seats"}, {"waitFor": "[data-cy=email-input]"}, {"waitForText": "unused license"},
//       {"hover": "..."}, {"fill": ["input[name=x]", "text"]}, {"press": "Escape"}, {"wait": 500},
//       {"scrollTo": "..."}, {"shot": "seats-tab-dialog-after"}   // extra PNG mid-flow
//     ]
//   }]
// }
//
// Env: APP_URL (set by with-app.sh), WITH_APP_CHECKOUT (its .env holds VITE_E2E_PASSWORD and the
// Auth0 domain), FACTORY_STAGING_USER (default e2e@cypress.com, the Cypress user),
// FACTORY_CHROME (default: the system Chrome channel).
import fs from 'node:fs'
import path from 'node:path'
import { chromium } from 'playwright-core'

const VIEWPORTS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } }
const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function die(msg) {
  console.error(`shoot: ${msg}`)
  process.exit(2)
}

function readEnvFile(file) {
  const env = {}
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/)
    if (m) env[m[1]] = m[2].replace(/^(['"])(.*)\1$/, '$2')
  }
  return env
}

const specFile = process.argv[2] || die('usage: shoot.mjs <shots.json>')
const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'))
const appUrl = process.env.APP_URL || die('APP_URL not set; run through with-app.sh')
const checkout = process.env.WITH_APP_CHECKOUT || die('WITH_APP_CHECKOUT not set; run through with-app.sh')
const outDir = spec.outDir || die('shots.json needs an absolute outDir')
if (!path.isAbsolute(outDir)) die('outDir must be absolute')
fs.mkdirSync(outDir, { recursive: true })

const env = readEnvFile(path.join(checkout, '.env'))
const user = process.env.FACTORY_STAGING_USER || 'e2e@cypress.com'
const password = env.VITE_E2E_PASSWORD || die('VITE_E2E_PASSWORD missing from the checkout .env')
const authHost = (env.VITE_AUTH0_DOMAIN || '').replace(/^https?:\/\//, '').replace(/\/.*$/, '')
const stagingPattern = new RegExp(process.env.FACTORY_STAGING_PATTERN || 'platform-test|staging')
if (!stagingPattern.test(authHost)) die(`Auth0 domain "${authHost}" is not staging; refusing to log in`)

// ---- read-only network guard ---------------------------------------------------------------------
function allowed(req) {
  if (READ_METHODS.has(req.method())) return true
  const url = new URL(req.url())
  if (url.host === authHost || url.host.endsWith('.auth0.com')) return true // login form + /oauth/token
  const body = req.postData() || ''
  if (/graphql/i.test(url.pathname) || url.host === 'graphql.datocms.com') {
    try {
      const q = JSON.parse(body).query || ''
      return !/^\s*mutation\b/.test(q)
    } catch {
      return false
    }
  }
  return false
}

// ---- auth ----------------------------------------------------------------------------------------
async function ensureLoggedIn(page) {
  // The SPA keeps tokens in memory, so every full load re-authenticates: either silently through
  // the Auth0 session cookie, or through the universal login form.
  for (let i = 0; i < 3; i++) {
    await page.waitForLoadState('domcontentloaded')
    const host = new URL(page.url()).host
    if (host === authHost) {
      const username = page.locator('input#username')
      if (await username.isVisible({ timeout: 10000 }).catch(() => false)) {
        await username.fill(user)
        await page.locator('button[value=default]', { hasText: 'Continue' }).click()
      }
      const pw = page.locator('input#password')
      await pw.waitFor({ timeout: 15000 })
      await pw.fill(password)
      await page.locator('button[value=default]', { hasText: 'Continue' }).click()
      await page.waitForURL((u) => u.origin === new URL(appUrl).origin, { timeout: 30000 })
      continue
    }
    if (host === new URL(appUrl).host) return
    await page.waitForTimeout(1000)
  }
  if (new URL(page.url()).host !== new URL(appUrl).host) throw new Error(`login did not return to the app (at ${page.url()})`)
}

async function open(page, route) {
  await page.goto(new URL(route, appUrl).toString(), { waitUntil: 'domcontentloaded' })
  await ensureLoggedIn(page)
  // onRedirectCallback lands on /dashboard or returnTo; make sure we end on the requested route.
  if (new URL(page.url()).pathname !== new URL(route, appUrl).pathname) {
    await page.goto(new URL(route, appUrl).toString(), { waitUntil: 'domcontentloaded' })
    await ensureLoggedIn(page)
  }
  await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {})
}

async function runStep(page, step, shotOut) {
  const [kind] = Object.keys(step)
  const arg = step[kind]
  switch (kind) {
    case 'click': return page.locator(arg).first().click()
    case 'hover': return page.locator(arg).first().hover()
    case 'fill': return page.locator(arg[0]).first().fill(arg[1])
    case 'press': return page.keyboard.press(arg)
    case 'waitFor': return page.locator(arg).first().waitFor({ timeout: 20000 })
    case 'waitForText': return page.getByText(arg).first().waitFor({ timeout: 20000 })
    case 'wait': return page.waitForTimeout(Number(arg))
    case 'scrollTo': return page.locator(arg).first().scrollIntoViewIfNeeded()
    case 'shot': return shotOut(arg)
    default: throw new Error(`unknown step ${JSON.stringify(step)}`)
  }
}

// ---- run -----------------------------------------------------------------------------------------
const browser = await chromium.launch(
  process.env.FACTORY_CHROME ? { executablePath: process.env.FACTORY_CHROME } : { channel: 'chrome' },
)
const context = await browser.newContext({ viewport: VIEWPORTS.desktop, ignoreHTTPSErrors: false })
const report = { appUrl, checkout, shots: [] }
let failures = 0

for (const shot of spec.shots || []) {
  const entry = { name: shot.name, path: shot.path, files: [], console: [], pageErrors: [], blocked: [], failedRequests: [] }
  report.shots.push(entry)
  const page = await context.newPage()
  page.on('console', (m) => ['error', 'warning'].includes(m.type()) && entry.console.push(`${m.type()}: ${m.text()}`))
  page.on('pageerror', (e) => entry.pageErrors.push(String(e)))
  page.on('requestfailed', (r) => entry.failedRequests.push(`${r.method()} ${r.url()} ${r.failure()?.errorText || ''}`))
  page.on('response', (r) => r.status() >= 400 && entry.failedRequests.push(`${r.request().method()} ${r.url()} ${r.status()}`))
  await page.route('**/*', (route) => {
    const req = route.request()
    if (allowed(req)) return route.continue()
    entry.blocked.push(`${req.method()} ${req.url()}`)
    return route.abort('blockedbyclient')
  })
  const vp = typeof shot.viewport === 'object' ? shot.viewport : VIEWPORTS[shot.viewport || 'desktop']
  if (!vp) die(`unknown viewport ${shot.viewport}`)
  await page.setViewportSize(vp)
  const save = async (name) => {
    const file = path.join(outDir, `${name}.png`)
    await page.screenshot({ path: file, fullPage: !!shot.fullPage })
    entry.files.push(file)
  }
  try {
    await open(page, shot.path || '/')
    for (const step of shot.steps || []) await runStep(page, step, save)
    await page.waitForTimeout(300) // let MUI transitions settle
    await save(shot.name)
    console.log(`shoot: ${shot.name} ok`)
  } catch (e) {
    failures++
    entry.error = String(e)
    console.error(`shoot: ${shot.name} FAILED: ${e}`)
    await save(`${shot.name}-error`).catch(() => {})
  }
  await page.close()
}

await browser.close()
const reportFile = path.join(outDir, `${path.basename(specFile, '.json')}.report.json`)
fs.writeFileSync(reportFile, JSON.stringify(report, null, 2))
console.log(`shoot: report at ${reportFile}`)
process.exit(failures ? 1 : 0)
