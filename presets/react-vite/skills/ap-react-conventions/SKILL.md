---
name: ap-react-conventions
description: How code is structured in accounts-portal (pages/components/contexts layout, the axios + Auth0 token pattern, handleAxiosError, env access through configurations.ts, permission gating, MUI sx + theme, Iconify, data-cy). Use when designing, writing or reviewing React/TypeScript in this repo.
---

# React conventions in accounts-portal

## Where code goes

| Kind | Place | Notes |
|---|---|---|
| Route-level view | `src/pages/<feature>/Index.tsx` | registered in `src/navigation/Router.tsx` inside the right guard and layout |
| Feature component | `src/pages/<feature>/components/<Name>.tsx` | dialogs, tabs, cards for one feature |
| Shared component | `src/components/<kebab-name>/` | `index.ts` barrel, `types.ts` when it has props types |
| Server state shared across pages | `src/contexts/DashboardContext.tsx` or `GalleryContext.tsx` | both are already 500-700 lines: add a focused hook or util and expose only its result through the context |
| Checkout wizard state | `src/redux/reducers/appUserReducer.ts` | the only slice; plain `useSelector` / `useDispatch` |
| Types | `src/types/<area>.ts` | reuse `ErrorProps` from `types/general.ts` for errors |
| Pure helpers | `src/utils/<name>.ts` | tests in `src/utils/__test__/` |
| Env values | `src/utils/configurations.ts` only | `import.meta.env.VITE_X || ""`; elsewhere import the constant |
| Route paths | `src/navigation/paths.ts` | for paths used in more than one place |

## Talking to the backend

There is no axios instance. The pattern, everywhere:

```ts
const { getAccessTokenSilently } = useAuth0();
const token = await getAccessTokenSilently();
try {
  const { data } = await axios.get<Thing>(`${API_ACCOUNTS_ROOT_URL}/tenant`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  setThing(data);
} catch (e) {
  handleAxiosError(e as AxiosError, setError, "Message for a 422");
}
```

- Base URLs: `API_BILLING_ROOT_URL` (subscription, seats, checkout-session,
  credit/seat overview, invoices, categories), `API_ACCOUNTS_ROOT_URL` (tenant,
  members, API keys), `API_UGC_ROOT_URL` (GraphQL, raw axios POST),
  `API_TRANSCODED_ROOT_URL`.
- `handleAxiosError` reports to Datadog and maps status → `ErrorProps`:
  401/403 give an empty message (the view decides), 422 the caller's message,
  404/500 fixed text, anything else "Something went wrong". Errors are shown
  with `components/error-snackbar` or an inline `failed-box`.
- Keep request and response types in `src/types/`. A new endpoint, or a new
  field the backend doesn't send yet, is a cross-repo change: say so in the
  design rather than inventing it.
- Every async effect must cope with unmount and re-render (stale responses,
  double calls under StrictMode): use a cancelled flag or `AbortController`
  the way neighbouring code does, and list dependencies honestly.

## Auth and permissions

- `AuthGuard` (`withAuthenticationRequired`) wraps the dashboard layout;
  `GuestGuard` keeps signed-in users off login/sign-up.
- `DashboardContext` decodes the access token and exposes `permissions`
  (raw strings), `readPermissions` (`readMembers`, `readSeats`,
  `readCreditsOverview`, `readCreditsAllowance`, `readSubscription`,
  `readBilling`, `readGalleryView`, `readSubscribedPlanDetails`) and
  `changePermissions` (`changeSubscription`, `changeSeats`, `createApiKey`,
  `createSinglecamJob`, `createMulticamJob`, `changeSinglecamMocapModel`).
- Gate UI with these flags. A user without the permission should see a
  disabled control with a tooltip, or nothing, never a control that fails
  with a 403. The backend is the real check.

## UI

- MUI 5 with the dark theme in `src/theme/` (palette, typography, overrides).
  Style with `sx` and theme values: `color: 'text.secondary'`,
  `bgcolor: 'background.paper'`, `theme.spacing`, `theme.palette.primary.main`.
  No hex colours or pixel font sizes where a theme value exists.
- `styled()` only where the file already uses it.
- Icons: `<Iconify icon="eva:..." />` from `components/iconify`, or the custom
  SVG components in `src/assets/icons/`. `@mui/icons-material` only where the
  neighbours use it.
- Responsive: `useResponsive('up', 'md')` from `src/utils/hooks/useResponsive`
  and `sx` breakpoints. Every view must work at 390px wide.
- Copy: sentence case, no em dashes, the same nouns as the rest of the portal
  (seats, members, credits, plan, API key, takes, assets).
- Add `data-cy="<kebab-id>"` to controls an e2e test needs, matching the
  existing `chip-<email>` / `overflow-button-<email>` style.

## Analytics

- GTM ecommerce events go through `src/utils/gtmEvents.ts`; don't call
  `TagManager.dataLayer` ad hoc.
- PostHog and Datadog are consent-gated by `components/cookie-banner`. Never
  send emails, names, tokens or payment data as event properties.

## TypeScript

`strict`, `noUnusedLocals`, `noUnusedParameters`. New code doesn't add `any`
(the existing code has plenty; don't copy it), and doesn't use non-null `!` to
silence a real possibility of `null`.
