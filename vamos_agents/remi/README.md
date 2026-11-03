# Remi

Remi is the debt-collection voice agent, the **candidate under test** for the
**VAmoS Debt Collection** benchmark on Veris Bench. It works for Tallmere
Account Services, LLC, places **outbound** calls to consumers in New York
City, and must apply the law, the agency's policy manual and each creditor's
policy against the benchmark's world:

- Policy: the prompt (`prompt/instructions.md`: the law on debts past the time to sue, and the agency's manual, each rule with its legal source) and, for each account, its creditor's policy, which the World prices for the call (`get_payment_options`)
- World: the benchmark's World v2 (not public): a Postgres collections database, a Fineract ledger holding every account's money, and PayNearMe for payments (no Stripe: Stripe does not serve debt collection agencies)

**Nothing refuses the agent.** The bench measures the agent, so an agent's
mistake must show as the agent's failure: no tool is gated on the opening
order, no tool refuses, blocks or steers an off-policy action, and no result
carries an answer or a verdict ("allowed now", "proceed", "next"). Tools read
facts and record what the agent records; the bench grades what the agent did
after the call, from what the World holds. Remi never imports or copies the
benchmark's grader.

Framework concepts (`AgentSpec`, call start, gates, clock, vendors, build and
push) are in the root [AGENTS.md](../../AGENTS.md). This file covers Remi only.

## How Remi fills the `AgentSpec`

| `AgentSpec` piece | Remi | File |
|---|---|---|
| `name` | `remi` (images `bench-e2e/remi-<vendor>`) | `agent.py` |
| `call_start` | `callee_first()`: Remi waits for whoever answers to speak first | `agent.py` |
| `screen_pop` | the connect-time reads (`get_account`, `get_debt_details`, `get_dialed_phone`, `get_contact_history`) run by Remi itself when the call connects, before any vendor session opens; rendered as "Account on screen" in the instructions | `screen_pop.py` |
| context view | `VERIS_AGENT_CONTEXT` → `CallContext(account_id TAS-NNNNNN, phone_id, call_time in Eastern)`; missing or malformed stops the pod | `context.py` |
| `call_time()` | the context's `call_time` (the World's day is 2026-11-03) | `agent.py` |
| `instructions(clock)` | `prompt/instructions.md`, rendered with the call's date and the agency row | `prompt/` |
| `tools` | 22 tools (below) | `tools/` |
| `gates` | none: Remi's `Gates` refuses nothing | `gates.py` |
| per-call state | `RemiCall`: the reads, what verification established, what the agent recorded; the call's one `contact_attempts` row, written once when the call ends (`end_call`, or `finalize()` the moment the caller hangs up) | `call.py` |
| world clients | `Database` (psycopg 3, timeouts, whitelisted columns) from `DATABASE_URL`, with the World's `creditor_policy` and `request_approval`; `FineractLedger` (async httpx, timeouts) from `FINERACT_API_BASE` and its credentials, for the money; `PayNearMe` (async httpx, signed requests) from `PAYNEARME_API_BASE` and the site's credentials, for payments | `db.py`, `ledger.py`, `payments.py` |
| records | how Remi writes what the agent did: business days, a plan's payments, dollars, one disposition per call | `policy.py` |

## The screen pop (call start)

A collector's dialer shows the account when the call connects; Remi's does
too. `screen_pop.screen_pop(call)` runs the connect-read handlers (the tools'
own code) on the call's `RemiCall`, so:

- the reads are Remi's SELECTs on `accounts`, `phones` and `contact_attempts`
  in the World's SQL log, and its read of the account's Fineract loan, before
  any vendor session opens and so before the first spoken turn;
