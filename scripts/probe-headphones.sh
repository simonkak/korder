#!/usr/bin/env bash
# probe-headphones.sh — read-only recon for the Sennheiser PXC 550 II
# Alexa-button + headphone-mic feature.
#
# Phase 1 of docs/../plans/new-feature-idea-i-elegant-catmull.md.
# Answers four questions:
#   1. What does BlueZ see (UUIDs / SDP records)?
#   2. Does the Alexa button emit anything visible to userspace?
#   3. Does HFP mSBC work on this card and does the mic appear?
#   4. How long does A2DP <-> HFP profile-switch take?
#
# Usage:
#   scripts/probe-headphones.sh                        # auto-detect Sennheiser
#   scripts/probe-headphones.sh AA:BB:CC:DD:EE:FF      # explicit MAC
#   scripts/probe-headphones.sh --record-only          # skip questions 1,4 — only the live monitor
#
# The script is non-destructive but it WILL flip the BT card profile to HFP
# and back during step 3. Music playing through the headphones will pause
# briefly. The script restores the original profile when it exits or you Ctrl-C.

set -uo pipefail

MAC=""
RECORD_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --record-only) RECORD_ONLY=1 ;;
    --help|-h) sed -n '2,22p' "$0"; exit 0 ;;
    *) MAC="$arg" ;;
  esac
done

hr() { printf '\n%s\n%s\n' "=== $* ===" "$(printf '%.0s-' $(seq 1 ${#1}))----"; }
note() { printf '  • %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

need() {
  command -v "$1" >/dev/null 2>&1 || { warn "missing tool: $1 (skipping section)"; return 1; }
}

# ---------------------------------------------------------------------------
# 0. Locate the device
# ---------------------------------------------------------------------------
hr "0. Device discovery"
if [ -z "$MAC" ]; then
  if need bluetoothctl; then
    MAC=$(bluetoothctl devices | awk 'tolower($0) ~ /sennheiser|pxc|m3aebt|m4aebt/ {print $2; exit}')
  fi
fi
if [ -z "$MAC" ]; then
  warn "no MAC argument and no Sennheiser found in 'bluetoothctl devices'."
  warn "pair the headphones first, then re-run with explicit MAC argument."
  if need bluetoothctl; then
    echo
    note "currently known devices:"
    bluetoothctl devices | sed 's/^/    /'
  fi
  exit 1
fi
note "using MAC: $MAC"

DEV_PATH="/org/bluez/hci0/dev_${MAC//:/_}"
note "BlueZ object path: $DEV_PATH"

# Connection state — many recon steps require a live ACL link.
CONNECTED=$(bluetoothctl info "$MAC" 2>/dev/null | awk -F': ' '/Connected:/ {print $2; exit}')
note "Connected: ${CONNECTED:-unknown}"
if [ "${CONNECTED:-no}" != "yes" ]; then
  warn "headphones not currently connected — connect them, then re-run."
  warn "(Settings → Bluetooth, or 'bluetoothctl connect $MAC')"
fi

# ---------------------------------------------------------------------------
# 1. SDP / UUIDs — what services does the device advertise?
# ---------------------------------------------------------------------------
if [ "$RECORD_ONLY" -eq 0 ]; then
  hr "1. Advertised services (looking for AMA / non-standard RFCOMM)"
  if need bluetoothctl; then
    note "bluetoothctl info — UUIDs:"
    bluetoothctl info "$MAC" 2>/dev/null | grep -E "UUID|Name|Class|Modalias|Icon" | sed 's/^/    /'
  fi
  echo
  if need busctl; then
    note "BlueZ Device1 properties (full UUID list, no truncation):"
    busctl --system get-property org.bluez "$DEV_PATH" org.bluez.Device1 UUIDs 2>/dev/null \
      | tr ' ' '\n' | grep -oE '"[0-9a-f-]{8,}"' | tr -d '"' | sort -u \
      | while read -r uuid; do
          # Annotate well-known UUIDs; flag unknowns as candidates.
          case "$uuid" in
            0000110b*) printf '    %s  (Audio Sink — A2DP)\n' "$uuid" ;;
            0000110a*) printf '    %s  (Audio Source — A2DP)\n' "$uuid" ;;
            0000110e*|0000110c*) printf '    %s  (AVRCP)\n' "$uuid" ;;
            0000111e*|0000111f*) printf '    %s  (HFP)\n' "$uuid" ;;
            00001108*) printf '    %s  (Headset)\n' "$uuid" ;;
            00001200*) printf '    %s  (PnP / DI)\n' "$uuid" ;;
            931c7e8a-540f-4686-b798-e8df0a2ad9f7) printf '    %s  *** AMA (Alexa Mobile Accessory) ***\n' "$uuid" ;;
            *) printf '    %s  (UNKNOWN — candidate for AMA / vendor RFCOMM)\n' "$uuid" ;;
          esac
        done
  fi
  echo
  note "raw 'bluetoothctl info' for the record:"
  bluetoothctl info "$MAC" 2>/dev/null | sed 's/^/    /'
fi

