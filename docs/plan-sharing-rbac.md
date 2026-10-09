# Plan: sharing, notifications, shared evaluations, infinite list, RBAC, per-user concurrency

Status: implemented (verified against the code on 2026-10-10) on top of migration `0012_auth_ownership` as `0013_sharing_rbac` (+ `0014_chat_shares`, `0015_chart_trash`, `0016_eval_cancel_reason`).
Research notes (web): keyset pagination = `(sort_key, id)` row-value seek + `limit+1` + a separate (capped) COUNT
([GitLab pagination guidelines](https://docs.gitlab.com/development/database/pagination_guidelines/));
"select all matching" must be a server-side filter selection, never a list of loaded ids; notifications = Postgres is
the system of record, unread count is a cheap partial-index count behind an ETag/304, SSE+Redis pub/sub only adds a
wake-up and still needs Postgres catch-up, so polling + ETag is chosen (stateless, no per-process queues);
account enumeration: the user explicitly wants "no such user" feedback, so it is mitigated (authenticated senders
only, per-sender Redis rate limit on lookups, deactivated users look absent, no self-invite, constant work for hit/miss).

## Assumptions (incl. the truncated sentence)
* "he should also see ..." = the sender sees the invitations they sent with status (pending/accepted/declined/revoked),
  can revoke a pending one and can remove a collaborator later. Collaborators can leave a chart.
* Usernames did not exist: `users.username` (nullable, unique case-insensitive) is added; login accepts email or username.
* Roles on a chart: `owner` (scorecards.owner_id) and `editor` (collaborator). One collaborator role keeps the matrix simple.
* A chart is "shared" when it has >= 1 accepted collaborator; the activity log is recorded always, shown only when shared.
* RE-SHARING (later requirement): the owner AND every accepted collaborator may invite further people, so chains
  (owner -> B -> C -> D) work in both directions between admins and users. Only the owner removes collaborators; an
  editor sees and can revoke only the invitations THEY sent.
* CHAT SHARING (later requirement, migration `0014_chat_shares`): a chat can be shared READ-ONLY with a named active user
  (`POST /chat/sessions/{id}/shares {identifier, with_chart}`): the recipient sees the conversation and the KPI draft
  under "Shared with me" (`/chat/shared/{id}`) but cannot post; `POST /chat/shared/{id}/save` copies the draft into the
  recipient's OWN chart (once). `with_chart` additionally sends the normal chart invitation (collaboration + the chart's
  evaluations). Chats stay private to everyone else (admins included). The "Share" button sits top-right of the chat.
* HANDLES (bug fix): sign-in and invitations accept an email, a username, or - when nobody has that username - the part
  of an email before the `@` if exactly one account has it (`app/auth/handles.py`), so accounts that predate usernames
  can still sign in / be invited by handle. `/auth/config.username_login` is therefore always true.
* `python -m app.scripts.share_everything --to <handle> [--dry-run]` adds one user as editor on every chart and as
  read-only recipient of every chat (idempotent, audit-logged, ownership untouched). It also creates ONE notification per
  share (`chart_shared` / `chat_shared`, dedupe-keyed, so a re-run never duplicates and also back-fills shares made by an
  earlier run). The shared charts show "Shared with you" + the owner's name in the Charts library; the chats appear
  under "Shared with me".
* CHAT RE-SHARING (chains): a recipient of a chat may pass it on, still read-only (owner -> B -> C -> D). The owner sees and
  revokes every share, a recipient only the ones they made (403 `owner_only` otherwise); the owner is notified of each hop;
  the shared view / list say who passed it on ("shared by"). "Chat and chart" from a recipient needs that they can invite
  to the chart themselves. The shares list reports the chart invitation status (pending / accepted / declined / revoked).
* CHART PAGE SHARE: when the sharer has a chat behind the chart, the chart's Share dialog offers "Only the chart" or "Chart and
  its chat" (`include_chat` on the invitation request, `source_chat_id` on `GET .../sharing`).
* The editing log says who joined via whom ("X joined the chart (invited by Y)").

## Data model (migration 0013_sharing_rbac, after 0012_auth_ownership)
| table / change | columns | indexes / constraints |
|---|---|---|
| `users` + | `username varchar(64) null`, `deleted_at timestamptz null` | unique `lower(username)`; role values normalised member/designer/evaluator -> `user`; `system` marker untouched; people roles validated as admin\|user in the API |
| `scorecard_collaborators` | `id`, `scorecard_id` FK cascade, `user_id` FK cascade, `role` ('editor'), `invited_by`, `created_at` | unique(scorecard_id,user_id); index(user_id) |
| `scorecard_invitations` | `id`, `scorecard_id`, `inviter_id`, `invitee_id`, `role`, `status` pending/accepted/declined/revoked, `created_at`, `responded_at` | partial unique(scorecard_id,invitee_id) WHERE status='pending'; index(invitee_id,status); index(scorecard_id) |
| `scorecard_activity` (append-only) | `id bigserial`, `scorecard_id` FK cascade, `actor_id` (SET NULL), `actor_name`, `action`, `entity_type`, `entity_id`, `summary`, `detail jsonb`, `created_at` | index(scorecard_id,id desc); BEFORE UPDATE trigger raises (append-only) |
| `notifications` | `id uuid`, `user_id` FK cascade, `type`, `title`, `body`, `data jsonb`, `link`, `dedupe_key`, `created_at`, `read_at` | unique(user_id,dedupe_key) WHERE dedupe_key NOT NULL (idempotent creation); index(user_id,created_at desc,id desc); partial index(user_id) WHERE read_at IS NULL |
| `evaluations` indexes | | (created_at desc,id desc); (scorecard_version_id,created_at desc); partial (owner_id) WHERE status in queued/ingesting/processing/scoring (job slot) |

Per-user concurrency needs no new table: the slot is DERIVED from existing rows (see below).

## Permission matrix (404 = indistinguishable from "does not exist")
| action | owner | editor (accepted) | pending invitee | declined / revoked / removed | non-collaborator | admin (no bypass) |
|---|---|---|---|---|---|---|
| read chart, versions, KPIs, guidelines | Y | Y | preview only via `/invitations/{id}/preview` | 404 | 404 | 404 unless shared |
| edit chart/KPIs/weights/guidelines/versions | Y | Y | 404 | 404 | 404 | 404 |
| delete chart (`DELETE /scorecards/{id}`) | Y = moves to trash | = leave (204, owner keeps it) | 404 | 404 | 404 | 404 |
| invite further people | Y | Y (re-share) | 404 | 404 | 404 | 404 |
| revoke an invitation / remove a collaborator | Y (any / any) | own invitations only / 403 `owner_only` | 404 | 404 | 404 | 404 |
| list/run/cancel/retry/export evaluations on chart | Y | Y | 404 | 404 | 404 | 404 |
| activity log | Y | Y | 404 | 404 | 404 | 404 |
| chat sessions | private to the user, always (admin too) |

Only people who can already see the chart get a 403 (`owner_only`); everybody else always gets 404.
Admin-only: `/api/v1/admin/users*` (403 for users), plus the `/admin` and `/settings` pages (redirect for users).

## Sharing flow
`POST /scorecards/{id}/invitations {identifier}` (owner): lookup by username or email among ACTIVE users
(rate limited per sender: `RATE_LIMIT_SHARE_LOOKUP`, default 20/hour; route limiter too) ->
404 `user_not_found` "No active user matches that username or email." | 422 `self_invite` | 409 `already_collaborator`
| 200 idempotent when a pending invite exists (`already_pending: true`) | 201 created + notification `invite_received`.
`GET /scorecards/{id}/sharing` (owner+editors), `DELETE .../invitations/{iid}` (revoke), `DELETE .../collaborators/{uid}` (remove),
`POST .../leave`. Receiver: `GET /invitations`, `GET /invitations/{id}/preview` (read-only KPI tree), `POST /invitations/{id}/accept|decline`.
Accept is a transaction: invitation pending->accepted, collaborator row (unique), activity `collaborator_joined`, notification to inviter.

## Notification catalogue
| type | recipient | created by | dedupe_key |
|---|---|---|---|
| `invite_received` | invitee | API | `invite:{id}` |
| `invite_accepted` / `invite_declined` | inviter | API | `invite:{id}:accepted|declined` |
| `collaborator_removed` / `collaborator_left` | removed user / owner | API | `collab:{sc}:{user}:{ts-bucket}` |
| `chart_edited` | owner + other editors | API (coalesced, <= 1 per actor/chart/10 min) | `edit:{sc}:{actor}:{bucket}` |
| `evaluation_completed` / `evaluation_failed` | runner (not for batch members) | worker (dispatcher `_drive`) | `eval:{id}:{attempt}:{status}` |
| `batch_completed` | batch creator | worker (batch refresh) | `batch:{id}:{n_finished}` -> `batch:{id}` |
| `scorecard_saved` | chat owner | worker/API on materialise | `saved:{session}:{turn_started_at}` |
| `invite_revoked`, `chart_shared` (admin CLI), `chat_shared`, `chart_trashed`, `chart_restored`, `evaluation_resumed`, `evaluation_retry_available` | see sections below | API / CLI | per event |
| `chat_question` | chat owner | background turn | `question:{session}:{turn_started_at}` |
Delivery: polling `GET /notifications/unread-count` (ETag/`If-None-Match` -> 304, ~15 s, paused when the tab is hidden, faster
after actions) and `GET /notifications?cursor=` (keyset by created_at,id) when the panel is open. Mark read: `POST /notifications/{id}/read`, `POST /notifications/read-all`.
All queries filter `user_id = caller` (per-user scoping). No in-process queue; works with any number of replicas.

## Shared evaluations
Access = owner OR accepted collaborator of the evaluation's scorecard (single clause in `app/authz.py`:
`scorecard_access_clause`). Evaluation rows keep their runner (`owner_id` / `evaluated_by`) for labels and slots, but
visibility follows the chart. Removed collaborator: their ACTIVE jobs on that chart are cancelled (documented decision),
finished rows stay with the chart. Worker/pipeline code carries only evaluation ids, so no pipeline change; notifications
and activity use the row's runner.

