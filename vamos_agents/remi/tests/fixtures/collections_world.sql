-- The VAmoS Debt Collection World's database, vendored for Remi's tests: schema, payment gateway
-- and rows, in the order the World is seeded (collections_bench/world/seed.py, seed_sql).
--
-- Source: veris-ai/utility-bench @ a43805a639c2cc0c107cb6a7a13b0c8f444995cd
--   collections_bench/generated/world/schema.sql
--   collections_bench/world/gateway.sql
--   collections_bench/generated/world/rows.sql
-- Test data, not a migration: Remi never imports utility-bench and never runs this at runtime.
--
-- Resync: vamos_agents/remi/tests/fixtures/resync_world.sh <utility-bench checkout> [ref]
-- (rewrites this file, header included), then run Remi's tests.

-- ===== collections_bench/generated/world/schema.sql =====
-- The creditors that place accounts with the agency.
create table creditors (
  creditor_id     text primary key,          -- 'cr_ashcombe'
  name            text not null,             -- 'Ashcombe Bank, N.A.'
  kind            text not null check (kind in ('bank_card','medical','telecom','retail')),
  mailing_address text,                      -- what "original creditor's address" answers; null if not on file
  financial_assistance_policy boolean not null default false  -- medical providers only
);

create table consumers (
  consumer_id   text primary key,            -- 'con_000123'
  first_name    text not null,
  last_name     text not null,
  date_of_birth date not null,               -- verification factor
  ssn_last4     char(4) not null,             -- verification factor
  street        text not null,
  city          text not null,               -- one of the five boroughs
  zip           char(5) not null,            -- verification factor
  language_preference text,                  -- null until recorded; 'en', 'es', ...
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
  creditor_id    text not null references creditors,
  original_account_last4 char(4) not null,
  placed_on      date not null,
  -- The money (charge-off date and balance, interest and fees since, payments,
  -- the current balance) is the Fineract loan whose externalId is account_id.
  sol_expires_on    date not null,           -- time-barred when < call date
  validation_notice_mailed_on   date not null,  -- always on/after 2027-01-01: NYC dispute rules apply
  time_barred_notice_mailed_on  date,        -- null if never mailed
  settlement_floor_pct  integer not null default 70,
  max_plan_months       integer not null default 12,
  min_instalment_cents  integer not null default 2500,
  -- Why collection stands where it does. Paid and settled are not here: they are
  -- money, and the Fineract loan's status says so.
  status  text not null default 'active' check (status in
          ('active','disputed','cease','attorney','bankruptcy','deceased','recalled'))
);

-- A card or bank account tokenized on a call. Written only by the gateway
-- functions. Never a full card number, security code or full bank account number.
create table payment_methods (
  method_id    text primary key,             -- 'pm_…'
  account_id   text not null references accounts,
  kind         text not null check (kind in ('card','ach')),
  brand        text check (brand in ('visa','mastercard','amex','discover','other')),  -- card only
  last4        char(4) not null,             -- card number's or bank account number's last four
  exp_month    integer check (exp_month between 1 and 12),  -- card only
  exp_year     integer check (exp_year between 2000 and 2099),  -- card only, four digits
  holder_name  text not null,
  created_at   timestamptz not null,         -- the `at` the tokenizing call passed
  check ((kind = 'card') = (brand is not null and exp_month is not null and exp_year is not null)),
  check (kind = 'card' or (brand is null and exp_month is null and exp_year is null))
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
  consumer_responded boolean not null default false,  -- drives the NYC 7-day rule
  duration_s   integer,
  disposition  text,                         -- see "Dispositions" below
  summary      text,                         -- plain language, NYC § 2-193
  agent_name   text
);

-- One row per dispute, cease, opt-out, attorney, bankruptcy, death, SCRA,
-- hardship, original-creditor request, medical financial-assistance claim, or
-- stop request from a person who did not verify.
create table account_flags (
  flag_id     text primary key,
  account_id  text not null references accounts,
  kind        text not null check (kind in
              ('dispute','original_creditor_request','cease','channel_optout',
               'attorney','bankruptcy','deceased','scra','hardship',
               'medical_financial_assistance','unverified_stop_request')),
  details     jsonb not null default '{}',   -- see "Flag details" below
  created_at  timestamptz not null
);

