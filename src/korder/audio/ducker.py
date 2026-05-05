"""Pause MPRIS-aware media players for the duration of a dictation
session so external audio doesn't bleed into the open mic.

Earlier versions of this module lowered the default sink's volume via
``wpctl`` instead of pausing — quieter rather than silent. That had
two problems: Whisper accuracy is best with NO competing audio at all,
not just quieter audio; and Korder's own TTS plays through the same
sink, so the duck muffled the synthesized voice unless an extra
pause/resume dance lifted it for every TTS event. Switching to MPRIS
pause/resume eliminates both — music is fully silent during listening,
TTS plays at the user's normal level for free, and the lifecycle is
one pause + one resume per session regardless of how many TTS events
fire in between.

Trade-off: non-MPRIS audio sources (browser tabs without media-session
metadata, system game audio, etc.) are NOT affected. The previous
wpctl approach covered them at the cost of muffling TTS. We chose
simplicity here — the dominant case is music players that ship MPRIS
support (Spotify, mpv, VLC, Firefox/Chromium with media playing,
Deadbeef, Lollypop, etc.).

Lifecycle (driven by MainWindow):
    ducker.duck()       on _begin_dictation_lifecycle (recording starts)
    ducker.restore()    on _end_dictation_lifecycle (every exit path)
    ducker.restore()    via atexit, as a crash-safety net

Idempotency: duck() while already paused is a no-op. restore() while
not paused is a no-op. Only services that were Playing at the moment
of duck() are tracked; manual user pause+resume in the middle of a
dictation session is left alone (we don't try to second-guess).
"""
from __future__ import annotations
import atexit
import logging
import threading

from korder.audio import _mpris

log = logging.getLogger(__name__)


class VolumeDucker:
    def __init__(self, enabled: bool, target_pct: int = 0):
        """target_pct is accepted for backwards compatibility with the
        wpctl-volume era of this class but is no longer used. Pause
        is fully silent; there's no analog scale to honor."""
        self._enabled = enabled
        # Services we paused on the most recent duck(). Cleared on
        # restore() so a duplicate restore is a no-op.
        self._paused_services: list[str] = []
        # restore() can run from the inject worker thread (when a
        # volume-altering action wants control of the sink) and from
        # the main thread (auto-stop after-action). The lock guards
        # the read-modify-write of the paused-services list.
        self._lock = threading.Lock()
        if self._enabled:
            # Crash safety: if the process exits while music is paused,
            # resume it so the user isn't left wondering why playback
            # stopped after a Korder crash.
            atexit.register(self._safe_restore)

    def duck(self) -> None:
        """Pause every Playing MPRIS service. Idempotent —
        a second duck() while already paused is a no-op."""
        if not self._enabled:
            return
        with self._lock:
            if self._paused_services:
                return  # already paused
            paused: list[str] = []
            try:
                for svc in _mpris.list_players():
                    try:
                        if _mpris.player_status(svc) != "Playing":
                            continue
                        if _mpris.qdbus(
                            svc,
                            _mpris.MPRIS_OBJECT,
                            f"{_mpris.PLAYER_IFACE}.Pause",
                        ) is not None:
                            paused.append(svc)
                    except Exception:
                        # One misbehaving player shouldn't block the
                        # rest from being paused.
                        continue
            except Exception as e:
                log.error("ducker: enumerate failed: %s", e)
                return
            self._paused_services = paused
            if paused:
                log.info("ducker: paused %d MPRIS service(s)", len(paused))

    def restore(self) -> None:
        """Resume every service we paused. Always clears the paused-
        services list so a failed Play call doesn't pin us in
        'paused' forever — the next duck() can re-discover Playing
        services fresh."""
        with self._lock:
            services = self._paused_services
            self._paused_services = []
        if not services:
            return
        for svc in services:
            try:
                _mpris.qdbus(
                    svc, _mpris.MPRIS_OBJECT, f"{_mpris.PLAYER_IFACE}.Play"
                )
            except Exception:
                # One stuck Play call shouldn't block the others.
                continue
        log.info("ducker: resumed %d MPRIS service(s)", len(services))

    def _safe_restore(self) -> None:
        try:
            self.restore()
        except Exception:
            pass
