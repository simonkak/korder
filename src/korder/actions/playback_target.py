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
from korder.audio.ducker import release_from_session_pause
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
    if not target:
        # Refuse to fire without a target. Field log: LLM emitted
        # pause_player with empty params and phrase='ok' (a 2-char
        # substring of 'okno' in the Polish input 'które okno jest');
        # the previous "no target → pause whatever's Playing" fallback
        # silently killed Spotify on a question that wasn't even a
        # command. Bare pause/play belongs on play_pause; pause_player
        # is the targeted variant by design.
        emit_progress("pause_player: no target")
        log.info("pause_player: refusing — empty target")
        return
    service = _resolve_target(target, prefer_status="Playing")
    if service is None:
        emit_progress(f"No player matches {target!r}")
        log.info("pause_player: no match for target=%r", target)
        return
    pretty = _mpris.short_player_name(service)
    if _mpris.pause_player(service):
        # Claim ownership: the dictation ducker also paused this
        # service at recording start, and its session-end restore
        # would auto-resume it — undoing the user's intent. release()
        # is idempotent so it's safe even when the ducker hadn't
        # touched this service.
        release_from_session_pause(service)
        emit_progress(f"Paused {pretty}")
        log.info("pause_player: paused %s (target=%r)", service, target)
    else:
        emit_progress(f"Failed to pause {pretty}")
        log.warning("pause_player: D-Bus pause failed on %s", service)


def _do_resume(target: str) -> None:
    if not target:
        # Same rationale as pause_player: don't fall back to "resume
        # whatever's Paused" without an explicit target. Bare
        # play/resume belongs on play_pause.
        emit_progress("resume_player: no target")
        log.info("resume_player: refusing — empty target")
        return
    service = _resolve_target(target, prefer_status="Paused")
    if service is None:
        emit_progress(f"No player matches {target!r}")
        log.info("resume_player: no match for target=%r", target)
        return
    pretty = _mpris.short_player_name(service)
    if _mpris.play_player(service):
        # Resume happens NOW (not waiting for session end). Drop
        # the service from the ducker's auto-resume list so its
        # later restore() doesn't fire a redundant Play that, if
        # the user re-paused mid-session via some other surface,
        # would clobber that newer state.
        release_from_session_pause(service)
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
        "required": True,
        "description": (
            "Player name and/or track-title fragment to pause / resume. "
            "Examples: 'Spotify', 'Firefox', 'Firefox How I Chose a Linux "
            "Distro' (matches the Firefox player whose current tab title "
            "contains those words). REQUIRED — when the user doesn't name "
            "a specific player, use play_pause (toggle) instead of "
            "pause_player / resume_player."
        ),
    },
}


register(Action(
    name="pause_player",
    description=(
        "Pause a SPECIFIC NAMED media player or browser tab via MPRIS. "
        "Use ONLY when the user explicitly names what they want paused — "
        "'Pause Spotify', 'Pause Firefox', 'Pause Firefox How I Chose a "
        "Linux Distro'. The target name is REQUIRED — without one, this "
        "action does NOT apply. For a bare pause / stop verb with no "
        "named player, use play_pause (toggle) or stop_playback (stop) "
        "instead. Never pick this action just because the input mentions "
        "audio or media — the user must be telling Korder which player "
        "to act on by name."
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
        "Resume playback on a SPECIFIC NAMED media player or browser "
        "tab via MPRIS. Use ONLY when the user explicitly names what they "
        "want resumed — 'Play Spotify', 'Resume Firefox', 'Play Firefox "
        "How I Chose a Linux Distro'. The target name is REQUIRED — "
        "without one, this action does NOT apply. For a bare play / "
        "resume verb with no named player, use play_pause (toggle) "
        "instead. Never pick this action just because the input has "
        "'play' or 'resume' verbs — the user must be telling Korder "
        "which player to start by name."
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
