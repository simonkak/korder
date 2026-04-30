"""Audio and media control actions.

Uses standard kernel media keycodes injected via /dev/uinput. KDE Plasma's
media-key handler picks these up and routes them: volume keys to PipeWire,
play/skip keys to whichever MPRIS player is currently active. No
playerctl, wpctl, or D-Bus glue required — same mechanism a hardware
keyboard's media buttons use.
"""
from korder.actions.base import Action, register
from korder.actions.codes import (
    KEY_MUTE,
    KEY_NEXTSONG,
    KEY_PLAYPAUSE,
    KEY_PREVIOUSSONG,
    KEY_STOPCD,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
)


register(Action(
    name="volume_up",
    description=(
        "Raise the system volume by one step. Use for any imperative meaning "
        "louder / increase volume / turn it up."
    ),
    triggers={
        "en": ["louder", "volume up"],
        "pl": ["głośniej", "zwiększ głośność"],
    },
    op_factory=lambda _args: ("key", KEY_VOLUMEUP),
))

register(Action(
    name="volume_down",
    description=(
        "Lower the system volume by one step. Use for any imperative meaning "
        "quieter / decrease volume / turn it down."
    ),
    triggers={
        "en": ["quieter", "volume down"],
        "pl": ["ciszej", "zmniejsz głośność"],
    },
    op_factory=lambda _args: ("key", KEY_VOLUMEDOWN),
))

register(Action(
    name="volume_mute",
    description="Toggle mute on the system audio output (mute/unmute).",
    triggers={
        "en": ["mute audio", "toggle mute"],
        "pl": ["wycisz", "wycisz dźwięk"],
    },
    op_factory=lambda _args: ("key", KEY_MUTE),
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
