#!/usr/bin/env bash
# Phase 0 runner for the Ajax DoorBell (and later the NVR).
#
#   bash tools/phase0/run_phase0.sh                 # DoorBell at 192.168.128.31
#   bash tools/phase0/run_phase0.sh --nvr --host <NVR IP> --events 0
#
# Asks for the ONVIF account created in the Ajax app (password typed hidden, never
# written to disk or shell history), then runs probe_onvif.py. The report holds
# no password and is saved under tools/phase0/reports/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HERE/reports"
REPORT="$HERE/reports/report-$(date +%Y%m%d-%H%M%S).txt"

read -rp "Utilisateur ONVIF [homeassistant] : " ONVIF_USER
ONVIF_USER="${ONVIF_USER:-homeassistant}"
read -rsp "Mot de passe ONVIF : " ONVIF_PASS
echo

EXTRA=()
if [[ " $* " != *" --nvr "* ]]; then
  read -rp "Jouer un bip d'1 s dans le haut-parleur de la sonnette pour tester le micro ? [o/N] " BEEP
  [[ "${BEEP,,}" == o* ]] && EXTRA+=(--beep)
  echo "Pendant l'écoute des événements (90 s, annoncée à l'écran), appuie sur la sonnette."
fi

status=0
AJAX_ONVIF_USER="$ONVIF_USER" AJAX_ONVIF_PASS="$ONVIF_PASS" \
  python3 -I "$HERE/probe_onvif.py" --report "$REPORT" "${EXTRA[@]}" "$@" || status=$?

unset ONVIF_PASS ONVIF_USER
exit "$status"
