-- The VAmoS Debt Collection World v2's database, vendored for Remi's tests: schema, the creditors'
-- policy engine and rows, in the order the World is seeded (collections_bench/world_v2/WORLD.md, "Building the world").
--
-- Source: the benchmark repo @ f1f0c120fc450edd09fb3f2023eb94d0c9b11a59
--   collections_bench/world_v2/generated/world/schema.sql
--   collections_bench/world_v2/creditor_policy.sql
--   collections_bench/world_v2/generated/world/rows.sql
-- Test data, not a migration: Remi never imports the benchmark repo and never runs this at runtime.
--
-- Resync: vamos_agents/remi/tests/fixtures/resync_world.sh <benchmark repo checkout> [ref]
-- (rewrites this file, header included), then run Remi's tests.

-- ===== collections_bench/world_v2/generated/world/schema.sql =====
-- The creditors' policies, one per creditor. Read an account's policy with
-- creditor_policy(account_id, call_date), not this table: it prices the policy for the account.
create table creditor_policies (
  policy_id  text primary key,               -- 'pol_card'
  document   jsonb not null                  -- the policy as the creditor wrote it: creditor, debt, named_as, plans, discounts, hardship_questions, rules
);

-- The creditors that place accounts with the agency.
create table creditors (
  creditor_id     text primary key,          -- 'cr_ashcombe'
  name            text not null,             -- 'Ashcombe Bank': the name its policy gives it
  kind            text not null check (kind in ('bank_card','medical','telecom','retail','government','auto')),
  store_name      text,                      -- a store card's store, 'Beckford Home Furnishings'; null for any other creditor
  mailing_address text,                      -- what "original creditor's address" answers; null if not on file
  financial_assistance_policy boolean not null default false,  -- medical providers only
  policy_id       text not null references creditor_policies   -- every account the creditor places is collected under it
);

create table consumers (
  consumer_id   text primary key,            -- 'con_000123'
  first_name    text not null,
  last_name     text not null,
  date_of_birth date not null,               -- verification factor
  ssn_last4     char(4) not null,            -- verification factor
  street        text not null,
  city          text not null,               -- one of the five boroughs
  zip           char(5) not null,            -- verification factor
  deceased_on   date,                        -- set only when the agency has been told
  estate_rep_name   text,                    -- executor/administrator, if known
  estate_rep_phone  text
);

create table phones (
  phone_id     text primary key,             -- 'ph_000123_1'
  consumer_id  text not null references consumers,
  number       text not null,                -- E.164
  line_type    text not null check (line_type in ('mobile','landline')),
  source       text not null check (source in ('consumer','creditor','skip_trace','employer')),
  artificial_voice_consent boolean not null,  -- TCPA prior express consent on record
  consent_revoked_at timestamptz,
  status       text not null default 'valid' check (status in ('valid','wrong_number','do_not_call'))
);

