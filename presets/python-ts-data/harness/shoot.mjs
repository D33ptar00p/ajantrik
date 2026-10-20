#!/usr/bin/env node
// Map screenshot harness for the OmniGrid factory.
//
//   .factory/harness/with-app.sh <checkout> -- node .factory/harness/shoot.mjs [--strict] <shots.json>
//
// For each shot: opens the app at a viewport, runs its steps and saves PNGs, recording console
// errors, page errors and failed requests in <shots>.report.json. The session is READ-ONLY: any
// request that is not GET/HEAD/OPTIONS is aborted (the app has no reason to write anywhere).
//
// --strict (the gate's smoke) exits 1 when a shot has a page error, a console error, or a failed
// request to the app's own origin. External failures (basemap tiles, Commons photos) are reported
// but never fail the run, so being offline doesn't fail a job.
//
// shots.json:
// {
//   "outDir": "/abs/path/shots",                  // required; PNGs and <name>.report.json land here
//   "shots": [{
//     "name": "legend-fuel-selected-after",       // PNG stem: <view>-<state>-<before|after|mockup>[-mobile]
//     "path": "/",                                  // app route, relative to APP_URL
//     "viewport": "desktop",                        // desktop 1440x900 | mobile 390x844 | {"width":..,"height":..}
//     "fullPage": false,
//     "steps": [                                    // Playwright selectors: text=..., css, #id
//       {"waitFor": "#views:not([hidden])"},        // the app has booted
//       {"click": "#views button[data-view=global]"},
//       {"click": "#legend-toggle"}, {"click": "text=Nuclear"},
//       {"select": ["#operator-select", "Drax Power Ltd"]},
//       {"jumpTo": {"center": [-1.08, 53.74], "zoom": 11}},   // dev server only (needs __map)
//       {"clickAt": [-1.08, 53.74]},                // click the map at lng/lat (dev server only)
//       {"mapIdle": true},                          // wait for tiles and sources to settle
//       {"hover": "..."}, {"fill": ["input", "text"]}, {"press": "Escape"}, {"wait": 500},
//       {"waitForText": "Drax"}, {"scrollTo": "..."}, {"shot": "gb-trace-open-after"}   // extra PNG mid-flow
//     ]
//   }]
// }
//
// Env: APP_URL (set by with-app.sh), FACTORY_CHROME (default: the system Chrome channel).
import fs from 'node:fs'
import path from 'node:path'
import { chromium } from 'playwright-core'

const VIEWPORTS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } }
const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function die(msg) {
  console.error(`shoot: ${msg}`)
  process.exit(2)
}

const args = process.argv.slice(2)
const strict = args.includes('--strict')
const specFile = args.find((a) => !a.startsWith('--')) || die('usage: shoot.mjs [--strict] <shots.json>')
const spec = JSON.parse(fs.readFileSync(specFile, 'utf8'))
const appUrl = process.env.APP_URL || die('APP_URL not set; run through with-app.sh')
const appOrigin = new URL(appUrl).origin
const outDir = spec.outDir || die('shots.json needs an absolute outDir')
if (!path.isAbsolute(outDir)) die('outDir must be absolute')
fs.mkdirSync(outDir, { recursive: true })

// The map draws to WebGL and loads tiles and GeoJSON asynchronously. In dev, window.__map tells us
// when everything is rendered; on a production build, fall back to network idle plus a pause.
async function mapIdle(page) {
  const hasMap = await page.evaluate(() => !!window.__map)
  if (!hasMap) {
    await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {})
    return page.waitForTimeout(1500)
  }
  await page.waitForFunction(() => window.__map.loaded() && window.__map.areTilesLoaded(), null, { timeout: 60000 })
  await page.evaluate(() => new Promise((resolve) => {
    const m = window.__map
    m.once('idle', resolve)
    m.triggerRepaint()
  }))
}

