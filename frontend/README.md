# Score Smith: frontend

Next.js 15 (App Router) + React 19 + TypeScript + Tailwind CSS 4 + Radix/shadcn primitives. The browser only talks to this app's origin:
`next.config.ts` rewrites `/api/v1/*` to the backend (`INTERNAL_API_BASE_URL`), so session cookies are first-party and no backend URL reaches client bundles.
Overview of the system: [../README.md](../README.md); diagrams (including the route map and auth flow): [../docs/architecture.md](../docs/architecture.md).

## Run

Normally through compose (`../infra`, service `frontend`, `npm run dev` with the source bind-mounted). On the host:

```bash
cd frontend
npm ci
INTERNAL_API_BASE_URL=http://localhost:8000 npm run dev      # PowerShell: $env:INTERNAL_API_BASE_URL="http://localhost:8000"; npm run dev
npx tsc --noEmit && npm run lint && npm run test:unit && npm run build     # the same gates as CI
```

Windows + Docker Desktop: file events from the host do not always reach the container, so the dev server may stop picking up edits. Run
`docker compose restart frontend` from `infra/` (or set `WATCHPACK_POLLING=true` on the service). Do not run `npm run build` on the host while the bind-mounted dev server
uses the same folder: both write `.next`. If `tsc`/`next build` complain about files or routes that no longer exist, delete the stale generated types (`.next/types`).

## Route map

Route groups (the parentheses are not part of the URL):

| Group | Layout | Routes | Notes |
|---|---|---|---|
| `(marketing)` | `app/(marketing)/layout.tsx`, `marketing.css` | `/` | Public Score Smith homepage |
| `(auth)` | `app/(auth)/layout.tsx`, `auth.css` | `/login`, `/signup`, `/setup`, `/forgot-password`, `/reset-password`, `/verify-email` | `/setup` is the production first-admin form (needs `BOOTSTRAP_TOKEN` on the server); `/signup` is hidden when signup is off |
| `(app)` | `app/(app)/layout.tsx` (AppShell, live status, chat session list; `force-dynamic`) | see below | Requires a session |

`(app)` routes:

| Route | Who | What |
|---|---|---|
| `/chat`, `/chat/[sessionId]` | everyone | Scorecard builder chat, live draft and trace, Share button |
| `/chat/shared/[sessionId]` | recipient | Read-only shared chat, "Save my own copy" |
| `/charts` | everyone | Chart library (owned and "Shared with you"), invitations, Trash button |
| `/charts/[id]` | owner / editor | Overview, guidelines, evaluate, history, sharing dialog, editing log |
| `/charts/[id]/evaluations/[evaluationId]` | owner / editor | Progress while running, then the result |
| `/charts/trash` | owner | Restore, delete permanently, empty trash |
| `/evaluations` | everyone | Infinite-scroll list, server-side filters, select all matching, bulk delete, Excel export |
| `/settings` | **admin** | Settings |
| `/admin/users` | **admin** | User management |
| `/` (signed in) | | redirects to `/chat` |

Navigation: `components/layout/nav-items.ts` (users: Chat, Charts, Evaluations; admins also Settings and Users). The sidebar header still reads "Quality Scorecards"; the product brand is Score Smith.
The favicon comes from `app/icon.svg`.

## Middleware behaviour

`middleware.ts` is a UX gate and silent session renewer (it holds no secret and does not verify signatures; the backend verifies every call). Matcher: everything except `api/`, `_next/`, `favicon.ico` and paths with a file extension.
Cookies: `qs_access` (15-minute JWT, has the `rol` claim), `qs_refresh`, `qs_csrf`.

| Path | Signed out (no usable cookies) | Valid or renewable session | Notes |
|---|---|---|---|
| `/` | homepage | redirect `/chat` | `?expired=1` clears cookies and shows the homepage |
| `/login` | form | redirect to the safe `?next=` (default `/chat`) | `next` must be a same-site path, never a sign-in page (no open redirect) |
| `/signup`, `/setup`, `/forgot-password`, `/reset-password` | page | redirect `/chat` | |
| `/verify-email` | page | page | always reachable (links from e-mail) |
| `/admin/**`, `/settings/**` | redirect `/login?next=...` | admin: page; user: redirect `/chat` | uses the `rol` claim; the API re-checks the role |
| any other page | redirect `/login?next=<path+query>` | page | |
| access expired, refresh present | n/a | middleware calls `POST /auth/refresh` before rendering, forwards `Set-Cookie`, rewrites the request cookies so Server Components see the new tokens | if refresh is rejected: `/login?next=...&expired=1`, cookies cleared |
| backend unreachable | pass through | pass through | cookies kept; the page shows its own error |

Sign out calls `POST /auth/logout` and then navigates to `/` (to `/?expired=1` if the call failed).

