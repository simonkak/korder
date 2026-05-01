# PXC 550 II — Linux recon

Phase 1 of the Alexa-button + headphone-mic feature
(plan: `~/.claude/plans/new-feature-idea-i-elegant-catmull.md`).

Run on **2026-04-30**, BlueZ on KDE Plasma 6 / WirePlumber.

---

## Device

- **Model**: Sennheiser PXC 550-II
- **MAC**: `00:1B:66:E8:90:10`
- **Modalias**: `bluetooth:v0492p600Dd0106`
  (vendor 0x0492 = Sennheiser, product 0x600D = PXC 550-II, hw rev 1.6)
- **Pairing notes**: Standard pairing via Plasma Bluetooth applet. No
  Sennheiser Smart Control app or Alexa-account registration was performed
  before recon — so any AMA gating that requires that registration would
  show up as a connection refusal in the next probe step.

---

## Q1 — What does BlueZ see?

**AMA UUID `931c7e8a-540f-4686-b798-e8df0a2ad9f7` is advertised.** Path is
viable in principle.

Other interesting (non-standard) UUIDs:

| UUID | Meaning | Notes |
|------|---------|-------|
| `931c7e8a-540f-4686-b798-e8df0a2ad9f7` | **AMA RFCOMM** (Amazon Mobile Accessory) | the target |
| `0000fe03-0000-1000-8000-00805f9b34fb` | Amazon GATT (BLE) | BLE companion service, ignore — we're going RFCOMM |
| `0000fdce-0000-1000-8000-00805f9b34fb` | Sennheiser GATT | BLE control protocol used by Smart Control app — possible alt button source |
| `1ddce62a-ecb1-4455-8153-0743c87aec9f` | Sennheiser vendor | possibly Smart Control RFCOMM control |
| `f7d92a0a-dfe8-98b7-8646-0f548a7e1c93` | unknown vendor | |
| `00000000-deca-fade-deca-deafdecacaff` | well-known "test" UUID | reserved/joke pattern, not real |
| `ffcacade-afde-cade-defa-cade00000000` | well-known "test" UUID | reserved/joke pattern, not real |

Standard UUIDs present (expected): A2DP source/sink/AVRCP/HFP/Headset/PnP/GAP/GATT/DI/Battery.

**Verdict**: pursue **AMA RFCOMM** (the `931c7e8a-...` UUID). Sennheiser
GATT (`fdce`) is a fallback if AMA refuses connection.

---

## Q2 — Does pressing the Alexa button produce a visible event?

**Inconclusive — but not blocking.**

- evdev: `/dev/input/event20` (PXC 550-II AVRCP) was not readable —
  user not in `input` group during the run. Re-run with `sudo` or after
  `sudo usermod -aG input $USER` + relogin to confirm.
- DBus: probe captured nothing during the 12 s window. The script's awk
  parser had a bug (interface/member on the same line as `signal `, not
  separate lines), so signals may have fired and been dropped. Will be
  fixed in the next probe revision, but in practice we don't need this
  signal — RFCOMM AMA delivers the button event directly.

**Verdict**: skip — AMA RFCOMM listener is the trigger source regardless.

---

## AMA RFCOMM connectivity (Phase 1.5)

**`scripts/probe-ama-connect.py` confirms AMA is reachable on RFCOMM
channel 19 with no handshake required.**

Channels that accepted RFCOMM connections: `[1, 14, 19]`.
- Channel 1 — accepted, silent. Most likely HSP/HFP audio gateway slot.
- Channel 14 — flaky (timed out then refused). Probably a Sennheiser
  vendor RFCOMM (Smart Control protocol).
- **Channel 19 — accepted, emits a 20-byte greeting on connect, then
  emits a frame on every Alexa-button press.** This is AMA.

**Greeting frame (t=0)**:
```
fe 03 01 00 00 00 00 00 00 00 00 00 00 00 00 00  ................
00 00 00 00                                      ....
```
The `fe 03 01` magic is consistent with AMA's protocol-version envelope.

