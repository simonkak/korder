#!/usr/bin/env bash
# probe-hfp-mic.sh — flip the BT card to HFP/mSBC, record 5 s from the
# headphone mic, restore A2DP. Outputs a WAV file you can play back to
# judge whether the mic audio quality is good enough for Whisper.
#
# Usage:
#   scripts/probe-hfp-mic.sh                      # auto-detect Sennheiser
#   scripts/probe-hfp-mic.sh AA:BB:CC:DD:EE:FF
#   scripts/probe-hfp-mic.sh AA:BB:CC:DD:EE:FF 10  # record 10 s
#
# After it finishes:
#   paplay /tmp/hfp-mic-sample.wav      # listen
#   uv run python -c "
#     from korder.transcribe.whisper_engine import WhisperEngine
#     import wave, numpy as np
#     w = wave.open('/tmp/hfp-mic-sample.wav','rb')
#     buf = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)/32768
#     e = WhisperEngine(model='medium', language='pl')
#     print(e.transcribe(buf))
#   "

set -euo pipefail

MAC="${1:-}"
SECONDS_TO_RECORD="${2:-5}"
OUT="/tmp/hfp-mic-sample.wav"

if [ -z "$MAC" ]; then
  MAC=$(bluetoothctl devices | awk 'tolower($0) ~ /sennheiser|pxc/ {print $2; exit}')
fi
if [ -z "$MAC" ]; then
  echo "ERROR: pass the headphone MAC, or pair them first." >&2
  exit 1
fi

CARD="bluez_card.${MAC//:/_}"
if ! pactl list cards short | grep -q "$CARD"; then
  echo "ERROR: card $CARD not present. Connect the headphones first." >&2
  exit 1
fi

ORIG=$(pactl list cards | awk -v c="$CARD" '$0 ~ "Name: "c {f=1} f && /Active Profile:/ {sub(/^[^:]+: /, ""); print; exit}')
echo "  original profile: $ORIG"

restore() {
  echo "  restoring: $ORIG"
  pactl set-card-profile "$CARD" "$ORIG" >/dev/null 2>&1 || true
}
trap restore EXIT INT TERM

# WirePlumber: bare 'headset-head-unit' is mSBC. PA-Bluetooth: '-msbc' suffix.
# Try both, in mSBC-first order (wideband, much better for Whisper).
for prof in headset-head-unit-msbc headset-head-unit handsfree-head-unit-msbc handsfree-head-unit headset-head-unit-cvsd; do
  if pactl list cards | grep -qE "^[[:space:]]+$prof:"; then
    if pactl set-card-profile "$CARD" "$prof" 2>/dev/null; then
      echo "  switched to: $prof"
      ACTIVE_PROF="$prof"
      break
    fi
  fi
done

if [ -z "${ACTIVE_PROF:-}" ]; then
  echo "ERROR: no HFP profile could be activated." >&2
  exit 1
fi

# PipeWire/WirePlumber creates the BT input source LAZILY — it doesn't show
# up in `pactl list sources` until a client opens it. So we (a) try to
# guess the source name and pass it directly to parecord, (b) fall back to
# parecord without --device (uses the default source, which on PipeWire
# follows the active profile if no other source is competing).
sleep 0.5
EXPECTED="bluez_input.${MAC//:/_}.0"
SRC=$(pactl list sources short | awk -v c="$CARD" '$2 ~ c || $2 ~ /bluez_input/ {print $2; exit}')

echo "  diagnostics — current sources matching bluez/bluez_input:"
pactl list sources short | grep -iE 'bluez|bluetooth|hfp|sco' | sed 's/^/    /' || echo "    (none yet — expected on PipeWire; will materialize when we open it)"
echo

if [ -z "${SRC:-}" ]; then
  SRC="$EXPECTED"
  echo "  source not yet visible; trying to open '$SRC' directly (PipeWire materializes on open)..."
else
  echo "  using visible source: $SRC"
fi

echo "  recording $SECONDS_TO_RECORD s from: $SRC"
echo "  >>> SPEAK NOW <<<"
parecord --device="$SRC" --file-format=wav --rate=16000 --channels=1 "$OUT" 2>"/tmp/parecord.err" &
PID=$!
sleep "$SECONDS_TO_RECORD"
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true

if [ ! -s "$OUT" ] && [ -s "/tmp/parecord.err" ]; then
  echo "  parecord error output:"
  sed 's/^/    /' /tmp/parecord.err
  echo
  echo "  retrying without --device (use whatever is the system default source)..."
  parecord --file-format=wav --rate=16000 --channels=1 "$OUT" 2>"/tmp/parecord.err" &
  PID=$!
  sleep "$SECONDS_TO_RECORD"
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
fi

if [ -s "$OUT" ]; then
  SIZE=$(stat -c%s "$OUT")
  echo "  saved: $OUT ($SIZE bytes)"
  echo
  echo "Listen:    paplay $OUT"
  echo "Transcribe (verifies Whisper handles HFP audio):"
  echo "  uv run python scripts/probe-hfp-transcribe.py $OUT"
else
  echo "ERROR: recording is empty." >&2
  exit 1
fi