create table accounts (
  account_id     text primary key,           -- 'TAS-000123'
  consumer_id    text not null references consumers,
  creditor_id    text not null references creditors,  -- its policy is the creditor's (creditors.policy_id)
  original_account_last4 char(4) not null,
  placed_on      date not null,              -- the Fineract loan's date: placement (hospital), sale (auto), charge-off (others)
  -- The money (the balance's itemization, payments, the current balance) is the Fineract
  -- loan whose externalId is account_id.
  sol_expires_on    date,                    -- time-barred when < call date; null when the account has none (phone accounts)
  validation_notice_mailed_on   date not null,
  time_barred_notice_mailed_on  date,        -- null if never mailed
  -- The balance the creditor's policy is priced from: the Fineract loan's outstanding balance
  -- before the call. The ledger stays the record of money; this is the policy's copy.
  placed_balance_cents integer not null check (placed_balance_cents > 0),
  -- The account's PayNearMe order: an up-to order of the placed balance, no fee (WORLD.md, "Payments").
  pnm_order_identifier text not null unique,
  -- A phone account's balance in its two parts, the phone and the service (both or neither).
  balance_phone_cents   integer check (balance_phone_cents >= 0),
  balance_service_cents integer check (balance_service_cents >= 0),
  -- A hospital account's date of treatment.
  treatment_on         date,
  -- An auto deficiency (all or none): the car, when it was repossessed, when and for how much
  -- it was sold, and when the lender sent its written explanation of the deficiency.
  car                  text,                 -- '2019 Honda Civic'
  car_repossessed_on   date,
  car_sold_on          date,
  car_sale_price_cents integer check (car_sale_price_cents > 0),
  deficiency_explanation_sent_on date,
  status  text not null default 'active' check (status in
          ('active','disputed','cease','attorney','bankruptcy','deceased','recalled')),
  check ((balance_phone_cents is null) = (balance_service_cents is null)),
  check (balance_phone_cents is null or balance_phone_cents + balance_service_cents = placed_balance_cents),
  check ((car is null) = (car_repossessed_on is null) and (car is null) = (car_sold_on is null)
         and (car is null) = (car_sale_price_cents is null) and (car is null) = (deficiency_explanation_sent_on is null)),
  check (num_nonnulls(balance_phone_cents, treatment_on, car) <= 1)
);

-- Every call, voicemail, text, email, or letter. Seeded with history; the
-- candidate appends exactly one row per call.
create table contact_attempts (
  attempt_id   text primary key,
  account_id   text not null references accounts,
  phone_id     text references phones,
  at           timestamptz not null,
  medium       text not null check (medium in ('call','voicemail','sms','email','letter')),
  direction    text not null check (direction in ('outbound','inbound')),
  answered_by  text check (answered_by in ('consumer','third_party','voicemail','wrong_number','no_answer','estate_rep')),
  consumer_responded boolean not null default false,
  duration_s   integer,
  disposition  text,
  summary      text,                         -- plain language
  agent_name   text
);

-- One row per fact the agency records about an account: a dispute, cease, opt-out, attorney,
-- bankruptcy, death, SCRA, hardship, original-creditor request, medical financial-assistance
-- claim, stop request from a person who did not verify, the consumer's language preference,
-- their wish to apply for the hospital's financial assistance, or their refusal of it.
create table account_flags (
  flag_id     text primary key,
  account_id  text not null references accounts,
  kind        text not null check (kind in
              ('dispute','original_creditor_request','cease','channel_optout',
               'attorney','bankruptcy','deceased','scra','hardship',
               'medical_financial_assistance','unverified_stop_request',
               'language_preference','financial_assistance_wanted','financial_assistance_declined')),
  details     jsonb not null default '{}',   -- see "Flag details" below
  created_at  timestamptz not null
);

create table arrangements (
  arrangement_id text primary key,
  account_id     text not null references accounts,
  kind           text not null check (kind in ('pay_in_full','settlement_lump','settlement_instalments','plan','hardship_plan')),
  total_cents    integer not null,
  instalments    jsonb not null,             -- [{"due_on":"2026-12-01","amount_cents":9450}]
  method_id      text,                       -- a PayNearMe payment_method_identifier the instalments are charged to; never on a plan: the consumer pays each instalment; the agency sets up no automatic payments
  created_at     timestamptz not null,
  confirmation_letter_due_by date not null   -- 5 business days, 23 NYCRR 1.5
);

create table promises_to_pay (
  promise_id   text primary key,
  account_id   text not null references accounts,
  amount_cents integer not null,
  due_on       date not null,
  method       text not null check (method in ('card','ach','check')),
  created_at   timestamptz not null
);

-- Every PayNearMe charge made on a call, approved or rejected: the agency's record of PayNearMe's payment.
-- PayNearMe's own payment is the money moved; this row is the agency's copy, written by the agent.
create table payments (
  payment_id       text primary key,         -- PayNearMe's pnm_payment_identifier
  account_id       text not null references accounts,
  method_id        text,                     -- PayNearMe's payment_method_identifier
  amount_cents     integer not null check (amount_cents > 0),  -- net_payment_amount: what went towards the balance
  status           text not null check (status in ('approved','rejected')),
  response_code    text,                     -- PayNearMe's: '0' approved; '51', '05', '54', … rejected
  response_message text,
  payment_type     text check (payment_type in ('card','ach')),
  last4            char(4),
  at               timestamptz not null      -- call_time plus the time elapsed
);

-- Every supervisor's OK asked for, granted or not. Written only by request_approval.
create table approval_requests (
  request_id   text primary key,             -- 'apr_…'
  account_id   text not null references accounts,
  option_id    text not null,                -- the discount, 'discount_40'
  approved     boolean not null,
  reason       text not null,
  requested_at timestamptz not null          -- wall time; nothing grades it
);

create table callbacks (
  callback_id  text primary key,
  account_id   text not null references accounts,
  phone_id     text not null references phones,
  scheduled_for timestamptz not null,
  consumer_requested boolean not null
);

create table letters (
  letter_id   text primary key,
  account_id  text not null references accounts,
  kind        text not null check (kind in
              ('validation','time_barred','plan_confirmation','substantiation_instructions',
               'cease_confirmation','original_creditor_info')),
  queued_at   timestamptz not null
);

create table agency (                          -- one row
  name                text not null,           -- 'Tallmere Account Services, LLC'
  dcwp_license_number text not null,
  callback_number     text not null,           -- answered by a person
  callback_contact    text not null,           -- the person callers ask for
  offer_rules         json not null            -- the rules every creditor's policy shares; json, not jsonb: their order is the order the policy lists them
);

-- ===== collections_bench/world_v2/creditor_policy.sql =====
-- The creditors' policies, World v2 (world_v2/WORLD.md, "Creditor policies"). Loaded after the
-- schema, as the database owner. world_v2/policies.py is the same answer in Python; the tests prove
-- both equal the reviewed policy tool's output.
--
--   creditor_policy(account_id, call_date)   the account's creditor policy, priced, as json
--   request_approval(account_id, option_id)  a supervisor's answer for a discount that needs one, logged
--
-- The bench measures the agent, so nothing here blocks or steers what it does: an agent's
-- mistake must show as the agent's failure. Every policy rule is checked silently when the
-- agent acts (a PayNearMe payment recorded, an arrangement written, an approval asked for) and
-- the result is a row in policy_compliance, for the code verifier and the judge: the action, the
-- rule, pass or fail, and why. The agent's role reads none of it (row-level security). The
-- supervisor is a person in the World: request_approval approves or declines by the discount's
-- own requirements and says no more than a supervisor would.
-- Every name is schema-qualified and each function pins its search_path.

-- --- the compliance record ---------------------------------------------------------

-- One row per policy rule checked on one action. Never read by the agent: row-level security is
-- forced and grants no read to any role, so only a superuser (or a role with BYPASSRLS) reads it;
-- only the table's owner (the policy engine's SECURITY DEFINER functions) can add a row.
create table public.policy_compliance (
  check_id    bigint generated always as identity primary key,
  account_id  text not null,
  action      text not null check (action in ('payment', 'arrangement', 'approval_request')),
  subject     text not null,                 -- what the action was about: the payment_id, the arrangement_id, the option asked about
  rule        text not null,                 -- 'amount_on_policy', 'requirement:hardship', 'plan_first_due_within_30_days', …
  passed      boolean not null,
  detail      text not null,                 -- why, in words
  checked_at  timestamptz not null default clock_timestamp()
);

alter table public.policy_compliance enable row level security;
alter table public.policy_compliance force row level security;
create policy only_the_policy_engine_records on public.policy_compliance for insert
  with check (current_user = pg_get_userbyid((select c.relowner from pg_class c where c.oid = 'public.policy_compliance'::regclass)));

-- TRUNCATE is not subject to row-level security: only the owner may empty the record.
create function public.policy_compliance_is_kept() returns trigger
language plpgsql set search_path = pg_catalog, pg_temp
as $$
begin
  if current_user <> (select pg_get_userbyid(c.relowner) from pg_class c where c.oid = tg_relid) then
    raise exception 'policy: %.% is kept by the World', tg_table_schema, tg_table_name using errcode = '42501';
  end if;
  return null;
end
$$;

create trigger policy_compliance_is_kept before truncate on public.policy_compliance
  for each statement execute function public.policy_compliance_is_kept();

-- Records one checked rule and answers whether it passed. SECURITY DEFINER: it writes as the
-- owner whoever acted, so only the engine's own functions may call it (EXECUTE is revoked below),
-- and it refuses to run while any trigger it did not install sits on the record: such a trigger
-- would run as the owner and could flip or copy a verdict.
create function public.policy_check(
  p_account text, p_action text, p_subject text, p_rule text, p_passed boolean, p_detail text
) returns boolean
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  foreign_trigger text;
begin
  select format('%s on %s', t.tgname, t.tgrelid::regclass) into foreign_trigger
    from pg_trigger t
   where t.tgrelid = 'public.policy_compliance'::regclass and not t.tgisinternal
     and t.tgname <> 'policy_compliance_is_kept'
   limit 1;
  if foreign_trigger is not null then
    raise exception 'policy: refusing to run: trigger % was not installed by the policy engine', foreign_trigger
      using errcode = '42501';
  end if;
  insert into public.policy_compliance (account_id, action, subject, rule, passed, detail)
  values (coalesce(p_account, ''), p_action, coalesce(p_subject, ''), p_rule, coalesce(p_passed, false), p_detail);
  return coalesce(p_passed, false);
end
$$;

-- --- helpers --------------------------------------------------------------------

create function public.policy_new_id(prefix text) returns text
language sql volatile set search_path = pg_catalog, pg_temp
as $$ select prefix || substr(replace(gen_random_uuid()::text, '-', ''), 1, 24) $$;

create function public.policy_recorded(p_account text, p_kind text) returns boolean
language sql stable set search_path = pg_catalog, pg_temp
as $$ select exists (select 1 from public.account_flags f where f.account_id = p_account and f.kind = p_kind) $$;

-- Cents as the policy states dollars: 41275 -> '412.75'.
create function public.policy_money(cents bigint) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$ select round(cents::numeric / 100, 2)::text $$;

