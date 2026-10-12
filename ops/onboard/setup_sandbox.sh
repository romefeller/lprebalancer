#!/bin/bash
# Moves the onboarding page (lp-onboard.service) from user ubuntu to its own
# sandboxed system user lp-onboard (2026-10-12 security audit). Run as root from
# the live directory:
#
#   sudo bash ops/onboard/setup_sandbox.sh            # install, try, switch
#   sudo bash ops/onboard/setup_sandbox.sh --check    # verify the running unit only
#   sudo bash ops/onboard/setup_sandbox.sh --rollback # back to the unit it replaced
#
# Steps, each one safe to run again:
#   1. system user and group lp-onboard (no home, no shell)
#   2. Postgres role "lp-onboard": SELECT on schema rebalancer, INSERT/UPDATE on
#      the six tables the store writes, the two sequences it draws from
#   3. /etc/lp-onboard/env (root 0600): LPBOT_RPC and LPBOT_ONBOARD_INVITE_SHA256
#      only, copied from ~ubuntu/.kamino-keys/env and onboard.env; never printed
#   4. the start spool and its root helper (lp-onboard-start.{path,service})
#   5. the new unit as lp-onboard-next.service on port 8091, beside the running
#      page, and checks: it is active as lp-onboard, serves /bot/api/config with
#      both chains, sees no /home but the code, holds none of the withheld
#      variables, and its user reads and writes nothing it should not in Postgres
#   6. only then: the old unit is kept as lp-onboard.service.pre-sandbox, the new
#      one replaces it and is restarted on 8090; if it is not active and serving
#      within 20 s, the old unit comes back by itself
# Nothing stops a bot (lp-bot@*). The page is down only for its own restart.
set -euo pipefail

REPO=/home/ubuntu/agent-fin/lp_bot
KEYS=/home/ubuntu/.kamino-keys
UNIT=/etc/systemd/system/lp-onboard.service
BACKUP=/etc/systemd/system/lp-onboard.service.pre-sandbox
NEXT=/etc/systemd/system/lp-onboard-next.service
ENVF=/etc/lp-onboard/env
WITHHELD='TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|JUPITER_API_KEY|ALCHEMY_RPC_KEY|KAMINO_RPC_KEY|WALLET_SECRET_PATH|LPBOT_SOL2_KEY_PATH|LPBOT_EVM_KEY_PATH|LPBOT_POLYGON_KEY_PATH|LPBOT_UNICHAIN_KEY_PATH'

say() { printf '== %s\n' "$*"; }
die() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo)"

serving() {   # port: the page answers with both chains
  curl -fsS --max-time 10 "http://127.0.0.1:$1/bot/api/config" 2>/dev/null | grep -q '"solana"' \
    && curl -fsS --max-time 10 "http://127.0.0.1:$1/bot/api/config" 2>/dev/null | grep -q '"polygon"'
}

wait_serving() {   # unit port
  for _ in $(seq 1 20); do
    if systemctl is-active --quiet "$1" && serving "$2"; then return 0; fi
    sleep 1
  done
  return 1
}

check_unit() {   # unit port: the sandbox as it runs
  local unit=$1 port=$2 pid
  wait_serving "$unit" "$port" || die "$unit is not active and serving on $port"
  pid=$(systemctl show -p MainPID --value "$unit")
  [ "$(ps -o user= -p "$pid")" = lp-onboard ] || die "$unit does not run as lp-onboard"
  grep -q ' /home .* - tmpfs ' "/proc/$pid/mountinfo" || die "$unit: /home is not a tmpfs"
  if nsenter -t "$pid" -m -- test -e "$KEYS"; then die "$unit can see $KEYS"; fi
  nsenter -t "$pid" -m -- test -r "$REPO/onboard/server.mjs" || die "$unit cannot read the code"
  if nsenter -t "$pid" -m -- test -w "$REPO/onboard/server.mjs"; then die "$unit can write the code"; fi
  if tr '\0' '\n' < "/proc/$pid/environ" | cut -d= -f1 | grep -qxE "$WITHHELD"; then
    die "$unit holds a withheld variable"
  fi
  [ "$(grep -c '^Seccomp:.*2' "/proc/$pid/status")" = 1 ] || die "$unit has no system call filter"
  grep -q '^NoNewPrivs:.*1' "/proc/$pid/status" || die "$unit may gain privileges"
  grep -q '^CapBnd:.*0000000000000000' "/proc/$pid/status" || die "$unit keeps capabilities"
  say "$unit: active as lp-onboard, serving on $port, /home hidden but the code (read-only), no withheld variable, seccomp on, no capabilities"
}

check_db() {   # the role reads what the store reads, and cannot write elsewhere
  local out
  out=$(printf '{"name":"zz-sandbox-check","wallet_id":"zz-sandbox-check-w"}' | systemd-run --quiet --wait --pipe \
    -p User=lp-onboard -p WorkingDirectory="$REPO" -p ProtectHome=tmpfs -p BindReadOnlyPaths="$REPO" \
    -E LPBOT_DSN=dbname=rebalancer -E PYTHONDONTWRITEBYTECODE=1 -E PATH=/usr/bin:/bin \
    /usr/bin/python3 "$REPO/onboard/store.py" taken) || die "the store does not run as lp-onboard"
  [ "$out" = '{"taken": false}' ] || die "the store as lp-onboard answered: $out"
  # in a transaction rolled back: even a wrong grant writes nothing
  if runuser -u lp-onboard -- psql -d rebalancer -Atq -v ON_ERROR_STOP=1 -c 'begin' \
      -c 'insert into rebalancer.events (ts, kind, detail) values (now(), $$x$$, $$x$$)' -c 'rollback' >/dev/null 2>&1; then
    die "lp-onboard may write the events table"
  fi
  say "Postgres: the store runs as lp-onboard; the role cannot write outside its tables"
}

