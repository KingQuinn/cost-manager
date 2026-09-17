# Family expense tracker — architecture and system specification

**Document status:** Living document — update in place as decisions change
**Version:** 1.3
**Owner:** Khadijah
**Last updated:** 2026-09-12

## Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-20 | Initial architecture document |
| 1.1 | 2026-08-20 | Reframed as an architecture + system specification: added functional requirements, expanded non-functional requirements, endpoint spec, testing strategy, configuration reference, glossary |
| 1.2 | 2026-09-12 | Webhook handler now parses `value.statuses` (delivery status events), not just `value.messages`, and verifies `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set. Codebase reorganized into a modular `app/` package (routers, services, db layer) — no behavior change to FR-1–FR-7. |
| 1.3 | 2026-09-12 | Extraction model confirmed: `gemini-3.5-flash-lite` validated against 6 real receipts (build order step 1) — 0.90–0.99 confidence, correct totals every time, zero failures. Planned benchmark against `gemini-2.5-pro` dropped: that model is retired, and its replacement `gemini-3.1-pro-preview` needs paid billing the project doesn't have. Committing to Flash-Lite without the cross-model comparison. |

*Add a row here every time a real decision changes — model swap, schema change, scope change. This document is only useful if it stays truthful.*

## 1. Purpose

A household spends significant, variable amounts weekly with no visibility
into where the money goes — not from lack of discipline, but from lack of
capture. This document specifies the architecture and system behavior for a
WhatsApp-based system that captures receipts with near-zero friction,
extracts and categorizes their contents automatically, and reports spending
patterns back to the family on a weekly cadence.

## 2. Goals and non-goals

### Goals (v1)

- Any family member can log a purchase by sending a receipt photo to a
  WhatsApp number — no app install, no manual data entry.
- Extraction and categorization happen automatically, with human-in-the-loop
  correction only when the system is genuinely uncertain.
- A weekly summary shows spend by category so the family can identify where
  to cut costs.
- The system is cheap enough to run indefinitely for personal use (single
  household, low message volume).

### Non-goals (v1)

Explicitly deferred, not accidentally omitted:

- **Group chat support.** The WhatsApp Cloud API doesn't support a bot
  passively monitoring an existing personal group chat. Meta's Groups API
  would allow a business-created group, but requires Official Business
  Account verification and caps at 8 participants — more setup overhead than
  v1 warrants. Every member DMs the bot 1:1 instead.
- **Split/shared purchase attribution.** A receipt is attributed entirely to
  its sender. No payer/beneficiary distinction.
- **A dashboard or app.** WhatsApp is the only interface. No frontend to
  build or maintain in v1.
- **Multi-family / multi-tenant support.** The schema has a `families` table
  for future-proofing, but v1 runs for a single household.

## 3. Functional requirements

Each requirement includes acceptance criteria — the thing to check against
when it's actually built.

**FR-1: Receipt capture**
A family member sends a photo to the bot's WhatsApp number.
- *Accept:* an image message from a known or unknown number triggers the
  capture flow within seconds.
- *Accept:* a non-image message from a known number is never mistaken for a
  receipt.

**FR-2: Onboarding**
A first-time sender is identified and registered before their receipt is
processed.
- *Accept:* an unrecognized phone number is asked for a name before any
  receipt logic runs.
- *Accept:* every subsequent message from that number is attributed
  automatically, with no repeated onboarding.

**FR-3: Extraction**
The system extracts vendor, date, line items, and total from a receipt image.
- *Accept:* output conforms to the JSON schema in §9.2, every time — no
  free-text, no partial JSON.
- *Accept:* a non-receipt image is detected and reported, not
  hallucinated into fake line items.

**FR-4: Categorization**
Each line item is assigned exactly one category.
- *Accept:* keyword-matched items never call the LLM.
- *Accept:* unmatched items get an LLM-assigned category, and that
  assignment's words are persisted back into the keyword table.

**FR-5: Confidence-based routing**
High-confidence receipts are confirmed automatically; low-confidence items
prompt a targeted follow-up.
- *Accept:* a receipt with one unclear item generates exactly one
  clarifying question, not a full-receipt review.
- *Accept:* a clarifying question names the specific item and vendor, never
  a generic "please check your receipt."

**FR-6: Correction**
A family member can correct a stored entry via a chat reply.
- *Accept:* a correction reply patches the specific `line_items` row it
  refers to, without requiring the receipt to be resent.

**FR-7: Weekly reporting**
Confirmed spend is aggregated and reported on a schedule.
- *Accept:* only `confirmed` receipts are included — `needs_review`
  receipts are excluded until resolved.
- *Accept:* every family member receives the summary, not just the sender
  of the largest receipt.

## 4. Non-functional requirements

- **Latency:** confirmation or clarifying question should arrive within a
  few seconds of a receipt being sent — slow turnaround kills the habit
  this system depends on.
- **Cost:** should run for well under $5/month at expected household volume
  (see §15).
- **Reliability:** a failed extraction should never silently drop a
  receipt. Worst case is "flagged for review," never "lost."
- **Maintainability:** the categorizer should require decreasing manual
  intervention over time (see the self-reinforcing keyword design in §11),
  not accumulate technical debt with every new item type.
- **Availability:** no formal SLA for a personal project, but the webhook
  handler should recover automatically from a restart without losing
  in-flight state, since all state lives in Supabase, not server memory.

## 5. System context

**Actors:**
- Family members (senders) — interact only via WhatsApp.
- The system owner (Khadijah) — deploys, monitors, and maintains the backend.

**External systems:**
- WhatsApp Business Cloud API (Meta) — message transport, both directions.
- Gemini API (Google) — vision-based receipt extraction, and LLM fallback
  for categorization.
- Supabase — Postgres database and object storage for receipt images.

**What's inside our system boundary:** only the webhook handler and the
weekly scheduler. Everything else is a managed third-party service.

## 6. High-level architecture

The system is two independent paths sharing one data store. Each is
separately deployable and testable — the capture path has no knowledge of
the reporting path, and vice versa.

**Path 1 — capture (event-driven, per message):**

```
Family member
  -> WhatsApp Business Cloud API
  -> Webhook handler
       -> Gemini Vision API (extract + fallback categorize)
       -> Supabase (write)
       -> WhatsApp Business API (reply: confirmation or clarifying question)