**Button-press frame (captured ~18 s into hold)**:
```
00 00 0f 08 66 b2 06 0a 0a 08 08 86 04 18 80 80  ....f...........
90 03                                            ..
```
Decomposes as: 3-byte length header (`00 00 0f` = 15-byte payload),
then protobuf: `{field_1: 102, field_102: {field_1: {field_1: 518,
field_3: 6553600}}}`. Likely the AMA `Event` envelope with a
button-id (518) and timestamp (μs).

**Implication for Phase 2**: we don't need a full protobuf decoder.
The trigger is "any frame received after the connection-greeting" —
that's already enough to fire `toggle_recording()`. Refinement to
parse different button gestures (long-press, double-tap) can follow
once we capture more frame samples.

---

## Q3 — HFP mSBC + mic check

**mSBC profile activates correctly**, mic source materializes on
record-stream open (PipeWire BT source laziness). Quality test pending.

```
Profiles available on bluez_card.00_1B_66_E8_90_10:
    off
    a2dp-sink-sbc
    a2dp-sink-sbc_xq
    a2dp-sink-aac
    a2dp-sink                  (aptX)
    a2dp-sink-aptx_ll          (currently active)
    headset-head-unit-cvsd     (HSP/HFP, narrowband CVSD 8 kHz)
    headset-head-unit          (HSP/HFP, *wideband mSBC 16 kHz*)
```

Note: this is **PipeWire/WirePlumber**, where `headset-head-unit` *is* the
mSBC profile (no `-msbc` suffix). The probe script's `-msbc` suffix list
was for PulseAudio-Bluetooth and didn't match.

**Verdict**: HFP mSBC works in principle — pending an actual
record-and-listen test (next probe).

---

## Q4 — Profile-switch latency

| trial | A2DP → idle | idle → HFP | total |
|-------|-------------|------------|-------|
| 1     | 20 ms       | 23 ms      | 43 ms |
| 2     | 19 ms       | 34 ms      | 53 ms |
| 3     | 45 ms       | 37 ms      | 82 ms |

**Verdict**: well under 500 ms. Switch on demand at `_start_recording`,
flip back at `_stop_recording`. **No pre-arm thread, no buffer-pre-roll
required**. Music will pause briefly during recording (acceptable —
recording sessions are short).

---

## Phase 2 design adjustments

Based on this recon:

- **Trigger source**: **AMA RFCOMM** — `ama_button.py` thread will
  connect to UUID `931c7e8a-540f-4686-b798-e8df0a2ad9f7` and listen for
  button-press protobuf frames. Sennheiser GATT is the documented
  fallback if AMA refuses connection.
- **HFP pre-arm**: **none**. Switch on demand. The MicRecorder reroute
  happens inline with `_start_recording`; profile flip back to A2DP
  happens in `_stop_recording`.
- **A2DP music UX**: brief pause (~50 ms) is acceptable. No special
  handling needed.
- **`bt_presence.py`**: still needed — DBus subscribe to
  `org.bluez.Device1.Connected` so we re-arm the AMA listener and the
  source-route preference when the headphones reconnect after a power
  cycle.

**Open follow-ups before Phase 2 implementation**:

1. ☑ Run `scripts/probe-ama-connect.py` — **DONE, AMA confirmed on ch 19.**
2. ☐ Run revised `scripts/probe-hfp-mic.sh` — record 5 s from BT mic,
   listen back, run through Whisper. Verifies HFP audio is intelligible
   enough for transcription. (PipeWire source-laziness fix in place.)

### Field-test gotcha (2026-05-01)

After Phase 2 was wired up and `[bluetooth] enabled = true`, korder
hit `ECONNREFUSED` on ch 19 *and* the fallback channels — the same
device that emitted button frames during recon now refuses every
RFCOMM connect. Diagnosis: pressing the Alexa button while no AMA
host is connected triggers the headphones' **"haven't connected to
Alexa app" voice prompt** AND deactivates the AMA service until
re-armed.

The PXC 550-II expects an initial registration handshake with
Amazon's Alexa app on a phone. During the recon-time window this
seems to have been satisfied transiently (perhaps the headphones
were within their original pairing-window grace period); after the
unregistered button-press, the service goes dormant.