# ---------------------------------------------------------------------------
# 2. Live monitor — what fires when you press the Alexa button?
# ---------------------------------------------------------------------------
hr "2. Live event monitor — press the Alexa button now (and play/pause too)"
note "monitoring for ~12 s. Press the Alexa button 2-3 times during this window."
note "also try: short-press play/pause, long-press play/pause, vol+/-."
note "we capture (a) BlueZ DBus signals, (b) any new evdev key events."
note "NOTE: if /dev/input/eventN nodes show 'not readable', re-run as"
note "      'sudo bash $0 $MAC' OR add yourself to the 'input' group:"
note "          sudo usermod -aG input \$USER  # then log out + back in"
echo

# 2a) DBus capture (system bus, BlueZ signals only)
DBUS_OUT=$(mktemp)
dbus-monitor --system \
  "type='signal',sender='org.bluez'" \
  "type='signal',interface='org.freedesktop.DBus.Properties',path_namespace='/org/bluez'" \
  >"$DBUS_OUT" 2>/dev/null &
DBUS_PID=$!

# 2b) evtest snapshot — list all input devices that look BT/HID, and start
#     a parallel reader on each. evtest needs read access to /dev/input/event*;
#     if the user isn't in the 'input' group some nodes will be unreadable.
EVTEST_DIR=$(mktemp -d)
EVTEST_PIDS=()
if need evtest; then
  while IFS= read -r line; do
    case "$line" in
      *"$MAC"*|*Sennheiser*|*PXC*|*Headset*|*Bluetooth*)
        # The next "/dev/input/event*" line in the listing belongs to this device,
        # but parsing /proc/bus/input/devices is more reliable:
        :
        ;;
    esac
  done < <(evtest --version 2>&1 || true)

  # Use /proc/bus/input/devices for a stable mapping name -> eventN.
  awk '
    /^I:/  { handlers=""; name="" }
    /^N: Name=/  { sub(/^N: Name="/, ""); sub(/"$/, ""); name=$0 }
    /^H: Handlers=/ {
      sub(/^H: Handlers=/, ""); handlers=$0
      if (match(handlers, /event[0-9]+/)) {
        ev=substr(handlers, RSTART, RLENGTH)
        if (tolower(name) ~ /sennheiser|pxc|headset|bluetooth|avrcp/)
          print "/dev/input/" ev "\t" name
      }
    }
  ' /proc/bus/input/devices > "$EVTEST_DIR/list" || true

  if [ -s "$EVTEST_DIR/list" ]; then
    note "watching evdev nodes:"
    sed 's/^/    /' "$EVTEST_DIR/list"
    while IFS=$'\t' read -r node name; do
      if [ -r "$node" ]; then
        evtest --grab "$node" >"$EVTEST_DIR/$(basename "$node").log" 2>&1 &
        EVTEST_PIDS+=($!)
      else
        warn "$node not readable (you may need to be in the 'input' group)."
      fi
    done < "$EVTEST_DIR/list"
  else
    note "no BT-related evdev nodes — the button is probably NOT exposed as a key event."
    note "(this is the expected case for AMA-protocol buttons.)"
  fi
fi

# Countdown so the user knows when to press.
for s in 12 10 8 6 4 2; do
  sleep 2
  printf '\r  monitoring... %ds remaining ' "$s"
done
printf '\r%-40s\n' "  monitoring window closed."

kill "$DBUS_PID" 2>/dev/null || true
for p in "${EVTEST_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
wait 2>/dev/null || true

echo
note "DBus signals captured:"
if [ -s "$DBUS_OUT" ]; then
  # dbus-monitor puts the whole signal envelope on one line:
  #   signal time=... path=/foo; interface=Bar; member=Baz
  # — semicolons separate fields after path. Parse all on the first line.
  awk '
    /^signal / {
      path=""; iface=""; member=""
      for (i=1; i<=NF; i++) {
        if ($i ~ /^path=/)      { path=$i;   sub(/^path=/,"",path);     sub(/;$/,"",path) }
        if ($i ~ /^interface=/) { iface=$i;  sub(/^interface=/,"",iface); sub(/;$/,"",iface) }
        if ($i ~ /^member=/)    { member=$i; sub(/^member=/,"",member); sub(/;$/,"",member) }
      }
      printf "    %-50s %-40s %s\n", path, iface, member
    }
  ' "$DBUS_OUT" | sort -u | head -40
  echo
  note "(full dump in: $DBUS_OUT — keep this around for the recon doc)"
else
  note "  no DBus signals from BlueZ during the window."
fi

