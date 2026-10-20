---
name: ap-staging-harness
description: How factory agents render accounts-portal views for screenshots - serve a checkout on the Auth0-allowed port under the shared lock, log in to staging as the e2e user, drive the view with a shots JSON, and capture before/after/mockup PNGs at desktop and mobile widths. Read-only against staging. Use for architect mockups, builder-ui checks, reviewer-ui and reviewer-ux.
---

# Staging screenshot harness

Two scripts in the **main checkout's** `.factory/harness/` (`<repo>` below):

- `with-app.sh <checkout> [--preview] -- <command…>`: takes the port lock
  (`<repo>/.factory/.port.lock`, shared with the gate's e2e and every other
  job), starts `vite` for `<checkout>` on `http://localhost:5173` (the only
  Auth0 callback the staging tenant allows), runs the command with `APP_URL`
  set, then stops the server. Expect to wait while another job holds the lock.
- `shoot.mjs <shots.json>`: logs in to staging Auth0 as `e2e@cypress.com`
  (password from the checkout's `.env`), opens each shot's route at its
  viewport, runs its steps and saves PNGs plus `<shots>.report.json` (console
  errors, page errors, failed and blocked requests per shot).

```bash
cd <repo> && .factory/harness/with-app.sh <checkout> -- node .factory/harness/shoot.mjs <scratch>/shots.json
```

Always run it in the foreground with a long timeout (up to 10 minutes); the lock
wait counts.

## shots.json

```json
{
  "outDir": "<job folder>/round-<N>/shots",
  "shots": [
    { "name": "seats-tab-typical-after", "path": "/dashboard", "viewport": "desktop",
      "steps": [{ "click": "text=Seats" }, { "waitForText": "unused license" }] },
    { "name": "seats-tab-typical-after-mobile", "path": "/dashboard", "viewport": "mobile",
      "steps": [{ "click": "text=Seats" }, { "waitForText": "unused license" }] },
    { "name": "add-member-dialog-after", "path": "/dashboard",
      "steps": [{ "click": "text=Seats" }, { "click": "button:has-text('Add member')" }, { "waitFor": "[data-cy=email-input]" }] }
  ]
}
```

Steps: `click`, `hover`, `fill` (`[selector, text]`), `press`, `waitFor`,
`waitForText`, `wait` (ms), `scrollTo`, and `shot` (an extra PNG mid-flow).
Selectors are Playwright selectors; prefer `[data-cy=...]`, then `text=` or
`role=`. Viewports: `desktop` 1440x900, `mobile` 390x844, or
`{"width":…, "height":…}`.

## Naming

`<view>-<state>-<before|mockup|after>[-mobile].png`, the same stem for every
member of a set so the dashboard shows them side by side. reviewer-ux uses
`ux-<criterion>-<step>-<before|after>.png`. States: typical, empty, loading,
error, no-permission, free-tier, long content, disabled-with-tooltip.

## Checkouts

- **After (branch):** the job worktree. It already has `node_modules` and a
  `.env` symlink.
- **Before (base):** a detached worktree of the base in your scratch folder:
  ```bash
  cd <worktree> && git worktree add --detach <scratch>/base <base>
  ln -s <worktree>/node_modules <scratch>/base/node_modules
  ln -s <repo>/.env <scratch>/base/.env
  ```
  Remove it when done: `cd <worktree> && git worktree remove --force <scratch>/base`.
- **Mockup (architect):** the same as before, then edit files in
  `<scratch>/base` to show the design. Stub data that doesn't exist yet with
  literals marked `// mockup:`.

## What the harness can't show

States that need specific staging data (an empty tenant, an exceeded credit
limit, a failed payment) may not be reachable with the e2e account. Then:
render what you can, and for the rest temporarily stub the value in a
**scratch copy** (never the job worktree) with a `// mockup:` literal, and say
in your file which shots are stubbed. Errors can be forced the same way.

## Rules

- **Read-only.** `shoot.mjs` aborts every non-GET request except Auth0 login
  and GraphQL queries; `blocked` in the report lists what it stopped. Don't try
  to get around it, and don't drive Stripe Checkout, invites, seat changes, key
  creation/revocation, deactivation, deletes or uploads. Open dialogs; don't
  confirm them.
- Never put the password, tokens or `.env` values in shots.json, scripts,
  reviews or logs. Screenshots of the e2e tenant are fine.
- Look at every PNG you produce with Read before you describe it.
- Nothing goes in the job worktree; scripts, shots.json and scratch checkouts
  live under the job folder.
- If login fails (`Auth0 domain … is not staging`, wrong password, the tenant
  is down), stop and report it: don't fall back to describing the UI from code
  as if you had seen it.
