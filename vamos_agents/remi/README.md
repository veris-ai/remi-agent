# Remi

Remi is the debt-collection voice agent, the **candidate under test** for the
**VAmoS Debt Collection** benchmark on Veris Bench. It works for Tallmere
Account Services, LLC, places **outbound** calls to consumers in New York
City, and must apply the benchmark's policy against the benchmark's world:

- Policy: [`collections_bench/RULES.md`](https://github.com/veris-ai/utility-bench/blob/main/collections_bench/RULES.md) (veris-ai/utility-bench)
- World: [`collections_bench/WORLD.md`](https://github.com/veris-ai/utility-bench/blob/main/collections_bench/WORLD.md): a Postgres collections database, with the payment gateway inside it (no Stripe: Stripe does not serve debt collection agencies), and a Fineract ledger holding every account's money

Remi reads those documents' rules into its own code (`policy.py`, the gates,
the prompt). It never imports or copies the benchmark's grader.

Framework concepts (`AgentSpec`, call start, gates, clock, vendors, build and
push) are in the root [AGENTS.md](../../AGENTS.md). This file covers Remi only.

## How Remi fills the `AgentSpec`

| `AgentSpec` piece | Remi | File |
|---|---|---|
| `name` | `remi` (images `bench-e2e/remi-<vendor>`) | `agent.py` |
| `call_start` | `callee_first()`: Remi waits for whoever answers to speak first | `agent.py` |
| `screen_pop` | the three connect reads (`get_account`, `get_dialed_phone`, `get_contact_history`) run by Remi itself when the call connects, before any vendor session opens; rendered as "Account on screen" in the instructions | `screen_pop.py` |
| context view | `VERIS_AGENT_CONTEXT` → `CallContext(account_id TAS-NNNNNN, phone_id, call_time in Eastern)`; missing or malformed stops the pod | `context.py` |
| `call_time()` | the context's `call_time` (World frozen at 2027-02-10) | `agent.py` |
| `instructions(clock)` | RULES.md distilled into `prompt/instructions.md`, rendered with the call's date and the agency row | `prompt/` |
| `tools` | 20 tools (below) | `tools/` |
| `gates` | `Gate` ladder NONE → ASSESSED → PROCEEDING → VERIFIED → DEBT_UNLOCKED, plus a collection stop on tools tagged `collection` (collection stopped, or the call transferred to a person) | `gates.py`, `call.py` |
| per-call state | `RemiCall`: connect reads, assessment, verification, outcomes; the call's one `contact_attempts` row, written once when the call ends (`end_call`, or `finalize()` the moment the caller hangs up) | `call.py` |
| world clients | `Database` (psycopg 3, timeouts, whitelisted columns) from `DATABASE_URL`; the creditor's decision engine (`menu.py`: `payment_options`, `request_approval`) and `PaymentGateway`, the client for the World's gateway functions, on that same database; `FineractLedger` (async httpx, timeouts) from `FINERACT_API_BASE` and its credentials, for the money | `db.py`, `payments.py`, `ledger.py` |
| rules | RULES.md's numbers and calendar as pure functions | `policy.py` |

## The opening, enforced

RULES.md fixes the order of a call's opening, and the gates refuse anything
early with a message saying what must happen first:

1. Connect reads: account, dialed phone, contact history, done by the screen
   pop (below) before anyone speaks. The last read returns `call_assessment`:
   proceed, or should-not-call with reasons
   (closed status, revoked or missing artificial-voice consent, phone not valid,
   employer phone, 3-in-7 cap, responded this
   week, time-barred notice not yet 14 days old, outside 8 a.m.–9 p.m. ET).
2. Right party: the consumer's name as the person said it or said yes to (a
   first name is enough; a surname, if said, must match) + two of date of
   birth / ZIP / SSN last four, which may come one at a time. Each
   `verify_right_party` call adds the facts that matched; the call state keeps
   the count. Compared server-side (the model never holds the secrets); three
   wrong answers (a wrong name or fact) lock. Before verification Remi says
   only RULES.md's permitted statements (its name, the business, automated
   assistant, a personal business matter for the consumer, why it must check
   (their privacy), whether now is a good time, the number on file, the
   callback number, that anything in writing goes to the address on file);
   never the debt, an amount, money or "your account". A refusal to verify is
   its own ending (`record_verification_refused` → `refused_verification`), and
   an unverified "stop calling" stops the number, not the account
   (`stop_calling_this_number` → consent revoked, `unverified_stop_request`
   flag, `unverified_stop`). A wrong number marks the phone `wrong_number`
   (`report_wrong_number`); its consent is revoked only if the person also
   asked to stop calling it.