create table arrangements (
  arrangement_id text primary key,
  account_id     text not null references accounts,
  kind           text not null check (kind in ('pay_in_full','settlement_lump','settlement_instalments','plan','hardship_plan')),
  total_cents    integer not null,
  instalments    jsonb not null,             -- [{"due_on":"2027-03-01","amount_cents":12500}]
  method_id      text references payment_methods,  -- the card or bank account the instalments are charged to
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

-- Every charge attempt, approved or declined. Written only by gateway_charge.
create table payments (
  payment_id    text primary key,            -- 'pay_…'
  account_id    text not null references accounts,
  method_id     text not null references payment_methods,
  amount_cents  integer not null check (amount_cents > 0),
  status        text not null check (status in ('approved','declined')),
  decline_code  text check (decline_code in ('insufficient_funds','card_declined','expired_card')),  -- null when approved
  processor_ref text not null,               -- 'rp_…', the processor's reference for the charge
  at            timestamptz not null,
  check ((status = 'approved') = (decline_code is null))
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
  callback_contact    text not null            -- natural person named in voicemails
);

-- ===== collections_bench/world/gateway.sql =====
-- The Tallmere payment gateway: the collections system's integrated processor
-- (WORLD.md, "The payment gateway"). Loaded after schema.sql, as the database
-- owner. The three public functions are SECURITY DEFINER: they run as the owner,
-- and they are the only writers of payment_methods, payments and public.processor_vault.
-- Every name is schema-qualified and each function pins its search_path, so no
-- object a candidate creates can stand in for one of these.

-- One row per saved instrument: one-way hashes, never a card or account number.
create table public.processor_vault (
  method_id   text primary key references public.payment_methods,
  fingerprint text not null,                 -- sha256 hex of 'card:<pan>' or 'ach:<routing>:<account>': the same instrument
  outcome_key text not null                  -- sha256 hex of 'card:<pan>' or 'ach:<account>': what decides the outcome
);

-- --- helpers --------------------------------------------------------------------

create function public.processor_fingerprint(instrument text) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$ select encode(sha256(convert_to(instrument, 'UTF8')), 'hex') $$;

-- The outcome the processor returns for an instrument, by its outcome_key: null
-- approves, else the decline_code. Stripe's published test numbers, so a case
-- stays recognizable.
create function public.processor_outcome_for(outcome_key text) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$
  select case outcome_key
    when public.processor_fingerprint('card:4000000000009995') then 'insufficient_funds'
    when public.processor_fingerprint('card:4000000000000002') then 'card_declined'
    when public.processor_fingerprint('card:4000000000000069') then 'expired_card'
    when public.processor_fingerprint('ach:000222222227') then 'insufficient_funds'
  end
$$;

create function public.processor_luhn_ok(digits text) returns boolean
language sql immutable set search_path = pg_catalog, pg_temp
as $$
  select coalesce(sum(case when (length(digits) - i) % 2 = 1
                           then (d * 2) / 10 + (d * 2) % 10
                           else d end) % 10 = 0, false)
  from (select i, substr(digits, i, 1)::integer as d from generate_series(1, length(digits)) as i) as s
$$;

create function public.processor_aba_ok(routing text) returns boolean
language sql immutable set search_path = pg_catalog, pg_temp
as $$
  select (3 * (d[1] + d[4] + d[7]) + 7 * (d[2] + d[5] + d[8]) + (d[3] + d[6] + d[9])) % 10 = 0
  from (select array(select substr(routing, i, 1)::integer from generate_series(1, 9) as i) as d) as s
$$;

create function public.processor_card_brand(pan text) returns text
language sql immutable set search_path = pg_catalog, pg_temp
as $$
  select case
    when pan ~ '^4' then 'visa'
    when pan ~ '^(5[1-5]|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720)' then 'mastercard'
    when pan ~ '^3[47]' then 'amex'
    when pan ~ '^(6011|65|64[4-9])' then 'discover'
    else 'other'
  end
$$;

create function public.processor_reject(message text) returns void
language plpgsql set search_path = pg_catalog, pg_temp
as $$ begin raise exception 'gateway: %', message using errcode = '22023'; end $$;

create function public.processor_new_id(prefix text) returns text
language sql volatile set search_path = pg_catalog, pg_temp
as $$ select prefix || substr(replace(gen_random_uuid()::text, '-', ''), 1, 24) $$;

-- --- write guards ---------------------------------------------------------------

-- Refuses any write that does not run as the table's owner. Inside a gateway
-- function (SECURITY DEFINER) current_user is the owner; a candidate's own
-- statement runs as its login role, which owns nothing.
create function public.processor_only_the_gateway_writes() returns trigger
language plpgsql set search_path = pg_catalog, pg_temp
as $$
begin
  if current_user <> (select pg_get_userbyid(c.relowner) from pg_class c where c.oid = tg_relid) then
    raise exception 'gateway: %.% is written only by the gateway functions', tg_table_schema, tg_table_name
      using errcode = '42501',
            hint = 'Use gateway_tokenize_card, gateway_tokenize_ach or gateway_charge.';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

-- A gateway function runs as the owner, so any trigger on a table it writes would
-- run as the owner too. The twin grants a candidate's role TRIGGER on every
-- table, so the gateway refuses to run while any trigger it did not install
-- (internal foreign-key triggers aside) sits on accounts, payment_methods,
-- payments or the vault: no candidate code ever runs with the owner's rights.
create function public.processor_refuse_foreign_triggers() returns void
language plpgsql set search_path = pg_catalog, pg_temp
as $$
declare
  foreign_trigger text;
begin
  select format('%s on %s', t.tgname, t.tgrelid::regclass) into foreign_trigger
    from pg_trigger t
   where t.tgrelid in ('public.accounts'::regclass, 'public.payment_methods'::regclass,
                       'public.payments'::regclass, 'public.processor_vault'::regclass)
     and not t.tgisinternal
     and t.tgname not in ('only_the_gateway_writes', 'only_the_gateway_truncates')
   limit 1;
  if foreign_trigger is not null then
    raise exception 'gateway: refusing to run: trigger % was not installed by the gateway', foreign_trigger
      using errcode = '42501';
  end if;
end
$$;

create trigger only_the_gateway_writes before insert or update or delete on public.payment_methods
  for each row execute function public.processor_only_the_gateway_writes();
create trigger only_the_gateway_truncates before truncate on public.payment_methods
  for each statement execute function public.processor_only_the_gateway_writes();
create trigger only_the_gateway_writes before insert or update or delete on public.payments
  for each row execute function public.processor_only_the_gateway_writes();
create trigger only_the_gateway_truncates before truncate on public.payments
  for each statement execute function public.processor_only_the_gateway_writes();
create trigger only_the_gateway_writes before insert or update or delete on public.processor_vault
  for each row execute function public.processor_only_the_gateway_writes();
create trigger only_the_gateway_truncates before truncate on public.processor_vault
  for each statement execute function public.processor_only_the_gateway_writes();

-- --- the gateway ----------------------------------------------------------------

-- Saves a card on the account and answers its method_id, or the existing one when
-- this card is already saved there. The security code is checked and discarded.
create function public.gateway_tokenize_card(
  account_id text, pan text, exp_month integer, exp_year integer, cvc text, holder_name text, at timestamptz
) returns table (method_id text, brand text, last4 text)
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
#variable_conflict use_variable
declare
  digits text := regexp_replace(coalesce(pan, ''), '[\s-]', '', 'g');
  year4 integer := case when exp_year between 0 and 99 then 2000 + exp_year else exp_year end;
  card_brand text;
  cvc_digits integer;
  fp text;
  found_id text;
  new_id text;
begin
  perform public.processor_refuse_foreign_triggers();
  if at is null then
    perform public.processor_reject('at (the call''s time) is required');
  end if;
  if not exists (select 1 from public.accounts a where a.account_id = account_id) then
    perform public.processor_reject(format('no account %s', account_id));
  end if;
  if nullif(btrim(holder_name), '') is null then
    perform public.processor_reject('the cardholder''s name is required');
  end if;
  if digits !~ '^[0-9]{13,19}$' then
    perform public.processor_reject('the card number must be 13 to 19 digits');
  end if;
  if not public.processor_luhn_ok(digits) then
    perform public.processor_reject('the card number is not valid (it fails the Luhn check)');
  end if;
  if exp_month is null or exp_month not between 1 and 12 then
    perform public.processor_reject('the expiry month must be 1 to 12');
  end if;
  if year4 is null or year4 not between 2000 and 2099 then
    perform public.processor_reject('the expiry year must be two or four digits');
  end if;
  if (make_date(year4, exp_month, 1) + interval '1 month')::date <= (at at time zone 'America/New_York')::date then
    perform public.processor_reject(format('the card expired at the end of %s/%s', lpad(exp_month::text, 2, '0'), year4));
  end if;
  card_brand := public.processor_card_brand(digits);
  cvc_digits := case when card_brand = 'amex' then 4 else 3 end;
  if coalesce(cvc, '') !~ ('^[0-9]{' || cvc_digits || '}$') then
    perform public.processor_reject(format('the security code must be %s digits', cvc_digits));
  end if;

  fp := public.processor_fingerprint('card:' || digits);
  select pm.method_id into found_id
    from public.payment_methods pm join public.processor_vault v on v.method_id = pm.method_id
   where pm.account_id = account_id and v.fingerprint = fp
     and pm.exp_month = exp_month and pm.exp_year = year4;
  if found_id is not null then
    return query select found_id, card_brand, right(digits, 4);
    return;
  end if;

  new_id := public.processor_new_id('pm_');
  insert into public.payment_methods (method_id, account_id, kind, brand, last4, exp_month, exp_year, holder_name, created_at)
  values (new_id, account_id, 'card', card_brand, right(digits, 4), exp_month, year4, btrim(holder_name), at);
  insert into public.processor_vault (method_id, fingerprint, outcome_key) values (new_id, fp, fp);
  return query select new_id, card_brand, right(digits, 4);
end
$$;

-- Saves a US bank account (ACH) on the account and answers its method_id, or the
-- existing one when this account is already saved there.
create function public.gateway_tokenize_ach(
  account_id text, routing_number text, account_number text, holder_name text, at timestamptz
) returns table (method_id text, last4 text)
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
#variable_conflict use_variable
declare
  routing text := regexp_replace(coalesce(routing_number, ''), '[\s-]', '', 'g');
  acct text := regexp_replace(coalesce(account_number, ''), '[\s-]', '', 'g');
  fp text;
  found_id text;
  new_id text;
begin
  perform public.processor_refuse_foreign_triggers();
  if at is null then
    perform public.processor_reject('at (the call''s time) is required');
  end if;
  if not exists (select 1 from public.accounts a where a.account_id = account_id) then
    perform public.processor_reject(format('no account %s', account_id));
  end if;
  if nullif(btrim(holder_name), '') is null then
    perform public.processor_reject('the account holder''s name is required');
  end if;
  if routing !~ '^[0-9]{9}$' then
    perform public.processor_reject('the routing number must be 9 digits');
  end if;
  if not public.processor_aba_ok(routing) then
    perform public.processor_reject('the routing number is not valid (it fails the ABA checksum)');
  end if;
  if acct !~ '^[0-9]{4,17}$' then
    perform public.processor_reject('the bank account number must be 4 to 17 digits');
  end if;

  fp := public.processor_fingerprint('ach:' || routing || ':' || acct);
  select pm.method_id into found_id
    from public.payment_methods pm join public.processor_vault v on v.method_id = pm.method_id
   where pm.account_id = account_id and v.fingerprint = fp;
  if found_id is not null then
    return query select found_id, right(acct, 4);
    return;
  end if;

  new_id := public.processor_new_id('pm_');
  insert into public.payment_methods (method_id, account_id, kind, brand, last4, exp_month, exp_year, holder_name, created_at)
  values (new_id, account_id, 'ach', null, right(acct, 4), null, null, btrim(holder_name), at);
  insert into public.processor_vault (method_id, fingerprint, outcome_key)
  values (new_id, fp, public.processor_fingerprint('ach:' || acct));
  return query select new_id, right(acct, 4);
end
$$;

-- Charges a saved method once and records it in payments, approved or declined.
-- The processor decides the outcome. It does not know the agency's balance and
-- writes nothing outside its own tables: posting an approved charge to the
-- account's Fineract loan is the caller's next step (WORLD.md).
create function public.gateway_charge(method_id text, amount_cents integer, at timestamptz)
returns table (payment_id text, status text, decline_code text, processor_ref text)
language plpgsql security definer set search_path = pg_catalog, pg_temp
as $$
#variable_conflict use_variable
declare
  owner_account text;
  outcome text;
  new_payment text := public.processor_new_id('pay_');
  new_ref text := public.processor_new_id('rp_');
  result_status text;
begin
  perform public.processor_refuse_foreign_triggers();
  if at is null then
    perform public.processor_reject('at (the call''s time) is required');
  end if;
  select pm.account_id, public.processor_outcome_for(v.outcome_key) into owner_account, outcome
    from public.payment_methods pm join public.processor_vault v on v.method_id = pm.method_id
   where pm.method_id = method_id;
  if owner_account is null then
    perform public.processor_reject(format('no payment method %s', coalesce(method_id, 'null')));
  end if;
  if amount_cents is null or amount_cents <= 0 then
    perform public.processor_reject('the amount must be more than zero');
  end if;

  result_status := case when outcome is null then 'approved' else 'declined' end;
  insert into public.payments (payment_id, account_id, method_id, amount_cents, status, decline_code, processor_ref, at)
  values (new_payment, owner_account, method_id, amount_cents, result_status, outcome, new_ref, at);
  return query select new_payment, result_status, outcome, new_ref;
end
$$;

-- ===== collections_bench/generated/world/rows.sql =====
-- Generated by `python -m collections_bench.world.seed --write` from manifest sha256:4fae8ab4d23a946ab0edeaa910f514c8afca410fdcc261fe431d6bbc5f9abfa4.
-- Do not edit; change collections_bench/tasks.py and regenerate.
insert into agency (name, dcwp_license_number, callback_number, callback_contact) values
  ('Tallmere Account Services, LLC', '2091843-DCA', '+12125550100', 'Dana Whitlock');

insert into creditors (creditor_id, name, kind, mailing_address, financial_assistance_policy) values
  ('cr_ashcombe', 'Ashcombe Bank, N.A.', 'bank_card', 'PO Box 4410, Wilmington, DE 19850', false),
  ('cr_beckford', 'Beckford Home Furnishings', 'retail', null, false),
  ('cr_corley', 'Corley Street Emergency Physicians, P.C.', 'medical', 'PO Box 7731, Brooklyn, NY 11207', false),
  ('cr_fenwick', 'Fenwick Hollow Hospital', 'medical', '88-20 Fenwick Hollow Road, Queens, NY 11432', true),
  ('cr_harlow', 'Harlow Point Medical Center', 'medical', '450 Pelham Parkway South, Bronx, NY 10461', true),
  ('cr_marlow', 'Marlow Wireless, Inc.', 'telecom', '1200 Commerce Plaza, Newark, NJ 07102', false);

insert into consumers (consumer_id, first_name, last_name, date_of_birth, ssn_last4, street, city, zip, language_preference, deceased_on, estate_rep_name, estate_rep_phone) values
  ('con_420101', 'Aaliyah', 'Alvarez', '1958-01-01', '2234', '100 7th Ave', 'Brooklyn', '11215', null, null, null, null),
  ('con_420102', 'Andre', 'Baptiste', '1965-06-12', '1153', '137 Whitney Ave, Apt 2B', 'Queens', '11373', 'en', null, null, null),
  ('con_420103', 'Bianca', 'Chen', '1972-11-23', '9072', '174 Grand Concourse, Apt 3C', 'Bronx', '10458', null, null, null, null),
  ('con_420104', 'Boris', 'Delgado', '1979-04-06', '7991', '211 W 104th St', 'Manhattan', '10025', 'en', null, null, null),
  ('con_420105', 'Carmen', 'Edwards', '1986-09-17', '6910', '248 Victory Blvd, Apt 5E', 'Staten Island', '10301', null, null, null, null),
  ('con_420106', 'Carlos', 'Fernandez', '1993-02-28', '5829', '285 Putnam Ave, Apt 6F', 'Brooklyn', '11221', 'en', null, null, null),
  ('con_420107', 'Deborah', 'Goldberg', '1958-07-11', '4748', '322 Main St', 'Queens', '11354', null, null, null, null),
  ('con_420108', 'Darnell', 'Haddad', '1965-12-22', '3667', '359 Riverdale Ave, Apt 2B', 'Bronx', '10463', 'en', null, null, null),
  ('con_420109', 'Esther', 'Jackson', '1972-05-05', '2586', '396 Hamilton Pl, Apt 3C', 'Manhattan', '10031', null, null, null, null),
  ('con_420110', 'Elijah', 'Kim', '1979-10-16', '1505', '433 Ocean Ave', 'Brooklyn', '11226', 'en', null, null, null),
  ('con_420111', 'Fatima', 'Lombardi', '1986-03-27', '9424', '470 31st St, Apt 5E', 'Queens', '11103', null, null, null, null),
  ('con_420112', 'Felix', 'Mendoza', '1993-08-10', '8343', '507 Bainbridge Ave, Apt 6F', 'Bronx', '10467', 'en', null, null, null),
  ('con_420113', 'Gloria', 'Nwosu', '1958-01-21', '7262', '544 Grand St', 'Manhattan', '10002', null, null, null, null),
  ('con_420114', 'Gabriel', 'O''Brien', '1965-06-04', '6181', '581 3rd Ave, Apt 2B', 'Brooklyn', '11209', 'en', null, null, null),
  ('con_420115', 'Hannah', 'Patel', '1972-11-15', '5100', '618 Hillside Ave, Apt 3C', 'Queens', '11432', null, null, null, null),
  ('con_420116', 'Hector', 'Quinones', '1979-04-26', '4019', '655 2nd Ave', 'Manhattan', '10128', 'en', null, null, null),
  ('con_420117', 'Ingrid', 'Rivera', '1986-09-09', '2938', '692 Richmond Ave, Apt 5E', 'Staten Island', '10314', null, null, null, null),
  ('con_420118', 'Ibrahim', 'Sokolov', '1993-02-20', '1857', '729 5th St, Apt 6F', 'Brooklyn', '11215', 'en', null, null, null),
  ('con_420119', 'Jasmine', 'Thompson', '1958-07-03', '9776', '766 Broadway', 'Queens', '11373', null, null, null, null),
  ('con_420120', 'Jamal', 'Usman', '1965-12-14', '8695', '803 Bedford Park Blvd, Apt 2B', 'Bronx', '10458', 'en', null, null, null),
  ('con_420121', 'Keisha', 'Vasquez', '1972-05-25', '7614', '840 Amsterdam Ave, Apt 3C', 'Manhattan', '10025', null, null, null, null),
  ('con_420122', 'Kevin', 'Washington', '1979-10-08', '6533', '877 Bay St', 'Staten Island', '10301', 'en', null, null, null),
  ('con_420123', 'Lucia', 'Yilmaz', '1986-03-19', '5452', '914 Halsey St, Apt 5E', 'Brooklyn', '11221', null, null, null, null),
  ('con_420124', 'Luis', 'Zhang', '1993-08-02', '4371', '951 Union St, Apt 6F', 'Queens', '11354', 'en', null, null, null),
  ('con_420125', 'Mei', 'Morales', '1958-01-13', '3290', '988 Johnson Ave', 'Bronx', '10463', null, null, null, null),
  ('con_420126', 'Marcus', 'Greene', '1965-06-24', '2209', '1025 W 145th St, Apt 2B', 'Manhattan', '10031', 'en', null, null, null),
  ('con_420127', 'Nadia', 'Okafor', '1972-11-07', '1128', '1062 Flatbush Ave, Apt 3C', 'Brooklyn', '11226', null, null, null, null),
  ('con_420128', 'Nikolai', 'Castillo', '1979-04-18', '9047', '1099 Ditmars Blvd', 'Queens', '11103', 'en', null, null, null),
  ('con_420129', 'Olga', 'Russo', '1986-09-01', '7966', '1136 Gun Hill Rd, Apt 5E', 'Bronx', '10467', null, null, null, null),
  ('con_420130', 'Omar', 'Brooks', '1993-02-12', '6885', '1173 Clinton St, Apt 6F', 'Manhattan', '10002', 'en', null, null, null),
  ('con_420131', 'Priya', 'Santos', '1958-07-23', '5804', '1210 Ridge Blvd', 'Brooklyn', '11209', null, null, null, null),
  ('con_420132', 'Pedro', 'Abramowitz', '1965-12-06', '4723', '1247 164th St, Apt 2B', 'Queens', '11432', 'en', null, null, null),
  ('con_420133', 'Rosa', 'Alvarez', '1972-05-17', '3642', '1284 E 92nd St, Apt 3C', 'Manhattan', '10128', null, null, null, null),
  ('con_420134', 'Rashid', 'Baptiste', '1979-10-28', '2561', '1321 Forest Hill Rd', 'Staten Island', '10314', 'en', null, null, null),
  ('con_420135', 'Shanice', 'Chen', '1986-03-11', '1480', '1358 Prospect Park West, Apt 5E', 'Brooklyn', '11215', null, null, null, null),
  ('con_420136', 'Samuel', 'Delgado', '1993-08-22', '9399', '1395 Whitney Ave, Apt 6F', 'Queens', '11373', 'en', null, null, null),
  ('con_420137', 'Tamar', 'Edwards', '1958-01-05', '8318', '1432 Grand Concourse', 'Bronx', '10458', null, null, null, null),
  ('con_420138', 'Tomas', 'Fernandez', '1965-06-16', '7237', '1469 W 104th St, Apt 2B', 'Manhattan', '10025', 'en', null, null, null),
  ('con_420139', 'Valentina', 'Goldberg', '1972-11-27', '6156', '1506 Victory Blvd, Apt 3C', 'Staten Island', '10301', null, null, null, null),
  ('con_420140', 'Victor', 'Haddad', '1979-04-10', '5075', '1543 Putnam Ave', 'Brooklyn', '11221', 'en', null, null, null),
  ('con_420141', 'Wendy', 'Jackson', '1986-09-21', '3994', '1580 Main St, Apt 5E', 'Queens', '11354', null, null, null, null),
  ('con_420142', 'Wei', 'Kim', '1993-02-04', '2913', '1617 Riverdale Ave, Apt 6F', 'Bronx', '10463', 'en', null, null, null),
  ('con_420143', 'Yolanda', 'Lombardi', '1958-07-15', '1832', '1654 Hamilton Pl', 'Manhattan', '10031', null, null, null, null),
  ('con_420144', 'Xavier', 'Mendoza', '1965-12-26', '9751', '1691 Ocean Ave, Apt 2B', 'Brooklyn', '11226', 'en', null, null, null),
  ('con_420145', 'Zoe', 'Nwosu', '1972-05-09', '8670', '1728 31st St, Apt 3C', 'Queens', '11103', null, null, null, null),
  ('con_420146', 'Yusuf', 'O''Brien', '1979-10-20', '7589', '1765 Bainbridge Ave', 'Bronx', '10467', 'en', null, null, null),
  ('con_420147', 'Imani', 'Patel', '1986-03-03', '6508', '1802 Grand St, Apt 5E', 'Manhattan', '10002', null, null, null, null),
  ('con_420148', 'Zachary', 'Quinones', '1993-08-14', '5427', '1839 3rd Ave, Apt 6F', 'Brooklyn', '11209', 'en', null, null, null),
  ('con_420149', 'Grace', 'Rivera', '1958-01-25', '4346', '1876 Hillside Ave', 'Queens', '11432', null, null, null, null),
  ('con_420150', 'Desmond', 'Sokolov', '1965-06-08', '3265', '113 2nd Ave, Apt 2B', 'Manhattan', '10128', 'en', null, null, null),
  ('con_420151', 'Leah', 'Thompson', '1972-11-19', '2184', '150 Richmond Ave, Apt 3C', 'Staten Island', '10314', null, null, null, null),
  ('con_420152', 'Raymond', 'Usman', '1979-04-02', '1103', '187 7th Ave', 'Brooklyn', '11215', 'en', null, null, null),
  ('con_420153', 'Marisol', 'Vasquez', '1986-09-13', '9022', '224 Broadway, Apt 5E', 'Queens', '11373', null, null, null, null),
  ('con_420154', 'Anthony', 'Washington', '1993-02-24', '7941', '261 Bedford Park Blvd, Apt 6F', 'Bronx', '10458', 'en', null, null, null),
  ('con_420155', 'Renee', 'Yilmaz', '1958-07-07', '6860', '298 Amsterdam Ave', 'Manhattan', '10025', null, null, null, null),
  ('con_420156', 'Michael', 'Zhang', '1965-12-18', '5779', '335 Bay St, Apt 2B', 'Staten Island', '10301', 'en', null, null, null),
  ('con_420157', 'Aaliyah', 'Morales', '1972-05-01', '4698', '372 Halsey St, Apt 3C', 'Brooklyn', '11221', null, null, null, null),
  ('con_420158', 'Andre', 'Greene', '1979-10-12', '3617', '409 Union St', 'Queens', '11354', 'en', null, null, null),
  ('con_420159', 'Bianca', 'Okafor', '1986-03-23', '2536', '446 Johnson Ave, Apt 5E', 'Bronx', '10463', null, null, null, null),
  ('con_420160', 'Boris', 'Castillo', '1993-08-06', '1455', '483 W 145th St, Apt 6F', 'Manhattan', '10031', 'en', null, null, null),
  ('con_420161', 'Carmen', 'Russo', '1958-01-17', '9374', '520 Flatbush Ave', 'Brooklyn', '11226', null, null, null, null),
  ('con_420162', 'Carlos', 'Brooks', '1965-06-28', '8293', '557 Ditmars Blvd, Apt 2B', 'Queens', '11103', 'en', null, null, null),
  ('con_420163', 'Deborah', 'Santos', '1972-11-11', '7212', '594 Gun Hill Rd, Apt 3C', 'Bronx', '10467', null, null, null, null),
  ('con_420164', 'Darnell', 'Abramowitz', '1979-04-22', '6131', '631 Clinton St', 'Manhattan', '10002', 'en', null, null, null),
  ('con_420165', 'Esther', 'Alvarez', '1986-09-05', '5050', '668 Ridge Blvd, Apt 5E', 'Brooklyn', '11209', null, null, null, null),
  ('con_420166', 'Elijah', 'Baptiste', '1993-02-16', '3969', '705 164th St, Apt 6F', 'Queens', '11432', 'en', null, null, null),
  ('con_420167', 'Fatima', 'Chen', '1958-07-27', '2888', '742 E 92nd St', 'Manhattan', '10128', null, null, null, null),
  ('con_420168', 'Felix', 'Delgado', '1965-12-10', '1807', '779 Forest Hill Rd, Apt 2B', 'Staten Island', '10314', 'en', null, null, null),
  ('con_420169', 'Gloria', 'Edwards', '1972-05-21', '9726', '816 5th St, Apt 3C', 'Brooklyn', '11215', null, null, null, null),
  ('con_420170', 'Gabriel', 'Fernandez', '1979-10-04', '8645', '853 Whitney Ave', 'Queens', '11373', 'en', null, null, null),
  ('con_420171', 'Hannah', 'Goldberg', '1986-03-15', '7564', '890 Grand Concourse, Apt 5E', 'Bronx', '10458', null, null, null, null),
  ('con_420172', 'Hector', 'Haddad', '1993-08-26', '6483', '927 W 104th St, Apt 6F', 'Manhattan', '10025', 'en', null, null, null),
  ('con_420173', 'Ingrid', 'Jackson', '1958-01-09', '5402', '964 Victory Blvd', 'Staten Island', '10301', null, null, null, null),
  ('con_420174', 'Ibrahim', 'Kim', '1965-06-20', '4321', '1001 Putnam Ave, Apt 2B', 'Brooklyn', '11221', 'en', null, null, null),
  ('con_420175', 'Jasmine', 'Lombardi', '1972-11-03', '3240', '1038 Main St, Apt 3C', 'Queens', '11354', null, null, null, null),
  ('con_420176', 'Jamal', 'Mendoza', '1979-04-14', '2159', '1075 Riverdale Ave', 'Bronx', '10463', 'en', null, null, null),
  ('con_420177', 'Keisha', 'Nwosu', '1986-09-25', '1078', '1112 Hamilton Pl, Apt 5E', 'Manhattan', '10031', null, null, null, null),
  ('con_420178', 'Kevin', 'O''Brien', '1993-02-08', '8997', '1149 Ocean Ave, Apt 6F', 'Brooklyn', '11226', 'en', null, null, null),
  ('con_420179', 'Lucia', 'Patel', '1958-07-19', '7916', '1186 31st St', 'Queens', '11103', null, null, null, null),
  ('con_420180', 'Luis', 'Quinones', '1965-12-02', '6835', '1223 Bainbridge Ave, Apt 2B', 'Bronx', '10467', 'en', null, null, null),
  ('con_420181', 'Mei', 'Rivera', '1972-05-13', '5754', '1260 Grand St, Apt 3C', 'Manhattan', '10002', null, null, null, null),
  ('con_420182', 'Marcus', 'Sokolov', '1979-10-24', '4673', '1297 3rd Ave', 'Brooklyn', '11209', 'en', null, null, null),
  ('con_420183', 'Nadia', 'Thompson', '1986-03-07', '3592', '1334 Hillside Ave, Apt 5E', 'Queens', '11432', null, null, null, null),
  ('con_420184', 'Nikolai', 'Usman', '1993-08-18', '2511', '1371 2nd Ave, Apt 6F', 'Manhattan', '10128', 'en', null, null, null),
  ('con_420185', 'Olga', 'Vasquez', '1958-01-01', '1430', '1408 Richmond Ave', 'Staten Island', '10314', null, null, null, null),
  ('con_420186', 'Omar', 'Washington', '1965-06-12', '9349', '1445 Prospect Park West, Apt 2B', 'Brooklyn', '11215', 'en', null, null, null),
  ('con_420187', 'Priya', 'Yilmaz', '1972-11-23', '8268', '1482 Broadway, Apt 3C', 'Queens', '11373', null, null, 'Simone Achterberg', '+16465550112'),
  ('con_420188', 'Pedro', 'Zhang', '1979-04-06', '7187', '1519 Bedford Park Blvd', 'Bronx', '10458', 'en', null, null, null),
  ('con_420189', 'Rosa', 'Morales', '1986-09-17', '6106', '1556 Amsterdam Ave, Apt 5E', 'Manhattan', '10025', null, null, null, null),
  ('con_420190', 'Rashid', 'Greene', '1993-02-28', '5025', '1593 Bay St, Apt 6F', 'Staten Island', '10301', 'en', null, null, null),
  ('con_420191', 'Shanice', 'Okafor', '1958-07-11', '3944', '1630 Halsey St', 'Brooklyn', '11221', null, null, null, null),
  ('con_420192', 'Samuel', 'Castillo', '1965-12-22', '2863', '1667 Union St, Apt 2B', 'Queens', '11354', 'en', null, null, null),
  ('con_420193', 'Tamar', 'Russo', '1972-05-05', '1782', '1704 Johnson Ave, Apt 3C', 'Bronx', '10463', null, null, null, null),
  ('con_420194', 'Tomas', 'Brooks', '1979-10-16', '9701', '1741 W 145th St', 'Manhattan', '10031', 'en', null, null, null),
  ('con_420195', 'Altagracia', 'Rosario', '1986-03-27', '8620', '1778 Flatbush Ave, Apt 5E', 'Brooklyn', '11226', null, null, null, null),
  ('con_420196', 'Victor', 'Abramowitz', '1993-08-10', '7539', '1815 Ditmars Blvd, Apt 6F', 'Queens', '11103', 'en', null, null, null),
  ('con_420197', 'Fang', 'Liu', '1958-01-21', '6458', '1852 Gun Hill Rd', 'Bronx', '10467', null, null, null, null),
  ('con_420198', 'Wei', 'Baptiste', '1965-06-04', '5377', '1889 Clinton St, Apt 2B', 'Manhattan', '10002', 'en', null, null, null),
  ('con_420199', 'Yolanda', 'Chen', '1972-11-15', '4296', '126 Ridge Blvd, Apt 3C', 'Brooklyn', '11209', null, null, null, null),
  ('con_420200', 'Xavier', 'Delgado', '1979-04-26', '3215', '163 164th St', 'Queens', '11432', 'en', null, null, null),
  ('con_420201', 'Zoe', 'Edwards', '1986-09-09', '2134', '200 E 92nd St, Apt 5E', 'Manhattan', '10128', null, null, null, null),
  ('con_420202', 'Yusuf', 'Fernandez', '1993-02-20', '1053', '237 Forest Hill Rd, Apt 6F', 'Staten Island', '10314', 'en', null, null, null),
  ('con_420203', 'Imani', 'Goldberg', '1958-07-03', '8972', '274 7th Ave', 'Brooklyn', '11215', 'en', null, null, null),
  ('con_420204', 'Zachary', 'Haddad', '1965-12-14', '7891', '311 Whitney Ave, Apt 2B', 'Queens', '11373', 'en', null, null, null),
  ('con_420205', 'Grace', 'Jackson', '1972-05-25', '6810', '348 Grand Concourse, Apt 3C', 'Bronx', '10458', 'en', null, null, null),
  ('con_420206', 'Desmond', 'Kim', '1979-10-08', '5729', '385 W 104th St', 'Manhattan', '10025', 'en', null, null, null),
  ('con_420207', 'Leah', 'Lombardi', '1986-03-19', '4648', '422 Victory Blvd, Apt 5E', 'Staten Island', '10301', 'en', null, null, null),
  ('con_420208', 'Raymond', 'Mendoza', '1993-08-02', '3567', '459 Putnam Ave, Apt 6F', 'Brooklyn', '11221', 'en', null, null, null),
  ('con_420209', 'Marisol', 'Nwosu', '1958-01-13', '2486', '496 Main St', 'Queens', '11354', null, null, null, null),
  ('con_420210', 'Anthony', 'O''Brien', '1965-06-24', '1405', '533 Riverdale Ave, Apt 2B', 'Bronx', '10463', 'en', null, null, null),
  ('con_420211', 'Renee', 'Patel', '1972-11-07', '9324', '570 Hamilton Pl, Apt 3C', 'Manhattan', '10031', null, null, null, null),
  ('con_420212', 'Michael', 'Quinones', '1979-04-18', '8243', '607 Ocean Ave', 'Brooklyn', '11226', 'en', null, null, null),
  ('con_420213', 'Aaliyah', 'Rivera', '1986-09-01', '7162', '644 31st St, Apt 5E', 'Queens', '11103', null, null, null, null),
  ('con_420214', 'Andre', 'Sokolov', '1993-02-12', '6081', '681 Bainbridge Ave, Apt 6F', 'Bronx', '10467', 'en', null, null, null),
  ('con_420215', 'Bianca', 'Thompson', '1958-07-23', '5000', '718 Grand St', 'Manhattan', '10002', null, null, null, null),
  ('con_420216', 'Boris', 'Usman', '1965-12-06', '3919', '755 3rd Ave, Apt 2B', 'Brooklyn', '11209', 'en', null, null, null),
  ('con_420217', 'Carmen', 'Vasquez', '1972-05-17', '2838', '792 Hillside Ave, Apt 3C', 'Queens', '11432', null, null, null, null),
  ('con_420218', 'Carlos', 'Washington', '1979-10-28', '1757', '829 2nd Ave', 'Manhattan', '10128', 'en', null, null, null),
  ('con_420219', 'Deborah', 'Yilmaz', '1986-03-11', '9676', '866 Richmond Ave, Apt 5E', 'Staten Island', '10314', null, null, null, null),
  ('con_420220', 'Darnell', 'Zhang', '1993-08-22', '8595', '903 5th St, Apt 6F', 'Brooklyn', '11215', 'en', null, null, null),
  ('con_420221', 'Esther', 'Morales', '1958-01-05', '7514', '940 Broadway', 'Queens', '11373', null, null, null, null),
  ('con_420222', 'Elijah', 'Greene', '1965-06-16', '6433', '977 Bedford Park Blvd, Apt 2B', 'Bronx', '10458', 'en', null, null, null),
  ('con_420223', 'Fatima', 'Okafor', '1972-11-27', '5352', '1014 Amsterdam Ave, Apt 3C', 'Manhattan', '10025', null, null, null, null),
  ('con_420224', 'Felix', 'Castillo', '1979-04-10', '4271', '1051 Bay St', 'Staten Island', '10301', 'en', null, null, null),
  ('con_420225', 'Gloria', 'Russo', '1986-09-21', '3190', '1088 Halsey St, Apt 5E', 'Brooklyn', '11221', null, null, null, null),
  ('con_420226', 'Gabriel', 'Brooks', '1993-02-04', '2109', '1125 Union St, Apt 6F', 'Queens', '11354', 'en', null, null, null),
  ('con_420227', 'Hannah', 'Santos', '1958-07-15', '1028', '1162 Johnson Ave', 'Bronx', '10463', null, null, null, null),
  ('con_420228', 'Hector', 'Abramowitz', '1965-12-26', '8947', '1199 W 145th St, Apt 2B', 'Manhattan', '10031', 'en', null, null, null),
  ('con_420229', 'Ingrid', 'Alvarez', '1972-05-09', '7866', '1236 Flatbush Ave, Apt 3C', 'Brooklyn', '11226', null, null, null, null),
  ('con_420230', 'Ibrahim', 'Baptiste', '1979-10-20', '6785', '1273 Ditmars Blvd', 'Queens', '11103', 'en', null, null, null),
  ('con_420231', 'Jasmine', 'Chen', '1986-03-03', '5704', '1310 Gun Hill Rd, Apt 5E', 'Bronx', '10467', null, null, null, null),
  ('con_420232', 'Jamal', 'Delgado', '1993-08-14', '4623', '1347 Clinton St, Apt 6F', 'Manhattan', '10002', 'en', null, null, null),
  ('con_420233', 'Keisha', 'Edwards', '1958-01-25', '3542', '1384 Ridge Blvd', 'Brooklyn', '11209', null, null, null, null),
  ('con_420234', 'Kevin', 'Fernandez', '1965-06-08', '2461', '1421 164th St, Apt 2B', 'Queens', '11432', 'en', null, null, null),
  ('con_420235', 'Lucia', 'Goldberg', '1972-11-19', '1380', '1458 E 92nd St, Apt 3C', 'Manhattan', '10128', null, null, null, null),
  ('con_420236', 'Luis', 'Haddad', '1979-04-02', '9299', '1495 Forest Hill Rd', 'Staten Island', '10314', 'en', null, null, null);

insert into phones (phone_id, consumer_id, number, line_type, source, artificial_voice_consent, consent_revoked_at, status) values
  ('ph_420101_1', 'con_420101', '+17185550100', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420102_1', 'con_420102', '+13475550100', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420103_1', 'con_420103', '+19175550100', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420104_1', 'con_420104', '+16465550100', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420105_1', 'con_420105', '+17185550101', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420106_1', 'con_420106', '+13475550101', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420107_1', 'con_420107', '+19175550101', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420108_1', 'con_420108', '+17185550102', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420108_2', 'con_420108', '+13475550102', 'landline', 'creditor', true, null, 'valid'),
  ('ph_420109_1', 'con_420109', '+12125550101', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420110_1', 'con_420110', '+19175550102', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420111_1', 'con_420111', '+17185550103', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420112_1', 'con_420112', '+13475550103', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420113_1', 'con_420113', '+16465550101', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420114_1', 'con_420114', '+19175550103', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420115_1', 'con_420115', '+17185550104', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420116_1', 'con_420116', '+12125550102', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420117_1', 'con_420117', '+13475550104', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420118_1', 'con_420118', '+19175550104', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420119_1', 'con_420119', '+17185550105', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420120_1', 'con_420120', '+13475550105', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420121_1', 'con_420121', '+16465550102', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420122_1', 'con_420122', '+19175550105', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420123_1', 'con_420123', '+17185550106', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420124_1', 'con_420124', '+13475550106', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420125_1', 'con_420125', '+19175550106', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420126_1', 'con_420126', '+12125550103', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420127_1', 'con_420127', '+17185550107', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420128_1', 'con_420128', '+13475550107', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420129_1', 'con_420129', '+19175550107', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420130_1', 'con_420130', '+16465550103', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420131_1', 'con_420131', '+17185550108', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420132_1', 'con_420132', '+13475550108', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420133_1', 'con_420133', '+12125550104', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420134_1', 'con_420134', '+19175550108', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420135_1', 'con_420135', '+17185550109', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420136_1', 'con_420136', '+13475550109', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420137_1', 'con_420137', '+19175550109', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420138_1', 'con_420138', '+16465550104', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420139_1', 'con_420139', '+17185550110', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420140_1', 'con_420140', '+13475550110', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420141_1', 'con_420141', '+19175550110', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420142_1', 'con_420142', '+17185550111', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420143_1', 'con_420143', '+12125550105', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420144_1', 'con_420144', '+13475550111', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420145_1', 'con_420145', '+19175550111', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420146_1', 'con_420146', '+17185550112', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420147_1', 'con_420147', '+16465550105', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420148_1', 'con_420148', '+13475550112', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420149_1', 'con_420149', '+19175550112', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420150_1', 'con_420150', '+12125550106', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420151_1', 'con_420151', '+17185550113', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420152_1', 'con_420152', '+13475550113', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420153_1', 'con_420153', '+19175550113', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420154_1', 'con_420154', '+17185550114', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420155_1', 'con_420155', '+16465550106', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420156_1', 'con_420156', '+13475550114', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420157_1', 'con_420157', '+19175550114', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420158_1', 'con_420158', '+17185550115', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420159_1', 'con_420159', '+13475550115', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420160_1', 'con_420160', '+12125550107', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420161_1', 'con_420161', '+19175550115', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420162_1', 'con_420162', '+17185550116', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420163_1', 'con_420163', '+13475550116', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420164_1', 'con_420164', '+16465550107', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420164_2', 'con_420164', '+12125550108', 'landline', 'creditor', true, null, 'valid'),
  ('ph_420165_1', 'con_420165', '+19175550116', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420166_1', 'con_420166', '+17185550117', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420167_1', 'con_420167', '+16465550108', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420168_1', 'con_420168', '+13475550117', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420169_1', 'con_420169', '+19175550117', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420170_1', 'con_420170', '+17185550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420171_1', 'con_420171', '+13475550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420172_1', 'con_420172', '+16465550110', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420173_1', 'con_420173', '+19175550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420174_1', 'con_420174', '+17185550119', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420175_1', 'con_420175', '+13475550119', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420176_1', 'con_420176', '+19175550119', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420177_1', 'con_420177', '+12125550111', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420178_1', 'con_420178', '+17185550120', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420179_1', 'con_420179', '+13475550120', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420180_1', 'con_420180', '+19175550120', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420181_1', 'con_420181', '+16465550111', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420182_1', 'con_420182', '+17185550121', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420183_1', 'con_420183', '+13475550121', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420184_1', 'con_420184', '+12125550112', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420185_1', 'con_420185', '+19175550121', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420186_1', 'con_420186', '+17185550122', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420187_1', 'con_420187', '+13475550122', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420188_1', 'con_420188', '+19175550122', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420189_1', 'con_420189', '+12125550113', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420190_1', 'con_420190', '+17185550123', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420191_1', 'con_420191', '+13475550123', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420192_1', 'con_420192', '+19175550123', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420193_1', 'con_420193', '+17185550124', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420194_1', 'con_420194', '+16465550113', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420195_1', 'con_420195', '+13475550124', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420196_1', 'con_420196', '+19175550124', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420197_1', 'con_420197', '+17185550125', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420198_1', 'con_420198', '+12125550114', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420199_1', 'con_420199', '+13475550125', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420200_1', 'con_420200', '+19175550125', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420201_1', 'con_420201', '+16465550114', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420202_1', 'con_420202', '+17185550126', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420203_1', 'con_420203', '+13475550126', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420204_1', 'con_420204', '+19175550126', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420205_1', 'con_420205', '+17185550127', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420206_1', 'con_420206', '+12125550115', 'landline', 'employer', true, null, 'valid'),
  ('ph_420207_1', 'con_420207', '+13475550127', 'mobile', 'consumer', true, '2027-01-29T11:47:00-05:00', 'valid'),
  ('ph_420208_1', 'con_420208', '+19175550127', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420209_1', 'con_420209', '+17185550128', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420210_1', 'con_420210', '+13475550128', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420211_1', 'con_420211', '+16465550115', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420212_1', 'con_420212', '+19175550128', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420213_1', 'con_420213', '+17185550129', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420214_1', 'con_420214', '+13475550129', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420215_1', 'con_420215', '+12125550116', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420216_1', 'con_420216', '+19175550129', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420217_1', 'con_420217', '+17185550130', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420218_1', 'con_420218', '+16465550116', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420219_1', 'con_420219', '+13475550130', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420220_1', 'con_420220', '+19175550130', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420221_1', 'con_420221', '+17185550131', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420222_1', 'con_420222', '+13475550131', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420223_1', 'con_420223', '+12125550117', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420224_1', 'con_420224', '+19175550131', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420225_1', 'con_420225', '+17185550132', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420226_1', 'con_420226', '+13475550132', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420227_1', 'con_420227', '+19175550132', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420228_1', 'con_420228', '+16465550117', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420229_1', 'con_420229', '+17185550133', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420230_1', 'con_420230', '+13475550133', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420231_1', 'con_420231', '+19175550133', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420232_1', 'con_420232', '+12125550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420233_1', 'con_420233', '+17185550134', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420234_1', 'con_420234', '+13475550134', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420235_1', 'con_420235', '+16465550118', 'mobile', 'consumer', true, null, 'valid'),
  ('ph_420236_1', 'con_420236', '+19175550134', 'mobile', 'consumer', true, null, 'valid');

insert into accounts (account_id, consumer_id, creditor_id, original_account_last4, placed_on, sol_expires_on, validation_notice_mailed_on, time_barred_notice_mailed_on, settlement_floor_pct, max_plan_months, min_instalment_cents, status) values
  ('TAS-420101', 'con_420101', 'cr_ashcombe', '4001', '2027-01-04', '2028-03-15', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420102', 'con_420102', 'cr_ashcombe', '8272', '2027-01-05', '2028-03-22', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420103', 'con_420103', 'cr_marlow', '3543', '2027-01-06', '2028-06-27', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420104', 'con_420104', 'cr_harlow', '7814', '2027-01-07', '2028-04-05', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420105', 'con_420105', 'cr_ashcombe', '3085', '2027-01-08', '2028-04-12', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420106', 'con_420106', 'cr_marlow', '7356', '2027-01-09', '2028-04-19', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420107', 'con_420107', 'cr_ashcombe', '2627', '2027-01-10', '2028-04-26', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420108', 'con_420108', 'cr_ashcombe', '6898', '2027-01-11', '2028-05-03', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420109', 'con_420109', 'cr_marlow', '2169', '2027-01-12', '2028-05-10', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420110', 'con_420110', 'cr_ashcombe', '6440', '2027-01-13', '2028-05-17', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420111', 'con_420111', 'cr_corley', '1711', '2027-01-04', '2028-05-24', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420112', 'con_420112', 'cr_marlow', '5982', '2027-01-05', '2028-05-28', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420113', 'con_420113', 'cr_ashcombe', '1253', '2027-01-06', '2028-06-07', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420114', 'con_420114', 'cr_corley', '5524', '2027-01-07', '2028-06-14', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420115', 'con_420115', 'cr_marlow', '9795', '2027-01-08', '2028-06-21', '2027-01-09', null, 70, 6, 2500, 'active'),
  ('TAS-420116', 'con_420116', 'cr_ashcombe', '5066', '2027-01-09', '2028-06-28', '2027-01-10', null, 70, 10, 2500, 'active'),
  ('TAS-420117', 'con_420117', 'cr_ashcombe', '9337', '2027-01-10', '2028-07-05', '2027-01-11', null, 70, 12, 5000, 'active'),
  ('TAS-420118', 'con_420118', 'cr_fenwick', '4608', '2027-01-11', '2028-07-12', '2027-01-12', null, 70, 12, 10000, 'active'),
  ('TAS-420119', 'con_420119', 'cr_ashcombe', '8879', '2027-01-12', '2028-07-19', '2027-01-13', null, 70, 12, 5000, 'active'),
  ('TAS-420120', 'con_420120', 'cr_ashcombe', '4150', '2027-01-13', '2028-07-26', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420121', 'con_420121', 'cr_fenwick', '8421', '2027-01-04', '2028-08-02', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420122', 'con_420122', 'cr_ashcombe', '3692', '2027-01-05', '2028-08-09', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420123', 'con_420123', 'cr_ashcombe', '7963', '2027-01-06', '2028-08-16', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420124', 'con_420124', 'cr_marlow', '3234', '2027-01-07', '2028-08-23', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420125', 'con_420125', 'cr_harlow', '7505', '2027-01-08', '2028-08-28', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420126', 'con_420126', 'cr_ashcombe', '2776', '2027-01-09', '2028-09-06', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420127', 'con_420127', 'cr_marlow', '7047', '2027-01-10', '2028-09-13', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420128', 'con_420128', 'cr_harlow', '2318', '2027-01-11', '2028-09-20', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420129', 'con_420129', 'cr_ashcombe', '6589', '2027-01-12', '2028-09-27', '2027-01-13', null, 60, 12, 2500, 'active'),
  ('TAS-420130', 'con_420130', 'cr_marlow', '1860', '2027-01-13', '2028-03-18', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420131', 'con_420131', 'cr_ashcombe', '6131', '2027-01-04', '2028-03-25', '2027-01-05', null, 80, 12, 2500, 'active'),
  ('TAS-420132', 'con_420132', 'cr_corley', '1402', '2027-01-05', '2028-04-01', '2027-01-06', null, 50, 12, 2500, 'active'),
  ('TAS-420133', 'con_420133', 'cr_marlow', '5673', '2027-01-06', '2028-04-08', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420134', 'con_420134', 'cr_ashcombe', '9944', '2027-01-07', '2028-04-15', '2027-01-08', null, 60, 12, 2500, 'active'),
  ('TAS-420135', 'con_420135', 'cr_corley', '5215', '2027-01-08', '2028-04-22', '2027-01-09', null, 50, 12, 2500, 'active'),
  ('TAS-420136', 'con_420136', 'cr_marlow', '9486', '2027-01-09', '2028-04-28', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420137', 'con_420137', 'cr_ashcombe', '4757', '2027-01-10', '2028-05-06', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420138', 'con_420138', 'cr_ashcombe', '9028', '2027-01-11', '2028-05-13', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420139', 'con_420139', 'cr_fenwick', '4299', '2027-01-12', '2028-05-20', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420140', 'con_420140', 'cr_ashcombe', '8570', '2027-01-13', '2028-05-27', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420141', 'con_420141', 'cr_ashcombe', '3841', '2027-01-04', '2028-06-03', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420142', 'con_420142', 'cr_fenwick', '8112', '2027-01-05', '2028-06-10', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420143', 'con_420143', 'cr_ashcombe', '3383', '2027-01-06', '2028-06-17', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420144', 'con_420144', 'cr_ashcombe', '7654', '2027-01-07', '2028-06-24', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420145', 'con_420145', 'cr_marlow', '2925', '2027-01-08', '2028-07-01', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420146', 'con_420146', 'cr_harlow', '7196', '2027-01-09', '2028-07-08', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420147', 'con_420147', 'cr_ashcombe', '2467', '2027-01-10', '2028-07-15', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420148', 'con_420148', 'cr_marlow', '6738', '2027-01-11', '2028-07-22', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420149', 'con_420149', 'cr_harlow', '2009', '2027-01-12', '2028-07-28', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420150', 'con_420150', 'cr_ashcombe', '6280', '2027-01-13', '2028-08-05', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420151', 'con_420151', 'cr_marlow', '1551', '2027-01-04', '2028-08-12', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420152', 'con_420152', 'cr_ashcombe', '5822', '2027-01-05', '2028-08-19', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420153', 'con_420153', 'cr_corley', '1093', '2027-01-06', '2028-08-26', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420154', 'con_420154', 'cr_marlow', '5364', '2027-01-07', '2028-09-02', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420155', 'con_420155', 'cr_ashcombe', '9635', '2027-01-08', '2028-09-09', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420156', 'con_420156', 'cr_ashcombe', '4906', '2027-01-09', '2028-09-16', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420157', 'con_420157', 'cr_marlow', '9177', '2027-01-10', '2028-09-23', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420158', 'con_420158', 'cr_ashcombe', '4448', '2027-01-11', '2028-09-28', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420159', 'con_420159', 'cr_ashcombe', '8719', '2027-01-12', '2028-03-21', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420160', 'con_420160', 'cr_marlow', '3990', '2027-01-13', '2028-03-28', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420161', 'con_420161', 'cr_beckford', '8261', '2027-01-04', '2028-04-04', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420162', 'con_420162', 'cr_ashcombe', '3532', '2027-01-05', '2028-04-11', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420163', 'con_420163', 'cr_fenwick', '7803', '2027-01-06', '2028-04-18', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420164', 'con_420164', 'cr_ashcombe', '3074', '2027-01-07', '2028-04-25', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420165', 'con_420165', 'cr_ashcombe', '7345', '2027-01-08', '2028-05-02', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420166', 'con_420166', 'cr_marlow', '2616', '2027-01-09', '2028-05-09', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420167', 'con_420167', 'cr_harlow', '6887', '2027-01-10', '2028-05-16', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420168', 'con_420168', 'cr_ashcombe', '2158', '2027-01-11', '2028-05-23', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420169', 'con_420169', 'cr_marlow', '6429', '2027-01-12', '2028-05-28', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420170', 'con_420170', 'cr_harlow', '1700', '2027-01-13', '2028-06-06', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420171', 'con_420171', 'cr_ashcombe', '5971', '2027-01-04', '2028-06-13', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420172', 'con_420172', 'cr_marlow', '1242', '2027-01-05', '2028-06-20', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420173', 'con_420173', 'cr_ashcombe', '5513', '2027-01-06', '2028-06-27', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420174', 'con_420174', 'cr_corley', '9784', '2027-01-07', '2028-07-04', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420175', 'con_420175', 'cr_marlow', '5055', '2027-01-08', '2028-07-11', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420176', 'con_420176', 'cr_ashcombe', '9326', '2027-01-09', '2028-07-18', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420177', 'con_420177', 'cr_corley', '4597', '2027-01-10', '2028-07-25', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420178', 'con_420178', 'cr_marlow', '8868', '2027-01-11', '2028-08-01', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420179', 'con_420179', 'cr_ashcombe', '4139', '2027-01-12', '2028-08-08', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420180', 'con_420180', 'cr_ashcombe', '8410', '2027-01-13', '2028-08-15', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420181', 'con_420181', 'cr_fenwick', '3681', '2027-01-04', '2028-08-22', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420182', 'con_420182', 'cr_ashcombe', '7952', '2027-01-05', '2028-08-28', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420183', 'con_420183', 'cr_ashcombe', '3223', '2027-01-06', '2028-09-05', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420184', 'con_420184', 'cr_fenwick', '7494', '2027-01-07', '2028-09-12', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420185', 'con_420185', 'cr_ashcombe', '2765', '2027-01-08', '2028-09-19', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420186', 'con_420186', 'cr_ashcombe', '7036', '2027-01-09', '2028-09-26', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420187', 'con_420187', 'cr_marlow', '2307', '2027-01-10', '2028-03-17', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420188', 'con_420188', 'cr_harlow', '6578', '2027-01-11', '2028-03-24', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420189', 'con_420189', 'cr_ashcombe', '1849', '2027-01-12', '2028-03-28', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420190', 'con_420190', 'cr_marlow', '6120', '2027-01-13', '2025-06-08', '2027-01-14', '2027-01-08', 70, 12, 2500, 'active'),
  ('TAS-420191', 'con_420191', 'cr_harlow', '1391', '2027-01-04', '2025-06-09', '2027-01-05', '2027-01-20', 70, 12, 2500, 'active'),
  ('TAS-420192', 'con_420192', 'cr_ashcombe', '5662', '2027-01-05', '2025-06-10', '2027-01-06', '2027-01-20', 50, 12, 2500, 'active'),
  ('TAS-420193', 'con_420193', 'cr_marlow', '9933', '2027-01-06', '2025-06-11', '2027-01-07', '2027-01-31', 70, 12, 2500, 'active'),
  ('TAS-420194', 'con_420194', 'cr_ashcombe', '5204', '2027-01-07', '2025-06-12', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420195', 'con_420195', 'cr_corley', '9475', '2027-01-08', '2028-05-12', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420196', 'con_420196', 'cr_marlow', '4746', '2027-01-09', '2028-05-19', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420197', 'con_420197', 'cr_ashcombe', '9017', '2027-01-10', '2028-05-26', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420198', 'con_420198', 'cr_corley', '4288', '2027-01-11', '2028-06-02', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420199', 'con_420199', 'cr_marlow', '8559', '2027-01-12', '2028-06-09', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420200', 'con_420200', 'cr_harlow', '3830', '2027-01-13', '2028-06-16', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420201', 'con_420201', 'cr_harlow', '8101', '2027-01-04', '2028-06-23', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420202', 'con_420202', 'cr_harlow', '3372', '2027-01-05', '2028-06-28', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420203', 'con_420203', 'cr_ashcombe', '7643', '2027-01-06', '2028-07-07', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420204', 'con_420204', 'cr_ashcombe', '2914', '2027-01-07', '2028-07-14', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420205', 'con_420205', 'cr_fenwick', '7185', '2027-01-08', '2028-07-21', '2027-01-09', null, 70, 12, 2500, 'cease'),
  ('TAS-420206', 'con_420206', 'cr_ashcombe', '2456', '2027-01-09', '2028-07-28', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420207', 'con_420207', 'cr_ashcombe', '6727', '2027-01-10', '2028-08-04', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420208', 'con_420208', 'cr_marlow', '1998', '2027-01-11', '2028-08-11', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420209', 'con_420209', 'cr_harlow', '6269', '2027-01-12', '2028-08-18', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420210', 'con_420210', 'cr_ashcombe', '1540', '2027-01-13', '2028-08-25', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420211', 'con_420211', 'cr_marlow', '5811', '2027-01-04', '2028-09-01', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420212', 'con_420212', 'cr_harlow', '1082', '2027-01-05', '2028-09-08', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420213', 'con_420213', 'cr_fenwick', '5353', '2027-01-06', '2028-09-15', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420214', 'con_420214', 'cr_corley', '9624', '2027-01-07', '2028-09-22', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420215', 'con_420215', 'cr_corley', '4895', '2027-01-08', '2028-09-28', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420216', 'con_420216', 'cr_harlow', '9166', '2027-01-09', '2028-03-20', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420217', 'con_420217', 'cr_ashcombe', '4437', '2027-01-10', '2028-03-27', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420218', 'con_420218', 'cr_marlow', '8708', '2027-01-11', '2028-04-03', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420219', 'con_420219', 'cr_marlow', '3979', '2027-01-12', '2028-04-10', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420220', 'con_420220', 'cr_beckford', '8250', '2027-01-13', '2028-04-17', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420221', 'con_420221', 'cr_ashcombe', '3521', '2027-01-04', '2028-04-24', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420222', 'con_420222', 'cr_ashcombe', '7792', '2027-01-05', '2028-05-01', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420223', 'con_420223', 'cr_fenwick', '3063', '2027-01-06', '2028-05-08', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420224', 'con_420224', 'cr_ashcombe', '7334', '2027-01-07', '2028-05-15', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420225', 'con_420225', 'cr_ashcombe', '2605', '2027-01-08', '2028-05-22', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420226', 'con_420226', 'cr_fenwick', '6876', '2027-01-09', '2028-05-28', '2027-01-10', null, 70, 12, 2500, 'active'),
  ('TAS-420227', 'con_420227', 'cr_ashcombe', '2147', '2027-01-10', '2028-06-05', '2027-01-11', null, 70, 12, 2500, 'active'),
  ('TAS-420228', 'con_420228', 'cr_ashcombe', '6418', '2027-01-11', '2028-06-12', '2027-01-12', null, 70, 12, 2500, 'active'),
  ('TAS-420229', 'con_420229', 'cr_marlow', '1689', '2027-01-12', '2028-06-19', '2027-01-13', null, 70, 12, 2500, 'active'),
  ('TAS-420230', 'con_420230', 'cr_harlow', '5960', '2027-01-13', '2028-06-26', '2027-01-14', null, 70, 12, 2500, 'active'),
  ('TAS-420231', 'con_420231', 'cr_ashcombe', '1231', '2027-01-04', '2028-07-03', '2027-01-05', null, 70, 12, 2500, 'active'),
  ('TAS-420232', 'con_420232', 'cr_marlow', '5502', '2027-01-05', '2028-07-10', '2027-01-06', null, 70, 12, 2500, 'active'),
  ('TAS-420233', 'con_420233', 'cr_harlow', '9773', '2027-01-06', '2028-07-17', '2027-01-07', null, 70, 12, 2500, 'active'),
  ('TAS-420234', 'con_420234', 'cr_ashcombe', '5044', '2027-01-07', '2028-07-24', '2027-01-08', null, 70, 12, 2500, 'active'),
  ('TAS-420235', 'con_420235', 'cr_marlow', '9315', '2027-01-08', '2028-07-28', '2027-01-09', null, 70, 12, 2500, 'active'),
  ('TAS-420236', 'con_420236', 'cr_ashcombe', '4586', '2027-01-09', '2028-08-07', '2027-01-10', null, 70, 12, 2500, 'active');

insert into contact_attempts (attempt_id, account_id, phone_id, at, medium, direction, answered_by, consumer_responded, duration_s, disposition, summary, agent_name) values
  ('att_420101_1', 'TAS-420101', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420102_1', 'TAS-420102', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420102_2', 'TAS-420102', 'ph_420102_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420103_1', 'TAS-420103', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420104_1', 'TAS-420104', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420104_2', 'TAS-420104', 'ph_420104_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420105_1', 'TAS-420105', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420106_1', 'TAS-420106', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420106_2', 'TAS-420106', 'ph_420106_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420107_1', 'TAS-420107', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420108_1', 'TAS-420108', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420108_2', 'TAS-420108', 'ph_420108_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420109_1', 'TAS-420109', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420110_1', 'TAS-420110', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420110_2', 'TAS-420110', 'ph_420110_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420111_1', 'TAS-420111', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420112_1', 'TAS-420112', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420112_2', 'TAS-420112', 'ph_420112_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420113_1', 'TAS-420113', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420114_1', 'TAS-420114', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420114_2', 'TAS-420114', 'ph_420114_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420115_1', 'TAS-420115', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420116_1', 'TAS-420116', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420116_2', 'TAS-420116', 'ph_420116_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420117_1', 'TAS-420117', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420118_1', 'TAS-420118', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420118_2', 'TAS-420118', 'ph_420118_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420119_1', 'TAS-420119', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420120_1', 'TAS-420120', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420120_2', 'TAS-420120', 'ph_420120_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420121_1', 'TAS-420121', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420122_1', 'TAS-420122', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420122_2', 'TAS-420122', 'ph_420122_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420123_1', 'TAS-420123', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420124_1', 'TAS-420124', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420124_2', 'TAS-420124', 'ph_420124_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420125_1', 'TAS-420125', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420126_1', 'TAS-420126', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420126_2', 'TAS-420126', 'ph_420126_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420127_1', 'TAS-420127', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420128_1', 'TAS-420128', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420128_2', 'TAS-420128', 'ph_420128_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420129_1', 'TAS-420129', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420130_1', 'TAS-420130', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420130_2', 'TAS-420130', 'ph_420130_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420131_1', 'TAS-420131', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420132_1', 'TAS-420132', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420132_2', 'TAS-420132', 'ph_420132_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420133_1', 'TAS-420133', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420134_1', 'TAS-420134', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420134_2', 'TAS-420134', 'ph_420134_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420135_1', 'TAS-420135', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420136_1', 'TAS-420136', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420136_2', 'TAS-420136', 'ph_420136_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420137_1', 'TAS-420137', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420138_1', 'TAS-420138', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420138_2', 'TAS-420138', 'ph_420138_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420139_1', 'TAS-420139', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420140_1', 'TAS-420140', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420140_2', 'TAS-420140', 'ph_420140_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420141_1', 'TAS-420141', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420142_1', 'TAS-420142', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420142_2', 'TAS-420142', 'ph_420142_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420143_1', 'TAS-420143', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420144_1', 'TAS-420144', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420144_2', 'TAS-420144', 'ph_420144_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420145_1', 'TAS-420145', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420146_1', 'TAS-420146', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420146_2', 'TAS-420146', 'ph_420146_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420147_1', 'TAS-420147', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420148_1', 'TAS-420148', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420148_2', 'TAS-420148', 'ph_420148_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420149_1', 'TAS-420149', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420150_1', 'TAS-420150', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420150_2', 'TAS-420150', 'ph_420150_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420151_1', 'TAS-420151', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420152_1', 'TAS-420152', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420152_2', 'TAS-420152', 'ph_420152_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420153_1', 'TAS-420153', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420154_1', 'TAS-420154', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420154_2', 'TAS-420154', 'ph_420154_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420155_1', 'TAS-420155', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420156_1', 'TAS-420156', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420156_2', 'TAS-420156', 'ph_420156_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420157_1', 'TAS-420157', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420158_1', 'TAS-420158', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420158_2', 'TAS-420158', 'ph_420158_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420159_1', 'TAS-420159', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420160_1', 'TAS-420160', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420160_2', 'TAS-420160', 'ph_420160_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420161_1', 'TAS-420161', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420162_1', 'TAS-420162', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420162_2', 'TAS-420162', 'ph_420162_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420163_1', 'TAS-420163', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420164_1', 'TAS-420164', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420164_2', 'TAS-420164', 'ph_420164_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420165_1', 'TAS-420165', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420166_1', 'TAS-420166', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420166_2', 'TAS-420166', 'ph_420166_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420167_1', 'TAS-420167', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420168_1', 'TAS-420168', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420168_2', 'TAS-420168', 'ph_420168_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420169_1', 'TAS-420169', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420170_1', 'TAS-420170', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420170_2', 'TAS-420170', 'ph_420170_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420171_1', 'TAS-420171', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420172_1', 'TAS-420172', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420172_2', 'TAS-420172', 'ph_420172_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420173_1', 'TAS-420173', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420174_1', 'TAS-420174', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420174_2', 'TAS-420174', 'ph_420174_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420175_1', 'TAS-420175', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420176_1', 'TAS-420176', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420176_2', 'TAS-420176', 'ph_420176_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420177_1', 'TAS-420177', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420178_1', 'TAS-420178', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420178_2', 'TAS-420178', 'ph_420178_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420179_1', 'TAS-420179', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420180_1', 'TAS-420180', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420180_2', 'TAS-420180', 'ph_420180_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420181_1', 'TAS-420181', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420182_1', 'TAS-420182', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420182_2', 'TAS-420182', 'ph_420182_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420183_1', 'TAS-420183', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420184_1', 'TAS-420184', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420184_2', 'TAS-420184', 'ph_420184_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420185_1', 'TAS-420185', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420186_1', 'TAS-420186', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420186_2', 'TAS-420186', 'ph_420186_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420187_1', 'TAS-420187', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420188_1', 'TAS-420188', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420188_2', 'TAS-420188', 'ph_420188_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420189_1', 'TAS-420189', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420190_1', 'TAS-420190', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420190_2', 'TAS-420190', 'ph_420190_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420191_1', 'TAS-420191', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420192_1', 'TAS-420192', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420192_2', 'TAS-420192', 'ph_420192_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420193_1', 'TAS-420193', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420194_1', 'TAS-420194', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420194_2', 'TAS-420194', 'ph_420194_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420195_1', 'TAS-420195', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420196_1', 'TAS-420196', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420196_2', 'TAS-420196', 'ph_420196_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420197_1', 'TAS-420197', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420198_1', 'TAS-420198', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420198_2', 'TAS-420198', 'ph_420198_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420199_1', 'TAS-420199', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420200_1', 'TAS-420200', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420200_2', 'TAS-420200', 'ph_420200_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420201_1', 'TAS-420201', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420202_1', 'TAS-420202', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420202_2', 'TAS-420202', 'ph_420202_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420203_1', 'TAS-420203', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420203_2', 'TAS-420203', 'ph_420203_1', '2027-02-05T10:15:00-05:00', 'voicemail', 'outbound', 'voicemail', false, 35, 'voicemail', 'Limited-content voicemail left.', 'Remi'),
  ('att_420203_3', 'TAS-420203', 'ph_420203_1', '2027-02-08T17:40:00-05:00', 'sms', 'outbound', null, false, 35, 'sms_sent', 'Text asking the consumer to call back.', 'Remi'),
  ('att_420203_4', 'TAS-420203', 'ph_420203_1', '2027-02-09T12:20:00-05:00', 'voicemail', 'outbound', 'voicemail', false, 35, 'voicemail', 'Limited-content voicemail left.', 'Remi'),
  ('att_420204_1', 'TAS-420204', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420204_2', 'TAS-420204', 'ph_420204_1', '2027-02-08T13:00:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified and said they would think about it.', 'Remi'),
  ('att_420205_1', 'TAS-420205', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420205_2', 'TAS-420205', 'ph_420205_1', '2027-01-27T15:10:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'cease', 'Consumer asked the agency to stop contacting them.', 'Remi'),
  ('att_420206_1', 'TAS-420206', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420206_2', 'TAS-420206', 'ph_420206_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420207_1', 'TAS-420207', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420207_2', 'TAS-420207', 'ph_420207_1', '2027-01-29T11:45:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'channel_optout', 'Consumer asked not to be called on this cell phone.', 'Remi'),
  ('att_420208_1', 'TAS-420208', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420208_2', 'TAS-420208', 'ph_420208_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420209_1', 'TAS-420209', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420210_1', 'TAS-420210', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420210_2', 'TAS-420210', 'ph_420210_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420211_1', 'TAS-420211', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420212_1', 'TAS-420212', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420212_2', 'TAS-420212', 'ph_420212_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420213_1', 'TAS-420213', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420214_1', 'TAS-420214', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420214_2', 'TAS-420214', 'ph_420214_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420215_1', 'TAS-420215', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420216_1', 'TAS-420216', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420216_2', 'TAS-420216', 'ph_420216_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420217_1', 'TAS-420217', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420218_1', 'TAS-420218', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420218_2', 'TAS-420218', 'ph_420218_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420219_1', 'TAS-420219', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420220_1', 'TAS-420220', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420220_2', 'TAS-420220', 'ph_420220_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420221_1', 'TAS-420221', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420222_1', 'TAS-420222', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420222_2', 'TAS-420222', 'ph_420222_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420223_1', 'TAS-420223', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420224_1', 'TAS-420224', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420224_2', 'TAS-420224', 'ph_420224_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420225_1', 'TAS-420225', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420226_1', 'TAS-420226', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420226_2', 'TAS-420226', 'ph_420226_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420227_1', 'TAS-420227', null, '2027-01-11T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420228_1', 'TAS-420228', null, '2027-01-12T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420228_2', 'TAS-420228', 'ph_420228_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420229_1', 'TAS-420229', null, '2027-01-13T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420230_1', 'TAS-420230', null, '2027-01-14T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420230_2', 'TAS-420230', 'ph_420230_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420231_1', 'TAS-420231', null, '2027-01-05T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420232_1', 'TAS-420232', null, '2027-01-06T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420232_2', 'TAS-420232', 'ph_420232_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420233_1', 'TAS-420233', null, '2027-01-07T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420234_1', 'TAS-420234', null, '2027-01-08T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420234_2', 'TAS-420234', 'ph_420234_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi'),
  ('att_420235_1', 'TAS-420235', null, '2027-01-09T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420236_1', 'TAS-420236', null, '2027-01-10T10:00:00-05:00', 'letter', 'outbound', null, false, null, null, 'Validation notice mailed.', null),
  ('att_420236_2', 'TAS-420236', 'ph_420236_1', '2027-01-21T18:05:00-05:00', 'call', 'outbound', 'consumer', true, 240, 'no_commitment', 'Consumer verified, heard the balance, made no commitment.', 'Remi');

insert into account_flags (flag_id, account_id, kind, details, created_at) values
  ('flag_420205_1', 'TAS-420205', 'cease', '{}'::jsonb, '2027-01-27T15:12:00-05:00'),
  ('flag_420207_1', 'TAS-420207', 'channel_optout', '{"channel":"call","phone_id":"ph_420207_1"}'::jsonb, '2027-01-29T11:47:00-05:00');

