#!/usr/bin/env bash
# Phase 0 runner for the Ajax DoorBell (and later the NVR).
#
#   bash tools/phase0/run_phase0.sh                 # DoorBell at 192.168.128.31
#   bash tools/phase0/run_phase0.sh --nvr --host <NVR IP> --events 0
#   bash tools/phase0/run_phase0.sh --cameras --host <NVR IP>   # every camera on the NVR
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
if [[ " $* " == *" --cameras "* ]]; then
  echo "Déroulé : environ 2 min de mesures automatiques (rien à faire), puis l'écran affiche"
  echo "« MAINTENANT : passe 2 ou 3 fois devant la TurretCam, puis va sonner à la porte »."
  echo "Tu as alors 3 min. Ne décroche pas dans l'app Ajax."
  echo
elif [[ " $* " != *" --nvr "* && " $* " != *" --listen "* ]]; then
  read -rp "Jouer des bips dans le haut-parleur de la sonnette pour tester le micro ? [o/N] " BEEP
  [[ "${BEEP,,}" == o* ]] && EXTRA+=(--beep)
  echo
  echo "Déroulé : environ 20 s de tests automatiques, puis l'écran affiche"
  echo "« APPUIE SUR LA SONNETTE MAINTENANT ». Tu as alors 3 min pour aller sonner."
  echo "Ne décroche pas dans l'app Ajax."
  [[ " ${EXTRA[*]-} " == *" --beep "* ]] &&
    echo "Après l'appui, reste devant la porte : 5 à 10 s plus tard, la sonnette doit jouer 3 bips courts et aigus, puis 2 bips longs et graves."
  echo
fi

status=0
AJAX_ONVIF_USER="$ONVIF_USER" AJAX_ONVIF_PASS="$ONVIF_PASS" \
  python3 -I "$HERE/probe_onvif.py" --report "$REPORT" ${EXTRA[@]+"${EXTRA[@]}"} "$@" || status=$?

unset ONVIF_PASS ONVIF_USER
exit "$status"