3. Disclosures: recording, mini-Miranda, and when due the time-barred
   disclosure and the alternative-format statement. The successful
   `verify_right_party` result carries them, in order, with their wording
   (`disclosures_due`; the recording notice and mini-Miranda verbatim), and the
   prompt says they come next, conversationally, before anything about the
   account. They are attested as `get_debt_details` arguments (so they appear
   in the trace, in order).
   Calls are English-only: there is no language step, Remi never writes
   `consumers.language_preference`, the prompt holds every word to English
   whatever the callee's name, and each vendor that takes a language setting
   has it pinned to English (AGENTS.md section 10).
4. Only then the debt, offers and payments (`get_payment_options` and every
   money tool need the debt unlocked). A dispute, cease, attorney,
   bankruptcy, death or SCRA claim stops collection for the rest of the call.

## The screen pop (call start)

A collector's dialer shows the account when the call connects; Remi's does
too. `screen_pop.screen_pop(call)` runs the three connect-read handlers (the
tools' own code and SQL) on the call's `RemiCall`, through Remi's database
connection, so:

- the reads are Remi's SELECTs on `accounts`, `phones` and `contact_attempts`
  in the World's SQL log, before any vendor session opens and so before the
  first spoken turn (the grader accepts these or the tool calls);
- the call's account, phone, history and assessment are set, so the gates
  start past the connect reads (verification is next, or the should-not-call
  close);
- the model gets the results as an "Account on screen" section at the end of
  its instructions, identical on every vendor: who is being called (and that
  the agency's callback contact is not that person), the assessment, then each
  read's result exactly as the tool returns it. Those results carry no date of
  birth, ZIP, SSN or street: verification stays server-side in
  `verify_right_party`.

The three read tools stay registered (ungated): the model may re-read, and if
the screen pop ever fails (it is logged and the call goes on without it), the
instructions tell the model to run them and the gates name any read missing.
Remi still decides from what the screen shows (should-not-call, cap reached,
cease on file) and still says nothing it may not.

## Tools

| Tool | Gate | What it does |
|---|---|---|
| `get_account`, `get_dialed_phone`, `get_contact_history` | none | Connect-time reads, already run by the screen pop; kept for re-reads and as the fallback if it failed. No DOB, ZIP, SSN, street, balance or creditor in the result. |
| `verify_right_party` | proceeding | Consumer: the name they said or confirmed (first name enough) + 2 facts, incrementally; returns the state (`name_confirmed`, `facts_confirmed`, `still_needed`, `attempts_left`), never which fact failed. `estate_rep`: full name only (deceased consumer). On success, `disclosures_due` lists the disclosures owed next, in order, with their wording. |
| `record_verification_refused` | proceeding | The person declined to verify: no disclosure, and the call's disposition is `refused_verification` (`answered_by` null), not `failed_verification`. |
| `record_flag` | proceeding (consumer only, except `deceased`) | cease, channel_optout, attorney, bankruptcy, deceased, scra, hardship, original_creditor_request: flag, letter, status and consent changes in one transaction. A cease or opt-out from an unverified person is refused and pointed at `stop_calling_this_number`. Fields must be what the person said: placeholders ("unknown", "the caller") are refused. `deceased`: the reporter's name and relationship; `died_on` only as they gave it (refused if today or later, or before birth), otherwise recorded without a date, and recording it again later completes the flag and `deceased_on`. `scra`: the branch is required, and `log_contact_attempt` / `end_call` are refused until `transfer_to_human` (RULES.md: record `scra` and transfer). |
| `report_wrong_number` | proceeding (unverified only) | The number is not the consumer's: phone `status` `wrong_number` at once; consent revoked only with `asked_to_stop_calling` (the person asked to stop, not merely "wrong number"). Disposition `wrong_number`; verification is refused afterwards. |
| `stop_calling_this_number` | proceeding (unverified only) | Revokes consent on the dialed phone and records an `unverified_stop_request` flag with its `phone_id` (disposition `unverified_stop`); after `report_wrong_number` it only revokes the consent (no flag). Never touches the account's status; verification is refused afterwards. |
| `log_dispute` | verified | dispute (including `insurance`) or medical financial assistance; collection stops. |
| `get_debt_details` | verified | Needs the disclosure attestations; returns creditor, last four, balance and itemization (no offer limits: what may be offered is `get_payment_options`), every amount in dollars (`"$1,430.00"`). The balance and itemization are the ledger's, read now (the account's Fineract loan and its `tas_itemization` row). |
| `get_payment_options` | debt unlocked, `collection` | The creditor's menu (below), as the World's decision engine answers it now: the ordered ladder in dollars with each `option_id`, plan terms, a settlement's `pay_by`, and a `status` on any option the rules still lock (no settlement allowed, hardship not recorded on this call, approval needed or denied); and `rules`, the creditor policy's own plain-English rules (what to offer first, what to ask, what unlocks each option), passed through verbatim from the World, never reworded, filtered or added to. Never a percentage or a limit. A menu without a non-empty list of rules is malformed and refused. Refused (and logged) if the menu's balance is not the ledger's. |
| `request_approval` | debt unlocked, `collection` | `option_id` of an approval tier: asks the World (`request_approval`, which logs the answer); only after the hardship is recorded when the creditor requires one; a decision is kept for the call (a denied tier is never chargeable). |
| `add_payment_method` | debt unlocked, `collection` | Card or ACH to `gateway_tokenize_card` / `gateway_tokenize_ach`; returns `method_id` and last four; every card/bank field redacted. |
| `take_payment` | debt unlocked, `collection` | `method_id` + `option_id`, no amount: `pif` (the ledger's balance, no menu read), a settlement option, or a plan option's down payment; anything the menu does not list, or locks, is refused before the gateway. Then `gateway_charge` on a method saved this call. One charge per call: reserved before the first await (a concurrent duplicate is refused), kept once the gateway approves (posted or not), released only on a decline or a charge that never happened. Approved: the charge is posted to the account's loan (a repayment carrying the gateway's `payment_id`, so a retry posts once) and a settlement's rest is written off and its `settlement_lump` agreement recorded (a World refusal of that record is logged and returned as `settlement_record`, the charge itself being done); no `accounts` row changes. The result states that the consumer has not heard the outcome yet (`consumer_has_heard: false`), and, if the ledger could not be reached after two tries, that the payment was taken but not posted (logged). Declined: the decline code and reason go to the model, the method is not charged again, and it falls back to another card, ACH or a promise. |
| `create_arrangement` | debt unlocked, `collection` | A plan `option_id` + `method_id` + `first_due_on` (within 30 days): the loan is re-aged to the option's months (`reAge`) and the agreement is recorded with the ledger's own schedule (equal to the cent, the last instalment absorbing the difference). If that schedule is not the option's monthly amount, or the agreement cannot be recorded, the re-age is undone. A plan with a down payment needs its `take_payment` first, and the balance after it. Charged to a `method_id` saved this call. |
| `record_promise` | debt unlocked, `collection` | `pif` or a settlement `option_id` (the amount is the option's), on an exact date ≤14 days out and by a settlement's `pay_by`; the same menu checks as `take_payment`. |
| `schedule_callback` | verified | 8 a.m.–<9 p.m. ET. |
| `queue_letter` | verified | Idempotent per call. |
| `transfer_to_human` | proceeding | On request, or when nothing on the creditor's menu fits (including "can't pay" with no settlement allowed), or SCRA. Disposition `transferred` (by WORLD.md's precedence); the row's summary always carries "Transferred to a person.", the form the bench reads a transfer from when a candidate has no trace. |
| `log_contact_attempt` | assessed | Before the goodbye: who answered and a plain summary (again to replace it). The row is written when the call ends, from everything recorded by then: time, duration and disposition from the session and the clock, and a summary that leads with the facts the session recorded (disposition, who answered, whether the debt was read and after which attested disclosures, outcomes, a transfer), then the model's notes. `answered_by` is what the call established: the verified party, `wrong_number` after `report_wrong_number`, `third_party` for an unverified person who reported a death, null after a refusal or an unverified stop. |
| `end_call` | assessed | Requires the log; writes the contact row, then the line closes after the last words play (`json` framing: `{"type": "end"}` first, so the bench records a normal end, not a disconnect). |

### The contact row on every ending

Every call leaves exactly one `contact_attempts` row, written once, under a lock:

- the agent ends it: `end_call` writes the row before the line closes;
- the caller hangs up or the socket drops first: the framework's
  `CallSession.caller_gone` finalizes at once (not after the vendor's own
  teardown, which can take seconds), after any tool call already running, and
  refuses new ones; `RemiCall.finalize` writes the row from the model's log, or
  from the session alone if it never logged;
- the provider fails, or the pod is stopped mid-call: the server finalizes the
  call before it closes Remi's database.

Why: in trial `trial_vj4i37mlw6q9lqc58lprk`, 11 of 212 attempts had no row.
In each the caller hung up before the model logged; the fallback row was
written 1.1 s (Mistral) to 2.1 s (OpenAI Realtime, Grok, GPT-Live) later,
because finalize waited for the provider to close its vendor socket, and the
bench drains the World's log one to two seconds after the hang-up (the same
delay was kept on most calls and lost on 11). Providers that returned at once
(Gemini Live, Deepgram, Pipecat: 10 to 90 ms) never lost one.
The log now comes before the goodbye too, so the row carries the model's
`answered_by` (a third party, a voicemail) even when the caller hangs up on the
goodbye.

## How Remi talks (the prompt) and what its tools say back

The prompt (`prompt/instructions.md`) is one text for every vendor. It gives
the call as stages with a goal, limits and a couple of example phrasings each,
not fixed lines: greet (use a name the callee already gave, never re-ask it;
otherwise ask for the consumer by first name), ask whether now is a good time,
say why the check is needed (privacy), verify one fact at a time, offer a way
out (callback number answered by a person, or a letter) to a wary person by
their second push-back, then the disclosures in short turns and the debt.
Push-back questions get a reason and a way out. A short "how you talk"
section bans narration, stock fillers and re-asking. The legally worded
recording notice and mini-Miranda stay quoted.

Tool results and gate refusals describe state and rules ("allowed_now"),
never words to say: the model reads hints aloud verbatim, so a hint that is a
sentence becomes a scripted line (`tests/test_tools.py` scans the tool sources
for speech scripts). The one exception is `disclosures_due`, whose sentences
RULES.md grades word for word. No amount goes into a money tool (an
`option_id` carries it); results carry dollars as strings (`"$250.00"`). The World's
rows and the gateway calls stay in cents, and the ledger's wire takes dollars
(converted in `ledger.py` only).

## The creditor's menu

Remi works like a real AI collector: the LLM talks; code and the creditor's
rules decide money and compliance. The World's decision engine (two SQL
functions in the collections database, `menu.py` the client) answers
`payment_options(account_id)`: the creditor's ordered ladder for the account
(pay in full, plans, then any settlement tiers, some needing a supervisor's
approval, and whether settlement is allowed at all or only after a hardship)
plus the policy's `rules` in plain English, and
`request_approval(account_id, option_id)` (approved only with a hardship on the
account; logged). The prompt holds no creditor's rules: it tells the model to
follow the rules `get_payment_options` returns (the World's text, verbatim),
offer only the menu's amounts, and hand anything outside it to a person. Code enforces the
same: the money tools take an `option_id`, never an amount
(`menu.checked_option`), and the World refuses off-menu money itself: inside
`gateway_charge` before any payment row, and in a trigger on `arrangements`
(SQLSTATE 23514, `policy: ...`, raised as `menu.PolicyRefused`, logged, never
swallowed). The menu is priced from the balance at the start of the call and
is not repriced after a payment, so Remi compares it with the ledger only
before the call's first charge.
Remi needs the hardship heard on this call before a settlement that requires
one, even if one is on file from an earlier call.

## Payments

The World's payment gateway is three SQL functions in the collections database
(WORLD.md, "The payment gateway"); `payments.PaymentGateway` is Remi's one client
for them. The gateway decides approve or decline and writes `payments`; it does
not know the balance. The money is the ledger's (below).

- Remi never writes `payment_methods` or `payments` (they are not in `db.WRITABLE`),
  never reads or writes the processor's `processor_vault` or `processor_*`
  functions, and runs no DDL. `Transaction.call_gateway` calls only the three
  `gateway_*` functions, with named, bound arguments.
- Every `at` is the call's clock (`CallClock.now()`), never the database's `now()`.
- A gateway error (SQLSTATE 22023, `gateway: ...`) becomes a tool error in the
  gateway's words ("The payment gateway refused it: the card number is not
  valid ..."); nothing is written. A decline is not an error: `take_payment`
  returns `status: declined` with `decline_code` (`insufficient_funds`,
  `card_declined`, `expired_card`) and a reason to relay.
- Card number, expiry, security code and bank account number pass only into the
  gateway call; they are redacted in logs and never stored or returned.

## The ledger

Each placed account's money is a Fineract loan in the World (WORLD.md, "Money: the
Fineract ledger"): `externalId` = the account id, principal = the placed balance,
the itemization in its `tas_itemization` datatable row, payments as repayments,
and the balance its `totalOutstanding`. `ledger.FineractLedger` is Remi's one
client for it (`ledger.Ledger` is the protocol; the tests use an in-memory one
that keeps the measured refusals, `tests/fake_ledger.py`).

- Read when the debt is (`get_debt_details`), and again before each money tool,
  so every amount is checked against the balance as the ledger holds it. The
  screen pop and every call that never reaches the debt make no ledger request.
- An approved charge is posted with `transactionDate` = the call's date (the
  ledger's business date is the World's day and it refuses a later one),
  `externalId` = the gateway's `payment_id`, `receiptNumber` = its
  `processor_ref`, and the Card or ACH payment type. A settlement's rest is then
  written off. Paid and settled are the loan's state: Remi writes neither as an
  account status.