**Unblock (registration)**: install the Alexa Android app, sign in
to any Amazon account, pair the headphones via Add Device. After
that, AMA accepts RFCOMM connections from Linux. *But* — see below.

### AMA protocol exploration (2026-05-01) — DEAD END

After Alexa-app registration, the device accepts our connection,
sends a 20-byte greeting, and responds to a discovered subset of
the AMA control protocol. We mapped the command space empirically:

| cmd | response | likely meaning |
|-----|----------|----------------|
| 20 | DeviceInformation (serial, name, transports, type) | GetDeviceInformation |
| 21 | DeviceConfiguration {field_2: 1} | GetDeviceConfiguration |
| 23, 24, 31, 55, 56, 60 | empty success | KeepAlive / Synchronize* (ack-style) |
| 30 | empty submessage at field 8 | GetState (no feature) |
| 40 | UNKNOWN error | known, requires args |
| 50 | empty success + unsolicited cmd 103 | **Disconnect/EndSession — DO NOT send** |
| 100 | INVALID + empty field 7 | **Authentication command — gated** |
| 102 | (device→host) | ButtonNotification |
| 103 | (device→host) | session-end notification |

The full handshake `[20, 21, 23, 24, 30, 31]` puts the device into
"host present, awaiting auth" state — the voice prompt changes
from "haven't connected to Alexa app" to **"Open the Alexa app and
try again"**. Real progress, but not enough.

**Cmd 100 is the wall.** It validates input cryptographically:
empty payload returns `Response{error_code=INVALID, field_7=empty}`
— no schema hint, no missing-field name. Field 7 in Response.payload
is the validation-error oneof, deliberately opaque to keep
unauthorized clients from inferring the request shape. Without
either (a) the AMA `.proto` files (gated behind Amazon developer
agreement) or (b) a captured Alexa-app handshake (LWA-token-bound,
not replayable across sessions), cmd 100 cannot be satisfied.

**Status**: stuck at cmd 100 in the unauthenticated-host state.

### Remaining avenues (2026-05-01)

1. **Populated cmd 100 (low odds, no cost).** The cmd 100 INVALID
   response could be triggered by *any* malformed payload, not
   necessarily missing-credential. Worth a one-shot attempt at
   sending a populated payload (mirror of DeviceInformation) before
   concluding the request shape is unreachable.

2. **Sennheiser vendor RFCOMM (ch 14).** The recon SDP listing
   showed UUID `1ddce62a-ecb1-4455-8153-0743c87aec9f` plus the
   Sennheiser GATT service `0000fdce-...`. The Smart Control app
   uses these to talk to the headphones outside the AMA stack —
   button events *might* also be mirrored there with weaker auth.
   Brief probe-ama scan showed ch 14 intermittently accepts.

3. **Android HCI snoop capture (high odds, ~30 min).** Enable
   "Bluetooth HCI snoop log" on a phone, re-pair the PXC 550-II
   through the Alexa app, capture the post-greeting bytes. Replay
   the captured cmd 100 payload from korder. This is the realistic
   path if (1) and (2) fail — works iff the device-side auth state
   is "registered with account X, accept any host that says X"
   rather than a per-session token rotation.

4. **AMA Kit download (free Amazon developer signup).** Sign up at
   developer.amazon.com (free), agree to AVS terms, download AMA
   Kit which contains `accessories.proto`. With the schema we'd
   know exactly what cmd 100 wants. Combined with (3) for the
   actual values.

---

## Final status (2026-05-01)

### What ships and works

- **Auto-mic switching** when the configured Bluetooth headphones
  connect/disconnect. `BluezPresenceWatcher` listens via `gdbus monitor`,
  flips `MicRecorder.device` between the BT input source
  (`bluez_input.<MAC>.0`) and the desk mic.
- **HFP profile switching** at recording boundaries — flips the BT card
  to `headset-head-unit` (mSBC) at `_start_recording`, restores the prior
  A2DP profile at `_stop_recording`. ~50 ms each way.