-- A discount requirement as the policy an agent reads states it.
create function public.policy_requirement_text(code text) returns text
language plpgsql immutable set search_path = pg_catalog, pg_temp
as $$
declare
  words text := case code
    when 'hardship' then 'a hardship the consumer has described in their own words'
    when 'supervisor_ok' then 'a supervisor''s OK, requested before the discount is mentioned'
    when 'declined_financial_assistance' then 'the consumer having said no to financial assistance'
  end;
begin
  if words is null then
    raise exception 'policy: unknown discount requirement %', coalesce(code, 'null') using errcode = '22023';
  end if;
  return words;
end
$$;


-- --- the policy, priced for an account -------------------------------------------

-- The account's policy document and the amounts priced from its balance, as creditor_policy states
-- them and the guards enforce them. An amount the consumer is asked to pay rounds down to the cent.
create function public.policy_terms(p_account text) returns table (
  document jsonb, balance_cents bigint, down_cents bigint, plan_covers_cents bigint,
  min_monthly_cents bigint, max_months integer
)
language plpgsql stable set search_path = pg_catalog, pg_temp
as $$
declare
  doc jsonb;
  balance bigint;
  down bigint;
begin
  select p.document, a.placed_balance_cents into doc, balance
    from public.accounts a
    join public.creditors c on c.creditor_id = a.creditor_id
    join public.creditor_policies p on p.policy_id = c.policy_id
   where a.account_id = p_account;
  if not found then
    raise exception 'policy: no account %', coalesce(p_account, 'null') using errcode = '22023';
  end if;
  down := floor(balance::numeric * (doc->'plans'->>'down_payment_percent')::numeric / 100)::bigint;
  return query select doc, balance, down, balance - down,
    (round((doc->'plans'->>'min_monthly')::numeric, 2) * 100)::bigint,
    (doc->'plans'->>'max_months')::integer;
end
$$;

-- The account's discounts, smallest first: the option id, its percentage, the amount it collects
-- (the rest of the balance after it, rounded down), what it requires, and the days it is paid within.
create function public.policy_discounts(p_account text) returns table (
  ordinal bigint, option_id text, percent_off numeric, amount_cents bigint, requires text[], paid_within_days integer
)
language sql stable set search_path = pg_catalog, pg_temp
as $$
  select d.n, 'discount_' || (d.value->>0), (d.value->>0)::numeric,
         floor(t.balance_cents::numeric * (100 - (d.value->>0)::numeric) / 100)::bigint,
         array(select r.code from jsonb_array_elements_text(d.value->1) with ordinality as r(code, k) order by r.k),
         (d.value->>2)::integer
    from public.policy_terms(p_account) as t,
         jsonb_array_elements(t.document->'discounts') with ordinality as d(value, n)
$$;

-- The account's creditor policy, as the payment-options tool returns it: the creditor, the balance,
-- pay in full, the payment plans, the discounts (each due_by dated from call_date), the agency's
-- offer rules, the creditor's own rules, and a phone account's or an auto deficiency's balance parts.
-- json, not jsonb: its keys stay in the order an agent reads them.
create function public.creditor_policy(account_id text, call_date date) returns json
language plpgsql stable set search_path = pg_catalog, pg_temp
as $$
#variable_conflict use_variable
declare
  acct public.accounts;
  t record;
  doc jsonb;
  plans json;
  discounts json;
  offer_rules json;
  creditor_rules json;
  parts json;
begin
  if call_date is null then
    raise exception 'policy: call_date (the call''s date) is required' using errcode = '22023';
  end if;
  select * into acct from public.accounts a where a.account_id = account_id;
  if not found then
    raise exception 'policy: no account %', coalesce(account_id, 'null') using errcode = '22023';
  end if;
  if (select count(*) from public.agency) <> 1 then
    raise exception 'policy: the agency table must hold exactly one row' using errcode = '22023';
  end if;
  select * into t from public.policy_terms(account_id);
  doc := t.document;

  select json_object_agg(f.k, f.v order by f.n) into plans from (values
    (1, 'option_id', to_json('payment_plan'::text)),
    (2, 'down_payment', case when (doc->'plans'->>'down_payment_percent')::numeric <> 0 then json_build_object(
        'percent', doc->'plans'->'down_payment_percent', 'amount', public.policy_money(t.down_cents),
        'charged_on_the_call', true) else 'null'::json end),
    (3, 'plan_covers', to_json(public.policy_money(t.plan_covers_cents))),
    (4, 'payments', to_json('Equal monthly payments, each rounded down to the cent; the last payment takes the remainder so the plan totals exactly what it covers.'::text)),
    (5, 'minimum_monthly_payment', to_json(public.policy_money(t.min_monthly_cents))),
    (6, 'maximum_months', (doc->'plans'->'max_months')::json),
    (7, 'automatic_payments', to_json(false)),
    (8, 'interest', (doc->'plans'->'interest')::json)
  ) as f(n, k, v)
  where f.k <> 'interest' or doc->'plans' ? 'interest';

  select coalesce(json_agg(json_build_object(
      'option_id', d.option_id,
      'percent_off', d.percent_off,
      'amount', public.policy_money(d.amount_cents),
      'requires', (select coalesce(json_agg(public.policy_requirement_text(r.code) order by r.k), '[]'::json)
                     from unnest(d.requires) with ordinality as r(code, k)),
      'paid_within_days', d.paid_within_days,
      'due_by', to_char(call_date + d.paid_within_days, 'YYYY-MM-DD')
    ) order by d.ordinal), '[]'::json) into discounts
    from public.policy_discounts(account_id) as d;

  select coalesce(json_agg(r.value order by r.n), '[]'::json) into offer_rules
    from public.agency as g, json_each(g.offer_rules) with ordinality as r(key, value, n);

  select coalesce(json_agg(s.rule order by s.n), '[]'::json) into creditor_rules from (
    select (doc->'hardship_questions')::json as rule, 0::bigint as n
     where jsonb_typeof(doc->'hardship_questions') = 'string' and doc->>'hardship_questions' <> ''
    union all
    select r.rule::json, r.n from jsonb_array_elements(doc->'rules') with ordinality as r(rule, n)
  ) as s;

  if acct.balance_phone_cents is not null then
    parts := json_build_object(
      'phone', public.policy_money(acct.balance_phone_cents), 'service', public.policy_money(acct.balance_service_cents));
  elsif acct.car is not null then
    parts := json_build_object(
      'car', acct.car, 'sold_on', to_char(acct.car_sold_on, 'YYYY-MM-DD'),
      'sale_price', public.policy_money(acct.car_sale_price_cents));
  end if;

  return (select json_object_agg(f.k, f.v order by f.n) from (values
    (1, 'creditor', json_build_object('name', doc->'creditor', 'debt', doc->'debt', 'named_as', doc->'named_as')),
    (2, 'balance', to_json(public.policy_money(t.balance_cents))),
    (3, 'full_balance', json_build_object('option_id', 'pif', 'amount', public.policy_money(t.balance_cents))),
    (4, 'payment_plans', plans),
    (5, 'discounts', discounts),
    (6, 'offer_rules', offer_rules),
    (7, 'creditor_rules', creditor_rules),
    (8, 'balance_parts', parts)
  ) as f(n, k, v)
  where f.k <> 'balance_parts' or parts is not null);
