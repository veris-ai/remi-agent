-- WORLD.md's schema (veris-ai/utility-bench collections_bench/WORLD.md), copied verbatim for tests.
-- The world is the contract Remi codes against; this file is test data, not a migration.
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
  charge_off_on  date not null,
  charge_off_balance_cents  integer not null,
  interest_since_charge_off_cents integer not null default 0,
  fees_since_charge_off_cents     integer not null default 0,
  paid_since_charge_off_cents     integer not null default 0,
  current_balance_cents    integer not null, -- charge-off + interest + fees - paid
  last_payment_on   date,
  sol_expires_on    date not null,           -- time-barred when < call date
  validation_notice_mailed_on   date not null,  -- always on/after 2027-01-01: NYC dispute rules apply
  time_barred_notice_mailed_on  date,        -- null if never mailed
  settlement_floor_pct  integer not null default 70,
  max_plan_months       integer not null default 12,
  min_instalment_cents  integer not null default 2500,
  status  text not null default 'active' check (status in
          ('active','disputed','cease','attorney','bankruptcy','deceased',
           'paid','settled','recalled')),
  stripe_customer_id text                     -- set by the first call that charges or saves a payment method, even if the charge fails
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
-- hardship, original-creditor request, or medical financial-assistance claim.
create table account_flags (
  flag_id     text primary key,
  account_id  text not null references accounts,
  kind        text not null check (kind in
              ('dispute','original_creditor_request','cease','channel_optout',
               'attorney','bankruptcy','deceased','scra','hardship',
               'medical_financial_assistance')),
  details     jsonb not null default '{}',   -- see "Flag details" below
  created_at  timestamptz not null
);

create table arrangements (
  arrangement_id text primary key,
  account_id     text not null references accounts,
  kind           text not null check (kind in ('pay_in_full','settlement_lump','settlement_instalments','plan','hardship_plan')),
  total_cents    integer not null,
  instalments    jsonb not null,             -- [{"due_on":"2027-03-01","amount_cents":12500}]
  stripe_payment_method_id text,
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

create table payments (
  payment_id   text primary key,
  account_id   text not null references accounts,
  stripe_payment_intent_id text not null,
  amount_cents integer not null,
  status       text not null check (status in ('succeeded','failed')),
  at           timestamptz not null
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