- A plan re-ages the loan; the agreement's instalments are the schedule the
  ledger computed. Fineract takes one re-age a day per loan.
- A refusal (`LedgerRefused`, Fineract's own code) is a tool error the model can
  act on; an unreachable ledger (`LedgerUnavailable`) is never hidden: reading the
  debt answers that the balance cannot be read, and posting an approved charge
  says the payment was taken but not posted, and logs it.

## Config

| Variable | Set by | Meaning |
|---|---|---|
| `VERIS_AGENT_CONTEXT` | bench, per attempt | `{"account_id","call_time","phone_id"}`. Required. |
| `DATABASE_URL` | bench (world) | Collections twin DSN, the payment gateway included. Required. |
| `FINERACT_API_BASE` | bench (world) | The Fineract twin's host; the API root `/fineract-provider/api/v1` is beneath it. Required. |
| `FINERACT_USER`, `FINERACT_PASSWORD`, `FINERACT_TENANT` | bench candidate-env | Fineract's Basic pair and tenant header (the World's published `mifos`/`password`/`default`). Required; no defaults in code. |

Plus the framework's and the vendor's variables (AGENTS.md section 11).

## On the managed voice agents (Deepgram, ElevenLabs, Fluxions)

These vendors run STT, the LLM and TTS on their side; Remi's tools come back
over the same socket as client-side calls, so every call still goes through
Remi's gates. What differs for Remi (AGENTS.md section 10 has the protocol
details):

