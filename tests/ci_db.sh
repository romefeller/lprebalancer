#!/usr/bin/env bash
# Build an empty test database from sql/ and seed one profile, as CI does.
# The tests refuse any database whose name does not end in _test.
#
#   tests/ci_db.sh rebalancer_test
set -euo pipefail
cd "$(dirname "$0")/.."
db="${1:?usage: tests/ci_db.sh <name>_test}"
case "$db" in *_test) ;; *) echo "refusing: $db does not end in _test" >&2; exit 2 ;; esac
dropdb --if-exists "$db"
createdb "$db"
for migration in sql/*.sql; do
  PGOPTIONS="-c client_min_messages=warning" psql -q -v ON_ERROR_STOP=1 -d "$db" -f "$migration" > /dev/null
done
LPBOT_DSN="dbname=$db" python3 db.py seed > /dev/null
echo "$db ready"
