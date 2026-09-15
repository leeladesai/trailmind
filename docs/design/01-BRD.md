# Business Requirements Document (BRD)
## TrailMind — Embeddable Behavioral Recommendation Platform

| | |
|---|---|
| Project | TrailMind (originated at the TrailMind Build Challenge 2026 hackathon — see §1a) |
| Document owner | [Your name / team] |
| Version | 2.0 — updated for the platform pivot |
| Status | §1–§9 below describe the post-pivot business shape; historical hackathon-only context (rubric, judges, CI screener) is retained in §1a since it explains *why* the architecture exists, not because it's still the active goal |

---

## 0. Platform pivot summary

As of the decision in
[`09-Platform-Pivot-Decision.md`](09-Platform-Pivot-Decision.md), TrailMind is no longer a
single-tenant demo app judged once and done — it is becoming a **multi-tenant, embeddable
recommendation platform**: any site embeds a tracker snippet + chat-bot widget, we ingest that
site's own catalog (via feed, DOM scrape, or manual entry), and surface real-time, grounded
recommendations to that site's own visitors. The sections below have been updated for this; §1a is
kept as historical record of the hackathon origin.

## 1. Background

TrailMind Build Challenge 2026 asks teams to build a catalog/marketplace platform where an
agentic AI system observes user behavior, retrieves relevant catalog items via RAG, and produces
persuasive, personalized recommendations that refresh as behavior evolves. Submissions are
screened by an automated system, then judged by humans. Faked features (hardcoded recs, unused
vector DB, unused LLM client) score poorly; efficient, production-minded AI usage is explicitly
rewarded.

The team chose an **AI model & tool catalog** as the domain — a catalog of AI models across
providers and modalities (LLM, voice, image, video) that users browse, search, and compare, with
the agent recommending models based on that evaluation behavior. See
[`docs/00-Domain-Decision.md`](00-Domain-Decision.md) for the alternatives considered and the
reasoning behind this choice; this domain replaces the learning-platform (courses/bootcamps)
domain used in the first planning pass.

## 1a. Historical: hackathon origin (kept for context, not the active goal)

TrailMind Build Challenge 2026 asked teams to build a catalog/marketplace platform where an
agentic AI system observes user behavior, retrieves relevant catalog items via RAG, and produces
persuasive, personalized recommendations that refresh as behavior evolves, screened by an
automated system then judged by humans. That constraint shaped the original architecture (two
decoupled loops, LangGraph pipeline, Mesh-only LLM calls, dual-write SQL/vector store) — all of
which the platform pivot reuses rather than discards. What's retired is the *hackathon-specific*
framing: rubric scoring, CI screener compliance, judge demo readiness, and the single-tenant,
self-curated AI-model catalog as the only subject.

## 2. Business objective

Deliver a platform that:
1. Lets a third-party site embed a tracker + chat-bot widget with minimal integration effort and
   get real-time, grounded, behaviorally-triggered recommendations from its own catalog.
2. Proves the agentic pipeline generalizes across tenants/catalogs, not just the one we curated —
   i.e. the architecture is genuinely domain-agnostic, not domain-shaped code with a demo catalog
   swapped in.
3. Stays trustworthy enough for a regulated first vertical (e.g. banking/financial products) to
   adopt: strict catalog-only grounding, no invented facts, auditable answers.

## 3. Stakeholders