| Vendor | Bench candidate | Remi-specific notes |
|---|---|---|
| `deepgram` (`remi-deepgram`) | `trace_adapter: deepgram-voice-agent`; candidate-env `DEEPGRAM_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json` (+ optional `VAMOS_DEEPGRAM_LLM_MODEL`, default `gpt-4.1-mini`) | No greeting in `Settings`: Remi stays silent until the callee's first words. Checked against the live API: no agent audio before "Hello?", tool calls round-trip, `end_call` hangs up after the goodbye. |
| `elevenlabs` (`remi-elevenlabs`) | `trace_adapter: elevenlabs-convai`; candidate-env `ELEVENLABS_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json` (+ optional `VAMOS_ELEVENLABS_LLM`, default `gpt-4.1-mini`) | A stored agent is created per call with Remi's instructions (screen pop included) and deleted after it: about 2 s between `/voice` connecting and the vendor hearing the callee (their "Hello?" is buffered, not lost). Parameters Remi leaves undescribed get their own name as description (ElevenLabs requires one). Checked against the live API, agents cleaned up. |
| `fluxions` (`remi-fluxions`) | `trace_adapter: fluxions-realtime`; candidate-env `FLUXIONS_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json`; no `public_endpoint` | **Remi cannot run on `api.fluxions.ai` today:** its instructions are ~13,400 characters with the screen pop and that host keeps 4,000 of the soul (silently). The provider refuses such a call before connecting rather than run Remi on a third of its policy. It needs a Fluxions host with a larger `max_soul_chars` (`VAMOS_FLUXIONS_BASE_URL`). The protocol itself was checked live with a cut prompt (callee first, tool calls, paced playback). |