```

**Path 2 — reporting (scheduled, weekly):**

```
Weekly scheduler
  -> Supabase (read confirmed entries)
  -> aggregate by category / member / week
  -> WhatsApp Business API (send summary to all members)
```

Supabase is the seam between the two paths. It's intentionally the only
component both paths touch — that's what lets capture logic and reporting
logic be built, tested, and changed independently.

## 7. Component details

### 7.1 WhatsApp Business Cloud API

Managed by Meta. Handles all message transport in both directions. The
system registers a webhook URL with Meta; Meta calls that URL on every
inbound message. Outbound messages (confirmations, clarifying questions,
weekly summaries) go through the same API via authenticated REST calls from
the webhook handler and the scheduler.

Constraint to design around: 1:1 conversations only in v1 (see §2).

### 7.2 Webhook handler

The one piece of custom infrastructure that must be deployed and kept
running. Responsibilities:

- Verify Meta's webhook handshake on setup.
- Receive inbound message events, identify the sender by phone number.
- On an image message: download it, call Gemini for extraction, run the
  categorizer, write results to Supabase, and reply.
- On a text reply to a pending clarification: match it to the open
  `clarifications` row, patch the relevant `line_items` row, mark the
  clarification resolved.
- Handle onboarding: first message from an unrecognized phone number
  triggers a "what's your name?" flow that creates a `members` row.

Stateless between requests — all state lives in Supabase, so the handler
can be restarted or scaled without losing in-flight context.

### 7.3 Gemini Vision API (extraction)

Called once per receipt image. Model choice: `gemini-3.5-flash-lite`,
confirmed — validated on 6 real family receipts (see changelog v1.3):
0.90–0.99 confidence, correct totals/VAT reconciliation on every one, zero
parse failures. A cross-model benchmark against a Pro-tier model was
attempted but dropped: `gemini-2.5-pro` has been retired by Google, and its
replacement (`gemini-3.1-pro-preview`) requires a paid billing tier the
project doesn't have enabled (free-tier quota is 0 for that model). Revisit
if Flash-Lite's accuracy degrades on harder input (faded thermal print,
handwriting) once more real receipts are seen. See the extraction contract
in §9.2.

Also used, with a separate short prompt, as the categorization fallback when
the rules table finds no keyword match (§11).

### 7.4 Categorization engine

Not a separate service — logic embedded in the webhook handler, but
architecturally distinct enough to call out:

1. Rules-based keyword match against `categories.keywords` (Supabase).
2. LLM fallback (Gemini) only on no match.
3. Self-reinforcing: whichever category gets assigned, the item's own words
   are appended back to that category's keyword list, so the rules table
   improves weekly and LLM calls — and their cost — trend down over time.

### 7.5 Supabase (data + storage)

Postgres database (schema in §8) plus object storage for receipt images
(referenced by `receipts.image_url`). Also the natural place for Row Level
Security policies once there's more than one family sharing the instance
(not needed for a single-household v1, but worth setting up correctly from
the start — see §13).

### 7.6 Weekly scheduler

A scheduled job, independent of the webhook handler's request/response
cycle. Options: Supabase `pg_cron` (keeps everything in one place) or an
external scheduler like GitHub Actions or a Render cron job (keeps the
schedule decoupled from the database, easier to change independently).
Reads confirmed entries, aggregates, formats, sends via WhatsApp API.

## 8. Data architecture

### 8.1 Entity overview

- `families` — future-proofing for multi-household support; single row in v1.
- `members` — one row per family member, keyed by WhatsApp phone number.
- `categories` — the fixed category list plus a growing keyword list per
  category.
- `receipts` — one row per receipt sent, with a status field driving the
  confirm/review workflow.
- `line_items` — one row per item extracted from a receipt.
- `clarifications` — open questions the bot has asked and is waiting on.
- `weekly_summaries` — cached output of the reporting job, useful for
  historical comparison ("are we spending more than last month").

### 8.2 Schema

```sql
create table families (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz default now()
);

