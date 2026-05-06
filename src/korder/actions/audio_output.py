"""audio_output_switch — pick a PipeWire sink by spoken name.

PipeWire's wireplumber default-policy moves existing audio streams to
whatever sink is marked as the default — so flipping the default with
`wpctl set-default <id>` is the entire mechanic the user expects when
they say "play through Denon" or "przełącz na głośniki monitora". No
per-stream `pactl move-sink-input` walk needed; the policy follows the
default within a frame.

Sink discovery uses `wpctl status` and parses the Sinks block. Looks
like:

    Audio
     ├─ Sinks:
     │  *   59. Głośniki monitora                 [vol: 0.45]
     │      95. Denon DHT-S517                    [vol: 0.70]

The leading `*` marks the current default; we capture it to short-
circuit "already on this output" the same way bluetooth_connect skips
re-issuing connect when the device is already linked. Filter sinks
(loopback nodes like `spotify_mic_sink`) live under a separate
`Filters` heading and are intentionally NOT part of this list — they're
virtual routing nodes, not user-visible outputs.

We shell out to wpctl rather than calling the PipeWire C API directly
because (a) the user has wpctl by definition (Korder's volume actions
already depend on it) and (b) re-implementing wireplumber's default-
sink semantics in Python would duplicate a moving target.
"""
from __future__ import annotations
import logging
import re
import shutil
import subprocess

from korder.actions.base import Action, register
from korder.ui.i18n import t, tf
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)

# `[^\W_]+` is "word char excluding underscore" — splits slug-shaped
# inputs into separate tokens. Plain `\w+` keeps underscores as part
# of the word, which is hostile to multi-token matching against
# space-separated sink names.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Minimum query-token length to participate in fuzzy matching. Short
# tokens (1–3 chars) match too many candidates by coincidence; the
# floor keeps a stray "to" or "na" from steering the result.
_FUZZY_MIN_CHARS = 4
_WPCTL_TIMEOUT_S = 3.0

# Section header in `wpctl status` output. Box-drawing characters
# (├─ / └─) frame the categories: Devices, Sinks, Sources, Filters,
# Streams. We isolate the Sinks block by finding "Sinks:" and stopping
# at the next branch character.
_SINKS_HEADER_RE = re.compile(r"Sinks:\s*$")
_SECTION_BREAK_RE = re.compile(r"^\s*[│ ]?\s*[├└]")

# Per-sink line — examples:
#   " │  *   59. Głośniki monitora                 [vol: 0.45]"
#   " │      95. Denon DHT-S517                    [vol: 0.70]"
# We capture (default_marker, id, name). The trailing [vol: …] is
# stripped out so the name doesn't carry the volume into the matcher.
_SINK_LINE_RE = re.compile(
    r"^\s*│\s*(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:.*\])?\s*$"
)


def _tokens(text: str) -> set[str]:
    return {t for t in (m.lower() for m in _TOKEN_RE.findall(text or "")) if t}


def _parse_wpctl_status(stdout: str) -> tuple[list[tuple[int, str]], int | None]:
    """Pull the Sinks block out of `wpctl status` text. Returns
    (sinks, default_id) where sinks is [(id, name), …] and
    default_id is the id with the `*` marker (None when no sink
    is default — shouldn't happen on a working PipeWire but we
    don't crash on it)."""
    sinks: list[tuple[int, str]] = []
    default_id: int | None = None
    in_sinks = False
    for line in stdout.splitlines():
        if not in_sinks:
            if _SINKS_HEADER_RE.search(line):
                in_sinks = True
            continue
        # Once we hit the next ├─ / └─ heading, the Sinks block is done.
        if _SECTION_BREAK_RE.match(line):
            break
        m = _SINK_LINE_RE.match(line)
        if not m:
            continue
        marker, sid, name = m.group(1), m.group(2), m.group(3).strip()
        sid_int = int(sid)
        sinks.append((sid_int, name))
        if marker == "*":
            default_id = sid_int
    return sinks, default_id


