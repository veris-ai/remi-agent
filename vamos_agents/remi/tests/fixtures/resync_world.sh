#!/usr/bin/env bash
# Regenerate Remi's World v2 fixtures from the benchmark repo (collections_bench/world_v2):
#
# - collections_world.sql: the World's database in the order it is seeded (WORLD.md, "Building the
#   world"): the schema, the creditors' policy engine (creditor_policy.sql), then the rows;
# - world_v2_manifest.json: the World's manifest, as generated: the Fineract loans, the PayNearMe
#   orders and test instruments, and each pilot task's hand-off (agent_contexts);
# - payment_options/<TASK>.json: the reviewed policy tool's output for each pilot account, the
#   oracle get_payment_options must return byte for byte.
#
#   vamos_agents/remi/tests/fixtures/resync_world.sh <benchmark repo checkout> [ref, default origin/main]
#
# Fetch the checkout first (git -C <checkout> fetch origin). Then run Remi's tests.
set -euo pipefail

checkout=${1:?usage: resync_world.sh <benchmark repo checkout> [ref]}
ref=${2:-origin/main}
sha=$(git -C "$checkout" rev-parse "$ref")
here="$(cd "$(dirname "$0")" && pwd)"
world=collections_bench/world_v2
out="$here/collections_world.sql"
parts=(
  "$world/generated/world/schema.sql"
  "$world/creditor_policy.sql"
  "$world/generated/world/rows.sql"
)

{
  cat <<EOF
-- The VAmoS Debt Collection World v2's database, vendored for Remi's tests: schema, the creditors'
-- policy engine and rows, in the order the World is seeded ($world/WORLD.md, "Building the world").
--
-- Source: the benchmark repo @ $sha
--   ${parts[0]}
--   ${parts[1]}
--   ${parts[2]}
-- Test data, not a migration: Remi never imports the benchmark repo and never runs this at runtime.
--
-- Resync: vamos_agents/remi/tests/fixtures/resync_world.sh <benchmark repo checkout> [ref]
-- (rewrites this file, header included), then run Remi's tests.

EOF
  for part in "${parts[@]}"; do
    echo "-- ===== $part ====="
    git -C "$checkout" show "$sha:$part"
    echo
  done
} > "$out"
echo "wrote $out from $sha"

git -C "$checkout" show "$sha:$world/generated/manifest.json" > "$here/world_v2_manifest.json"
echo "wrote $here/world_v2_manifest.json from $sha"

rm -rf "$here/payment_options"
mkdir -p "$here/payment_options"
for example in $(git -C "$checkout" ls-tree --name-only "$sha" "$world/oracle/examples/"); do
  git -C "$checkout" show "$sha:$example" > "$here/payment_options/$(basename "$example")"
done
echo "wrote $(ls "$here/payment_options" | wc -l | tr -d ' ') examples to $here/payment_options from $sha"
