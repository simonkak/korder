"""Audio and media control actions.

Play/skip uses standard kernel media keycodes injected via /dev/uinput —
KDE Plasma's media-key handler picks these up and routes them to whichever
MPRIS player is currently active.

Volume actions go through wpctl directly rather than KEY_VOLUMEUP/DOWN/
MUTE keycodes. KDE's media-key path uses a separate volume cache that
takes a moment to catch up to wpctl writes from the ducker, so a keycode
arriving while the ducker had just restored to the user's true level
would race against an out-of-date cache and step from the still-displayed
ducked volume. Routing through wpctl puts ducker and volume-action writes
on the same IPC, eliminating the race.

The system_volume op carries a (direction, step_pct) tuple so the LLM
can extend the default 5% step when the user says "znacznie głośniej",
"much louder", or "głośniej o 20 procent". Mute is direction-only;
step is ignored.
"""
import re

from korder.actions.base import Action, register
from korder.actions.codes import (
    KEY_NEXTSONG,
    KEY_PLAYPAUSE,
    KEY_PREVIOUSSONG,
    KEY_STOPCD,
)

# Bounds on the per-call step. 1% lets "trochę głośniej" register a real
# change without snapping the slider, 100% covers "max it out" requests.
_DEFAULT_STEP_PCT = 5
_MIN_STEP_PCT = 1
_MAX_STEP_PCT = 100


_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

# Salvage map: small models sometimes return the qualifier word itself
# (e.g. step_pct="znacznie" / "much" / "trochę") instead of inferring a
# number. Lower-cased substring match against the raw value lets us
# DTRT for those cases.
_QUALIFIER_STEP: tuple[tuple[str, int], ...] = (
    # "a bit / slightly" — small step.
    ("trochę", 2), ("trochu", 2), ("odrobin", 2), ("lekko", 2),
    ("a bit", 2), ("slightly", 2), ("a little", 2),
    # "much / significantly / a lot" — big step.
    ("znacznie", 20), ("dużo", 20), ("sporo", 20), ("mocno", 20),
    ("much", 20), ("a lot", 20), ("significantly", 20),
)


def _coerce_step_pct(raw) -> int:
    """Pull an int from whatever the LLM returned: number, numeric
    string, noisy string like '20%' / '20 procent' / '20 percent', or
    qualifier word like 'znacznie' / 'much' that small models echo back
    instead of inferring a number. Out-of-range or unparseable values
    fall back to the default so a fumbled extraction degrades to the
    standard step rather than blasting the volume."""
    if raw is None or raw == "":
        return _DEFAULT_STEP_PCT
    if isinstance(raw, bool):
        # bool is a subclass of int; treat True/False as junk so we don't
        # silently set step to 1 when the LLM gets confused.
        return _DEFAULT_STEP_PCT
    if isinstance(raw, (int, float)):
        n = int(round(float(raw)))
        return max(_MIN_STEP_PCT, min(_MAX_STEP_PCT, abs(n)))

    s = str(raw).strip()
    m = _NUMBER_RE.search(s)
    if m:
        try:
            n = int(round(float(m.group(0).replace(",", "."))))
            return max(_MIN_STEP_PCT, min(_MAX_STEP_PCT, abs(n)))
        except (TypeError, ValueError):
            pass
    lo = s.lower()
    for needle, step in _QUALIFIER_STEP:
        if needle in lo:
            return step
    return _DEFAULT_STEP_PCT


_STEP_PCT_PARAM = {
    "step_pct": {
        "type": "integer",
        "description": (
            "Optional. How much to change the volume, as a percentage of "
            "full scale (1-100). Default 5 (one normal step). Use ~20 for "
            "'much/significantly louder/quieter' (znacznie / dużo / mocno / "
            "sporo / much / a lot / significantly), ~2 for 'a bit / "
            "slightly' (trochę / odrobinę / lekko / slightly / a bit), or "
            "the explicit number the user names ('głośniej o 20 procent', "
            "'louder by 15%', 'quieter by 30' → 20, 15, 30 respectively). "
            "Omit when the user gives no magnitude qualifier."
        ),
    },
}


register(Action(
    name="volume_up",
    description=(
        "Raise the system volume. Default step is 5% of full scale; the "
        "step_pct parameter overrides that when the user qualifies the "
        "request (znacznie / much / o 20%)."
    ),
    triggers={
        "en": ["louder", "volume up"],
        "pl": ["głośniej", "zwiększ głośność"],
    },
    op_factory=lambda args: ("system_volume", ("up", _coerce_step_pct(args.get("step_pct")))),
    parameters=_STEP_PCT_PARAM,
))

register(Action(
    name="volume_down",
    description=(
        "Lower the system volume. Default step is 5% of full scale; the "
        "step_pct parameter overrides that when the user qualifies the "
        "request (znacznie / much / o 20%)."
    ),
    triggers={
        "en": ["quieter", "volume down"],
        "pl": ["ciszej", "zmniejsz głośność"],
    },
    op_factory=lambda args: ("system_volume", ("down", _coerce_step_pct(args.get("step_pct")))),
    parameters=_STEP_PCT_PARAM,
))

register(Action(
    name="volume_mute",
    description="Toggle mute on the system audio output (mute/unmute).",
    triggers={
        "en": ["mute audio", "toggle mute"],
        "pl": ["wycisz", "wycisz dźwięk"],
    },
    op_factory=lambda _args: ("system_volume", ("mute_toggle", 0)),
))

register(Action(
    name="play_pause",
    description=(
        "Toggle the currently active media playback between play and pause. "
        "Use for ANY imperative meaning start, pause, resume, or toggle "
        "music / video — match the user's intent, not the literal phrase. "
        "Distinct from stop_playback, which fully halts rather than just "
        "pausing. If the user is plainly saying 'pause' / 'play' / 'resume' / "
        "the equivalent in their language, this is the action."
    ),
    triggers={
        "en": ["play music", "pause music", "toggle music", "pause", "resume"],
        "pl": [
            "puść muzykę",
            "odtwórz muzykę",
            "zatrzymaj muzykę",
            "wstrzymaj muzykę",
            "wstrzymaj",
            "pausa",
            "pauza",
            "pauzuj",
            "wznów",
            "wznów odtwarzanie",
            "odtwórz znów",
        ],
    },
    op_factory=lambda _args: ("key", KEY_PLAYPAUSE),
))

register(Action(
    name="next_track",
    description=(
        "Skip to the next track / song / video on the active media player."
    ),
    triggers={
        "en": ["next song", "next track", "skip song"],
        "pl": ["następna piosenka", "następny utwór"],
    },
    op_factory=lambda _args: ("key", KEY_NEXTSONG),
))

register(Action(
    name="previous_track",
    description=(
        "Go back to the previous track / song / video on the active media player."
    ),
    triggers={
        "en": ["previous song", "previous track"],
        "pl": ["poprzednia piosenka", "poprzedni utwór"],
    },
    op_factory=lambda _args: ("key", KEY_PREVIOUSSONG),
))

register(Action(
    name="stop_playback",
    description=(
        "COMPLETELY STOP media playback (different from pausing). Use only "
        "when the user explicitly asks to halt or stop — not for pause / "
        "resume / toggle, which are play_pause. If unsure between this and "
        "play_pause, prefer play_pause."
    ),
    triggers={
        "en": ["stop music", "stop playback"],
        "pl": ["zatrzymaj odtwarzanie"],
    },
    op_factory=lambda _args: ("key", KEY_STOPCD),
))
