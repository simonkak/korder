"""Audio and media control actions.

Volume changes go through wpctl (PipeWire native). Media transport
(play/pause/skip) goes through playerctl which talks MPRIS to whatever
player is currently active — Spotify, Strawberry, browser tabs, etc.

Failures are silent (subprocess op uses check=False) — saying "next song"
with nothing playing harmlessly no-ops rather than surfacing an error.
"""
from korder.actions.base import Action, register


_SINK = "@DEFAULT_AUDIO_SINK@"


register(Action(
    name="volume_up",
    description="Increase system volume by 5%",
    triggers={
        "en": ["louder", "volume up"],
        "pl": ["głośniej", "zwiększ głośność"],
    },
    op_factory=lambda _args: ("subprocess", ["wpctl", "set-volume", _SINK, "5%+"]),
))

register(Action(
    name="volume_down",
    description="Decrease system volume by 5%",
    triggers={
        "en": ["quieter", "volume down"],
        "pl": ["ciszej", "zmniejsz głośność"],
    },
    op_factory=lambda _args: ("subprocess", ["wpctl", "set-volume", _SINK, "5%-"]),
))

register(Action(
    name="volume_mute",
    description="Toggle mute on the default audio sink",
    triggers={
        "en": ["mute audio", "toggle mute"],
        "pl": ["wycisz", "wycisz dźwięk"],
    },
    op_factory=lambda _args: ("subprocess", ["wpctl", "set-mute", _SINK, "toggle"]),
))

register(Action(
    name="play_pause",
    description="Toggle play/pause on the active media player (MPRIS)",
    triggers={
        "en": ["play music", "pause music", "toggle music"],
        "pl": ["puść muzykę", "zatrzymaj muzykę", "wstrzymaj muzykę"],
    },
    op_factory=lambda _args: ("subprocess", ["playerctl", "play-pause"]),
))

register(Action(
    name="next_track",
    description="Skip to the next track",
    triggers={
        "en": ["next song", "next track", "skip song"],
        "pl": ["następna piosenka", "następny utwór"],
    },
    op_factory=lambda _args: ("subprocess", ["playerctl", "next"]),
))

register(Action(
    name="previous_track",
    description="Go to the previous track",
    triggers={
        "en": ["previous song", "previous track"],
        "pl": ["poprzednia piosenka", "poprzedni utwór"],
    },
    op_factory=lambda _args: ("subprocess", ["playerctl", "previous"]),
))

register(Action(
    name="stop_playback",
    description="Stop media playback",
    triggers={
        "en": ["stop music", "stop playback"],
        "pl": ["zatrzymaj odtwarzanie"],
    },
    op_factory=lambda _args: ("subprocess", ["playerctl", "stop"]),
))