create table members (
  id uuid primary key default gen_random_uuid(),
  family_id uuid references families(id) on delete cascade,
  name text not null,
  phone_number text unique not null,
  created_at timestamptz default now()
);

create table categories (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  keywords text[] default '{}'
);

create table receipts (
  id uuid primary key default gen_random_uuid(),
  member_id uuid references members(id) on delete cascade,
  vendor text,
  purchase_date date,
  total numeric(12,2),
  image_url text,
  status text not null default 'pending'
    check (status in ('pending', 'confirmed', 'needs_review')),
  raw_extraction jsonb,
  created_at timestamptz default now()
);

create table line_items (
  id uuid primary key default gen_random_uuid(),
  receipt_id uuid references receipts(id) on delete cascade,
  name text not null,
  quantity numeric default 1,
  price numeric(12,2) not null,
  category_id uuid references categories(id),
  confidence numeric(3,2),
  needs_review boolean default false
);

create table clarifications (
  id uuid primary key default gen_random_uuid(),
  receipt_id uuid references receipts(id) on delete cascade,
  line_item_id uuid references line_items(id),
  question text not null,
  resolved boolean default false,
  answer text,
  created_at timestamptz default now()
);

create table weekly_summaries (
  id uuid primary key default gen_random_uuid(),
  family_id uuid references families(id),
  week_start date,
  week_end date,
  summary_text text,
  total_spend numeric(12,2),
  created_at timestamptz default now()
);

create index idx_members_phone on members(phone_number);
create index idx_receipts_member on receipts(member_id, created_at);
create index idx_line_items_receipt on line_items(receipt_id);
create index idx_line_items_category on line_items(category_id);
```

### 8.3 Receipt lifecycle (state machine)

```
pending  --(all line items confident)-->  confirmed
pending  --(any line item unclear)-->     needs_review
needs_review --(clarification resolved, all items now clear)--> confirmed
```

A receipt only appears in weekly aggregation once `status = 'confirmed'`.
`needs_review` receipts are excluded from summaries until resolved — this is
a deliberate choice: better to under-report a pending item for a week than
report a guess as fact.

## 9. API and integration contracts

### 9.1 Webhook handler endpoints

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/webhook` | Meta's webhook verification handshake (echoes a challenge token) | Verify token (query param) |
| `POST` | `/webhook` | Receives inbound WhatsApp message events *and* delivery-status events (sent/delivered/read/failed) | `X-Hub-Signature-256` (checked when `WHATSAPP_APP_SECRET` is set) |
| `POST` | `/cron/weekly-summary` | Manual/scheduled trigger for the reporting job, if not using `pg_cron` | Shared secret header |
| `GET` | `/health` | Liveness check for the hosting platform | None |
| `POST` | `/test/message` | Dev-only: exercises the capture pipeline without a real Meta webhook payload. Mounted only when `ENVIRONMENT=development` | None |

Keep this table current as real routes get added — health checks, admin
endpoints, etc. all belong here once they exist.

### 9.2 Gemini extraction contract

Request: image bytes + the extraction system prompt. Response: strict JSON,
no prose.

```
Schema:
{
  "vendor": string | null,
  "date": string | null,        // YYYY-MM-DD
  "currency": "NGN",
  "total": number | null,
  "line_items": [
    {
      "name": string,
      "quantity": number,
      "unit_price": number | null,
      "total_price": number,
      "confidence": number       // 0.0-1.0
    }
  ],
  "overall_confidence": number,  // 0.0-1.0
  "extraction_notes": string | null
}
```

