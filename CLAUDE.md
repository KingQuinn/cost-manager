# CLAUDE.md — family expense tracker

This file is project memory. Read it at the start of every session before
writing code. It exists so context isn't lost between sessions — decisions
made here are made, not open questions to re-litigate unless something has
genuinely changed.

## What this project is

A household spends a large, variable amount every week with no visibility
into where the money goes. The fix: a WhatsApp bot family members DM
directly. Send a receipt photo → a vision model extracts and categorizes it
→ the bot confirms in chat or asks a targeted question if unsure → confirmed
entries accumulate → a weekly job reports spend by category so the family
can see where to cut.

**The core bet:** capture is the product. If logging a receipt isn't
near-zero friction, the data never accumulates and nothing downstream
matters. That's why the interface is WhatsApp (already open on everyone's
phone) rather than a new app.

## Where we are

Design phase complete, implementation not started. Two reference documents
exist and should be treated as authoritative:

- **`architecture.md`** — the full architecture and system specification.
  Living document with a changelog. This is the source of truth for
  components, data model, API contracts, error handling, and requirements
  (FR-1 through FR-7). If this file and any code disagree, the code is
  wrong unless the architecture doc's changelog shows the decision changed.
- **Build spec / master prompt** — step-by-step build order, starting with
  validating Gemini extraction against real sample receipts *before*
  building any infrastructure. That validation step has not been run yet —
  do it first if picking this project up cold.

Read `architecture.md` in full before making any structural decision. This
file is a summary and pointer, not a replacement for it.

## Decisions already made — do not re-litigate these

- **1:1 WhatsApp DMs, not group chat.** The Cloud API can't monitor an
  existing personal group chat. Meta's Groups API exists but requires
  Official Business Account verification and caps at 8 members — deferred
  to post-v1. Every family member messages the bot's number directly.
- **No split/shared purchases.** A receipt belongs entirely to whoever sent
  it. Don't add payer/beneficiary logic.
- **No app or dashboard.** WhatsApp is the only interface for v1.
- **Extraction model: Gemini, `gemini-3.5-flash-lite` by default.**
  Benchmark against `gemini-2.5-pro` on real receipts before committing —
  this hasn't been done yet. Don't assume Flash-Lite is confirmed-good
  until that test has run.
- **Categorization is rules-first, LLM-fallback, self-reinforcing.**
  Keyword match against `categories.keywords` in Supabase; only call the
  LLM when nothing matches; whatever the LLM assigns gets appended back
  into the keyword list so the rules table improves over time and LLM
  calls trend down. Don't skip the self-reinforcement step — it's the
  reason this stays cheap.
- **Confidence routing is per line item, not per receipt.** A receipt with
  one unclear item should generate one targeted question, never a
  full-receipt review. Thresholds (`overall_confidence >= 0.75`, each
  `line_item.confidence >= 0.6`) are starting points from the design phase,
  not validated — expect to tune them once real confidence distributions
  exist, and log any change in `architecture.md`'s changelog.
- **Two independent paths, one shared data store.** The webhook handler
  (real-time capture) and the weekly scheduler (reporting) should not
  depend on each other beyond both reading/writing Supabase. Don't couple
  them — that decoupling is what makes each independently testable.
- **`needs_review` receipts are excluded from weekly summaries** until
  resolved. Under-reporting a pending item for a week is the correct
  tradeoff over reporting a guess as fact.

## Tech stack

- Backend: Python (FastAPI) or Node (Express) — pick one, be consistent.
- Extraction + categorization fallback: Gemini API.
- Database + image storage: Supabase (Postgres + Storage).
- Messaging: WhatsApp Business Cloud API, 1:1 only.
- Scheduler: Supabase `pg_cron` preferred over an external scheduler, for
  fewer moving parts.
- Hosting: Render, Railway, or Fly.io for the webhook handler.

## Data model

Full schema lives in `architecture.md` §8.2. Core tables: `families`,
`members`, `categories`, `receipts`, `line_items`, `clarifications`,
`weekly_summaries`. Receipt lifecycle: `pending` → `confirmed` or
`needs_review` → `confirmed` once clarifications resolve.

## Build order

1. Validate extraction — run 5-10 real family receipt photos through the
   Gemini extraction prompt as a throwaway script, before any other code.
   This is the highest-risk assumption in the whole design; nothing else
   should get built around an unvalidated extraction step.
2. Supabase: run the schema, confirm tables + seed categories.
3. WhatsApp Business Cloud API access: Meta Developer account, test number,
   confirm send/receive via curl before writing application code.
4. Webhook handler scaffold, deployed, verify Meta's webhook handshake.
5. Wire in extraction → categorization → Supabase write.
6. Confidence-routing: confirmation replies and clarifying questions, with
   corrections patching specific line items.
7. Onboard real family members, test with real receipts for a few days.
8. Weekly scheduler: aggregate, format, send summary.

Full detail, including acceptance criteria per step, is in `architecture.md`
§3 (functional requirements) and §17 (testing strategy — including the
"golden set" of sample receipts to re-test against whenever the extraction
prompt or model changes).

## Conventions

- Log raw Gemini extraction JSON to `receipts.raw_extraction` — this is the
  debugging trail when a categorization looks wrong later.
- Never let a failed extraction silently drop a receipt. Worst case is
  `needs_review`, never data loss.
- Environment variables and their purposes are listed in `architecture.md`
  §14.1 — keep that table current as new ones get added.
- Any time a real decision changes (model, thresholds, scope), add a row to
  `architecture.md`'s changelog. Don't let this file and that one drift
  apart.

## Explicitly out of scope right now

Don't build these unless the person explicitly asks to bring them into
scope — see `architecture.md` §2 and §20 for the full non-goals and
post-v1 roadmap:

- WhatsApp group chat / Groups API
- Split or shared purchase attribution
- Any dashboard, PWA, or frontend beyond WhatsApp
- Multi-household / multi-tenant support
