#!/usr/bin/env bash
# Regenerate collections_world.sql from veris-ai/utility-bench: the World's schema, its payment
# gateway, the creditors' decision engine and its rows, concatenated in the order the World is
# seeded (seed.seed_sql); and collections_ledger.json: every account's money, which the World keeps
# in its Fineract ledger (collections_bench/world/fineract.py seeds it from the same manifest).
#
#   vamos_agents/remi/tests/fixtures/resync_world.sh <utility-bench checkout> [ref, default origin/main]
#
# Fetch the checkout first (git -C <checkout> fetch origin). Then run Remi's tests.
set -euo pipefail

checkout=${1:?usage: resync_world.sh <utility-bench checkout> [ref]}
ref=${2:-origin/main}
sha=$(git -C "$checkout" rev-parse "$ref")
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/collections_world.sql"
ledger="$here/collections_ledger.json"
parts=(
  collections_bench/generated/world/schema.sql
  collections_bench/world/gateway.sql
  collections_bench/world/creditor_policy.sql
  collections_bench/generated/world/rows.sql
)

{
  cat <<EOF
-- The VAmoS Debt Collection World's database, vendored for Remi's tests: schema, payment gateway,
-- the creditors' decision engine and rows, in the order the World is seeded (collections_bench/world/seed.py, seed_sql).
--
-- Source: veris-ai/utility-bench @ $sha
--   ${parts[0]}
--   ${parts[1]}
--   ${parts[2]}
--   ${parts[3]}
-- Test data, not a migration: Remi never imports utility-bench and never runs this at runtime.
--
-- Resync: vamos_agents/remi/tests/fixtures/resync_world.sh <utility-bench checkout> [ref]
-- (rewrites this file, header included), then run Remi's tests.

EOF
  for part in "${parts[@]}"; do
    echo "-- ===== $part ====="
    git -C "$checkout" show "$sha:$part"
    echo
  done
} > "$out"
echo "wrote $out from $sha"

git -C "$checkout" show "$sha:collections_bench/generated/manifest.json" | python3 -c '
import json, sys
manifest = json.load(sys.stdin)
keys = ("charge_off_on", "charge_off_balance_cents", "interest_since_charge_off_cents",
        "fees_since_charge_off_cents", "paid_since_charge_off_cents", "last_payment_on")
accounts = {a["account_id"]: {k: a[k] for k in keys} for a in manifest["accounts"]}
json.dump({"source": f"veris-ai/utility-bench @ {sys.argv[1]}: collections_bench/generated/manifest.json",
           "accounts": accounts}, sys.stdout, indent=1, sort_keys=True)
print()
' "$sha" > "$ledger"
echo "wrote $ledger from $sha"