- **AMA RFCOMM listener** with auto-reconnect, exponential backoff,
  channel-fallback sweep, and proper "AMA dormant — register via Alexa
  app" hint after persistent refusals. Connects fine post-Alexa-app
  registration; receives the device's greeting frame.
- **Tests**: 105 passing (87 pre-existing + 18 new for sockaddr_rc
  packing and pactl parsing). Zero regressions.
- **Probe scripts**: `probe-headphones.sh`, `probe-ama-connect.py`,
  `probe-ama-handshake.py`, `probe-ama-scan.py`, `probe-ama-cmd100.py`,
  `probe-hfp-mic.sh`, `probe-hfp-transcribe.py`, `parse-btsnoop.py`.
  Reproducible from-zero recon for any future PXC-class device.

### What doesn't work — Alexa button events

The Alexa button itself remains unreachable from Linux on this firmware.
We climbed several walls and hit a final hard one:

1. **AMA service is gated** until first registration via the Alexa
   Android app. *Resolved* — the user registered through the Android app.
2. **RFCOMM ch 19 accepts our connection** post-registration, sends the
   20-byte greeting, and responds to GetDeviceInformation (returning real
   serial / name / transports / device_type). *Resolved.*
3. **Cmd 100 is the auth gate.** Empirically: schema is `{field 1:
   varint}`. No varint value we tried passes validation. The error response
   has `Response{error_code=INVALID, field_7=<echoed input>}` — field 7
   echoes our parsed input, confirming we're at the wire layer correctly
   but failing the value check.
4. **Capturing the Alexa app's cmd 100 from Android failed** because the
   user's phone is a Heytap (OnePlus/Realme/Oppo) variant whose
   `dumpsys bluetooth_manager` BTSNOOP_LOG_SUMMARY uses a custom record
   format (16-byte OEM header per HCI packet) that doesn't match standard
   btsnoop and isn't parseable by Wireshark or our `parse-btsnoop.py`.
   The bug-report extraction has no separate snoop file.

### How to ship the Alexa button later (if pursued)

- Re-run the capture path on a stock-Android device (Pixel/GrapheneOS) —
  those produce standard `btsnoop_hci.log` files that Wireshark reads
  directly. Filter to RFCOMM channel 19, follow stream, extract the cmd
  100 payload bytes. Replay verbatim from `AmaButtonListener` after the
  greeting.
- Or, sign up at developer.amazon.com (free), accept the AVS terms, and
  download the AMA Kit which contains `accessories.proto`. With the
  schema we'd know exactly what cmd 100 wants and could possibly
  construct a valid request without capture.
- Either path, once unblocked, requires only adding a few bytes worth
  of post-greeting `sendall()` to `AmaButtonListener._read_loop`.

### Summary of what we learned about AMA on PXC 550-II

| cmd | response shape | meaning |
|-----|----------------|---------|
| 20 | DeviceInformation (serial, name, transports, type) | GetDeviceInformation |
| 21 | DeviceConfiguration {field_2: 1} | GetDeviceConfiguration |
| 23, 24, 31, 55, 56, 60 | empty Response | ack-style commands |
| 30 | Response with empty submsg at field 8 | GetState (no feature) |
| 40 | UNKNOWN error | known but rejects empty |
| 50 | empty Response + unsolicited cmd 103 | **Disconnect/EndSession — never send** |
| 100 | INVALID + field_7 echoes input | **auth gate (single-varint payload, value-checked)** |
| 102 | (device→host) | ButtonNotification (the prize) |
| 103 | (device→host) | session-end notification |

Configuration in `~/.config/korderrc`:

```ini
[bluetooth]
enabled = false              # set true to engage BT integration
device_mac =                 # 00:1B:66:E8:90:10
ama_channel = 19
hfp_profile = headset-head-unit
switch_profile_for_recording = true
```

With `enabled = true` the mic-switch + profile-flip parts work today;
the AMA listener will connect and idle (greeting received, no button
events) until the auth blocker above is resolved.

---

*Last updated 2026-05-01.*