## Vendors

Remi runs on every vendor in `vamos_voice/providers/` (image
`bench-e2e/remi-<vendor>`); the candidate settings are in AGENTS.md section 13.
On the hosted orchestrators and LiveKit:

- **Vapi**: the candidate needs `"public_endpoint": true` (Vapi calls the tools
  back on the pod). Callee first is Vapi's `assistant-waits-for-user`.
- **Cartesia**: tools run over the agent's own socket (client tools), so no
  public endpoint. Cartesia reserves the name `end_call` for its system tool,
  so Remi's `end_call` is declared as `end_call_` there (same description) and
  mapped back; its gate (the call must be logged first) is unchanged. Cartesia's
  own system `end_call` would hang up inside Cartesia, skipping that gate. Cartesia
  accepts only `type`, `description`, `enum` and `items` in a parameter schema,
  so `format`/`minimum`/`maximum`/`minLength` are cut; Remi's descriptions state
  them in words (YYYY-MM-DD, 1-12, ISO-8601) and its argument validation still
  refuses an out-of-range value. Tool results go unchanged, as on every vendor:
  Cartesia documents a 4096-byte result cap, but it was tested live on 2026-10-14
  and not enforced up to 9 KB (a fact at the very end of a 9 KB result reached the
  model). Remi's largest result, `get_payment_options` on a long plan ladder, is
  ~5.4 KB.