async function needMap(page, step) {
  if (!(await page.evaluate(() => !!window.__map))) {
    throw new Error(`${step} needs window.__map; run with-app.sh in dev mode (no --preview)`)
  }
}

async function runStep(page, step, shotOut) {
  const [kind] = Object.keys(step)
  const arg = step[kind]
  switch (kind) {
    case 'click': return page.locator(arg).first().click()
    case 'hover': return page.locator(arg).first().hover()
    case 'fill': return page.locator(arg[0]).first().fill(arg[1])
    case 'select': return page.locator(arg[0]).first().selectOption({ label: arg[1] })
    case 'press': return page.keyboard.press(arg)
    case 'waitFor': return page.locator(arg).first().waitFor({ timeout: 30000 })
    case 'waitForText': return page.getByText(arg).first().waitFor({ timeout: 30000 })
    case 'wait': return page.waitForTimeout(Number(arg))
    case 'scrollTo': return page.locator(arg).first().scrollIntoViewIfNeeded()
    case 'mapIdle': return mapIdle(page)
    case 'jumpTo':
      await needMap(page, 'jumpTo')
      await page.evaluate((o) => window.__map.jumpTo(o), arg)
      return mapIdle(page)
    case 'clickAt': {
      await needMap(page, 'clickAt')
      const { x, y } = await page.evaluate((ll) => window.__map.project(ll), arg)
      const box = await page.locator('#map canvas').first().boundingBox()
      return page.mouse.click(box.x + x, box.y + y)
    }
    case 'shot': return shotOut(arg)
    default: throw new Error(`unknown step ${JSON.stringify(step)}`)
  }
}

// ---- run -----------------------------------------------------------------------------------------
const browser = await chromium.launch({
  ...(process.env.FACTORY_CHROME ? { executablePath: process.env.FACTORY_CHROME } : { channel: 'chrome' }),
  // Headless Chrome needs a software GL fallback for MapLibre on machines without a GPU.
  args: ['--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
})
const context = await browser.newContext({ viewport: VIEWPORTS.desktop })
const report = { appUrl, strict, shots: [] }
let failures = 0

for (const shot of spec.shots || []) {
  const entry = { name: shot.name, path: shot.path, files: [], console: [], pageErrors: [], blocked: [], failedRequests: [], externalFailures: [] }
  report.shots.push(entry)
  const page = await context.newPage()
  const failed = (line, url) => (new URL(url).origin === appOrigin ? entry.failedRequests : entry.externalFailures).push(line)
  page.on('console', (m) => ['error', 'warning'].includes(m.type()) && entry.console.push(`${m.type()}: ${m.text()}`))
  page.on('pageerror', (e) => entry.pageErrors.push(String(e)))
  page.on('requestfailed', (r) => {
    const why = r.failure()?.errorText || ''
    if (why !== 'net::ERR_BLOCKED_BY_CLIENT') failed(`${r.method()} ${r.url()} ${why}`, r.url())
  })
  page.on('response', (r) => r.status() >= 400 && failed(`${r.request().method()} ${r.url()} ${r.status()}`, r.url()))
  await page.route('**/*', (route) => {
    const req = route.request()
    if (READ_METHODS.has(req.method())) return route.continue()
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
    await page.goto(new URL((shot.path || '/').replace(/^\//, ''), appUrl).toString(), { waitUntil: 'domcontentloaded' })
    for (const step of shot.steps || []) await runStep(page, step, save)
    await page.waitForTimeout(300) // let panel transitions settle
    await save(shot.name)
    const consoleErrors = entry.console.filter((c) => c.startsWith('error:'))
    if (strict && (entry.pageErrors.length || consoleErrors.length || entry.failedRequests.length)) {
      failures++
      entry.error = 'strict: page errors, console errors or failed app requests (see this entry)'
      console.error(`shoot: ${shot.name} FAILED (strict): ${[...entry.pageErrors, ...consoleErrors, ...entry.failedRequests].join(' | ')}`)
    } else {
      console.log(`shoot: ${shot.name} ok`)
    }
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