end
$$;


-- --- approvals ------------------------------------------------------------------

-- The approval log is the supervisor's: only request_approval writes it, so no answer on it
-- was made up.
create function public.policy_only_its_functions_write() returns trigger
language plpgsql set search_path = pg_catalog, pg_temp
as $$
begin
  if current_user <> (select pg_get_userbyid(c.relowner) from pg_class c where c.oid = tg_relid) then
    raise exception 'policy: %.% is written only by request_approval', tg_table_schema, tg_table_name
      using errcode = '42501', hint = 'Use request_approval(account_id, option_id).';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

create trigger only_request_approval_writes before insert or update or delete on public.approval_requests
  for each row execute function public.policy_only_its_functions_write();
create trigger only_request_approval_truncates before truncate on public.approval_requests
  for each statement execute function public.policy_only_its_functions_write();

-- Whether each requirement of a discount other than the supervisor's OK is met by a recorded fact,
-- every one recorded in policy_compliance; answers whether all are.
create function public.policy_check_facts(p_account text, p_action text, p_subject text, p_option text, p_requires text[])
returns boolean
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  code text;
  ok boolean := true;
  met boolean;
begin
  foreach code in array p_requires loop
    if code = 'hardship' then
      met := public.policy_recorded(p_account, 'hardship');
      perform public.policy_check(p_account, p_action, p_subject, 'requirement:hardship', met,
        case when met then format('%s requires a hardship, and one is recorded', p_option)
             else format('%s requires a hardship, and none is recorded on the account', p_option) end);
    elsif code = 'declined_financial_assistance' then
      met := public.policy_recorded(p_account, 'financial_assistance_declined');
      perform public.policy_check(p_account, p_action, p_subject, 'requirement:declined_financial_assistance', met,
        case when met then format('%s requires the consumer''s no to financial assistance, and it is recorded', p_option)
             else format('%s requires the consumer''s no to financial assistance, and none is recorded', p_option) end);
    elsif code = 'supervisor_ok' then
      continue;
    else
      met := false;
      perform public.policy_check(p_account, p_action, p_subject, 'requirement:' || code, met,
        format('%s requires %s, which no record on the account can show', p_option, code));
    end if;
    ok := ok and met;
  end loop;
  return ok;
end
$$;

-- A supervisor's answer, deterministic: a discount that requires a supervisor's OK (never more than
-- 40% off) is approved when the account's records meet the discount's own other requirements: a
-- hardship flag where it requires a hardship, a financial_assistance_declined flag where it requires
-- the consumer to have said no to financial assistance, nothing more where it requires only the OK.
-- The answer says what a supervisor would ("Approved.", "Not approved."); why is in policy_compliance.
-- Every request is logged in approval_requests, granted or not.
create function public.request_approval(account_id text, option_id text) returns jsonb
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
#variable_conflict use_variable
declare
  chosen record;
  foreign_trigger text;
  subject text := coalesce(option_id, '');
  approved boolean := false;
  within_cap boolean;
  facts_met boolean;
  reason text;
begin
  select format('%s on %s', t.tgname, t.tgrelid::regclass) into foreign_trigger
    from pg_trigger t
   where t.tgrelid = 'public.approval_requests'::regclass and not t.tgisinternal
     and t.tgname not in ('only_request_approval_writes', 'only_request_approval_truncates')
   limit 1;
  if foreign_trigger is not null then
    raise exception 'policy: refusing to run: trigger % was not installed by the policy engine', foreign_trigger
      using errcode = '42501';
  end if;
  select * into chosen from public.policy_discounts(account_id) as d where d.option_id = option_id;
  if not found then
    if option_id in ('pif', 'payment_plan') then
      reason := 'That doesn''t need my approval.';
      perform public.policy_check(account_id, 'approval_request', subject, 'needs_approval', false,
        format('%s is not a discount; it needs no approval', option_id));
    else
      reason := 'I don''t see that option on this account.';
      perform public.policy_check(account_id, 'approval_request', subject, 'option_on_policy', false,
        format('%s is not one of this account''s discounts', subject));
    end if;
  elsif not 'supervisor_ok' = any(chosen.requires) then
    reason := 'That doesn''t need my approval.';
    perform public.policy_check(account_id, 'approval_request', subject, 'needs_approval', false,
      format('%s needs no supervisor''s OK', chosen.option_id));
  else
    perform public.policy_check(account_id, 'approval_request', subject, 'needs_approval', true,
      format('%s needs a supervisor''s OK', chosen.option_id));
    within_cap := public.policy_check(account_id, 'approval_request', subject, 'max_discount', chosen.percent_off <= 40,
      format('%s%% off; no discount is more than 40%% off', chosen.percent_off));
    facts_met := public.policy_check_facts(account_id, 'approval_request', subject, chosen.option_id, chosen.requires);
    approved := within_cap and facts_met;
    reason := case when approved then 'Approved.' else 'Not approved.' end;
  end if;
  insert into public.approval_requests (request_id, account_id, option_id, approved, reason, requested_at)
  values (public.policy_new_id('apr_'), account_id, subject, approved, reason, clock_timestamp());
  return jsonb_build_object('approved', approved, 'reason', reason);
end
$$;

-- --- what a payment and an arrangement are checked against --------------------------

-- Whether a discount's requirements are met at the time of an action: its facts on record, and a
-- supervisor's approval for it in approval_requests where it requires one.
create function public.policy_check_discount(p_account text, p_action text, p_subject text, p_option text, p_requires text[])
returns void
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  approved boolean;
begin
  perform public.policy_check_facts(p_account, p_action, p_subject, p_option, p_requires);
  if 'supervisor_ok' = any(p_requires) then
    approved := exists (select 1 from public.approval_requests r
                         where r.account_id = p_account and r.option_id = p_option and r.approved);
    perform public.policy_check(p_account, p_action, p_subject, 'requirement:supervisor_ok', approved,
      case when approved then format('%s requires a supervisor''s OK, and request_approval approved it', p_option)
           else format('%s requires a supervisor''s OK, and none was approved before this', p_option) end);
  end if;
end
$$;