- **LiveKit**: the image runs its own `livekit-server`; nothing extra on the bench.

## Tests

```bash
uv run --all-extras --no-extra livekit pytest -q vamos_agents/remi
```

- `tests/test_tools.py`: every flow against a real Postgres loaded with the
  World's own SQL (`docker run postgres:16-alpine`, or `REMI_TEST_DATABASE_URL`),
  asserting the exact rows the answer key expects (REMI-001 diffed against the
  whole World); declines and their fallbacks; card data in no row, log or
  result; the wall-clock guard over every Remi module.
- `tests/fixtures/collections_world.sql`: the World's schema, payment gateway and
  rows, vendored from utility-bench (the header names the commit). Resync with
  `tests/fixtures/resync_world.sh <utility-bench checkout> [ref]`. Remi never
  imports utility-bench. The tests seed as the owner and connect Remi as a login
  that owns nothing, so the gateway's write guards bind.
- `tests/test_payment_options.py`: the creditor's menu against the same Postgres: the ladder the model
  sees, off-menu and locked options refused before any charge, approvals (denied is never chargeable),
  the World's refusal surfaced, plans on the ledger's schedule (and a down payment), the transfer and
  one-charge guards, the disclosures on verification, the prompt routing each intent to its tool.
- The decision engine is the World's own SQL (`collections_bench/world/creditor_policy.sql`, vendored
  into `collections_world.sql` by `resync_world.sh`); `seed_world` gives the account
  `support.TEST_POLICY` (overridable per test) priced from the ledger's balance.
