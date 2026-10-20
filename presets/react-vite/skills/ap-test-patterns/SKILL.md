---
name: ap-test-patterns
description: How tests are written and run in accounts-portal (Vitest + React Testing Library + jsdom, __test__ folders, test-utils helpers, mocking axios/Auth0/Stripe/contexts, coverage thresholds and the autoUpdate trap, Cypress e2e against staging). Use when writing, fixing, running or reviewing tests in this repo.
---

# Tests in accounts-portal

## Running

From the checkout (worktree) root:

```bash
npx vitest run <files>                       # never bare `npx vitest`: the config sets watch: true
npx vitest run <files> -t "<test name>"
npx vitest run --coverage --coverage.thresholds.autoUpdate=false   # full suite with coverage
```

- **autoUpdate trap:** `vite.config.ts` has `coverage.thresholds.autoUpdate:
  true`, so a coverage run without the flag above rewrites `vite.config.ts`.
  If `git status` shows it modified, `git checkout -- vite.config.ts`.
- Thresholds (enforced by CI and the factory gate): 85 statements, 80
  branches, 80 functions, 85 lines. New code that isn't covered can push the
  total under them.
- Excluded from coverage: `src/components/*`, `src/theme/overrides`,
  `src/utils/hooks`, `src/types`, `src/assets/icons`, `src/App.tsx`,
  `ThreeDView.tsx`, layout/drawer barrels. Tests for shared components are
  still welcome; they just don't count.
- `silent: true` hides console output. Use `--silent=false` when debugging.

## Layout and naming

- Tests sit next to the code: `src/pages/<feature>/__test__/<Name>.test.tsx`,
  `src/pages/<feature>/components/__test__/<Name>.test.tsx`,
  `src/utils/__test__/<name>.test.ts`. (`apikeys` and `billing` use
  `__tests__/`; follow the folder already there.)
- `describe("<Area> - <Component>")`, `it("<behaviour in words>")`. No Jira keys.
- Globals are on, but the files import `describe, it, expect, vi` from
  `vitest` explicitly; do the same.

## Helpers (`src/utils/__test__/test-utils.tsx`)

- `renderWithRedux(ui, preloadedState?)`: renders with a real RTK store
  (`rootReducer`), returns RTL's result plus `store`.
- `mockDashboardContext`: a full `DashboardContext` value (every permission
  on). Override what the test needs:
  `<DashboardContext.Provider value={{ ...mockDashboardContext, changePermissions: { ...mockDashboardContext.changePermissions, changeSeats: false } }}>`.
- `mockGalleryContext`, `mockGalleryContextValues` for `GalleryContext`.
- Product fixtures: `mockStarterMoveProduct`, `mockTeamsMoveProduct`,
  `mockProductList`, …; `mockStoreInitialValues` for the Redux state.
- Add new shared fixtures there rather than copying 50-line objects into tests.

## Mocking

```ts
vi.mock("axios");                                     // then vi.mocked(axios.get).mockResolvedValueOnce({ data })
vi.mock("@auth0/auth0-react", () => ({
  useAuth0: () => ({ getAccessTokenSilently: vi.fn().mockResolvedValue("fake-token"), user: { org_id: "org_1" } }),
}));
vi.mock("@stripe/react-stripe-js", async () => {
  const actual: any = await vi.importActual("@stripe/react-stripe-js");
  return { ...actual, useStripe: () => ({ ...actual.useStripe(), handleNextAction: mockHandleNextAction }) };
});
```

- `mockReset: true` is on: mock implementations reset before each test, so set
  return values inside the test or `beforeEach`.
- Wrap Stripe-using components in `<Elements stripe={stripePromise}>`.
- `redux-mock-store` and `axios-mock-adapter` are installed but unused; don't
  introduce them.
- Assert what the user sees or what was sent: rendered text, enabled/disabled
  state, the URL and body passed to `axios.post`, dispatched state in `store`.
  Don't assert on mock internals or implementation details.
- Prefer `userEvent` over `fireEvent` for new interaction tests; await
  `findBy*` / `waitFor` for async UI.

## Cypress (e2e)

- One spec today: `cypress/e2e/spec.cy.ts`. Login is a real Auth0 UI login
  (`cypress/support/loginViaAuth0.ts`, `cy.session`) to
  `auth.platform-test.move.ai` as `e2e@cypress.com` with `VITE_E2E_PASSWORD`
  from `.env`. Tests run against staging data.
- Selectors: `[data-cy="..."]` first, visible text second, never MUI class names
  except where the existing spec already does.
- New e2e tests are for critical flows the spec marks as such. They must be
  read-only, or undo what they change in the same test (the existing
  deactivate/reactivate test is the model). Never create checkout sessions,
  invites to real addresses, API keys that stay, or uploads.
- Run: `npx cypress run --spec cypress/e2e/<file>` with the app served on
  `http://localhost:5173` (the factory's `with-app.sh` serves it under the port
  lock). The factory gate runs the whole suite.