## Concurrency (per user, derived slots)
* Evaluation job slot: at most one active job per user = rows with `owner_id=user` and status in queued/ingesting/processing/scoring;
  a batch is one job (rows sharing `batch_id`). Admission takes `pg_advisory_xact_lock(hash(user,'eval'))`, checks, inserts and
  commits -> atomic across replicas. 409 `{code:'user_job_active', evaluation_id, batch_id, message}`; idempotency-key replay
  returns the stored result BEFORE the check. Retry re-checks the slot of the evaluation's runner under the same lock
  (members of the same batch do not block each other). Self-healing: the slot IS the row state, so completion, failure, cancel,
  lease expiry (another worker adopts and finishes/fails it) and deactivation (admin cancels active rows) free it; nothing to leak.
* Chat slot: at most one running turn across ALL a user's sessions (`pending_turn_started_at` non-stale). Marker claim takes
  `pg_advisory_xact_lock(hash(user,'chat'))`; 409 `{code:'user_chat_active', session_id}`. Lease expiry/reaper already clears dead turns.
* A chat turn and an evaluation job run side by side; different users never block each other on the same chart.
* Dispatcher fairness: the global cap is kept; queued rows are ordered by (per-user rank, queued_at) = oldest-first with per-user
  round robin, so a user's backlog never starves another user; a user with a single job still uses spare capacity.