- `tests/test_context_and_policy.py`: context parsing, the clock, RULES.md numbers.
- `tests/test_payments.py`: `PaymentGateway` against the real gateway functions: approvals, each test instrument's decline, validation errors in the gateway's words, no account change, no direct writes.
- `tests/fixtures/collections_ledger.json`: every account's money from the same utility-bench commit, loaded into the in-memory ledger by `seed_full_world` (`resync_world.sh` writes both fixtures).
- `tests/test_ledger.py`: `FineractLedger` on the wire (`httpx.MockTransport`): the requests it sends, the debt it reads, idempotent posting, refusals vs an unreachable ledger.
- `tests/test_ledger_live.py`: the same client against a real Fineract seeded as the World (opt-in: `REMI_TEST_FINERACT_URL`).
- `tests/test_tools.py` also runs the screen pop against that Postgres: reads done, gates past them, no secrets in the section, the should-not-call case.
- `tests/test_smoke.py`: the real server running Remi over `/voice` with a fake provider (the screen pop has run before the provider starts); startup config errors.

Remi also runs in the root parity matrix (`tests/test_parity.py`) on every vendor.

## Vendors

One image per vendor, `bench-e2e/remi-<vendor>` (AGENTS.md section 13 has each
candidate's `trace_adapter`, candidate-env secrets and `image.env`). Every
vendor gets the same instructions (with the screen pop), the same tools and
the same gates; only the voice stack differs.

| Vendor | Stack | Turn taking |
|---|---|---|
| `mistral` | Voxtral Realtime STT → `mistral-medium-2604` → Voxtral TTS, cascaded in the pod | Silero + 0.8 s of silence, then a Voxtral flush; barge-in after 0.5 s of Silero speech |
| `huggingface` | Whisper large-v3 → `openai/gpt-oss-120b:groq` → Kokoro (fal-ai), all on HF inference | Silero + 0.8 s of silence; barge-in after 0.5 s of Silero speech |
| `grok_cascade` | xAI STT → `gpt-4.1-mini` (or `grok-4.3`, `VAMOS_GROK_CASCADE_LLM`) → xAI TTS | xAI's default endpointing (400 ms); barge-in after 0.5 s of Silero speech |
| `gradium` | gradbot: Gradium STT → `gpt-4.1-mini` → Gradium TTS | gradbot's own |

On the cascades Remi's first words wait for the callee's first finished turn
(callee first), and on gradbot for its first transcribed words.

### Full setup disclosure

What differs between the vendors, and who set it. Everything not listed is
identical: the instructions (with the screen pop), the tools, their gates and
the call-start rule. "Vendor" means the vendor's (or its framework's) own
default, untouched; a value is ours only where the vendor has none or demands
one, and then it is the same for every vendor that needs it:

- **Tool rounds**: `MAX_TOOL_ROUNDS` = 10 LLM rounds per caller turn, wherever
  this pod runs the tool loop (far above a normal turn; it only stops a model
  that loops).
- **Retries**: `RETRY_BUDGET_S` = ~25 s of backoff on a transient error (429,
  5xx, a dropped connection) for every vendor request this pod makes itself
  (`openai` SDK: 6 retries; Mistral SDK: 25 s; LiveKit: 12 x 2 s; Hugging Face
  STT/TTS: 25 s). Where the vendor makes the request (speech-to-speech
  sessions, managed agents, hosted orchestrators), the vendor's own policy
  applies; a speech-to-speech socket that fails ends the call (no reconnect).
