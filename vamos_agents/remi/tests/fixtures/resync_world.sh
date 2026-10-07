#!/usr/bin/env bash
# Regenerate collections_world.sql from veris-ai/utility-bench: the World's schema, its payment
# gateway and its rows, concatenated in the order the World is seeded (seed.seed_sql).
#
#   vamos_agents/remi/tests/fixtures/resync_world.sh <utility-bench checkout> [ref, default origin/main]
#
# Fetch the checkout first (git -C <checkout> fetch origin). Then run Remi's tests.
set -euo pipefail

checkout=${1:?usage: resync_world.sh <utility-bench checkout> [ref]}
ref=${2:-origin/main}
sha=$(git -C "$checkout" rev-parse "$ref")
out="$(cd "$(dirname "$0")" && pwd)/collections_world.sql"
parts=(
  collections_bench/generated/world/schema.sql
  collections_bench/world/gateway.sql
  collections_bench/generated/world/rows.sql
)

{
  cat <<EOF
-- The VAmoS Debt Collection World's database, vendored for Remi's tests: schema, payment gateway
-- and rows, in the order the World is seeded (collections_bench/world/seed.py, seed_sql).
--
-- Source: veris-ai/utility-bench @ $sha
--   ${parts[0]}
--   ${parts[1]}
--   ${parts[2]}
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
