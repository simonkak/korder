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
        "List running MPRIS players: [{short_name, status, title, "
        "artist}, …]. USE before pause_player / resume_player when "
        "the user named a player or song — title/artist disambiguate "
        "when the name refers to a track or tab. "
        "SKIP for bare 'pause' / 'play' verbs (those route to "
        "play_pause; no target needed)."
    ),
    executor=_list_active_mpris_players,
))
