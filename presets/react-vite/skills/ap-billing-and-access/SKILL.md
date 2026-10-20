---
name: ap-billing-and-access
description: The money and access model of accounts-portal (plans and billing periods, Stripe Checkout and 3DS flows, AfterPayment polling, credits and overage, seats and members, permission strings, GTM ecommerce events, checkout state in Redux/localStorage). Use when specifying, designing, building or reviewing anything that touches subscriptions, prices, credits, seats, invoices or permissions.
---

# Billing and access in accounts-portal

The billing API (`API_BILLING_ROOT_URL`) owns prices, Stripe customers and
subscriptions. The portal **displays** what the API returns and **starts**
flows; it never computes a charge itself and never talks to Stripe with a
secret key. Anything that needs the API to behave differently is a change in
another repo and must be called out.

## Products and plans (`src/types/subscription.ts`)

- `ProductCategory[]` from `GET /categories` → `MoveProduct` (`id`, `name`,
  `priority`, `features`, `billing_periods`, `is_active`, `cta_url`).
- Each `BillingPeriod.display_price` is one of:
  - `NO_STRIPE`: free plan; price 0, `included_credits`, no checkout.
  - `STRIPE`: starter/basic/team; `price`, `currency`, `billing_period_interval`;
    `stripe_products.seats` and `.usage` carry `stripe_price_id`,
    `included_units` and `price.unit_amount` (minor units, e.g. pence).
  - `CTA_ONLY`: enterprise / trials; a contact link, no price.
- `src/utils/currentBillingPeriodFromProducts.ts` picks the period in force.
- The current plan name is `currentProductName` in `DashboardContext`; `"free"`
  drives the free-tier UI (`CreditAllowanceCard`: "No credits", no overage).

Rules: show money with the currency the API sent (never assume GBP/USD),
convert `unit_amount` from minor units exactly once, and never round credits or
prices differently from the neighbouring cards.

## Flows

| Flow | Where | How |
|---|---|---|
| New subscription | `pages/subscription/*`, `SubscribeButton`, `BookingModal*` | `POST /subscription/checkout-session` → `window.location.href = data.url` (hosted Stripe Checkout) |
| Return from Checkout | `/product/payment-status` → `pages/subscription/AfterPayment.tsx` | polls `GET /subscription` (8s steps) until active, then fires the GTM `purchase` event |
| Upgrade check | `GET /subscription/is-user-upgrading/:price` | before checkout; `UpgradeModal` |
| Add seats / seats + invites | `pages/dashboard/components/AddSeatDialog.tsx`, `AddSeatsAndInviteMembersDialog.tsx` | `POST /subscription/seats`; on `requires_action` call `stripe.handleNextAction` (3DS) and handle both outcomes |
| Members | `pages/dashboard/tabs/Members.tsx`, `ReactivateMemberDialog.tsx` | `/tenant/members`, deactivate/reactivate consumes or frees a seat |
| Billing / invoices | `pages/billing/Index.tsx`, `pages/dashboard/tabs/Billing.tsx` | `GET /tenant/invoice`; "Manage" opens `stripe_customer_portal_url` from the API |
| Credits | `CreditAllowanceCard`, `CreditOverflowCard`, `components/credit-bar` | `GET /tenant/credit-overview` (`utils/service.ts:getCreditOverview`) |

Checkout wizard state (`billingPlan`, `selectedMoveProduct`,
`stripeCustomerId`, `tenantId`, `paymentStatus`) lives in
`redux/reducers/appUserReducer.ts`, with some values mirrored in
`localStorage` (`selectedBillingPlan`, `selectedMoveProduct`,
`payment_intent_id`, `subscribedPlanName`). Those are non-secret; keep it so.

## What must hold

- **No double charge:** buttons that start checkout, add seats or reactivate
  members are disabled while the request is in flight and can't be triggered
  twice by Enter + click.
- **3DS:** every `requires_action` path calls `handleNextAction`, and both the
  success and the failure/cancel outcomes update the UI and the seat count.
- **Polling ends:** AfterPayment and any new polling has a maximum time and a
  clear message when it gives up; timers are cleared on unmount.
- **Seat maths:** "unused licences" = seats − active members; deactivating
  frees one, reactivating needs one free or offers to buy one.
- **Copy:** prices, intervals ("per month"), proration and "you will be charged"
  wording match what the API returns and what Stripe will actually do. Copy
  about money or plans is a product decision: the integrator escalates it.
- **Analytics:** `begin_checkout` / `view_item_list` through
  `utils/gtmEvents.ts`; `purchase` fires once per completed payment (guard
  against re-firing on reload of `/product/payment-status`); no PII in events.

## Permissions (from the access token, see `DashboardContext`)

| Token permission | Context flag | Gates |
|---|---|---|
| `read:tenant_members` | `readPermissions.readMembers` | Members tab |
| `read:tenant_seats_overview` | `readSeats` | seats cards |
| `read:tenant_credits_overview` / `read:tenant_credit_allowance` | `readCreditsOverview` / `readCreditsAllowance` | credit cards |
| `read:subscription` | `readSubscription` | plan, subscription calls |
| `read:billing_info` | `readBilling` | Billing tab, invoices |
| `read:gallery_view` | `readGalleryView` | assets |
| `change:subscription` | `changePermissions.changeSubscription` | upgrade, checkout, portal |
| `update:subscription_seats` | `changeSeats` | add seats, invite, reactivate |
| `create:api_key` | `createApiKey` | API keys page actions |
| `create:singlecam_job` / `create:multicam_job` | `createSinglecamJob` / `createMulticamJob` | upload |
| `change:singlecam_mocap_model` | `changeSinglecamMocapModel` | model picker |

A new privileged action needs a permission the backend enforces. If none of
these fits, the spec must name the new permission and say the backend (Auth0
API + accounts/billing service) has to add it.