| Stakeholder | Interest |
|---|---|
| Tenant (site owner embedding the widget, e.g. a bank's marketing team) | Reliable catalog ingestion, on-brand widget, real behavioral lift, no compliance exposure |
| End visitor (the tenant's own customer/prospect) | Useful, honest recommendations; no dark patterns; no invented product facts |
| Platform operator (us) | Multi-tenant reliability, grounding correctness, low integration friction for new tenants |
| Team | Shippable, well-architected, testable codebase as the platform grows |

## 4. Scope

### 4.1 In scope
- Multi-tenant auth/isolation: tenant onboarding, API key issuance, per-tenant data isolation in
  SQL and the vector store.
- Embeddable tracker SDK: async snippet, anonymous visitor identification, cross-origin batched
  event ingestion, tagged by `tenant_id` + visitor id.
- Catalog ingestion, pluggable per tenant across three adapters — feed/API pull, DOM scrape,
  manual entry — normalizing into the same catalog schema + vector index per tenant (see
  `09-Platform-Pivot-Decision.md` §3.3).
- Agent pipeline (LangGraph) — the existing analyze → retrieve → rerank → grade/refine → generate
  → store shape, scoped per tenant, strictly grounded to that tenant's own catalog only.
- Trigger + caching logic so the LLM is not called on every event (unchanged principle, now
  per-tenant-per-visitor).
- Chat-bot launcher widget on the host page: opens proactively on a fired trigger, supports
  catalog-grounded follow-up Q&A.
- Real-time push (SSE/WebSocket) from a fired trigger to the visitor's open widget, within seconds.
- All LLM calls routed through Mesh API.
- Tenant/admin console: onboard a site, configure catalog ingestion adapter(s), manage manual
  entries, view per-tenant event/recommendation analytics (generalizes the former curator
  console).

### 4.2 Stretch scope
- Scheduled proactive digest via email/Telegram, now per-tenant.
- LangSmith observability across the agent graph, filterable per tenant.
- Retrieval polish: re-ranking / metadata filtering / better chunking.
- Cookie-consent integration for third-party tracking (see open risk in the pivot record).

### 4.3 Out of scope (for now)
- Real payment processing / checkout.
- SSO for tenant admin accounts, password reset flows.
- Mobile native apps.
- Horizontal scaling / multi-region infra (documented in HLD as a future concern only).
- General-purpose financial/product advice beyond the tenant's own catalog (bot stays
  catalog-only, strictly grounded — see pivot record §3.4).

Note: **multi-tenant orgs** — listed as out of scope in the hackathon-era BRD — is now the core of
the product and has moved into §4.1.

## 5. Success metrics

| Metric | Target |
|---|---|
| Grounding | 100% of bot answers traceable to that tenant's own catalog data — no invented facts, no cross-tenant leakage |
| LLM call efficiency | No LLM call fired on a single raw event; demonstrable caching/trigger logic, per tenant |
| Real-time latency | Trigger-fire to widget update in single-digit seconds under normal load |
| Tenant isolation | No event, catalog, or recommendation data visible across tenants under test |
| Catalog ingestion coverage | All three adapters (feed, scrape, manual) functional for at least one reference tenant each |
| Onboarding effort | A new tenant can go from signup to a working embedded widget without engineering support from us, for at least the manual-entry adapter |

## 6. Assumptions

- FastAPI, LangGraph, and Chroma remain the default stack; Postgres remains a config swap for
  production multi-tenant scale rather than a hard requirement yet.
- At least one tenant will be reachable for real integration testing beyond the reference
  AI-model-catalog tenant (the pivot record's example is a banking/credit-card vertical).
- Tenants vary in technical capability — hence three catalog ingestion adapters rather than one.

## 7. Constraints

- Every LLM/AI call must go through Mesh API — unchanged, non-negotiable.
- No secrets committed; `.env` gitignored.
- No PII/PCI fields ever captured in tracked events — a hard constraint now that tenants may be
  regulated (e.g. banking).
- Bot responses must be strictly grounded to the tenant's own catalog — no general advice, no
  invented product facts (pivot record §3.4).

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Vector DB / SQL drift (dual-write bugs), now per-tenant | High | `vector_synced` flag + idempotent upsert per tenant, covered by integration tests |
| DOM-scrape adapter breaks silently on host layout changes | High for tenants using that adapter | Scrape-health/staleness signal, analogous to `vector_synced` (open risk, not yet designed — see pivot record §5) |
| Cross-tenant data leakage (events, catalog, or recommendations) | Critical | Tenant isolation enforced at the data-access layer, not just query filters; covered by isolation tests |
| Bot states an incorrect fact about a regulated product (e.g. a bank's card terms) | Critical — real liability, not just a demo miss | Strict catalog-only grounding (§3.4 of pivot record); every LLM-referenced fact validated against retrieved catalog data before delivery |
| Third-party tracking without consent handling | Medium-high, jurisdiction-dependent | Cookie-consent integration flagged as stretch scope; not yet designed |
| Real-time transport unavailable (corporate proxy blocks WS/SSE) | Medium | Fallback behavior not yet designed — flagged as open risk in pivot record §5 |

## 9. Deliverables (this SDLC package)

1. BRD (this document)
2. FRD — functional + non-functional requirements
3. UX flows + low-fidelity wireframes
4. MVP definition and iteration roadmap (Agile/Scrum)
5. HLD — system architecture
6. LLD — schema, API contracts, agent node contracts, sequence diagrams
7. Test strategy (TDD-driven) mapped to requirements
8. [`09-Platform-Pivot-Decision.md`](09-Platform-Pivot-Decision.md) — the pivot decision record this update implements