- the model gets the results as an "Account on screen" section at the end of
  its instructions, identical on every vendor: who is being called (and that
  the agency's callback contact is not that person), then each read's result
  exactly as the tool returns it: the account's status and creditor, the date
  the time to sue ends, the loan's date and the notices' dates, a phone
  account's phone and service parts, a hospital account's treatment date, an
  auto deficiency's car, sale date and price, the balance and its itemization
  since its basis, the dialed phone's consent, and the contact history as
  recorded. No verdict (nothing says whether the call may continue), and no
  date of birth, ZIP, SSN or street: verification stays server-side in
  `verify_right_party`.

A read that fails shows as its error and the rest still show; a screen pop
that fails is logged and the call goes on without it. The read tools stay
registered for re-reads.

## Tools

| Tool | What it does |
|---|---|
| `get_account`, `get_dialed_phone`, `get_contact_history` | Connect-time reads, already run by the screen pop; facts only: the account (status, creditor and store, dates, the time to sue's end and whether it has passed, balance parts, treatment date, car and sale), the dialed phone (consent and when it was revoked, status), the contact history as recorded (times in Eastern). No counts against a limit, no assessment. |
| `get_debt_details` | The debt from the ledger, now: creditor, last four, balance, the itemization from its basis (`charge_off`, `placement`, `sale`) with interest, fees and payments since, the validation notice's date, the time to sue's end and whether it has passed. Every amount in dollars (`"$1,430.00"`). |
| `verify_right_party` | The name the person said or confirmed (a first name is enough) and any of date of birth, ZIP and SSN last four, compared server-side; facts count as they come. Returns whether this answer matched, what is confirmed, how many are needed and how many wrong answers there have been, never which fact failed. `estate_rep`: an executor's full name. Nothing locks. |
| `record_verification_refused` | The person declined to verify (disposition `refused_verification` when nobody verified). |
| `log_dispute` | A dispute in the consumer's own words (`details.words`); the account is marked `disputed` and the letter on asking for proof in writing is queued. |
| `record_flag` | `cease` (status `cease`, confirmation letter), `channel_optout`, `attorney`, `bankruptcy`, `deceased` (status, the date of death on the consumer), `scra`, `hardship` (in the consumer's own words), `original_creditor_request` (returns the name and address on file; a letter when there is no address), `language_preference` (an ISO 639-1 code), `financial_assistance_wanted`, `financial_assistance_declined`. Recorded as the agent passes them, whoever is on the line. |
| `report_wrong_number` | The dialed phone's status `wrong_number`. |
| `stop_calling_this_number` | An `unverified_stop_request` flag naming the dialed number, for review. |
| `revoke_consent` | Revokes the dialed number's artificial-voice consent (a cease or a stop request does not by itself). |
| `get_payment_options` | The World's `creditor_policy(account_id, call_date)`, verbatim: the creditor and how it is named, the balance, the options (`pif`, `payment_plan`, each `discount_<pct>`) with amounts, requirements and due dates, the agency's offer rules and the creditor's rules. Fixed policy priced from the account: no call state. |
| `request_approval` | An option id to the World's supervisor; returns only `{"approved", "reason"}`. |
| `add_payment_method` | Card or bank account to PayNearMe's `create_payment_method` on the account's order; returns the `method_id` and last four; every card and bank field redacted. |
| `take_payment` | `method_id` + `option_id`, no amount: the full balance, a discount, or the plan's down payment, charged on PayNearMe's `make_payment`. Returns PayNearMe's answer as given (`status`, `response_code`, `response_message`). The agent's record of it is the `payments` row; an approved payment is posted to the account's loan keyed by PayNearMe's payment id, and a discount's rest is written off. A refusal (an up-to order's limit, a wrong signature) is PayNearMe's words. |
| `create_arrangement` | `option_id` + `first_due_on` (+ `months` for `payment_plan`): a plan of the months the agent chose over what the plan covers, equal payments rounded down with the remainder last, one in each month, with no payment method (no automatic payments); or a discount or the full balance as one payment on a date. Queues the written confirmation. |
| `record_promise` | The full balance or a discount, on a date, and how they will pay. |
| `schedule_callback` | A callback on the dialed number, recording whether the consumer asked for or agreed to the time. |
| `queue_letter` | Idempotent per call. |
| `transfer_to_human` | Disposition `transferred` (by precedence); the row's summary always carries "Transferred to a person.", the form the bench reads a transfer from when a candidate has no trace. |
| `log_contact_attempt` | Who answered, as the agent says (required: one of the World's `answered_by` values, so a row with no one in `answered_by` and "The call ended before Remi logged it." is a call the agent never logged), and a plain summary; again to replace it. The row is written when the call ends: time, duration and disposition from the call and the clock, and a summary that leads with what the call recorded, then the agent's notes. |
| `end_call` | Writes the contact row (with the agent's log, if it logged), then the line closes after the last words play (`json` framing: `{"type": "end"}` first, so the bench records a normal end, not a disconnect). |

### The contact row on every ending

Every call leaves exactly one `contact_attempts` row, written once, under a lock:

- the agent ends it: `end_call` writes the row before the line closes;
- the caller hangs up or the socket drops first: the framework's
  `CallSession.caller_gone` finalizes at once (not after the vendor's own
  teardown, which can take seconds), after any tool call already running, and
  refuses new ones; `RemiCall.finalize` writes the row from the model's log, or
  from the call alone if it never logged;
- the provider fails, or the pod is stopped mid-call: the server finalizes the
  call before it closes Remi's database.

Why: on an earlier benchmark run, 11 of 212 attempts had no row.
In each the caller hung up before the model logged; the fallback row was
written 1.1 s (Mistral) to 2.1 s (OpenAI Realtime, Grok, GPT-Live) later,
because finalize waited for the provider to close its vendor socket, and the
bench drains the World's log one to two seconds after the hang-up (the same
delay was kept on most calls and lost on 11). Providers that returned at once
(Gemini Live, Deepgram, Pipecat: 10 to 90 ms) never lost one.

## How Remi talks (the prompt) and what its tools say back

The prompt (`prompt/instructions.md`) is one text for every vendor: the law on
debts past the time to sue, quoted where a collector must convey its words,
and the agency's policy manual as rules, each with its legal source, not as
scripts. It holds no creditor's policy: after the required statements the
agent reads it from `get_payment_options` (manual 8.3).

Tool results describe facts, never words to say or what to do next:
`tests/test_payment_options.py` scans the tool sources for "allowed now"
guidance and steering refusals. No amount goes into a money tool (an
`option_id` carries it); results carry dollars as strings (`"$250.00"`). The
World's rows stay in cents, and the ledger's and PayNearMe's wires take dollars.

## The creditor's policy

Each creditor places its accounts under its own policy (World v2:
`creditors.policy_id`, `creditor_policies.document`). The World prices it for
one account with `creditor_policy(account_id, call_date)` and Remi hands it to
the model verbatim (`get_payment_options`): what may be offered (`pif`,
`payment_plan`, each `discount_<pct>`), what each discount requires (a
hardship, the consumer's no to financial assistance, a supervisor's OK), when
it is due, and the creditor's other rules. A supervisor's OK is the World's
`request_approval`, a person in the World who answers only "Approved.", "Not
approved." and the like. Remi enforces none of it: the money tools take an
option id and do what the agent chose, and the bench grades it after the call
from what the World holds.

## Payments

Payments are PayNearMe's (World v2, "Payments: PayNearMe"); `payments.PayNearMe`
is Remi's one client for the vendor's JSON API v3.0 on the World's twin. Each
placed account has one `up-to` order of its placed balance
(`accounts.pnm_order_identifier`).

- Every request is signed: `version` 3.0, the site id, a `timestamp` and an
  HMAC-SHA256 `signature` over the sorted parameters. The twin refuses a
  timestamp more than 300 seconds from its virtual clock, so every `at` is the
  call's clock (`CallClock.now()`), never wall time.
- PayNearMe decides approve or decline; a decline is an answer
  (`status: rejected`, `response_code`, `response_message`), not an error. A
  refusal (its error envelope) is PayNearMe's own words; an unreachable twin is
  never hidden.
- The agent's record of every payment, approved or not, is a `payments` row
  (PayNearMe's payment id and answer). An approved payment is posted to the
  ledger (below).
- Card number, expiry, security code and bank account number pass only into
  PayNearMe's `_pii` parameters; they are redacted in logs and never stored or
  returned. The HMAC secret is in no repr.

## The ledger

Each placed account's money is a Fineract loan in the World (World v2, "Money:
the Fineract ledger"): `externalId` = the account id, principal = the itemized
balance plus interest and fees since, the itemization (`itemized_from`,
`itemized_on`, `itemized_balance`, `interest_since`, `fees_since`) in its
`tas_itemization` datatable row, payments as repayments, and the balance its
`totalOutstanding`. `ledger.FineractLedger` is Remi's one client for it
(`ledger.Ledger` is the protocol; the tests use an in-memory one that keeps the
measured refusals, `tests/fake_ledger.py`).

- An approved payment is posted with `transactionDate` = the call's date (the
  ledger's business date is the World's day and it refuses a later one),
  `externalId` = PayNearMe's payment id (so a retry posts once), and the Card or
  ACH payment type. Once a discount's payment is posted, the rest is written off.
- A plan never re-ages the loan: its schedule is the `arrangements` row only.
- A refusal (`LedgerRefused`, Fineract's own code) or an unreachable ledger
  (`LedgerUnavailable`) after an approved payment is reported
  (`posted_to_ledger: false`) and logged; the payment stands.

## Config

| Variable | Set by | Meaning |
|---|---|---|
| `VERIS_AGENT_CONTEXT` | bench, per attempt | `{"account_id","call_time","phone_id"}`. Required. |
| `DATABASE_URL` | bench (world) | Collections twin DSN. Required. |
| `FINERACT_API_BASE` | bench (world) | The Fineract twin's host; the API root `/fineract-provider/api/v1` is beneath it. Required. |
| `FINERACT_USER`, `FINERACT_PASSWORD`, `FINERACT_TENANT` | bench candidate-env | Fineract's Basic pair and tenant header (the World's published `mifos`/`password`/`default`). Required; no defaults in code. |
| `PAYNEARME_API_BASE` | platform (world) | The PayNearMe twin's base URL; `/json-api` is beneath it. Required. |
| `PAYNEARME_SITE_IDENTIFIER`, `PAYNEARME_HMAC_SECRET` | bench candidate-env | The twin's site id and HMAC secret. Required; no defaults in code. |

Plus the framework's and the vendor's variables (AGENTS.md section 11).

## On the managed voice agents (Deepgram, ElevenLabs, Fluxions)

These vendors run STT, the LLM and TTS on their side; Remi's tools come back
over the same socket as client-side calls, so every call still goes through
Remi's one dispatch path. What differs for Remi (AGENTS.md section 10 has the protocol
details):

| Vendor | Bench candidate | Remi-specific notes |
|---|---|---|
| `deepgram` (`remi-deepgram`) | `trace_adapter: deepgram-voice-agent`; candidate-env `DEEPGRAM_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json` (+ optional `VAMOS_DEEPGRAM_LLM_MODEL`, default `gpt-4.1-mini`) | No greeting in `Settings`: Remi stays silent until the callee's first words. Checked against the live API: no agent audio before "Hello?", tool calls round-trip, `end_call` hangs up after the goodbye. |
| `elevenlabs` (`remi-elevenlabs`) | `trace_adapter: elevenlabs-convai`; candidate-env `ELEVENLABS_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json` (+ optional `VAMOS_ELEVENLABS_LLM`, default `gpt-4.1-mini`) | A stored agent is created per call with Remi's instructions (screen pop included) and deleted after it: about 2 s between `/voice` connecting and the vendor hearing the callee (their "Hello?" is buffered, not lost). Parameters Remi leaves undescribed get their own name as description (ElevenLabs requires one). Checked against the live API, agents cleaned up. |
| `fluxions` (`remi-fluxions`) | `trace_adapter: fluxions-realtime`; candidate-env `FLUXIONS_API_KEY`; `image.env` `VAMOS_VOICE_FRAMING=json`; no `public_endpoint` | **Remi cannot run on `api.fluxions.ai` today:** its instructions are about 21,200 characters with the screen pop and that host keeps 4,000 of the soul (silently). The provider refuses such a call before connecting rather than run Remi on a third of its policy. It needs a Fluxions host with a larger `max_soul_chars` (`VAMOS_FLUXIONS_BASE_URL`). The protocol itself was checked live with a cut prompt (callee first, tool calls, paced playback). |

## Vendors

Remi runs on every vendor in `vamos_voice/providers/` (image
`bench-e2e/remi-<vendor>`); the candidate settings are in AGENTS.md section 13.
On the hosted orchestrators and LiveKit:

- **Vapi**: the candidate needs `"public_endpoint": true` (Vapi calls the tools
  back on the pod). Callee first is Vapi's `assistant-waits-for-user`.
- **Cartesia**: tools run over the agent's own socket (client tools), so no
  public endpoint. Cartesia reserves the name `end_call` for its system tool,
  so Remi's `end_call` is declared as `end_call_` there (same description) and
  mapped back, so it still writes the call's record before the line closes. Cartesia's
  own system `end_call` would hang up inside Cartesia, without that record. Cartesia
  accepts only `type`, `description`, `enum` and `items` in a parameter schema,
  so `format`/`minimum`/`maximum`/`minLength` are cut; Remi's descriptions state
  them in words (YYYY-MM-DD, 1-12, ISO-8601) and its argument validation still
  refuses an out-of-range value. Tool results go unchanged, as on every vendor:
  Cartesia documents a 4096-byte result cap, but it was tested live on 2026-10-14
  and not enforced up to 9 KB (a fact at the very end of a 9 KB result reached the
  model). Remi's largest result, `get_payment_options` (the creditor's policy), is
  about 3.3 KB.
- **LiveKit**: the image runs its own `livekit-server`; nothing extra on the bench.

## Tests

```bash
uv run --all-extras --no-extra livekit pytest -q vamos_agents/remi
```

- `tests/fixtures/`: World v2, vendored from the benchmark repo (the header of
  `collections_world.sql` names the commit): the schema, the creditors' policy
  engine and the rows (`collections_world.sql`), the manifest
  (`world_v2_manifest.json`: the Fineract loans, the PayNearMe orders and test
  instruments, each pilot task's hand-off), and the reviewed policy tool's output
  for the twelve pilot accounts (`payment_options/`). Resync with
  `tests/fixtures/resync_world.sh <benchmark repo checkout> [ref]`. Remi never
  imports the benchmark repo. The tests seed the World as the candidate's own role
  (`app`), as the bench does, and connect Remi as it; PayNearMe's clock starts at
  each task's call time and runs.
- `tests/test_payment_options.py`: `get_payment_options` byte for byte for every
  pilot account; the supervisor's answers; payments on PayNearMe recorded and
  posted, a discount's rest written off; declines and PayNearMe's refusals as
  given; requests signed with the World's clock; card data in no row, log or
  result; plans by the policy's rounding with no payment method and no re-age;
  off-policy money recorded as the agent chose it, not refused; Remi calls only the
  World's `creditor_policy` and `request_approval`;
  the prompt's money rules.
- `tests/test_tools.py`: the reads as facts (no verdict, no secret), the screen
  pop, no tool refused for order or policy, verification that never locks, every
  record World v2 defines (language preference, financial assistance, a cease and
  the number's consent as two records, disputes and hardships in the consumer's
  words), callbacks, the contact row as the agent logged it, the wall-clock guard
  over every Remi module.
- `tests/test_before_verification.py`: the prompt's opening, verification and
  push-back rules, rendered.
- `tests/test_context_and_policy.py`: context parsing, the clock, business days,
  a plan's payments, dispositions.
- `tests/test_payments.py`: `PayNearMe` against a double of the twin's vendor API
  (`tests/fake_paynearme.py`): signing, the time it signs with, answers and
  refusals, an unreachable twin, card data.
- `tests/test_ledger.py`: `FineractLedger` on the wire (`httpx.MockTransport`):
  the requests it sends, the debt it reads, idempotent posting, refusals vs an
  unreachable ledger.
- `tests/test_ledger_live.py`: the same client against a real Fineract seeded as
  World v2 (opt-in: `REMI_TEST_FINERACT_URL`).
- `tests/test_smoke.py`: the real server running Remi over `/voice` with a fake
  provider (the screen pop has run before the provider starts); startup config
  errors.
