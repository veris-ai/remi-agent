# Remi

Remi is the debt-collection voice agent, the **candidate under test** for the
**VAmoS Debt Collection** benchmark on Veris Bench. It works for Tallmere
Account Services, LLC, places **outbound** calls to consumers in New York
City, and must apply the benchmark's policy against the benchmark's world:

- Policy: [`collections_bench/RULES.md`](https://github.com/veris-ai/utility-bench/blob/main/collections_bench/RULES.md) (veris-ai/utility-bench)
- World: [`collections_bench/WORLD.md`](https://github.com/veris-ai/utility-bench/blob/main/collections_bench/WORLD.md): one Postgres collections database, with the payment gateway inside it (no Stripe: Stripe does not serve debt collection agencies)

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
| `tools` | 17 tools (below) | `tools/` |
| `gates` | `Gate` ladder NONE → ASSESSED → PROCEEDING → VERIFIED → DEBT_UNLOCKED, plus a collection stop on tools tagged `collection` | `gates.py`, `call.py` |
| per-call state | `RemiCall`: connect reads, assessment, verification, outcomes; `finalize()` writes the call's one `contact_attempts` row if the model did not | `call.py` |
| world clients | `Database` (psycopg 3, timeouts, whitelisted columns) from `DATABASE_URL`; `PaymentGateway`, the client for the World's gateway functions, on that same database | `db.py`, `payments.py` |
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
2. Right party: full name + two of date of birth / ZIP / SSN last four,
   compared server-side (the model never holds the secrets); 3 failures lock.
   Before verification Remi says only RULES.md's permitted statements (its
   name, the business, automated assistant, a personal matter for the named
   consumer, the number on file, the callback number). A refusal to verify is
   its own ending (`record_verification_refused` → `refused_verification`), and
   an unverified "stop calling" stops the number, not the account
   (`stop_calling_this_number` → consent revoked, `unverified_stop_request`
   flag, `unverified_stop`).
3. Disclosures: recording, mini-Miranda, and when due the time-barred
   disclosure and the alternative-format statement, attested as
   `get_debt_details` arguments (so they appear in the trace, in order).
   Calls are English-only: there is no language step, and Remi never writes
   `consumers.language_preference`.
4. Only then the debt, offers and payments. A dispute, cease, attorney,
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
| `verify_right_party` | proceeding | Consumer (name + 2 factors) or `estate_rep` (name only, deceased consumer). |
| `record_verification_refused` | proceeding | The person declined to verify: no disclosure, and the call's disposition is `refused_verification` (`answered_by` null), not `failed_verification`. |
| `record_flag` | proceeding (consumer only, except `deceased`) | cease, channel_optout, attorney, bankruptcy, deceased, scra, hardship, original_creditor_request: flag, letter, status and consent changes in one transaction. A cease or opt-out from an unverified person is refused and pointed at `stop_calling_this_number`. |
| `stop_calling_this_number` | proceeding (unverified only) | Revokes consent on the dialed phone; records an `unverified_stop_request` flag with its `phone_id` (disposition `unverified_stop`), or for a wrong number marks it `wrong_number`. Never touches the account's status; verification is refused afterwards. |
| `log_dispute` | verified | dispute (including `insurance`) or medical financial assistance; collection stops. |
| `get_debt_details` | verified | Needs the disclosure attestations; returns creditor, last four, balance, itemization and offer limits. |
| `add_payment_method` | debt unlocked, `collection` | Card or ACH to `gateway_tokenize_card` / `gateway_tokenize_ach`; returns `method_id` and last four; every card/bank field redacted. |
| `take_payment` | debt unlocked, `collection` | `gateway_charge` on a method saved this call: pay in full or a one-payment settlement at/above the floor. Approved: Remi sets `status` `paid`/`settled` (the gateway already moved the balance). Declined: the decline code and reason go to the model, the method is not charged again, and it falls back to another card, ACH or a promise. |
| `create_arrangement` | debt unlocked, `collection` | plan / hardship_plan / settlement_instalments, equal monthly instalments, charged to a `method_id` saved this call. |
| `record_promise` | debt unlocked, `collection` | Exact amount and date ≤14 days out. |
| `schedule_callback` | verified | 8 a.m.–<9 p.m. ET. |
| `queue_letter` | verified | Idempotent per call. |
| `transfer_to_human` | proceeding | Disposition `transferred`. |
| `log_contact_attempt` | assessed | The call's one row; time, duration and disposition come from the session and the clock, and the summary leads with the facts the session recorded (disposition, who answered, whether the debt was read and after which attested disclosures, outcomes), then the model's notes. |
| `end_call` | assessed | Requires the log; the line closes after the last words play (`json` framing: `{"type": "end"}` first, so the bench records a normal end, not a disconnect). |

## Payments

The World's payment gateway is three SQL functions in the collections database
(WORLD.md, "The payment gateway"); `payments.PaymentGateway` is Remi's one client
for them, called inside the tool's own transaction so a charge and the status or
arrangement that depends on it land together.

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

## Config

| Variable | Set by | Meaning |
|---|---|---|
| `VERIS_AGENT_CONTEXT` | bench, per attempt | `{"account_id","call_time","phone_id"}`. Required. |
| `DATABASE_URL` | bench (world) | Collections twin DSN, payments included. Required; the only world variable. |

Plus the framework's and the vendor's variables (AGENTS.md section 11).

## Tests

```bash
uv run --all-extras pytest -q vamos_agents/remi
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
- `tests/test_context_and_policy.py`: context parsing, the clock, RULES.md numbers.
- `tests/test_payments.py`: `PaymentGateway` against the real gateway functions: approvals, each test instrument's decline, validation errors in the gateway's words, balance effects, no direct writes.
- `tests/test_tools.py` also runs the screen pop against that Postgres: reads done, gates past them, no secrets in the section, the should-not-call case.
- `tests/test_smoke.py`: the real server running Remi over `/voice` with a fake provider (the screen pop has run before the provider starts); startup config errors.

Remi also runs in the root parity matrix (`tests/test_parity.py`) on every vendor.
