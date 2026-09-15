# Build Status

## TrailMind

This file tracks what's actually built, not what's planned — `docs/04-MVP-Roadmap.md` remains the
spec of record for scope and phase ordering, `docs/02-FRD.md` for acceptance criteria per ID, and
`docs/06-LLD.md` for schema/API/node contracts. This file only answers two questions: **what's
done**, and **what's next**.

**Convention:** whoever closes an item checks the box and updates "Next up" as part of the same
commit as the code — don't let this drift into a second, stale plan.

---

## Next up

> All bonus scope (Iteration 2 + 3) and every numbered NFR are done. `NFR-1` required a real fix,
> not just a measurement: the triggered agent pipeline (a live Mesh network call) was blocking the
> `/api/events/batch` response, which would have failed the <150ms budget on every trigger — moved
> to a `BackgroundTasks` job (`app/main.py`), verified live to return in ~50ms while the real
> narrative lands a few seconds later via the DLV-2 dashboard poll.
>
> Post-roadmap hardening pass (this session): session-aware recommendation weighting (current
> session dominates over diluted history instead of a flat last-20-events window), a per-user
> `asyncio.Lock` fixing a real race that produced duplicate `Recommendation` rows, copy-to-clipboard
> + watchlist engagement features, a Mesh-driven catalog expansion (9 → 27 models), a
> content-similarity "you might also be interested in" endpoint distinct from the activity-driven
> dashboard recs, and an expanded compare table (4 → 7 parameters, fixing a mixed-modality
> mislabeling bug). Also fixed a real gap flagged in review: the admin "story" field (the
> persuasive "why this model" grounding text) was captured in the admin UI but never reached the
> database or the Mesh prompt — `Model.story` is now a real column (migrated in `app/db.py`, no
> Alembic in this MVP), flows through `ModelCreate`/`ModelResponse`, is folded into the Chroma
> embedding text, and is included in the candidate payload sent to Mesh for narrative generation.
> Also added an admin-controlled "judge demo mode" (`DemoModeSetting`, `GET`/`PUT
> /api/settings/demo-mode`) — off by default, shows a live queued→sent event-tracking overlay in
> the model drawer/detail page for every AI-engineer session while flipped on, for live pipeline
> demos only.
>
> Dashboard redesign (this session): `why_this` per recommendation card is now computed
> deterministically (`contextual_reason`, `agent_graph.py`) from what the user actually did this
> session — a latency win against models they compared/viewed, or a search term matching the
> candidate's own use-case tags — instead of a flat Chroma-distance label, with the old label kept
> only as the final fallback. A new `session_evidence()` (`recommendation.py`) itemizes that same
> current-session activity for a new Dashboard "Because you looked at these" row (`GET
> /api/recommendations/me` now returns an `evidence` list). The Mesh prompt (`mesh.py`) now
> receives price/latency/context-window/use-case-tag facts it was previously missing, and asks for
> one-sentence intent framing ("building a real-time voice agent") before the comparison pitch, so
> the top narrative card reads as inferred intent rather than a flat activity recap. Verified live
> against the real Mesh API: comparing two Voice models and searching a matching term produced
> `"beats ElevenLabs Turbo v2.5 on latency"` on the winning card and an intent-framed narrative.
>
> Admin observability (this session): `OBS-2` brings LangSmith's pipeline traces into the
> admin portal itself (`/admin/observability`) instead of requiring a curator's own
> LangSmith login — see the Iteration 3 section below for detail.
>
> Bonus round 2 (this session, working through the BRD's "additional bonus items" list):
> real semantic embeddings via Mesh (`google/embeddinggemma-300m`, `MESH_EMBEDDING_MODEL`)
> replacing the deterministic hashed bag-of-words fallback whenever `MESH_API_KEY` is set —
> verified live that a paraphrased query with zero literal term overlap now correctly
> surfaces the right modality's models. Plus a new 6th LangGraph node, `rerank_candidates`
> (`app/services/agent_graph.py`), between `retrieve_models` and `grade_refine`: a
> deterministic hybrid dense+sparse re-rank blending Chroma's embedding distance with
> lexical term overlap, additive and capped so it stays compatible with the existing
> WEAK/STRONG distance thresholds — no extra LLM/network call. Also bumped
> `prepare_retrieval_recommendation`'s LangGraph `recursion_limit` (25 → 60): the 6th node
> pushed the bounded-retry loop's actual internal step count past LangGraph's default even
> though the visible node count is small, caught by `tests/test_nfr.py`'s forced-retry test.
>
> Explicit feedback loop (this session): 👍/👎 on a dashboard recommendation card is a new
> `recommendation_feedback` `Event` type (no new table/endpoint — same batched
> `/api/events/batch` path everything else uses). `rerank_candidates` now also reads recent
> feedback (`recommendation.recent_feedback_by_model`, 14-day lookback) and adjusts each
> candidate's distance — asymmetric on purpose, a downvote penalizes more than an upvote
> rewards. Real bug caught while verifying live: `EventInput`'s `event_type` regex
> (`app/schemas.py`) hadn't been updated for the new type, so every feedback submission
> would have silently 422'd before reaching the database — fixed, with a regression test.
> Verified live end-to-end: downvoting a #1-ranked model dropped it to last place in the
> next generated recommendation.
>
> Cost/latency rollup (this session): `Recommendation` gained `mesh_latency_ms`/
> `mesh_prompt_tokens`/`mesh_completion_tokens`/`mesh_cost_usd` (idempotent ALTER TABLE
> in `app/db.py`, same pattern as `story`), captured directly off the Mesh response in
> `app/services/mesh.py` — latency via wall-clock timing around the call, tokens from
> `response.usage`, cost priced from Mesh's own `/models` catalog (fetched once per
> process, cached; a failed lookup degrades to unknown cost only, never breaks
> latency/token capture — verified both paths with mocked failures). New `GET
> /api/admin/observability/costs` aggregates straight from our own DB (not another
> LangSmith call) and a stat-tile row on `/admin/observability` surfaces it. Verified
> live: a real generation call showed up with real numbers (807 prompt / 2537
> completion tokens, ~16.7s latency — the model's own reasoning time, not a regression;
> confirmed the pricing lookup itself is ~0.3s and properly cached after the first call).
>
> Per-user Telegram digest (this session): `User.telegram_chat_id` (idempotent ALTER
> TABLE in `app/db.py`), settable via a new "Proactive digest delivery" panel on the
> Dashboard (`GET`/`PUT /api/auth/me`, `/api/auth/me/telegram-chat-id`).
> `TelegramNotifier` (`app/services/digest.py`) now resolves `user.telegram_chat_id`
> first, falling back to the single shared `TELEGRAM_CHAT_ID` broadcast chat only for a
> user who hasn't set their own, and raising (counted as a skipped delivery, same as
> any other send failure) if neither exists — closes the "single broadcast chat, not
> per-user" limitation the README previously documented. Verified live: set a real
> chat ID via the API, confirmed `TelegramNotifier` resolves that user's own ID over
> the shared fallback, and a user with none set correctly falls back.
>
> Bulk catalog upload (this session): `POST /api/admin/models/bulk-upload` accepts a CSV or
> JSON file (up to 500 rows) instead of one-at-a-time entry. New
> `app/services/catalog_import.py` parses the file, coerces each row (tag-splitting,
> latency-string-to-int), validates via `ModelCreate` per row, dedupes case-insensitively by
> title (skipped, not overwritten), and inserts through the same `catalog.create_model`
> dual-write path the single-model admin API uses — one bad row never aborts the batch, same
> per-row report shape as `scripts/expand_catalog_via_mesh.py`. New "Upload catalog file"
> modal on `/admin`. Verified live: a 3-row CSV produced 1 inserted / 1 skipped duplicate / 1
> invalid, exactly as reported.
>
> Registration flow fixes (this session): the confirm-password field on `/login` was present
> in the markup but never actually read/compared; switching to "Register" also kept the demo
> login email pre-filled, risking an accidental signup attempt against a known address. Now
> confirm-password and an 8-char minimum are checked client-side, mode switches clear stale
> field values, and errors surface the real API reason (duplicate email, weak password)
> instead of one hardcoded string. Verified live: 201/409/422 all produce distinct, correct
> messages via the new `authErrorMessage()` helper.
>
> Product rebrand (this session): SmartReco → TrailMind everywhere the string referred to our
> app (page titles, nav, cookie name, LangSmith project, digest copy, localStorage keys, demo
> account domain, `pyproject.toml`, every doc header). Verified live: `/health` returns
> `service: "trailmind"`, session cookie is `trailmind_session`.
>
> Follow-up rebrand (later session): remaining "SmartReco Build Challenge 2026" hackathon-program
> references across the docs, the GitHub repo/directory name, and the dev script were also
> updated to TrailMind, at the user's explicit request, superseding the exclusion noted above.
>
> Context-scoped feedback loop (this session): `recent_feedback_by_model` now also resolves
> the `behavior_summary` of the recommendation each rating was tied to (via the
> `recommendation_id` already sent in the feedback event's metadata), and
> `apply_feedback_adjustment` only carries a rating forward if the current query overlaps
> enough (lexical) with that original context — fixes a real gap where a downvote on an
> irrelevant recommendation (e.g. a voice model shown for a "rack-based server" search) would
> have silently suppressed that same model the next time it was genuinely the right answer.
> Fails open (rating still applies) when no context is stored, so older feedback events keep
> working.
>
> Admin Users list (this session): the `User` table and register/login/logout/me endpoints
> already existed and worked, but there was no way to see who had registered from the admin
> portal. New `GET /api/admin/users` (admin-only) + `/admin/users` page (`users.html`), same
> per-screen-template/admin-table pattern as Models/Observability — read-only: email, role,
> join date, Telegram-connection status.
>
> Email delivery, verified live end-to-end (this session): configured real Gmail SMTP
> (App Password) in `.env` and confirmed the actual scheduled-digest path —
> `prepare_retrieval_recommendation` (real Mesh call) → stored `Recommendation` →
> `EmailNotifier.send()` — delivers a genuine email, not a mocked test. Along the way, fixed a
> `.env` parsing bug (a pasted app password had landed on its own malformed line) and a
> pre-existing test-isolation gap in `tests/test_digest.py` (`Settings()` was reading the
> developer's real `.env`, so `build_notifier`'s Telegram-only and neither-configured cases
> silently broke once `.env` had real SMTP creds — fixed by explicitly nulling the fields each
> case isn't testing, matching `conftest.py`'s existing isolation pattern).
>
> Only the final submission-readiness pass remains on the whole roadmap.

---

## Frontend routing migration — Jinja2 templates + vanilla JS

Replaces the current `FileResponse(docs/03-mockups.html)` shortcut in `app/main.py`, which is why
every screen currently shows the same URL (`/`) — it's one static file with JS toggling which
`<div class="page">` is visible, not real routing. This was a deliberate decision (see the
conversation this task was added from): Jinja2 + vanilla JS over React/TypeScript or Streamlit,
because it already matches `docs/05-HLD.md`/`AGENTS.md`'s documented architecture, needs no new
build tooling (keeps the "single container, no separate frontend deploy" story), and reuses ~90%
of the existing HTML/CSS design system instead of a rewrite. `docs/03-mockups.html` stops being
served as live app code once this lands — it goes back to being purely the design reference.

- [x] Add `jinja2` to `pyproject.toml` dependencies
- [x] `app/static/css/app.css` — extract the `<style>` block from `docs/03-mockups.html` unchanged
- [x] `app/static/js/app.js` — extract the API/interaction JS (`trackEvent`, compare tray, modal,
      forms); `go(page)` now updates real browser URLs for in-shell transitions
- [x] `app/templates/base.html` — shared nav chrome (already auth-state-driven in the mockup),
      `{% block content %}`
- [x] Per-screen templates extending `base.html`: `login.html`, `admin_login.html`, `catalog.html`,
      `model_detail.html`, `compare.html`, `dashboard.html`, `activity.html`, `admin.html`.
      **Correction (this was previously checked off but not actually true):** each route now
      genuinely ships only its own screen's markup — verified live: `/catalog` no longer contains
      `id="admin-model-table"`/`id="model-modal"`, `/admin` no longer contains `id="catalog-grid"`,
      etc. (`tests/test_handshake.py`). `base.html` holds only the head, the single nav bar, and
      the compare tray (shared across catalog/detail/compare — kept in the shell rather than
      duplicated into three files); the admin model-management modal moved into `admin.html`
      specifically since it's genuinely admin-only.
- [x] Wire `Jinja2Templates` + `StaticFiles` in `app/main.py`; add the `GET` route per template
      below, each auth-gated **server-side** (redirect to `/login` or `/admin/login` on a
      missing/wrong-role session cookie — real, not just visual: with per-screen templates now
      actually isolated, a direct hit on a gated route with no/wrong session genuinely cannot
      render that screen's markup at all, vs. the old single-file build where everything was
      always in the DOM regardless of auth state)
- [x] `go(page)` now performs a real browser navigation (`window.location.href`) instead of
      toggling `.page.active` client-side — necessary once each route stopped shipping every
      screen's DOM. A new `initPage(page)` runs once per real page load to wire up the screen
      that was actually server-rendered. The old `popstate` listener is gone — back/forward now
      works via the browser's native history against real URLs instead of a hand-rolled router.
- [x] **Bug found and fixed while wiring this up:** `logout()` never actually called a backend
      endpoint — it only changed the nav-pill text client-side, leaving the session cookie valid.
      Harmless under the old client-only nav-toggle (no real re-navigation to re-check auth), but
      a genuine bug once routes are truly isolated and re-checked per navigation. Added
      `POST /api/auth/logout` (clears the session cookie) and wired `logout()` to call it before
      navigating away. Verified live: dashboard access 200 → logout (204) → dashboard access 303.
- [x] Compare tray selection persists via `localStorage` (an in-memory JS variable doesn't survive
      a real page navigation); `/compare?ids=12,7` reads the actual query string, making a specific
      comparison a real, shareable link
- [x] Retire `FileResponse(MOCKUP_PATH)` and the catch-all `/` route
- [x] Update `README.md` quick-start once routes/URLs are final

**Route map:**

| Route | Template | Auth |
|---|---|---|
| `GET /login` | `login.html` | none |
| `GET /admin/login` | `admin_login.html` | none |
| `GET /` or `/catalog` | `catalog.html` | AI engineer (public browse allowed per UX doc; personalized recs require login) |
| `GET /models/{id}` | `model_detail.html` | AI engineer |
| `GET /compare` | `compare.html` | AI engineer |
| `GET /dashboard` | `dashboard.html` | AI engineer |
| `GET /activity` | `activity.html` | AI engineer |
| `GET /admin` | `admin.html` | curator |

---

## MVP-0 — Walking skeleton

**Build order within this phase** (narrower than the roadmap's scope list, which doesn't imply
sequencing): data layer + seed script → AI-engineer walking skeleton (login → browse → one real
Mesh call → dashboard) → admin console UI last. The admin UI isn't on the critical path once the
`create_model()` service function it will reuse already works via the seed script — see the
conversation this file was created from for the full reasoning.

- [x] `AUTH-1` — AI engineer register (`POST /api/auth/register`), always role `user`
- [x] `AUTH-2` — AI engineer login (`POST /api/auth/login`)
- [x] `AUTH-3` — two roles exist on the user record, ≥1 admin seeded
- [x] `AUTH-4` — admin-only routes 403 for non-admins, server-side
- [x] `AUTH-5` — separate admin login module (`POST /api/admin/login`), no admin-register endpoint
- [x] `AUTH-6` — admin login rejects non-admin accounts with the same generic error as wrong password
- [x] `CAT-1` — create a model (dual-write service function; UI can come later)
- [x] `CAT-4` — create/edit/delete dual-writes SQL + Chroma, `vector_synced` flag
- [x] `CAT-5` — browse/search catalog (needed for the walking skeleton to be clickable at all —
      implied by the walking-skeleton goal, not explicit in the roadmap's MVP-0 bullet list)
- [x] `TRK-1` — frontend captures model views/searches/comparisons/dwell (dwell fires on
      navigation-away/tab-hide via a start/finalize timer, not on open)
- [x] `TRK-4` — bulk event ingestion (naive per-event POST acceptable here; batching is Iteration 1)
- [x] `AGT-3` — retrieve candidate models via Chroma (grounded — no result outside the vector store)
- [x] `AGT-5` — generate a persuasive narrative referencing only retrieved models (grounding filter).
      Verified live against the real Mesh API, not mocked — see AGT-7.
- [x] `AGT-7` — LLM calls route through Mesh API only. **Live and verified**, not just configured:
      fixed a wrong default base URL (`api.mesh.ai` doesn't resolve; real host is `api.meshapi.ai`),
      a hardcoded invalid model name (`mesh-default`), and a silent-exception bug that hid Mesh
      failures with no log line. Default model is `tencent/hy3` (free tier); real key lives in
      local `.env` only.
- [x] `DLV-1` — latest recommendation shown on the dashboard

Explicitly deferred: retries/grading, caching, scheduling, observability, and the recommendation
pipeline until retrieval and generation are implemented.

---

## Iteration 1 — Production hardening of the core loop

- [x] `TRK-2` — client-side event batching
- [x] `TRK-3` — flush on timer/threshold/unload (`sendBeacon`)
- [x] `TRK-5` — tracking never blocks/slows the page
- [x] `TRK-6` — `model_compare` fires only on explicit "add to comparison," never inferred
- [x] `AGT-1` — trigger evaluator (event-count / time-elapsed / cooldown), not per-event LLM calls
- [x] `AGT-2` — behavior summary aggregation: natural-language summary (not a raw event dump),
      searches/views/explicit compares aggregated by title, and 2+ distinct same-modality
      `model_view`s within 15 min folded in as a soft "browsing multiple X models" signal —
      never phrased as a `model_compare`. Covered by
      `test_analyze_activity_clusters_same_modality_views`.
- [x] `AGT-6` — activity-hash caching skips redundant generations
- [x] `CAT-2` — edit a model
- [x] `CAT-3` — delete a model
- [x] `DLV-4` — `GET /api/activity/me` (read-only; reuses data already persisted for AGT-2/AGT-6)
- [x] `NFR-1` — event ingestion < 150ms p95. Was failing by design until now: the triggered
      pipeline (real Mesh network call) ran inline on `/api/events/batch` before returning.
      Moved to a `BackgroundTasks` job (`run_pipeline_in_background`, `app/main.py`) — ingestion
      now only does the DB insert + cheap `should_trigger` check. Measured p95 ≈3.7ms in-process
      (`tests/test_nfr.py::test_event_ingestion_p95_latency_under_budget`, 40 samples, budget
      150ms) and confirmed live against the real Mesh API: a triggering request returned in
      ~50ms over a real HTTP round trip while the narrative landed a few seconds later, picked
      up by the DLV-2 dashboard poll. Note: `recommendation_triggered` in the response now means
      "the cheap AGT-1 check fired," not "a new recommendation was stored" — AGT-6 dedupe still
      applies, just inside the background task, verified in
      `test_recommendation_retrieves_only_indexed_catalog_candidates`.
- [x] `NFR-2` — ≤ 1 LLM generation call per trigger event (excl. bounded retries). Already true
      by construction (`generate` is not inside the retrieve/grade-refine retry loop) — added
      `test_agent_pipeline_calls_generation_at_most_once_per_trigger` (`tests/test_nfr.py`) to
      prove it under a forced 2-retry scenario: 3 retrieval calls, exactly 1 Mesh call.
- [x] `NFR-3` — dual-write failures never silently orphan a browsable-but-unindexed model
- [x] Admin console UI: model list + sync-status column + `+ Add model` modal (create/edit shared
      form, description/story split) + delete — see `docs/03-mockups.html` for the target design

**Sprint review demo:** two browsing sessions — one that changes the recommendation, one that
doesn't (proves the cache) — plus network tab showing batched, not per-click, event calls.

---

## Iteration 2 — Real agentic depth (bonus scope begins)

- [x] `AGT-4` — grade/refine node, bounded retry (max 2) on weak retrieval. Weak = best Chroma
      distance > 1.5, calibrated empirically against the deterministic hashed bag-of-words
      embedding in `app/vector.py`; on retry, the query is broadened by dropping the summary's
      most specific trailing clause. Covered by `test_grade_refine_retries_on_weak_retrieval`
      and `test_grade_refine_stops_after_max_retries_with_no_candidates`.
- [x] Converted the single-shot pipeline into the 5-node LangGraph graph (`app/services/
      agent_graph.py`: analyze → retrieve → grade/refine → generate → store); `analyze` also now
      owns the AGT-6 hash-dedupe + cooldown short-circuit, previously inline in
      `prepare_retrieval_recommendation`. Verified live against the real Mesh API (both the
      normal path and the repeat-batch short-circuit), plus full existing suite passes unchanged
      (behavior parity).
- [x] `DLV-2` — dashboard polls `/api/recommendations/me` every 15s while open and re-renders
      when a newer recommendation id appears (e.g. from events flushed in another tab); polling
      starts/stops with dashboard nav in `go()` (`app/static/js/app.js`)
- [x] "Why this" tags on dashboard sourced from retrieval metadata — `Recommendation.retrieval_meta`
      (new JSON column) persists each final model's Chroma distance + a plain-language reason
      (`app/services/agent_graph.py:retrieval_reason`); `/api/recommendations/me` attaches
      `why_this` per model for both the stored and live-preview (no-Recommendation-yet) paths.
      Covered by `test_retrieval_meta_reason_reflects_distance_without_retry` and an assertion in
      `test_grade_refine_retries_on_weak_retrieval`.

**Sprint review demo:** deliberately weak query → show the retry/refine loop firing (logs or
LangSmith if Iteration 3 is already in place).

---

## Iteration 3 — Bonus polish (prioritize by capacity, not all required)

1. [x] `OBS-1` — LangSmith trace per pipeline run. `app/services/tracing.py:configure_langsmith`
       flips the `LANGSMITH_*` env vars on app startup only if `LANGSMITH_API_KEY` is set (off by
       default — `@traceable` is a no-op otherwise). Every graph node, `prepare_retrieval_
       recommendation` (top-level pipeline span), and `MeshNarrativeGenerator.generate` (the LLM
       call itself, which bypasses LangChain wrappers so it wouldn't auto-trace) are decorated.
2. [x] `DLV-3` — scheduled digest, real cron via `BackgroundScheduler` + `CronTrigger`
       (`app/main.py`, default 09:00 daily, configurable via `DIGEST_CRON_HOUR`/`_MINUTE`), started
       in the app lifespan. `app/services/digest.py` runs the same agent pipeline per user
       (`trigger_reason="scheduled_digest"`) and delivers the latest recommendation via
       `EmailNotifier` (per-user, `User.email`, prioritized in `build_notifier` whenever SMTP is
       configured) or `TelegramNotifier` (per-user `User.telegram_chat_id`, self-serve via `PUT
       /api/auth/me/telegram-chat-id`, falling back to the single shared `TELEGRAM_CHAT_ID`
       broadcast chat only for a user who hasn't set their own) if configured, else
       `LoggingNotifier` so a run is never silently dropped. `POST /api/admin/digest/run`
       (admin-only) triggers it on demand for the sprint-review demo without waiting for the cron.
       Covered by `tests/test_digest.py` + an admin-auth test in `tests/test_mvp_api.py`. Verified
       live end-to-end with real Gmail SMTP (see "Next up" for detail).
3. [x] Retrieval polish — metadata pre-filtering before ranking, *and* a full re-ranking pass
       (originally descoped here as "not needed given the modest catalog size" — added anyway in
       a later bonus-round pass once retrieval quality against paraphrased queries proved that
       assumption wrong; see the 6th `rerank_candidates` LangGraph node in "Next up").
       `ModelVectorStore.query`/`query_scored` (`app/vector.py`) take an optional Chroma `where`
       filter. `recommendation.dominant_modality` reuses the same same-modality-clustering signal
       as the AGT-2 narrative clause; the `retrieve` node applies it as `{"modality": ...}` on the
       first retrieval pass only, dropping it on any grade/refine retry since the query text is
       already being broadened then. Covered by
       `test_retrieval_applies_modality_filter_on_first_pass_only`.
4. [x] `OBS-2` (post-roadmap) — admin-only "Observability" screen (`/admin/observability`,
       `app/templates/observability.html`) pulls recent `agent_pipeline` LangSmith runs
       (status, latency, error, trace link) directly into the admin portal via
       `app/services/observability.py:fetch_recent_runs` (`Client.list_runs`,
       `execution_order=1` so only the top-level run per trigger shows, not every child
       node) — no separate LangSmith login needed to see whether recent runs succeeded.
       `GET /api/admin/observability/runs` returns `{"available": false, "message": ...}`
       rather than erroring when `LANGSMITH_API_KEY` is unset. Covered by
       `tests/test_observability.py` (admin-gating, unconfigured-key, live-run rendering,
       and API-failure paths, LangSmith `Client` mocked so tests never hit the network).

---

## Cross-cutting NFRs (not phase-bound in the roadmap)

- [x] `NFR-4` — no secrets in repo; passwords hashed; admin routes checked server-side
- [x] `NFR-5` — agent pipeline is named, testable LangGraph nodes, not one monolith
      (`app/services/agent_graph.py`)
- [x] `NFR-6` — every agent run persists `trigger_reason` + timestamps (schema supports this from
      MVP-0; the *trigger logic itself* is Iteration 1's `AGT-1`)
- [x] `NFR-7` — one-command local setup, SQLite by default (finalized in the submission-readiness pass)

---

## Final sprint — submission readiness

- [x] README: architecture summary, setup steps, bonus features implemented, known limitations
- [x] `.gitignore` covers `.env`; GitHub secrets (`MESH_API_KEY`, `SUBMISSION_TOKEN`) configured —
      both secrets confirmed present via `gh secret list`
- [ ] CI workflow verified — it now runs and all 4 critical checks pass (compiles, requirements,
      Mesh usage, Mesh key valid), but the job still exits non-zero: the organizer's result-recording
      step 403s with "You have not submitted your entry yet." This is an external hackathon-dashboard
      submission-form step, not a code defect — fill in and submit that form, then this goes green.
- [x] `seed_data.py` populates demo users/models from curated data (`docs/00-Domain-Decision.md` §6)
      — seeds a curator/admin and an AI-engineer account plus 9 curated models (catalog additionally
      grew to 27 via `scripts/expand_catalog_via_mesh.py`, a Mesh-generated expansion, this session)
- [ ] Optional: deployed URL + 3–5 min demo video
