#!/bin/bash
# Starts the profiles the onboarding page asks for (root; installed as
# /usr/local/sbin/lp-onboard-start by ops/onboard/setup_sandbox.sh and run by
# lp-onboard-start.service when lp-onboard-start.path sees a file in the spool).
#
# The sandboxed page (ops/lp-onboard.service, user lp-onboard) cannot sudo. It
# asks for a start by an empty file named after the profile in the spool. Each
# entry is removed first, then its name is checked as the page checks it
# (onboard/intent.mjs NAME, no "--"), then
#     systemctl enable --now lp-bot@<name>.service
# runs, and nothing else: the same right the old sudoers rule gave
# (ops/onboard/lp-onboard.sudoers). Anything that is not a regular file, or
# whose name does not match, is removed and logged, never acted on. At most
# MAX entries per run; the path unit runs this again while the spool is not
# empty.
#
# The environment may move the spool and systemctl for the tests
# (tests/test_onboard_sandbox.py); the unit sets neither.
set -euo pipefail
SPOOL="${LPBOT_ONBOARD_SPOOL:-/var/lib/lp-onboard/start}"
SYSTEMCTL="${LPBOT_ONBOARD_SYSTEMCTL:-/usr/bin/systemctl}"
MAX=20
NAME_RE='^[a-z][a-z0-9-]{1,30}[a-z0-9]$'

log() { printf 'lp-onboard-start: %s\n' "$*"; }

[ -d "$SPOOL" ] || { log "no spool $SPOOL"; exit 0; }
n=0
while IFS= read -r -d '' entry; do
  name="${entry##*/}"
  kind=other
  if [ -f "$entry" ] && [ ! -L "$entry" ]; then kind=file; fi
  # removed before anything else: a bad entry never runs twice
  rm -rf --one-file-system -- "$entry"
  if [ "$kind" != file ]; then log "refused: ${name@Q} is not a regular file"; continue; fi
  if ! [[ "$name" =~ $NAME_RE ]] || [[ "$name" == *--* ]]; then log "refused: name ${name@Q}"; continue; fi
  if "$SYSTEMCTL" enable --now "lp-bot@${name}.service"; then
    log "started lp-bot@${name}.service"
  else
    log "failed: systemctl enable --now lp-bot@${name}.service"
  fi
  n=$((n + 1))
  [ "$n" -lt "$MAX" ] || break
done < <(find "$SPOOL" -mindepth 1 -maxdepth 1 -print0)