Full prompt rules (normalization, never inventing items, flagging total
mismatches, handling non-receipt images) are in the build spec companion
document.

### 9.3 Outbound message formats

- **Confirmation** (high confidence): itemized list, category per item,
  total, sent immediately.
- **Clarifying question** (low confidence): names the specific item and
  vendor, asks one targeted question — never a generic "please check your
  receipt."
- **Weekly summary**: total spend, breakdown by category, and ideally a
  flag on the category with the largest week-over-week change, since that's
  the actionable signal the family is looking for.

## 10. Sequence flows

### 10.1 Capture — high confidence

Family member sends photo → webhook receives event → Gemini extracts →
categorizer resolves every item confidently → receipt written as
`confirmed` → confirmation reply sent. End to end, single round trip.

### 10.2 Capture — needs clarification

Same as above until confidence check → one or more line items below
threshold → receipt written as `needs_review`, `clarifications` row(s)
created → targeted question sent → family member replies → webhook matches
reply to open clarification → patches the specific `line_items` row → if no
other open clarifications remain on that receipt, status flips to
`confirmed`.

### 10.3 Reporting

Scheduler wakes on cron schedule → queries `receipts` where
`status = 'confirmed'` and `created_at` within the reporting window → joins
`line_items` and `categories` → aggregates sums per category and per member
→ formats summary text → writes a `weekly_summaries` row (for historical
comparison) → sends to every member via WhatsApp API.

## 11. Confidence and categorization logic

Confidence routing happens **per line item**, not per receipt, so a
9-item receipt with one illegible line only generates one targeted
question rather than punting the whole receipt to review.

```
if overall_confidence >= 0.75 and every line_item.confidence >= 0.6:
    auto-confirm
else:
    flag only the specific low-confidence line item(s) for clarification
```

These thresholds are a starting point, not a fixed spec — expect to tune
them after seeing real extraction confidence distributions across a few
weeks of actual family receipts. Log threshold changes in the changelog.

## 12. Error handling and resilience

| Failure mode | Handling |
|---|---|
| Gemini extraction fails or times out | Retry once; on second failure, mark receipt `needs_review` with a generic "couldn't read this, can you resend or describe it" message. Never drop silently. |
| WhatsApp send fails (rate limit, transient error) | Queue and retry with backoff; log for manual follow-up if retries exhaust. |
| Duplicate receipt (same photo or same purchase sent twice) | Fuzzy-match on vendor + total + date before insert; flag likely duplicates rather than silently double-counting. |
| Non-receipt image sent | Gemini returns `overall_confidence: 0`; bot replies asking the sender to confirm or resend, rather than storing garbage data. |
| Unrecognized sender (not yet onboarded) | Triggers the onboarding flow instead of the receipt flow. |

## 13. Security and privacy considerations

- **Phone numbers are PII.** Store only what's needed (`members.phone_number`
  for attribution), and don't log full numbers in plaintext application
  logs beyond what's needed for debugging.
- **Receipt images may contain incidental financial detail** (partial card
  numbers on some printed receipts). Store images in Supabase Storage with
  access restricted to the service role — never expose a public bucket.
- **Row Level Security**: not strictly required for a single-household v1
  where only the service role touches the database, but worth enabling from
  the start with policies scoped to `family_id` — retrofitting RLS after
  data exists is more error-prone than starting with it.
- **API keys** (Gemini, WhatsApp, Supabase service role) belong in
  environment variables / the hosting platform's secret manager — never
  committed to the repo.

## 14. Deployment architecture

- **Webhook handler**: single deployable service (FastAPI or Express) on
  Render, Railway, or Fly.io. One environment is sufficient for v1 (no
  separate staging needed at household scale) — but keep a local
  `.env.example` so the setup is reproducible if that changes later.