def _list_sinks() -> tuple[list[tuple[int, str]], int | None]:
    """Returns (sinks, default_id). Empty list + None on any failure."""
    if not shutil.which("wpctl"):
        log.warning("audio_output: wpctl not on PATH")
        return [], None
    try:
        result = subprocess.run(
            ["wpctl", "status"],
            capture_output=True,
            timeout=_WPCTL_TIMEOUT_S,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("audio_output: wpctl status failed: %s", e)
        return [], None
    return _parse_wpctl_status(result.stdout)


def _substring_overlap(qtokens: set[str], ntokens: set[str]) -> int:
    """Count query tokens that share a contiguous substring run with
    any sink token (in either direction), beyond exact equality.
    Generic fuzzy-match primitive — no assumption about where the
    variation lives (prefix, suffix, infix, truncation, expansion).

    Both tokens must clear the length floor so that matching on a
    short sub-run like 'to' or 'set' doesn't dominate. Counts only
    tokens not already paired by exact equality, so this is the
    *additional* signal on top of the strict matcher."""
    extras = 0
    for q in qtokens:
        if len(q) < _FUZZY_MIN_CHARS or q in ntokens:
            continue
        for n in ntokens:
            if len(n) < _FUZZY_MIN_CHARS or n in qtokens:
                continue
            if q in n or n in q:
                extras += 1
                break
    return extras


def _resolve_sink(query: str, sinks: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Pick the sink whose name best overlaps `query`. Two signals:

    1. EXACT token equality — strongest. ("Denon" → "Denon DHT-S517")
    2. SUBSTRING containment — secondary signal. Generic: handles
       morphological variation (plural/singular, gendered endings),
       truncation, slug-style expansion, partial transliteration —
       any case where two words share a meaningful contiguous run
       without being identical. Language-neutral.

    Floor: at least one signal hits at the meaningful-length
    threshold. Sink descriptions can be terse ("HDMI", "USB", a
    brand name), so the floor stays looser than launcher.py's."""
    qtokens = _tokens(query)
    if not qtokens or not sinks:
        return None
    best: tuple[int, int, int, int, str] | None = None
    for sid, name in sinks:
        ntokens = _tokens(name)
        common = qtokens & ntokens
        exact = sum(1 for t in common if len(t) >= 3)
        fuzzy = _substring_overlap(qtokens, ntokens)
        if exact == 0 and fuzzy == 0:
            continue
        # Exact matches outrank fuzzy-only matches; ties broken by
        # total overlap. The score tuple compares left-to-right.
        score = (exact, fuzzy, len(common))
        if best is None or score > (best[0], best[1], best[2]):
            best = (exact, fuzzy, len(common), sid, name)
    if best is None:
        return None
    return (best[3], best[4])


def _set_default_sink(sink_id: int) -> bool:
    """Run `wpctl set-default <id>`. Returns True on rc==0."""
    try:
        result = subprocess.run(
            ["wpctl", "set-default", str(sink_id)],
            capture_output=True,
            timeout=_WPCTL_TIMEOUT_S,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("audio_output: set-default %d failed: %s", sink_id, e)
        return False
    if result.returncode != 0:
        log.warning(
            "audio_output: set-default %d rc=%d stderr=%r",
            sink_id, result.returncode, result.stderr,
        )
        return False
    return True


def _do_switch(query: str) -> None:
    sinks, default_id = _list_sinks()
    if not sinks:
        emit_progress(t("progress_audio_no_sinks"))
        return
    resolved = _resolve_sink(query, sinks)
    if resolved is None:
        emit_progress(tf("progress_audio_sink_not_found", query=query))
        return
    sink_id, name = resolved
    if sink_id == default_id:
        emit_progress(tf("progress_audio_already_default", name=name))
        return
    emit_progress(tf("progress_audio_switching", name=name))
    if _set_default_sink(sink_id):
        emit_progress(tf("progress_audio_switched", name=name))
    else:
        emit_progress(tf("progress_audio_switch_failed", name=name))


def _switch_op(args: dict) -> tuple | None:
    raw = (args or {}).get("sink_name", "")
    if not isinstance(raw, str):
        raw = ""
    raw = raw.strip()
    if not raw:
        return None  # → pending, ask which output
    return ("callable", lambda name=raw: _do_switch(name))


register(Action(
    name="audio_output_switch",
    description=(
        "Switch the system audio output to a different sink — speakers, "
        "headphones, HDMI, a Bluetooth device, etc. Use for any "
        "imperative meaning 'route audio elsewhere' (e.g. 'play "
        "through headphones', 'switch audio to Denon', 'output to "
        "HDMI', or the equivalent in any other language). Extract "
        "the destination NAME or BRAND the user spoke into "
        "params.sink_name. Strip out the verb, the words for "
        "'audio'/'sound', and filler.\n"
        "CRITICAL — sink_name MUST be the destination words verbatim, "
        "in the user's spoken language. Do NOT translate, do NOT "
        "slugify, do NOT canonicalize across languages. PipeWire "
        "sink descriptions on the user's machine are in their "
        "system's language; the match depends on the user's literal "
        "words, not a translated form.\n"
        "Distinct from bluetooth_connect: that LINKS a paired BT "
        "device (radio handshake); this picks an output AMONG "
        "ALREADY-AVAILABLE sinks (which may include a connected BT "
        "device, HDMI, USB DAC, built-in speakers, etc.). When the "
        "user wants to establish a fresh BT link, use "
        "bluetooth_connect; when they just want to redirect audio "
        "between sinks that already exist, use this."
    ),
    triggers={
        "en": [
            "switch audio",
            "switch sound",
            "switch output",
            "audio output",
            "play through",
            "output to",
        ],
        "pl": [
            "przełącz dźwięk",
            "przełącz audio",
            "przełącz wyjście",
            "wyjście audio",
            "dźwięk na",
            "audio na",
        ],
    },
    op_factory=_switch_op,
    tools=["list_audio_sinks"],
    parameters={
        "sink_name": {
            "type": "string",
            "required": True,
            "description": (
                "Free-form name of the audio output, VERBATIM in the "
                "user's spoken language. Whatever they said is what "
                "this is — never a translation, slug, or "
                "canonicalized form. Korder fuzzy-matches this "
                "against PipeWire sinks; partial brand names and "
                "morphological variants work, so leaving the user's "
                "wording intact gives the best chance of a hit. "
                "Required."
            ),
        },
    },
))