- **Tokens**: no cap anywhere but Vapi, which requires one (default 250, which
  cuts turns short): its maximum, 10,000.

| Vendor | Text model | Turn taking, VAD, endpointing | Tool-round cap | Token cap | Retries (LLM) | English pinned by |
|---|---|---|---|---|---|---|
| `openai_realtime` | `gpt-realtime` (speech-to-speech) | vendor: `server_vad`, no numbers sent | vendor | vendor (none) | none: one socket | transcription `language: "en"` |
| `grok_voice` | `grok-voice-think-fast-2.0` (speech-to-speech) | vendor: `server_vad`, no numbers sent | vendor | vendor (none) | none: one socket | transcription `language_hint: "en"` (no spoken-language setting) |
| `gemini_live` | `gemini-2.5-flash-native-audio-preview-12-2025` (default) or a Gemini 3 Live model (`VAMOS_GEMINI_MODEL`), speech-to-speech | vendor: automatic activity detection on its defaults | vendor | vendor (none) | none: one socket | transcription `languageCodes: ["en"]` (native audio takes no spoken-language code) |
| `gpt_live` | `gpt-live-1`, delegating to `gpt-5.6-terra` | vendor (no VAD settings exist) | vendor | vendor (none) | none: one socket | none documented: the prompt |
| `pipecat` | `gpt-4.1-mini` | vendor (Pipecat): Silero `VADParams()`, default start strategies, Smart Turn v3 | none (Pipecat's loop) | none | ours: 6 SDK retries (~25 s) | Deepgram/OpenAI STT and Cartesia TTS `Language.EN` |
| `deepgram` | `gpt-4.1-mini` (Deepgram-managed) | vendor | vendor | vendor | vendor | `language: "en"` |
| `elevenlabs` | `gpt-4.1-mini` (ElevenLabs-managed) | vendor (learned turn model) | vendor | vendor | vendor | `language: "en"` |
| `fluxions` | the host's model | vendor (adaptive end-of-utterance) | vendor | vendor | vendor | none documented: the prompt |
| `mistral` | `mistral-medium-2604` (Mistral's own) | ours, none at the vendor: Silero + 800 ms silence; barge-in after 0.5 s of speech (LiveKit Agents' default) | 10 | none | ours: Mistral SDK backoff, 25 s | none documented (Voxtral): the prompt |
| `huggingface` | `openai/gpt-oss-120b:groq` (HF router) | ours, none at the vendor: Silero + 800 ms silence; barge-in after 0.5 s of speech | 10 | none | ours: 6 SDK retries (~25 s) | Whisper `language: "en"` |
| `grok_cascade` | `gpt-4.1-mini` (default) or `grok-4.3` | vendor: xAI STT default endpointing (400 ms); barge-in ours (0.5 s, as above) | 10 | none | ours: 6 SDK retries (~25 s) | xAI STT/TTS `language=en` |
| `gradium` | `gpt-4.1-mini` | vendor (gradbot) | vendor (gradbot) | vendor (gradbot) | vendor (gradbot) | `Lang.En` |
| `vapi` | `gpt-4.1-mini` (Vapi-managed) | vendor (no `startSpeakingPlan`) | vendor | 10,000 (Vapi's maximum; it requires one) | vendor | transcriber `language: "en"` |
| `cartesia` | `gpt-4.1-mini` (Cartesia-managed) | vendor | vendor | vendor | vendor | `language.primary: "en"` |
| `livekit` | `gpt-4.1-mini` | vendor (LiveKit Agents): Silero and endpointing defaults | 10 | none | ours: 12 x 2 s (~22 s), STT/LLM/TTS | Deepgram STT `en-US` (the TTS model is English-only) |

The text model is the vendor's own where the vendor sells one (Mistral, the
Hugging Face open model, the speech-to-speech models); every pipeline that
takes a bring-your-own model runs `gpt-4.1-mini`.