## API client and session handling

All backend access goes through `lib/api-client.ts` (camelCase mapping of the Pydantic shapes) and the small clients `auth-client.ts`, `collab-client.ts`, `chat-share-client.ts`, `trash-client.ts`.

- In the browser every call is same-origin with `credentials: "include"`; unsafe methods add `X-CSRF-Token` read from the `qs_csrf` cookie.
- On `401` the browser refreshes once (single flight, `refreshSession()`), retries, and otherwise navigates to `/?expired=1`. Auth routes (`authRoute: true`) skip this (a 401 there is a wrong password).
- On the server (Server Components) the same functions call `INTERNAL_API_BASE_URL` directly and forward the request's cookies; the middleware has already renewed the token, so a 401 there redirects to `/login?expired=1`.
- Live data is polled, not pushed: chat turns (`useAdaptivePoll`, backoff with jitter, slower in hidden tabs), evaluation progress, the notification bell (`ETag` / `If-None-Match`, about 15 s), the evaluations list (`/evaluations/refresh` for visible rows).
  `components/live/LiveStatusProvider.tsx` tracks the user's busy slots (`409 user_job_active` / `user_chat_active` show `JobBusyNotice`).
- Edits send `If-Match` (the entity's `updated_at`); a `409 stale_edit` becomes a "changed by someone else, refresh" message.
- Pure logic lives in framework-free helpers so it can be unit-tested: `auth-helpers.ts`, `chat-turn-state.ts`, `eval-filters.ts`, `eval-selection.ts`, `eval-bulk.ts`, `trash-selection.ts`, `download.ts`, `ai-upload.ts`.

## Design system

Yellow/gold glass on a warm off-white page. Tokens are CSS variables at the top of `app/globals.css` (`--lemon: #ffc21a`, `--lemon-hover`, `--lemon-soft`, `--lemon-ink`, `--ink`, `--glass-*`, RAG colours `--rag-*`).
Use the classes, not ad-hoc blur:

| Class | Use |
|---|---|
| `.glass-subtle` (alias `.glass-elev-1`) | Chrome: top bars, tab pills |
| `.glass` (`.glass-elev-2`) | Cards, the nav rail |
| `.glass-strong` (`.glass-elev-3`) | Modals, sheets, menus |
| `.glass-tint` | Tint only, no blur: glass nested in glass, small inline elements |
| `.glass-interactive` | Adds hover / pressed states to any glass surface (clickable cards) |
| `.glass-field` | Inputs and selects: nearly opaque, clear border, strong focus |
| `.app-backdrop` | The static yellow gradient blobs behind the app (painted by `AppShell`) so the blur has something to show |
| `.solid-panel` / `SolidPanel` | Dense data (guideline matrices, KPI / weight tables, evaluation tables, forms): **never** glass |

Rules: glass for chrome, cards, modals, hero; solid for dense data. Nested glass drops its own blur (blur budget); never transition `backdrop-filter`; `@supports not` and
`prefers-reduced-transparency` / `prefers-contrast: more` fall back to opaque fills. React helpers: `components/design-system/GlassCard.tsx` (`elevation` 1-3), `SolidPanel.tsx`, `RagBadge.tsx`,
and shadcn primitives in `components/ui/`. Marketing and auth pages have their own CSS (`marketing.css`, `auth.css`).

## Tests and tools

| What | Command | Notes |
|---|---|---|
| Unit tests | `npm run test:unit` | `node --test tests/*.test.mjs`, no browser: auth helpers, middleware (home, origin, roles), api session handling, chat turn state and watchers, evaluation filters / selection / bulk delete, trash selection, collab client, uploads, downloads, target bands |
| Type check / lint / build | `npx tsc --noEmit`, `npm run lint`, `npm run build` | CI (`.github/workflows/frontend-ci.yml`) runs all four on Node 24 |
| `tools/e2e-auth.mjs` | `node tools/e2e-auth.mjs --url http://localhost:3000` | Playwright smoke of sign-in: redirects, cookie flags, wrong password, silent renewal, sign-out. Creates a throwaway `e2e-...@example.com` account (clean up with `purge_users`) |
| `tools/e2e-home.mjs` | `node tools/e2e-home.mjs [--url ...] [--shots dir]` | Homepage and routing smoke; reads `ADMIN_EMAIL` / `ADMIN_PASSWORD` from the environment or `infra/.env` (never printed) |
| `tools/e2e-responsive.mjs` | `node tools/e2e-responsive.mjs [--url ...] [--shots dir]` | Homepage at ten viewports: screenshots, no horizontal scroll, tap targets |

The e2e tools need Playwright's browsers (`npx playwright install chromium`) and a running stack; they are developer tools, not part of CI.