* Optimistic concurrency: PATCH of scorecard/version/KPI node/guideline accept `If-Match: <updated_at ISO>`; mismatch -> 409
  `{code:'stale_edit'}` with the current `updated_at`; UI shows "changed by X, refresh".

## Evaluations list (infinite scroll)
`GET /evaluations/page?cursor&limit(1-100, default 40)&sort&status&scorecard_id&batch_id&band&min_score&max_score&meets_target&date_from&date_to&q&runner&shared`
-> `{items, next_cursor, total, selectable, exportable, total_capped, count_cap}`; `sort` = `newest|oldest|score_desc|score_asc|name_asc|name_desc`;
`status` = `all|active|completed|failed`; keyset `(key,id)` cursor bound to its sort; count capped. `POST /evaluations/refresh` returns the current
state of up to 100 visible rows. There is **no** separate "select ids" endpoint: "select all matching" is a server-side filter. `POST /evaluations/bulk-delete`
takes `{ids}` (max 5000) **or** `{filter, exclude_ids}`; `POST /evaluations/export` takes `{evaluation_ids}` (max 200) **or** `{filter, exclude_ids}` (max 500 matches).
Frontend: IntersectionObserver sentinel, list resets on filter/sort change, id-set dedupe, `content-visibility:auto` rows,
selection = explicit set OR `{allMatching, excluded}` (`lib/eval-selection.ts`, `lib/eval-bulk.ts`). After an export the success popup shows and the selection is cleared.

