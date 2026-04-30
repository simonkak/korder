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
    description="Raise volume one step",
    triggers={
        "en": ["louder", "volume up"],
        "pl": ["głośniej", "zwiększ głośność"],
    },
    op_factory=lambda _args: ("key", KEY_VOLUMEUP),
))

register(Action(
    name="volume_down",
    description="Lower volume one step",
    triggers={
        "en": ["quieter", "volume down"],
        "pl": ["ciszej", "zmniejsz głośność"],
    },
    op_factory=lambda _args: ("key", KEY_VOLUMEDOWN),
))

register(Action(
    name="volume_mute",
    description="Toggle mute",
    triggers={
        "en": ["mute audio", "toggle mute"],
        "pl": ["wycisz", "wycisz dźwięk"],
    },
    op_factory=lambda _args: ("key", KEY_MUTE),
))

register(Action(
    name="play_pause",
    description="Toggle play/pause on the active media player",
    triggers={
        "en": ["play music", "pause music", "toggle music", "pause"],
        "pl": [
            "puść muzykę",
            "odtwórz muzykę",
            "zatrzymaj muzykę",
            "wstrzymaj muzykę",
            "wstrzymaj",
            "pausa",
            "pauza",
        ],
    },
    op_factory=lambda _args: ("key", KEY_PLAYPAUSE),
))

register(Action(
    name="next_track",
    description="Skip to the next track",
    triggers={
        "en": ["next song", "next track", "skip song"],
        "pl": ["następna piosenka", "następny utwór"],
    },
    op_factory=lambda _args: ("key", KEY_NEXTSONG),
))

register(Action(
    name="previous_track",
    description="Go to the previous track",
    triggers={
        "en": ["previous song", "previous track"],
        "pl": ["poprzednia piosenka", "poprzedni utwór"],
    },
    op_factory=lambda _args: ("key", KEY_PREVIOUSSONG),
))

register(Action(
    name="stop_playback",
    description="Stop media playback",
    triggers={
        "en": ["stop music", "stop playback"],
        "pl": ["zatrzymaj odtwarzanie"],
    },
    op_factory=lambda _args: ("key", KEY_STOPCD),
))