echo
note "evdev events captured:"
shopt -s nullglob
ev_logs=("$EVTEST_DIR"/*.log)
if [ "${#ev_logs[@]}" -gt 0 ]; then
  for log in "${ev_logs[@]}"; do
    if grep -q "Event code" "$log"; then
      note "  $(basename "$log"):"
      grep -E "Event:.*code|EV_KEY" "$log" | head -20 | sed 's/^/      /'
    fi
  done
fi
shopt -u nullglob
[ "${#ev_logs[@]}" -eq 0 ] && note "  (no evdev nodes were watched.)"

# ---------------------------------------------------------------------------
# 3. HFP mSBC + mic check
# ---------------------------------------------------------------------------
if [ "$RECORD_ONLY" -eq 0 ]; then
  hr "3. HFP profile / mic source / mSBC support"
  if need pactl; then
    CARD_NAME="bluez_card.${MAC//:/_}"
    note "looking for card: $CARD_NAME"
    if ! pactl list cards short | grep -q "$CARD_NAME"; then
      warn "card not present — headphones must be connected."
    else
      note "card profiles available:"
      pactl list cards | awk -v card="$CARD_NAME" '
        $0 ~ "Name: "card { in_card=1 }
        in_card && /^\tProfiles:/ { in_profiles=1; next }
        in_card && in_profiles && /^\t[A-Z]/ { in_profiles=0 }
        in_card && in_profiles { print "    " $0 }
        in_card && /^\tActive Profile:/ { print "    " $0; in_card=0 }
      '

      ORIG_PROFILE=$(pactl list cards | awk -v card="$CARD_NAME" '
        $0 ~ "Name: "card { in_card=1 }
        in_card && /Active Profile:/ { sub(/^[^:]+: /, ""); print; exit }
      ')
      note "active profile right now: $ORIG_PROFILE"

      # Try to switch to head-unit-msbc, fall back to head-unit (CVSD).
      restore_profile() {
        if [ -n "${ORIG_PROFILE:-}" ]; then
          note "restoring original profile: $ORIG_PROFILE"
          pactl set-card-profile "$CARD_NAME" "$ORIG_PROFILE" 2>/dev/null || true
        fi
      }
      trap restore_profile EXIT INT TERM

      # PipeWire/WirePlumber drops the -msbc/-cvsd suffix and uses bare
      # 'headset-head-unit' for mSBC; PulseAudio-Bluetooth uses suffixes.
      # Try both naming schemes.
      TAB=$(printf '\t')
      for prof in headset-head-unit-msbc headset-head-unit handsfree-head-unit-msbc handsfree-head-unit headset-head-unit-cvsd; do
        if pactl list cards | awk -v card="$CARD_NAME" '
          $0 ~ "Name: "card { in_card=1 }
          in_card && /^\tProfiles:/ { in_profiles=1; next }
          in_card && in_profiles { print }
          in_card && /^\t[A-Z]/ { in_profiles=0 }
        ' | grep -qF "${TAB}${prof}:"; then
          note "trying profile: $prof"
          T0=$(date +%s%N)
          if pactl set-card-profile "$CARD_NAME" "$prof" 2>/dev/null; then
            T1=$(date +%s%N)
            note "  switch took $(( (T1 - T0) / 1000000 )) ms"
            sleep 1   # let sources show up
            note "  sources visible now:"
            pactl list sources short | grep -i "$CARD_NAME\|bluez" | sed 's/^/      /' || note "      (none — mic did not appear)"

            BT_SOURCE=$(pactl list sources short | awk -v card="$CARD_NAME" '$2 ~ card { print $2; exit }')
            if [ -n "$BT_SOURCE" ]; then
              note "  recording 3 s sample from $BT_SOURCE..."
              SAMPLE=$(mktemp --suffix=.wav)
              if parecord -d "$BT_SOURCE" --file-format=wav --rate=16000 --channels=1 "$SAMPLE" &
                  PA_PID=$!
                  sleep 3
                  kill "$PA_PID" 2>/dev/null
                  wait "$PA_PID" 2>/dev/null
                  [ -s "$SAMPLE" ]
              then
                note "  sample saved: $SAMPLE ($(stat -c%s "$SAMPLE") bytes)"
                note "  → play with: paplay $SAMPLE"
              else
                warn "  sample recording produced no data."
              fi
            fi
            break
          else
            warn "  profile switch failed."
          fi
        fi
      done
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 4. Profile-switch latency (round-trip)
# ---------------------------------------------------------------------------
if [ "$RECORD_ONLY" -eq 0 ] && command -v pactl >/dev/null 2>&1 && [ -n "${ORIG_PROFILE:-}" ]; then
  hr "4. A2DP <-> HFP round-trip latency (3 trials)"
  CARD_NAME="bluez_card.${MAC//:/_}"
  for trial in 1 2 3; do
    A0=$(date +%s%N)
    pactl set-card-profile "$CARD_NAME" a2dp-sink 2>/dev/null || true
    A1=$(date +%s%N)
    pactl set-card-profile "$CARD_NAME" headset-head-unit-msbc 2>/dev/null \
      || pactl set-card-profile "$CARD_NAME" headset-head-unit 2>/dev/null || true
    A2=$(date +%s%N)
    note "trial $trial: A2DP→idle = $(( (A1-A0)/1000000 )) ms, idle→HFP = $(( (A2-A1)/1000000 )) ms"
    sleep 1
  done
fi

hr "Done"
note "next: paste the highlighted sections of this output into"
note "      docs/headphone-recon.md (template generated alongside this script)."