## RBAC / admin
Roles `admin|user`. `/admin/users`: list/search, create, patch (name,email,username,role), password, deactivate, reactivate, delete.
Guards: not self, never the last active admin (advisory lock). Deactivate/password/role change revokes sessions
(`sessions_valid_after`) and cancels active jobs. Delete = anonymise: row kept as tombstone (`Deleted user`, no login,
no email), private charts + chats removed, shared charts transferred to the longest-standing collaborator, memberships/invites/
notifications removed, evaluations they ran are kept (attributed to "Deleted user"). All audit-logged and rate limited.
Nav: users see Charts/Chat/Evaluations; admins also Settings and Users. Own profile/password via the user menu "Account" dialog.

## Trash (chart soft delete, migration `0015_chart_trash`)
- `scorecards.deleted_at` / `deleted_by` (null = live). `scorecard_access_clause` requires `deleted_at IS NULL`, so a trashed chart is
  404 for EVERYONE (owner included) on every path built on it: chart / versions / KPIs / guidelines / sharing / activity / evaluations
  (list, page, export, bulk delete, detail) / batches / job creation. Also excluded: pending invitations of a trashed chart (not listed,
  preview + accept answer 409 `chart_in_trash`), chat `linked_scorecard_id`, similar-chart suggestions, the dispatcher's queued-job
  claim, and `materialize_draft` for a linked trashed chart (saves a new chart instead). Collaborator rows, evaluations and invitations
  are kept, so a restore brings everything back.
- `DELETE /scorecards/{id}`: owner -> soft delete (204): `chart_trashed` activity, `chart_trashed` notification to each collaborator,
  running/queued evaluations of EVERY runner are cancelled (`app/trash.py::cancel_all_jobs_on_scorecard`). Collaborator -> the same as
  `POST .../leave` (204; `leave_chart`). Non-members / admins -> 404. (Before: a collaborator got 403 `owner_only`.)