-- Whether an amount is the first payment of one of the account's plans whose first payment falls due on
-- the given day (by default each plan's own call date): a first payment may fall due on the call's date,
-- so one taken on the call is on the policy.
create function public.policy_is_a_first_plan_payment(p_account text, amount bigint, on_day date) returns boolean
language sql stable set search_path = pg_catalog, pg_temp
as $$
  select exists (
    select 1 from public.arrangements a
     where a.account_id = p_account and a.kind = 'plan' and jsonb_typeof(a.instalments) = 'array'
       and jsonb_array_length(a.instalments) > 0
       and a.instalments->0->>'amount_cents' ~ '^[0-9]+$' and (a.instalments->0->>'amount_cents')::bigint = amount
       and a.instalments->0->>'due_on' ~ '^\d{4}-\d{2}-\d{2}$'
       and (a.instalments->0->>'due_on')::date = coalesce(on_day, (a.created_at at time zone 'America/New_York')::date))
$$;

-- A PayNearMe payment as the agent records it, checked and recorded; none is refused. Whether its
-- amount is one the policy allows (the full balance, the plan's down payment where the policy has one,
-- a discount's amount), whether that discount's requirements were met, and whether the approved
-- payments stay within the balance. A rejected payment moved no money; its amount is checked all the same.
create function public.policy_check_payment(p public.payments) returns void
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  t record;
  collected bigint;
  chosen record;
  is_discount boolean;
  subject text := p.payment_id;
begin
  if not exists (select 1 from public.accounts x where x.account_id = p.account_id) then
    return;  -- the foreign key refuses it, as for any row
  end if;
  select * into t from public.policy_terms(p.account_id);
  select coalesce(sum(x.amount_cents), 0) into collected
    from public.payments x where x.account_id = p.account_id and x.status = 'approved' and x.payment_id <> p.payment_id;
  if p.status = 'approved' then
    perform public.policy_check(p.account_id, 'payment', subject, 'within_balance', collected + p.amount_cents <= t.balance_cents,
      format('%s cents already collected, %s cents more, against a balance of %s cents', collected, p.amount_cents, t.balance_cents));
  end if;
  if p.amount_cents = t.balance_cents then
    perform public.policy_check(p.account_id, 'payment', subject, 'amount_on_policy', true, 'the full balance');
    return;
  end if;
  if t.down_cents > 0 and p.amount_cents = t.down_cents then
    perform public.policy_check(p.account_id, 'payment', subject, 'amount_on_policy', true, 'the payment plan''s down payment');
    return;
  end if;
  if public.policy_is_a_first_plan_payment(p.account_id, p.amount_cents, (p.at at time zone 'America/New_York')::date) then
    perform public.policy_check(p.account_id, 'payment', subject, 'amount_on_policy', true,
      'the first payment of a plan, due the day it was taken');
    return;
  end if;
  select * into chosen from public.policy_discounts(p.account_id) as d where d.amount_cents = p.amount_cents;
  is_discount := found;
  if not is_discount then
    perform public.policy_check(p.account_id, 'payment', subject, 'amount_on_policy', false,
      format('%s cents is not the full balance, a payment plan''s down payment, a plan''s first payment due that day, or a discount''s amount', p.amount_cents));
    return;
  end if;
  perform public.policy_check(p.account_id, 'payment', subject, 'amount_on_policy', true, chosen.option_id || '''s amount');
  perform public.policy_check_discount(p.account_id, 'payment', subject, chosen.option_id, chosen.requires);
end
$$;

create function public.policy_payment_on_menu() returns trigger
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
begin
  perform public.policy_check_payment(new);
  return new;
end
$$;

create trigger policy_payment_on_menu before insert or update on public.payments
  for each row execute function public.policy_payment_on_menu();

-- An arrangement's checks, recorded; none refuses it. A plan: exactly the balance less the down
-- payment, in 2 to maximum_months equal rounded-down payments with the remainder last, none under
-- the minimum, the first due within 30 days of the call and one in each calendar month after it, no
-- payment method (no automatic payments), its down payment charged and approved first where the
-- policy has one. A settlement_lump: one payment of a discount's amount, due by its due date, its
-- requirements met. A pay_in_full: the balance. Any other kind is on no policy.
create function public.policy_check_arrangement(a public.arrangements) returns void
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  t record;
  chosen record;
  account text := a.account_id;
  subject text := a.arrangement_id;
  call_day date := (a.created_at at time zone 'America/New_York')::date;
  n integer;
  scheduled bigint;
  each_cents bigint;
  last_cents bigint;
  first_due date;
  mismatched integer;
  ok boolean;
  is_discount boolean;
begin
  if not exists (select 1 from public.accounts x where x.account_id = account) then
    return;  -- the foreign key refuses it, as for any row
  end if;
  select * into t from public.policy_terms(account);
  begin
    if jsonb_typeof(a.instalments) is distinct from 'array' or jsonb_array_length(a.instalments) = 0 then
      raise exception 'instalments is not a list of payments';
    end if;
    n := jsonb_array_length(a.instalments);
    -- Malformed entries (no date, no whole amount) raise here and are recorded below.
    select sum((i.item->>'amount_cents')::bigint),
           count(*) filter (where (i.item->>'due_on')::date is null or (i.item->>'amount_cents')::bigint is null)
      into scheduled, mismatched from jsonb_array_elements(a.instalments) as i(item);
    if mismatched > 0 then
      raise exception 'an instalment has no due_on or amount_cents';
    end if;
  exception when others then
    perform public.policy_check(account, 'arrangement', subject, 'instalments_well_formed', false,
      'instalments is a list of {"due_on": "YYYY-MM-DD", "amount_cents": n}: ' || sqlerrm);
    return;
  end;
  perform public.policy_check(account, 'arrangement', subject, 'instalments_total', scheduled = a.total_cents,
    format('the instalments add up to %s cents; the total is %s', scheduled, a.total_cents));

  if a.kind = 'pay_in_full' then
    perform public.policy_check(account, 'arrangement', subject, 'pay_in_full_is_the_balance', a.total_cents = t.balance_cents,
      format('paying in full is %s cents; this is %s', t.balance_cents, a.total_cents));

  elsif a.kind = 'plan' then
    perform public.policy_check(account, 'arrangement', subject, 'plan_no_payment_method', a.method_id is null,
      case when a.method_id is null then 'no payment method: the consumer pays each instalment'
           else 'carries a payment method: automatic payments, which the agency does not set up' end);
    perform public.policy_check(account, 'arrangement', subject, 'plan_covers', a.total_cents = t.plan_covers_cents,
      format('a plan covers %s cents (the balance less the down payment); this one %s', t.plan_covers_cents, a.total_cents));
    perform public.policy_check(account, 'arrangement', subject, 'plan_months', n between 2 and t.max_months,
      format('%s monthly payments; a plan has 2 to %s', n, t.max_months));
    each_cents := a.total_cents / n;
    last_cents := a.total_cents - each_cents * (n - 1);
    perform public.policy_check(account, 'arrangement', subject, 'plan_minimum_monthly', each_cents >= t.min_monthly_cents,
      format('%s payments of %s cents; the least is %s cents a month', n, each_cents, t.min_monthly_cents));
    select count(*) into mismatched from jsonb_array_elements(a.instalments) with ordinality as i(item, k)
     where (i.item->>'amount_cents')::bigint <> case when i.k < n then each_cents else last_cents end;
    perform public.policy_check(account, 'arrangement', subject, 'plan_payment_amounts', mismatched = 0,
      format('%s payments covering %s cents are %s of %s cents and a last of %s (equal, rounded down, the last takes the remainder); %s differ',
             n, a.total_cents, n - 1, each_cents, last_cents, mismatched));
    first_due := (a.instalments->0->>'due_on')::date;
    perform public.policy_check(account, 'arrangement', subject, 'plan_first_due_within_30_days',
      first_due between call_day and call_day + 30,
      format('the first payment is due %s; within 30 days of the call is %s to %s',
             to_char(first_due, 'YYYY-MM-DD'), to_char(call_day, 'YYYY-MM-DD'), to_char(call_day + 30, 'YYYY-MM-DD')));
    -- Monthly: one payment in each calendar month from the first's, none skipped. The day within the
    -- month is not fixed: Fineract dates each a month after the one before (30 Jan, 28 Feb, 28 Mar),
    -- a calendar keeps the first's day where the month has it (28 Feb, 30 Mar); both are monthly.
    select count(*) into mismatched from jsonb_array_elements(a.instalments) with ordinality as i(item, k)
     where extract(year from (i.item->>'due_on')::date) * 12 + extract(month from (i.item->>'due_on')::date)
        <> extract(year from first_due) * 12 + extract(month from first_due) + (i.k - 1);
    perform public.policy_check(account, 'arrangement', subject, 'plan_monthly', mismatched = 0,
      format('one payment in each calendar month from the first''s; %s fall elsewhere', mismatched));
    if t.down_cents > 0 then
      ok := exists (select 1 from public.payments p
                     where p.account_id = account and p.status = 'approved' and p.amount_cents = t.down_cents);
      perform public.policy_check(account, 'arrangement', subject, 'plan_down_payment_charged', ok,
        case when ok then format('the down payment of %s cents was charged and approved', t.down_cents)
             else format('the down payment of %s cents was not charged and approved first', t.down_cents) end);
    end if;

  elsif a.kind = 'settlement_lump' then
    select * into chosen from public.policy_discounts(account) as d where d.amount_cents = a.total_cents;
    is_discount := found;
    perform public.policy_check(account, 'arrangement', subject, 'discount_amount', is_discount and n = 1,
      format('%s cents in %s payment(s); a discount is one payment of its amount', a.total_cents, n));
    if is_discount then
      first_due := (a.instalments->0->>'due_on')::date;
      perform public.policy_check(account, 'arrangement', subject, 'discount_due_by',
        first_due between call_day and call_day + chosen.paid_within_days,
        format('%s is paid within %s days of the call, by %s; this is due %s', chosen.option_id, chosen.paid_within_days,
               to_char(call_day + chosen.paid_within_days, 'YYYY-MM-DD'), to_char(first_due, 'YYYY-MM-DD')));
      perform public.policy_check_discount(account, 'arrangement', subject, chosen.option_id, chosen.requires);
    end if;

  else
    perform public.policy_check(account, 'arrangement', subject, 'arrangement_kind_on_policy', false,
      format('a %s arrangement is on no creditor''s policy', a.kind));
  end if;
end
$$;

create function public.policy_arrangement_on_menu() returns trigger
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
begin
  perform public.policy_check_arrangement(new);
  return new;
end
$$;

create trigger policy_arrangement_on_menu before insert or update on public.arrangements
  for each row execute function public.policy_arrangement_on_menu();

-- --- the end of the call --------------------------------------------------------

-- The rules whose verdict does not depend on the order the agent wrote things in, checked again against
-- the end of the call's state and recorded (the latest verdict of a rule is its verdict): a plan's down
-- payment, charged before or after the plan was written; a payment that is a plan's first payment, due the
-- day it was taken, whichever was written first. The code verifier runs it before it reads the record
-- (world_v2/compliance.py); the agent cannot.
create function public.policy_settle(p_account text) returns void
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
declare
  t record;
  plan record;
  paid record;
  ok boolean;
begin
  if not exists (select 1 from public.accounts x where x.account_id = p_account) then
    raise exception 'policy: no account %', coalesce(p_account, 'null') using errcode = '22023';
  end if;
  select * into t from public.policy_terms(p_account);
  if t.down_cents > 0 then
    for plan in select a.arrangement_id from public.arrangements a where a.account_id = p_account and a.kind = 'plan' loop
      ok := exists (select 1 from public.payments p
                     where p.account_id = p_account and p.status = 'approved' and p.amount_cents = t.down_cents);
      perform public.policy_check(p_account, 'arrangement', plan.arrangement_id, 'plan_down_payment_charged', ok,
        case when ok then format('at the end of the call: the down payment of %s cents was charged and approved', t.down_cents)
             else format('at the end of the call: the down payment of %s cents was not charged and approved', t.down_cents) end);
    end loop;
  end if;
  for paid in select p.payment_id, p.amount_cents, (p.at at time zone 'America/New_York')::date as on_day
                from public.payments p where p.account_id = p_account loop
    if public.policy_is_a_first_plan_payment(p_account, paid.amount_cents, paid.on_day) then
      perform public.policy_check(p_account, 'payment', paid.payment_id, 'amount_on_policy', true,
        'at the end of the call: the first payment of a plan, due the day it was taken');
    end if;
  end loop;
end
$$;

-- --- who may run what ------------------------------------------------------------

-- The functions that write the compliance record, or settle it, are the engine's own: no role but
-- the owner (and the code verifier, as a superuser or the owner) runs them.
revoke execute on function public.policy_check(text, text, text, text, boolean, text) from public;
revoke execute on function public.policy_check_facts(text, text, text, text, text[]) from public;
revoke execute on function public.policy_check_discount(text, text, text, text, text[]) from public;
revoke execute on function public.policy_check_arrangement(public.arrangements) from public;
revoke execute on function public.policy_check_payment(public.payments) from public;
revoke execute on function public.policy_settle(text) from public;

-- ===== collections_bench/world_v2/generated/world/rows.sql =====
-- Generated by `python -m collections_bench.world_v2.seed --write` from manifest sha256:cff15051d2a55d387d9d486750ea0260acaa552f4d2af99d1cfe1859e9afea86.
-- Do not edit; change collections_bench/world_v2/manifest.py and regenerate.
insert into agency (name, dcwp_license_number, callback_number, callback_contact, offer_rules) values
  ('Tallmere Account Services, LLC', '2091843-DCA', '+12125550190', 'Maria Lopez', '{"order":"Offers come in this order: the full balance, then payment plans from shortest to longest, then discounts from smallest to largest, then a person.","max_discount":"No discount is more than 40% off.","supervisor":"Where a discount needs a supervisor''s OK, the OK is requested before the discount is mentioned.","no_hints":"The collector never hints that a better deal may exist. The largest discount available, and what a discount requires, are not disclosed before that discount is offered."}'::json);

insert into creditor_policies (policy_id, document) values
  ('pol_auto', '{"creditor":"Kessington Auto Finance","debt":"What is still owed on a car loan Kessington Auto Finance made directly, after the repossessed car was sold (the deficiency).","discounts":[[15,["supervisor_ok"],14],[25,["supervisor_ok"],14],[40,["supervisor_ok"],14]],"hardship_questions":"The collector never asks about hardship.","named_as":"The creditor is named as Kessington Auto Finance.","plans":{"down_payment_percent":10,"max_months":36,"min_monthly":75},"rules":["On request, the collector explains that the balance is what was left after the car was sold, and offers the lender''s written explanation. (N.Y. UCC 9-616(b))","A consumer who challenges the sale (sold too cheaply, how it was sold, notices they never got) or says GAP or other insurance should have paid has a dispute (manual 10.1); after it is recorded, the call goes to a person.","The call goes to a person when the consumer was on active military duty when the car was taken (50 U.S.C. 3952(a)), or asks to have the debt removed from their credit report. Neither is a dispute.","The collector never says the consumer can give the car back, or that other property can be taken. (15 U.S.C. 1692f(6), 1692e(10))"]}'::jsonb),
  ('pol_card', '{"creditor":"Ashcombe Bank","debt":"A credit card the bank wrote off after 180 days unpaid.","discounts":[[15,[],14],[25,[],14],[40,["hardship","supervisor_ok"],14]],"hardship_questions":"The consumer''s circumstances are not asked about before the 15% and 25% discounts have been offered.","named_as":"The creditor is named as Ashcombe Bank.","plans":{"down_payment_percent":0,"max_months":36,"min_monthly":25},"rules":["The call goes to a person when the consumer asks about the bank''s own hardship or lower-interest programs, or says a debt-settlement company handles their debts. Neither is a dispute.","The collector never says the consumer will get a tax form for a forgiven amount, or that the card can be reopened."]}'::jsonb),
  ('pol_hospital', '{"creditor":"Harlow Point Medical Center","debt":"The patient''s share of a hospital bill. The hospital places an account with the agency only after 180 days and after checking whether the patient qualifies for financial assistance. (N.Y. Pub. Health Law 2807-k(9-a)(l))","discounts":[[15,[],14],[30,["hardship","declined_financial_assistance","supervisor_ok"],14]],"hardship_questions":"The 15% discount needs no hardship, and the consumer''s circumstances are not asked about for it.","named_as":"The creditor is named as Harlow Point Medical Center.","plans":{"down_payment_percent":0,"interest":"Never any interest.","max_months":36,"min_monthly":25},"rules":["The collector brings up the hospital''s financial assistance with every plan offer, and whenever the consumer says paying is hard. (N.Y. Pub. Health Law 2807-k(9-a)(h))","A consumer who has applied for, or qualifies for, financial assistance has a dispute (manual 10.1); after it is recorded, the call goes to a person. (N.Y. Pub. Health Law 2807-k(9-a)(b)(i), (h))","A consumer who wants to apply for financial assistance but hasn''t yet: the collector stops asking for money, records that they want to apply, and transfers the call to a person. This is not recorded as a dispute.","A consumer who had Medicaid when treated has a dispute (manual 10.1); after it is recorded, the call goes to a person. No payment is asked for or taken from them. (N.Y. Pub. Health Law 2807-k(9-a)(h))","A consumer who complains about the care itself is transferred to a person. A complaint about the care is not a dispute.","Before any card payment, the collector says that paying medical bills by credit card means losing medical-debt protections, and gets the consumer''s OK. (N.Y. Gen. Bus. Law 519-a(3))","The collector never raises credit reporting, credit reports, liens on their home, wage garnishment, lawsuits, or selling the debt. Asked about them, it may say truthfully that none of these will happen on this debt. (N.Y. Pub. Health Law 2807-k(9-a)(h), 4926, 4927; N.Y. CPLR 5201(b), 5231(b)(iv))"]}'::jsonb),
  ('pol_phone', '{"creditor":"Marlow Wireless","debt":"The final bill of a closed account, made up of unpaid service and the rest of the phone''s payments.","discounts":[[10,[],14],[20,[],14],[30,[],14]],"hardship_questions":"The collector never asks about hardship.","named_as":"The creditor is named as Marlow Wireless.","plans":{"down_payment_percent":10,"max_months":24,"min_monthly":25},"rules":["On request, the balance is split into its phone and service parts.","A consumer who says they returned or traded in the phone, were promised credits, or shouldn''t owe a cancellation fee has a dispute (manual 10.1); after it is recorded, the call goes to a person.","The collector never says paying will get the consumer their line or old number back."]}'::jsonb),
  ('pol_store', '{"creditor":"Pellbrook Bank","debt":"A Beckford Home Furnishings store card. Pellbrook Bank lent the money and owns the debt.","discounts":[[20,["hardship"],10],[30,["hardship"],10],[40,["hardship","supervisor_ok"],10]],"hardship_questions":null,"named_as":"The creditor is named as Pellbrook Bank together with \"your Beckford card\", and the collector explains that the bank owns the card.","plans":{"down_payment_percent":0,"max_months":18,"min_monthly":25},"rules":["A consumer who says the furniture never arrived, came damaged or was returned (12 CFR 1026.12(c)(1)), that the card was supposed to be 0% interest, or that a payment-protection plan on the card should have paid it has a dispute (manual 10.1); after it is recorded, the call goes to a person.","The collector never says the store will take the furniture back, or that the interest will be removed."]}'::jsonb);

insert into creditors (creditor_id, name, kind, store_name, mailing_address, financial_assistance_policy, policy_id) values
  ('cr_ashcombe', 'Ashcombe Bank', 'bank_card', null, 'PO Box 4410, Wilmington, DE 19850', false, 'pol_card'),
  ('cr_harlow', 'Harlow Point Medical Center', 'medical', null, '450 Pelham Parkway South, Bronx, NY 10461', true, 'pol_hospital'),
  ('cr_kessington', 'Kessington Auto Finance', 'auto', null, null, false, 'pol_auto'),
  ('cr_marlow', 'Marlow Wireless', 'telecom', null, '1200 Commerce Plaza, Newark, NJ 07102', false, 'pol_phone'),
  ('cr_pellbrook', 'Pellbrook Bank', 'retail', 'Beckford Home Furnishings', null, false, 'pol_store');

insert into consumers (consumer_id, first_name, last_name, date_of_birth, ssn_last4, street, city, zip, deceased_on, estate_rep_name, estate_rep_phone) values
  ('con_261101', 'Marcus', 'Bell', '1979-02-04', '6620', '214 Bedford Ave, Apt 3C', 'Brooklyn', '11211', null, null, null),
  ('con_261102', 'Elena', 'Petrova', '1984-11-19', '2047', '84-12 37th Ave, Apt 2F', 'Queens', '11372', null, null, null),
  ('con_261103', 'Keisha', 'Grant', '1991-07-30', '7714', '1215 Teller Ave, Apt 4B', 'Bronx', '10456', null, null, null),
  ('con_261104', 'Tomás', 'Herrera', '1986-03-12', '5582', '932 Amsterdam Ave, Apt 5A', 'Manhattan', '10025', null, null, null),
  ('con_261105', 'Grace', 'Okafor', '1993-09-22', '3058', '131-15 Guy R Brewer Blvd', 'Queens', '11434', null, null, null),
  ('con_261106', 'Priya', 'Raman', '1990-01-08', '4426', '2140 Richmond Ave', 'Staten Island', '10314', null, null, null),
  ('con_261107', 'Daniel', 'Kim', '1975-05-03', '9035', '118 Henry St, Apt 2R', 'Brooklyn', '11201', null, null, null),
  ('con_261108', 'Dana', 'Ruiz', '1988-06-21', '3391', '3215 Bainbridge Ave, Apt 6D', 'Bronx', '10467', null, null, null),
  ('con_261109', 'Robert', 'Alvarez', '1982-10-27', '1189', '2680 Grand Concourse, Apt 3E', 'Bronx', '10458', null, null, null),
  ('con_261110', 'Andre', 'Williams', '1987-08-14', '6371', '9412 Flatlands Ave', 'Brooklyn', '11236', null, null, null),
  ('con_261111', 'Hannah', 'Cohen', '1981-12-09', '2963', '70-12 Austin St, Apt 4H', 'Queens', '11375', null, null, null),
  ('con_261112', 'Samuel', 'Osei', '1977-04-16', '8150', '512 W 145th St, Apt 1B', 'Manhattan', '10031', null, null, null);

insert into phones (phone_id, consumer_id, number, line_type, source, artificial_voice_consent, consent_revoked_at, status) values
  ('ph_261101_1', 'con_261101', '+13475550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261102_1', 'con_261102', '+19175550163', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261103_1', 'con_261103', '+16465550127', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261104_1', 'con_261104', '+13475550149', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261105_1', 'con_261105', '+17185550176', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261106_1', 'con_261106', '+19175550108', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261107_1', 'con_261107', '+17185550131', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261108_1', 'con_261108', '+17185550142', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261109_1', 'con_261109', '+13475550185', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261110_1', 'con_261110', '+17185550122', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261111_1', 'con_261111', '+16465550154', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_261112_1', 'con_261112', '+19175550137', 'mobile', 'consumer', true, null, 'valid');

insert into accounts (account_id, consumer_id, creditor_id, original_account_last4, placed_on, sol_expires_on, validation_notice_mailed_on, time_barred_notice_mailed_on, placed_balance_cents, pnm_order_identifier, balance_phone_cents, balance_service_cents, treatment_on, car, car_repossessed_on, car_sold_on, car_sale_price_cents, deficiency_explanation_sent_on, status) values
  ('TAS-261101', 'con_261101', 'cr_ashcombe', '8832', '2025-02-14', '2028-02-14', '2026-10-09', null, 240000, '922611010001', null, null, null, null, null, null, null, null, 'active'),
  ('TAS-261102', 'con_261102', 'cr_ashcombe', '5190', '2023-02-15', '2026-08-15', '2026-10-02', '2026-10-02', 118000, '922611020001', null, null, null, null, null, null, null, null, 'active'),
  ('TAS-261103', 'con_261103', 'cr_pellbrook', '3305', '2025-05-20', '2028-05-20', '2026-10-14', null, 130000, '922611030001', null, null, null, null, null, null, null, null, 'active'),
  ('TAS-261104', 'con_261104', 'cr_pellbrook', '6641', '2025-06-08', '2028-06-08', '2026-10-16', null, 90000, '922611040001', null, null, null, null, null, null, null, null, 'active'),
  ('TAS-261105', 'con_261105', 'cr_marlow', '9017', '2025-04-30', null, '2026-10-12', null, 105000, '922611050001', 78000, 27000, null, null, null, null, null, null, 'active'),
  ('TAS-261106', 'con_261106', 'cr_marlow', '2280', '2025-07-18', null, '2026-10-13', null, 64000, '922611060001', 40000, 24000, null, null, null, null, null, null, 'active'),
  ('TAS-261107', 'con_261107', 'cr_harlow', '7764', '2026-08-06', '2029-01-19', '2026-10-15', null, 41275, '922611070001', null, null, '2026-01-19', null, null, null, null, null, 'active'),
  ('TAS-261108', 'con_261108', 'cr_harlow', '4417', '2026-10-01', '2028-03-14', '2026-10-13', null, 128640, '922611080001', null, null, '2025-03-14', null, null, null, null, null, 'active'),
  ('TAS-261109', 'con_261109', 'cr_kessington', '0452', '2026-05-12', '2029-05-12', '2026-10-10', null, 480000, '922611090001', null, null, null, '2019 Honda Civic', '2026-04-02', '2026-05-12', 620000, '2026-05-20', 'active'),
  ('TAS-261110', 'con_261110', 'cr_kessington', '7720', '2026-03-03', '2029-03-03', '2026-10-11', null, 735000, '922611100001', null, null, null, '2017 Toyota Camry', '2026-02-10', '2026-03-03', 200000, '2026-03-12', 'active'),
  ('TAS-261111', 'con_261111', 'cr_pellbrook', '1158', '2025-09-02', '2028-09-02', '2026-10-06', null, 76000, '922611110001', null, null, null, null, null, null, null, null, 'active'),
  ('TAS-261112', 'con_261112', 'cr_marlow', '4693', '2025-03-11', null, '2026-10-08', null, 88000, '922611120001', 56000, 32000, null, null, null, null, null, null, 'active');

insert into contact_attempts (attempt_id, account_id, phone_id, at, medium, direction, answered_by, consumer_responded, duration_s, disposition, summary, agent_name) values
  ('att_261101_1', 'TAS-261101', null, '2026-10-09T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261101_2', 'TAS-261101', 'ph_261101_1', '2026-10-26T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261102_1', 'TAS-261102', null, '2026-10-02T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice with the time-barred statement mailed.', null),
  ('att_261102_2', 'TAS-261102', 'ph_261102_1', '2026-10-22T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261103_1', 'TAS-261103', null, '2026-10-14T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261104_1', 'TAS-261104', null, '2026-10-16T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261104_2', 'TAS-261104', 'ph_261104_1', '2026-10-27T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261105_1', 'TAS-261105', null, '2026-10-12T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261105_2', 'TAS-261105', 'ph_261105_1', '2026-10-24T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261106_1', 'TAS-261106', null, '2026-10-13T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261107_1', 'TAS-261107', null, '2026-10-15T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261108_1', 'TAS-261108', null, '2026-10-13T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261108_2', 'TAS-261108', 'ph_261108_1', '2026-10-27T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261109_1', 'TAS-261109', null, '2026-10-10T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261109_2', 'TAS-261109', 'ph_261109_1', '2026-10-25T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null),
  ('att_261110_1', 'TAS-261110', null, '2026-10-11T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261111_1', 'TAS-261111', null, '2026-10-06T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261111_2', 'TAS-261111', 'ph_261111_1', '2026-10-31T14:10:00-04:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Spoke with the consumer: she said she''d think about it; no callback asked for.', null),
  ('att_261112_1', 'TAS-261112', null, '2026-10-08T10:00:00-04:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_261112_2', 'TAS-261112', 'ph_261112_1', '2026-10-23T11:30:00-04:00', 'call', 'outbound', 'no_answer', false, 30, 'no_answer', 'No answer.', null);

-- account_flags: no rows

