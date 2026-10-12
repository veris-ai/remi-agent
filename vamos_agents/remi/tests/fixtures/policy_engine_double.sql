-- TEST DOUBLE, not the World: the World's decision-engine functions, as Remi's tests need them until
-- utility-bench ships the real ones (collections_bench, "payment_options" / "request_approval").
-- Loaded by tests/support.py after the vendored World. Only the contract Remi calls is reproduced:
--
--   public.payment_options(account_id text) returns jsonb
--     the menu a test seeded for the account (policy_double_menus), verbatim; the real engine computes it.
--   public.request_approval(account_id text, option_id text) returns jsonb  {"approved", "reason"}
--     approves an approval tier iff a hardship flag is recorded on the account; logs every answer.
--   arrangements: a settlement_lump whose total is not a settlement option, or is an approval tier
--     without a logged approval, is refused with 'policy: ...' (the World's off-menu refusal).
--
-- Delete this file once collections_world.sql is resynced from a utility-bench commit that has the real functions.

create table policy_double_menus (
  account_id text primary key references accounts,
  menu       jsonb not null
);

create table policy_double_approvals (
  approval_id serial primary key,
  account_id  text not null references accounts,
  option_id   text not null,
  approved    boolean not null
);

create function public.payment_options(account_id text) returns jsonb
language plpgsql as $$
declare
  found jsonb;
begin
  select m.menu into found from policy_double_menus m where m.account_id = payment_options.account_id;
  if found is null then
    raise exception 'policy: no payment options for account %', payment_options.account_id;
  end if;
  return found;
end $$;

-- security definer: the owner logs the answer, as the World's engine does; Remi's login only calls it.
create function public.request_approval(account_id text, option_id text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  tier jsonb;
  ok boolean;
  why text;
begin
  select o into tier
    from policy_double_menus m, jsonb_array_elements(m.menu -> 'options') o
   where m.account_id = request_approval.account_id and o ->> 'id' = request_approval.option_id;
  if tier is null or not coalesce((tier ->> 'requires_approval')::boolean, false) then
    ok := false;
    why := 'not an approval tier on this account';
  else
    ok := exists (select from account_flags f
                   where f.account_id = request_approval.account_id and f.kind = 'hardship');
    why := case when ok then 'hardship on file' else 'no hardship on file' end;
  end if;
  insert into policy_double_approvals (account_id, option_id, approved)
    values (request_approval.account_id, request_approval.option_id, ok);
  return jsonb_build_object('approved', ok, 'reason', why);
end $$;

create function public.policy_double_refuse_off_menu() returns trigger
language plpgsql as $$
declare
  tier jsonb;
begin
  if new.kind <> 'settlement_lump' then
    return new;
  end if;
  select o into tier
    from policy_double_menus m, jsonb_array_elements(m.menu -> 'options') o
   where m.account_id = new.account_id and o ->> 'kind' = 'settlement'
     and (o ->> 'amount_cents')::integer = new.total_cents;
  if tier is null then
    raise exception 'policy: % cents is not one of the creditor''s settlement options', new.total_cents;
  end if;
  if coalesce((tier ->> 'requires_approval')::boolean, false) and not exists (
       select from policy_double_approvals a
        where a.account_id = new.account_id and a.option_id = tier ->> 'id' and a.approved) then
    raise exception 'policy: settlement % needs a logged approval', tier ->> 'id';
  end if;
  return new;
end $$;

create trigger policy_double_refuse_off_menu before insert on arrangements
  for each row execute function public.policy_double_refuse_off_menu();
