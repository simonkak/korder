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

    def release_all(self) -> list[str]:
        """Drop EVERY service from the auto-resume list. Returns the
        services that were dropped (so the caller can report what
        they did). Called when the user pauses without naming a
        target ('Wstrzymaj odtwarzanie', 'pause everything') — the
        ducker has already paused things at session start, and this
        signals 'leave them paused on session end' for the whole
        list. Equivalent to N release(svc) calls but atomic."""
        with self._lock:
            services = self._paused_services
            self._paused_services = []
        if services:
            log.info(
                "ducker: released ALL (%d service(s), user took control)",
                len(services),
            )
        return services

    def release(self, service: str) -> None:
        """Drop a service from the auto-resume list mid-session.
        Called when an action (pause_player, resume_player) takes
        explicit control of a service the ducker had paused — without
        this, the dictation-end restore() would undo the user's
        intent (e.g. user says 'pause YouTube', Korder pretends to,
        then auto-resumes it on session end). Idempotent — releasing
        a service we never paused is a no-op."""
        with self._lock:
            try:
                self._paused_services.remove(service)
                log.info("ducker: released %s (user took control)", service)
            except ValueError:
                pass

    def _safe_restore(self) -> None:
        try:
            self.restore()
        except Exception:
            pass


# Module-level reference to the dictation-active VolumeDucker. Set
# once at app startup; consulted by playback_target actions so they
# can release services from the ducker's restore list when the user
# has taken explicit control. Stays None in headless / test
# contexts that don't construct a real ducker.
_active_ducker: VolumeDucker | None = None


def register_active_ducker(ducker: VolumeDucker | None) -> None:
    """Register (or clear) the dictation-active ducker. App startup
    calls this once after constructing the ducker. Tests can call
    it with None to detach a stub between cases."""
    global _active_ducker
    _active_ducker = ducker


def release_from_session_pause(service: str) -> None:
    """Tell the active session ducker the user has explicit control
    of `service` — it must NOT auto-resume on dictation end. No-op
    when no ducker is registered (headless / test contexts) or when
    the ducker hadn't paused this service in the first place."""
    if _active_ducker is not None:
        _active_ducker.release(service)


def release_all_session_pauses() -> list[str]:
    """Tell the active session ducker the user paused EVERYTHING —
    drop the entire auto-resume list. Returns the dropped service
    names. Used by pause_player when the LLM picks it without a
    target on a 'pause everything' utterance ('Wstrzymaj
    odtwarzanie', 'pause music')."""
    if _active_ducker is not None:
        return _active_ducker.release_all()
    return []