- Owner-only trash endpoints (registered before `/{scorecard_id}`; strictly the caller's own trashed charts, an admin has no bypass,
  foreign ids are reported as `not_found`, 404 when none of the ids is in the caller's trash): `GET /scorecards/trash`
  (`id, name, domain, deleted_at, days_left, collaborator_count, evaluation_count`), `POST /scorecards/trash/restore {ids}`
  (`chart_restored` activity + notification), `POST /scorecards/trash/purge {ids}` (permanent, `delete_scorecard_cascade`),
  `POST /scorecards/trash/empty`. Responses: `{done: [ids], not_found: [ids]}`; at most 200 ids per call.
- Retention: `TRASH_RETENTION_DAYS` (default 30). `app.trash.purge_expired(db, now)` deletes charts trashed longer than that, in batches of 50
  claimed with `FOR UPDATE SKIP LOCKED` (idempotent, safe on several replicas). Called by the worker housekeeping loop (at most every
  10 minutes) and lazily by `GET /scorecards/trash`. `days_left = ceil((deleted_at + retention - now) / 1 day)`.
- Admin user deletion is unchanged: the deleted user's private charts (trashed or not) are purged, shared ones transferred (a transferred
  chart that was trashed stays in the new owner's trash).
- Frontend: Charts library card delete (owner: "Move to trash" dialog, with a warning when shared; editor: "Remove for me" dialog
  explaining only their own access goes), a "Trash" button with count badge at the top right of the Charts page, and `/charts/trash`
  (checkbox per row, header select-all with indeterminate state, shift-click range, sticky selection bar with Restore / Delete
  permanently (confirm, Cancel focused) / Empty trash (confirm), loading / empty / error states, aria-live announcements).
  Selection logic: `lib/trash-selection.ts` (on top of `lib/eval-bulk.ts`), API: `lib/trash-client.ts`.

### Trash <-> chats <-> running jobs (added)
- `ChatSessionRead` / `ChatTurnRead` carry `chart_state` (`none|active|trashed|deleted`) and `chart_can_restore`. A trashed chart's id is
  only returned to its owner (to restore it); `deleted` = the chat built a chart (completed) that no longer exists. The chat page shows
  an inline notice (owner: "in the trash" + **Restore chart**; editor: plain text, no link, no button; deleted: "was deleted").
- Jobs cancelled BECAUSE the chart was trashed get `evaluations.cancel_reason = 'cancelled_by_trash'`. Restoring re-queues exactly
  those (`app/trash.py::resume_trash_cancelled_jobs`): per runner, under the runner's slot lock, only when the runner has no other
  active job (a batch comes back whole); otherwise NOT queued behind (the slot is the row state, there is no waiting list) - the runner
  gets an `evaluation_retry_available` notification and presses Retry. Deactivated runners are skipped. Handled rows become
  `trash_restore_handled`, so repeated restores / later trash cycles never re-queue them again (idempotent). Re-queued jobs are ordinary
  queued rows (new attempt, fresh execution name) claimed by the dispatcher like any retry; each runner gets `evaluation_resumed`,
  the chart log gets `evaluations_resumed`. A purge after the retention deletes the rows; nothing is resurrected.

## Admin CLI: purge test / anonymised users (added)
`python -m app.scripts.purge_users --pattern 'e2e-%@example.com' [--pattern ...] [--dry-run | --yes]` HARD-deletes accounts that own
nothing. Dry run is the default. Patterns need an `@`, a literal domain and a literal prefix of >= 3 characters. Never touched: admins,
the `system` user, `*@qualityscorecard.local`, `arpankumar1119@gmail.com`. Refused (and reported): owns charts / versions / evaluations /
batches, has chat sessions or shares, is a collaborator or has accepted invitations. Cleaned with the row: refresh / reset tokens, OAuth
links, notifications, idempotency keys, pending/declined/revoked invitations. Audit: `audit_log.actor_id` is `ON DELETE SET NULL`
(rows stay, un-attributed) and one `user_purged` tombstone row (id + e-mail domain + count) is written per account.

## Brand colour
The app's "lemon" tokens are the logo / homepage amber gold: `--lemon #FFC21A`, hover `#E09F00`, soft `#FFF3CF`, ink `#4A2F00`.
Contrast (WCAG): `#16161A` on amber 11.2:1, `--lemon-ink` on amber 7.7:1 / on hover 5.4:1 / on soft 12.9:1 / on white 14.2:1; the
focus ring `#1D4ED8` is 6.4:1 on the page and 4.1:1 on amber. Amber is a fill colour only (1.6:1 on white).

## Known library bug worked around
SQLAlchemy 2.0.36 (pinned) mis-aligns the `RETURNING` columns of ORM-enabled `UPDATE`s under concurrent use
(sqlalchemy #13439, fixed in 2.0.52): the chat-turn claim returned `(started_at, ..., id)` instead of `(id, ..., started_at)` and
the leases tests failed ~40% of runs. All `update(...).returning(...)` statements now run with `synchronize_session=False`
(correct regardless of version); bump the pin to >= 2.0.52 when convenient.

## Tests
Backend: `tests/test_sharing.py` (invitations, preview, re-sharing, editing log, shared evaluations), `test_notifications.py`, `test_eval_page.py`, `test_admin_users.py`,
`test_user_slots.py`, `test_authz_isolation.py` / `test_authz_inventory.py` (cross-user matrix and a route inventory), `test_chat_shares.py`, `test_chart_trash.py`,
`test_trash_resume.py`, `test_chat_chart_state.py`, `test_dispatcher_stop.py`, `test_chat_runner_stop.py`, `test_purge_users.py`, `test_share_everything.py`.
Frontend (`node --test`): `collab-client`, `eval-bulk`, `eval-selection`, `eval-filters`, `trash-selection`, `role-routes`, `middleware-home` tests. Live Playwright smoke: `frontend/tools/e2e-*.mjs`.