- **Scheduler**: either co-located in the same service (a cron endpoint
  triggered by the hosting platform's scheduler) or fully separate
  (`pg_cron` inside Supabase). Prefer `pg_cron` for v1 — one less moving
  part to deploy and monitor.

### 14.1 Configuration reference

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Auth for extraction and categorization-fallback calls |
| `WHATSAPP_ACCESS_TOKEN` | Auth for outbound WhatsApp API calls |
| `WHATSAPP_VERIFY_TOKEN` | Shared secret for Meta's webhook handshake |
| `WHATSAPP_APP_SECRET` | Meta App Secret used to verify `X-Hub-Signature-256` on inbound webhook POSTs. Signature check is skipped (not enforced) until this is set — required before exposing `/webhook` publicly |
| `ENVIRONMENT` | `development` (default) mounts `/test/message`; any other value omits it |
| `SUPABASE_URL` | Supabase project endpoint |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side Supabase auth (never exposed client-side — irrelevant here since there's no client, but keep the discipline) |
| `CRON_SHARED_SECRET` | Only needed if `/cron/weekly-summary` is triggered externally rather than via `pg_cron` |

## 15. Cost considerations

Rough order-of-magnitude for a single household sending, say, 15-20
receipts/week:

- **Gemini Flash-Lite**: priced per token, receipt images are small;
  expect well under $1/month at this volume. Confirm current pricing before
  committing, since model pricing changes.
- **WhatsApp Business API**: Meta's conversation-based pricing has a free
  tier that likely covers household-scale volume entirely; confirm current
  terms during setup, as WhatsApp pricing structures have changed over time.
- **Supabase**: free tier (500MB database, 1GB storage) comfortably covers
  a single household for a long time.

Net expectation: low single-digit dollars per month, dominated by whichever
service's free tier is smallest — worth rechecking actual pricing pages at
build time rather than relying on this document.

## 16. Observability

Minimum viable logging for a personal project:

- Log raw Gemini extraction JSON per receipt (already stored in
  `receipts.raw_extraction` — useful for debugging misclassifications after
  the fact without needing separate log infrastructure).
- Track, even informally: clarification rate (how often the bot has to ask
  vs. auto-confirm) as the key signal for whether confidence thresholds need
  tuning, and category-fallback rate (how often the rules table misses and
  falls back to the LLM) as the signal for whether the keyword list is
  maturing.

## 17. Testing strategy

- **Unit tests:** rules-based categorizer matching (given a name, does it
  resolve to the right category or correctly fall through to the LLM path);
  confidence-threshold routing logic in isolation from any real API call.
- **Integration tests:** the extraction prompt against a fixed "golden set"
  of 10-15 real sample receipt photos (mix of clean, faded, and
  handwritten) with expected output — re-run this set any time the prompt
  or model changes, and record the result in the changelog.
- **Webhook tests:** simulated WhatsApp payloads (image message, text
  reply, onboarding message) against the handler with Gemini and Supabase
  mocked, to catch routing bugs without spending API credits.
- **Manual QA:** during the first few weeks of real usage, periodically
  review the clarification rate and category-fallback rate (§16) rather
  than assuming the thresholds set in §11 are correct out of the gate.
- Deliberately no automated end-to-end tests against live WhatsApp/Gemini
  in CI — cost and flakiness outweigh the benefit for a personal project;
  the golden-set integration test covers the extraction risk that actually
  matters.

## 18. Non-goals recap

See §2. Worth restating here because architecture documents tend to get
extended scope creep in review — anything not in §2's goals should be
treated as a deliberate v2+ candidate, not something to fold in during
implementation.

## 19. Open risks and assumptions

- **Extraction accuracy on real receipts is unvalidated.** This is the
  single highest-risk assumption in the whole design. It should be resolved
  by the golden-set test in §17 before any other component is built.
- **WhatsApp Business API approval/verification timelines** are outside our
  control and could gate the whole timeline if account verification takes
  longer than expected.
- **Category keyword coverage starts thin.** Early weeks will lean heavily
  on the LLM fallback until the self-reinforcing keyword list matures.

## 20. Future roadmap (explicitly post-v1)

- Migrate to WhatsApp Groups API once Official Business Account
  verification is in place, for a shared group-chat feel.
- Split/shared purchase attribution.
- A lightweight dashboard or PWA once there's enough historical data to
  make visualization worthwhile.
- Multi-household support, using the `families` table already reserved for
  this.

## 21. Glossary

- **Line item** — a single product/charge extracted from one receipt.
- **Confidence score** — a 0.0-1.0 value from Gemini indicating how certain
  the extraction is for a given field or line item.
- **Clarification** — an open question the bot has asked in chat about a
  specific low-confidence line item, awaiting a reply.
- **Rules-based categorizer** — the keyword-matching step that assigns a
  category before falling back to the LLM.
- **Confirmed vs. needs_review** — a receipt's status: `confirmed` means
  every line item is resolved and it's eligible for weekly aggregation;
  `needs_review` means at least one item is still awaiting clarification.
- **Golden set** — a fixed, versioned set of sample receipt images with
  known-correct expected extraction output, used to catch regressions when
  the prompt or model changes.
