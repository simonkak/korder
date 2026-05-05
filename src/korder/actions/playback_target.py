"""Targeted MPRIS pause / resume — 'Pause Spotify', 'Pause Firefox',
'Play Spotify and pause Firefox'.

Distinct from `play_pause` (which emits a generic media-key keypress
that the OS routes to the most-recent player) because keypress
routing can't pause Spotify while leaving a Firefox tab playing —
or vice versa. This module talks MPRIS directly so the user can
name the player or the tab title and we'll pause exactly that.

Target resolution is fuzzy: tokenize the user-supplied target, score
each MPRIS service by how many tokens overlap its short player name
plus current track title, and pick the highest-scoring candidate.
With no target supplied, fall back to the most-Playing player (for
pause) or any Paused player (for resume).
"""
from __future__ import annotations
import logging
import re

from korder.actions.base import Action, register
from korder.audio import _mpris
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _score(target_tokens: set[str], service: str) -> tuple[int, str]:
    """Return (overlap_score, status) for a service against the target.
    Status is fetched once per service so the caller can use it as a
    tie-break without a second round-trip."""
    if not target_tokens:
        return (0, _mpris.player_status(service))
    name = _mpris.short_player_name(service)
    md = _mpris.player_metadata(service)
    haystack = _tokens(f"{name} {md.get('title', '')} {md.get('artist', '')}")
    return (len(target_tokens & haystack), _mpris.player_status(service))


def _resolve_target(target: str, prefer_status: str) -> str | None:
    """Pick the MPRIS service that best matches `target`. With an
    empty target, prefer a service whose status equals `prefer_status`.
    With a non-empty target, prefer the highest-overlap match; break
    ties on `prefer_status`."""
    services = _mpris.list_players()
    if not services:
        return None
    target = (target or "").strip()
    target_tokens = _tokens(target)

    if not target_tokens:
        for s in services:
            if _mpris.player_status(s) == prefer_status:
                return s
        # No match for the preferred status — fall back to anything
        # that's at least responsive, same heuristic pick_active_player
        # uses for now_playing.
        return _mpris.pick_active_player(services)

    scored = [(_score(target_tokens, s), s) for s in services]
    # Sort: higher overlap first; on tie, the one matching prefer_status
    # wins; otherwise stable.
    scored.sort(
        key=lambda entry: (
            -entry[0][0],
            0 if entry[0][1] == prefer_status else 1,
        )
    )
    best = scored[0]
    if best[0][0] == 0:
        # No token overlap with anything — refuse to pick at random.
        return None
    return best[1]


def _do_pause(target: str) -> None:
    service = _resolve_target(target, prefer_status="Playing")
    if service is None:
        emit_progress(
            f"No player matches {target!r}" if target else "Nothing is playing"
        )
        log.info("pause_player: no match for target=%r", target)
        return
    pretty = _mpris.short_player_name(service)
    if _mpris.pause_player(service):
        emit_progress(f"Paused {pretty}")
        log.info("pause_player: paused %s (target=%r)", service, target)
    else:
        emit_progress(f"Failed to pause {pretty}")
        log.warning("pause_player: D-Bus pause failed on %s", service)


def _do_resume(target: str) -> None:
    service = _resolve_target(target, prefer_status="Paused")
    if service is None:
        emit_progress(
            f"No player matches {target!r}" if target else "No paused player to resume"
        )
        log.info("resume_player: no match for target=%r", target)
        return
    pretty = _mpris.short_player_name(service)
    if _mpris.play_player(service):
        emit_progress(f"Resumed {pretty}")
        log.info("resume_player: resumed %s (target=%r)", service, target)
    else:
        emit_progress(f"Failed to resume {pretty}")
        log.warning("resume_player: D-Bus play failed on %s", service)


def _pause_player_op(args: dict) -> tuple:
    target = (args or {}).get("target", "")
    if not isinstance(target, str):
        target = ""
    return ("callable", lambda t=target.strip(): _do_pause(t))


def _resume_player_op(args: dict) -> tuple:
    target = (args or {}).get("target", "")
    if not isinstance(target, str):
        target = ""
    return ("callable", lambda t=target.strip(): _do_resume(t))


_TARGET_PARAM = {
    "target": {
        "type": "string",
        "description": (
            "Player name and/or track-title fragment to pause / resume. "
            "Examples: 'Spotify', 'Firefox', 'Firefox How I Chose a Linux "
            "Distro' (matches the Firefox player whose current tab title "
            "contains those words). Omit when the user didn't name a "
            "specific player — Korder will pick a sensible default "
            "(the currently-Playing one for pause, the currently-Paused "
            "one for resume)."
        ),
    },
}


register(Action(
    name="pause_player",
    description=(
        "Pause a SPECIFIC media player or browser tab via MPRIS. Use when "
        "the user names what they want paused — 'Pause Spotify', 'Pause "
        "Firefox', 'Pause this YouTube video', 'Pause Firefox How I Chose "
        "a Linux Distro'. Extract the named player and/or title fragment "
        "into params.target. Distinct from play_pause: this action targets "
        "a particular player while play_pause toggles the OS-routed "
        "default. Prefer this over play_pause when the user's intent is "
        "clearly to pause one player while leaving others alone."
    ),
    triggers={
        "en": [
            "pause spotify",
            "pause firefox",
            "pause chromium",
            "pause this",
            "pause that",
        ],
        "pl": [
            "wstrzymaj spotify",
            "wstrzymaj firefoxa",
            "zapauzuj spotify",
            "zapauzuj firefoxa",
            "zatrzymaj spotify",
            "zatrzymaj firefoxa",
        ],
    },
    op_factory=_pause_player_op,
    parameters=_TARGET_PARAM,
))


register(Action(
    name="resume_player",
    description=(
        "Resume / start playback on a SPECIFIC media player or browser "
        "tab via MPRIS. Use when the user names what they want resumed — "
        "'Play Spotify', 'Resume Firefox', 'Play Firefox How I Chose a "
        "Linux Distro'. Extract the named player and/or title fragment "
        "into params.target. Distinct from play_pause: this action only "
        "starts (never pauses) playback on the named player, so it composes "
        "cleanly with pause_player when the user wants to swap "
        "(pause one, play another)."
    ),
    triggers={
        "en": [
            "play spotify",
            "play firefox",
            "play chromium",
            "resume spotify",
            "resume firefox",
        ],
        "pl": [
            # Note: "włącz spotify" / "włącz na spotify" are claimed by
            # spotify_play (which launches a query-based search). The
            # LLM disambiguates resume_player from spotify_play via the
            # action description — when the user says "włącz Spotify"
            # without naming a song, the LLM should still route here
            # despite the trigger overlap. Keep the unambiguous
            # phrasings only in this list to avoid a regex collision.
            "włącz firefoxa",
            "wznów spotify",
            "wznów firefoxa",
            "puść spotify",
        ],
    },
    op_factory=_resume_player_op,
    parameters=_TARGET_PARAM,
))
