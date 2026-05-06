"""MPRIS-player discovery tool.

Wraps ``korder.audio._mpris.list_players`` so the LLM can see active
media players (Spotify, Firefox, mpv, browser bridges) during
pause_player / resume_player parameter resolution."""
from __future__ import annotations
import logging

from korder.audio import _mpris
from korder.tools.base import Tool, register_tool

log = logging.getLogger(__name__)


def _list_active_mpris_players() -> list[dict]:
    """Returns [{short_name, status, title, artist}, …] for active
    MPRIS services. Status is one of 'Playing' / 'Paused' / 'Stopped'.
    Title/artist may be empty for players without metadata. Empty list
    on any failure (no MPRIS services, D-Bus error)."""
    try:
        services = _mpris.list_players()
    except Exception as e:
        log.warning("list_active_mpris_players: list failed: %s", e)
        return []
    out: list[dict] = []
    for svc in services:
        try:
            md = _mpris.player_metadata(svc) or {}
            out.append({
                "short_name": _mpris.short_player_name(svc),
                "status": _mpris.player_status(svc),
                "title": md.get("title", ""),
                "artist": md.get("artist", ""),
            })
        except Exception as e:
            log.warning("list_active_mpris_players: per-service failed for %s: %s", svc, e)
    return out


register_tool(Tool(
    name="list_active_mpris_players",
    description=(
        "Enumerate currently-running MPRIS media players (Spotify, "
        "Firefox, mpv, browser MPRIS bridges). Returns a list of "
        "{short_name, status, title, artist} entries with status one "
        "of 'Playing' / 'Paused' / 'Stopped'. Call this before filling "
        "target params for pause_player / resume_player so you can "
        "quote one of these short_name values verbatim instead of "
        "guessing. Title and artist help disambiguate when the user "
        "names a track or tab title rather than the player itself."
    ),
    executor=_list_active_mpris_players,
))