rollback() {
  [ -f "$BACKUP" ] || die "no $BACKUP: nothing to roll back to"
  cp -p "$BACKUP" "$UNIT"
  systemctl daemon-reload
  systemctl restart lp-onboard.service
  wait_serving lp-onboard.service 8090 || die "the old unit does not serve either: see journalctl -u lp-onboard"
  say "rolled back: lp-onboard runs the unit it ran before (as ubuntu)"
}

case "${1:-}" in
  --rollback)
    rollback
    systemctl disable --now lp-onboard-start.path 2>/dev/null || true
    say "kept: user lp-onboard, role \"lp-onboard\", $ENVF, /var/lib/lp-onboard (harmless; remove by hand if wanted)"
    exit 0 ;;
  --check)
    check_unit lp-onboard.service 8090; check_db; exit 0 ;;
  '') ;;
  *) die "usage: $0 [--check|--rollback]" ;;
esac

for f in "$REPO/ops/lp-onboard.service" "$REPO/ops/onboard/lp-onboard-start.sh" "$REPO/ops/onboard/lp-onboard-start.path" \
         "$REPO/ops/onboard/lp-onboard-start.service" "$KEYS/vault-test/payer.json" "$KEYS/vault-test/policy.json" \
         "$KEYS/vault-test/evm.secret" "$KEYS/env" "$KEYS/onboard.env"; do
  [ -e "$f" ] || die "missing $f"
done

say "1. user lp-onboard"
id lp-onboard >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin lp-onboard

say "2. Postgres role \"lp-onboard\""
runuser -u postgres -- psql -d rebalancer -v ON_ERROR_STOP=1 -q <<'SQL'
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'lp-onboard') then create role "lp-onboard" login; end if;
end $$;
grant connect on database rebalancer to "lp-onboard";
grant usage on schema rebalancer to "lp-onboard";
grant select on all tables in schema rebalancer to "lp-onboard";
grant insert, update on rebalancer.onboard, rebalancer.wallets, rebalancer.vaults, rebalancer.vault_policies,
  rebalancer.config, rebalancer.swing to "lp-onboard";
grant usage, select on sequence rebalancer.onboard_id_seq, rebalancer.config_id_seq to "lp-onboard";
SQL
# a table added later is readable too; writes stay granted by name
runuser -u postgres -- psql -d rebalancer -v ON_ERROR_STOP=1 -q \
  -c 'alter default privileges for role ubuntu in schema rebalancer grant select on tables to "lp-onboard"'

say "3. $ENVF (LPBOT_RPC, LPBOT_ONBOARD_INVITE_SHA256)"
install -d -m 0750 -o root -g root /etc/lp-onboard
(umask 077
 { grep -E '^LPBOT_RPC=' "$KEYS/env" || true; grep -E '^LPBOT_ONBOARD_INVITE_SHA256=' "$KEYS/onboard.env" || true; } > "$ENVF.new")
chmod 600 "$ENVF.new"; mv "$ENVF.new" "$ENVF"
grep -q '^LPBOT_ONBOARD_INVITE_SHA256=' "$ENVF" || say "warning: no invite hashes: the page refuses every request"
grep -q '^LPBOT_RPC=' "$ENVF" || say "warning: no LPBOT_RPC: the page reads Solana from the public endpoint"

say "4. the start spool and its root helper"
install -d -m 0700 -o lp-onboard -g lp-onboard /var/lib/lp-onboard /var/lib/lp-onboard/start
install -m 0755 -o root -g root "$REPO/ops/onboard/lp-onboard-start.sh" /usr/local/sbin/lp-onboard-start
install -m 0644 -o root -g root "$REPO/ops/onboard/lp-onboard-start.service" /etc/systemd/system/lp-onboard-start.service
install -m 0644 -o root -g root "$REPO/ops/onboard/lp-onboard-start.path" /etc/systemd/system/lp-onboard-start.path
systemctl daemon-reload
systemctl enable --now lp-onboard-start.path

say "5. the new unit beside the old one, on 8091"
install -m 0644 -o root -g root "$REPO/ops/lp-onboard.service" "$NEXT"
install -d /etc/systemd/system/lp-onboard-next.service.d
printf '[Service]\nEnvironment=LPBOT_ONBOARD_PORT=8091\nRestart=no\n' > /etc/systemd/system/lp-onboard-next.service.d/port.conf
systemctl daemon-reload
systemctl restart lp-onboard-next.service
cleanup_next() {
  systemctl stop lp-onboard-next.service 2>/dev/null || true
  rm -rf "$NEXT" /etc/systemd/system/lp-onboard-next.service.d
  systemctl daemon-reload
}
if ! ( check_unit lp-onboard-next.service 8091 && check_db ); then
  journalctl -u lp-onboard-next.service -n 30 --no-pager -o cat | grep -v '"connect_report"' || true
  cleanup_next
  die "the new unit failed its checks; the running page was not touched"
fi
cleanup_next

say "6. the switch: lp-onboard.service is replaced and restarted"
[ -f "$BACKUP" ] || cp -p "$UNIT" "$BACKUP"
install -m 0644 -o root -g root "$REPO/ops/lp-onboard.service" "$UNIT"
systemctl daemon-reload
systemctl restart lp-onboard.service
if ! wait_serving lp-onboard.service 8090; then
  say "the new unit did not come up on 8090; rolling back"
  rollback
  die "switched back to the old unit"
fi
check_unit lp-onboard.service 8090
say "done. The old unit is kept at $BACKUP; --rollback restores it."
say "The sudoers rule /etc/sudoers.d/lp-onboard (for user ubuntu) is no longer used by the page."
