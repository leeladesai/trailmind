# Domain Decision Record

## TrailMind — Behavioral AI Recommendation Agent

| | |
|---|---|
| Decision | Product/catalog domain for the TrailMind Build Challenge 2026 submission |
| Status | Finalized |
| Supersedes | "Learning platform (courses/bootcamps)" domain used in the original planning pass |

---

## 1. Why this decision needed a document

The agent architecture (event tracking → trigger evaluator → LangGraph retrieve/grade/generate →
grounded narrative) is domain-agnostic — it works the same whether the catalog is courses,
groceries, or AI models. But the *domain* materially affects how differentiated and demoable the
submission is, so it was deliberately re-opened and re-evaluated before implementation started,
rather than left as the default choice from the first planning pass.

## 2. Options considered

| # | Option | Summary |
|---|---|---|
| 1 | **AI model/tool catalog** | Catalog of AI models across providers/modalities (LLM, voice, image, video). User browses/compares models; agent recommends models based on evaluation behavior. Directly echoes the hackathon sponsor's (Mesh AI) own model catalog product. |
| 2 | Grocery / quick-commerce | Instamart/Zepto-style catalog. Agent recommends based on habitual/reorder behavior. |
| 3 | AI tool directory (broader) | Same structural benefits as #1 but sourced from a public AI-tools directory rather than one org's catalog — considered as a lower-risk variant of #1. |
| 4 | Learning platform (original default) | Courses/bootcamps — the domain already drafted in the first planning pass. |
| 5 | Other candidates raised and set aside | Recipes/meal planning, streaming/media catalog, job/gig marketplace, real estate listings — discussed briefly, not scored in detail (weaker fit on either behavioral-signal believability or judge stand-out, without a compensating advantage over #1/#2). |

## 3. Scoring matrix

Scored 1–5 against criteria chosen because they map directly to what makes this specific rubric
(agentic depth, grounding, efficiency, demoability) reward or penalize a submission:

| Criterion | #1 Model catalog | #2 Grocery |
|---|---|---|
| Catalog richness (attributes worth embedding for RAG) | 5 | 3 |
| Behavior-signal believability | 4 | 5 |
| Data feasibility within the delivery window | 3 (curation effort) | 5 (trivial to synthesize) |
| Judge stand-out / novelty | 5 | 2 |
| Demo-ability | 4 | 5 |
| Build risk | Medium | Low |

The two options represent different *flavors* of recommendation narrative, not just different
risk levels:
- **Model catalog = evaluation-based** — "you're comparing X against Y on latency/price, here's
  what fits your pattern." A one-time-complex-decision story.
- **Grocery = habit-based** — "you keep circling back to this category, you're due for a
  reorder." A recurring-need story.

## 4. Decision drivers

- **Sponsor alignment.** Mesh AI sponsors and judges the hackathon; a submission that is itself an
  agent for recommending AI models is a direct, memorable echo of the sponsor's own product
  surface (`app.meshapi.ai` model catalog) without literally depending on it.
- **Judge stand-out.** Grocery/quick-commerce is the single most common hackathon domain; a
  flawless implementation still risks blending into the field. The model-catalog domain has very
  low collision risk with other teams.
- **Catalog richness for RAG.** Models have naturally comparable, semantically rich attributes
  (provider, modality, price, latency, context window, use-case) that make retrieval and
  comparison narratives *demonstrably* smart rather than superficially plausible — this plays
  directly to the rubric's grounding and agentic-depth criteria.
- **Team preference.** Between the two live options, the evaluation-based narrative was the
  more compelling one to build and pitch.

## 5. Known risk and mitigation

**Risk:** we cannot instrument event tracking on Mesh's own production app
(`app.meshapi.ai`) — only on a catalog we build ourselves. The product is therefore "our own
model-comparison catalog, seeded with realistic AI model data," not literally live behavior on
Mesh's site. This is a framing risk (the pitch must be accurate about what's real), not a
technical blocker.

**Data sourcing plan** (resolves the "catalog richness" feasibility risk above):
1. First, check whether Mesh exposes a models-list endpoint we can query for real, accurate specs.
2. If not available, curate ~100–150 models from public provider sources (OpenAI, Anthropic,
   Google, Mistral, ElevenLabs, Cohere, Stability, etc.), AI-assisted, with a `source_url` kept per
   entry so figures are defensible if a judge who knows the space checks them.

## 6. Final decision

**Domain: AI Model & Tool Catalog and Comparison Platform.**

Concrete shape:
- **Entity:** `model` (replaces the generic `product`) — provider, modality (LLM / voice / image /
  video / embedding / multimodal), price (cost per unit), latency, context window, use-case tags,
  source URL.
- **Personas:** **AI engineer** (primary user, replaces "Learner") evaluates and compares models to
  decide what to integrate; **Curator** (replaces "Admin") manages the model catalog.
- **Behavior events:** `model_view`, `model_compare`, `search`, `click`, `dwell`.
- **Recommendation narrative:** comparison-driven, e.g. *"You've been comparing low-latency voice
  models — here's one you haven't tried, plus how it stacks up on the dimensions you seem to
  care about."*

## 7. Consequences / follow-up

This decision supersedes the learning-platform domain used in the first planning pass. As a direct
follow-up to this record, `README.md`, `AGENTS.md`, and `docs/01` through `docs/07` were updated in
place to use model-catalog terminology (entities, personas, event types, schema fields, wireframes)
instead of courses/learners. The agent architecture, trigger logic, LangGraph node structure, and
FRD/test traceability scheme (module codes, requirement IDs) are unchanged — only the domain nouns
and catalog attributes changed.
