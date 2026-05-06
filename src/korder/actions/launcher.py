"""app_launcher action — 'open Firefox', 'launch Konsole', 'otwórz
Spotify'.

Fuzzy app resolution with two paths:

1. Direct .desktop scan + token-overlap match. Common case: 'open
   Firefox' has a confident match against /usr/share/applications/
   firefox.desktop, so we go straight to `gtk-launch firefox` — no
   UI flash, no extra wait. The match scores against the file
   stem + Name= + GenericName= + Keywords= + Exec basename, with
   the stem weighted highest because that's what the user usually
   speaks.

2. KRunner fallback via D-Bus. When the .desktop scan returns no
   match (or an ambiguous low-score match), open KRunner with the
   query pre-filled. User sees Plasma's familiar launcher UI with
   their query in it and confirms by pressing Enter. KRunner has
   broader fuzzy matching than our token-overlap (it knows about
   .desktop aliases, Plasma plugins, file paths) so this is the
   right place for queries the simple scan can't handle.

The .desktop scan is cached for the process lifetime — apps don't
appear/disappear often enough to justify re-walking the filesystem
on every voice command. ~50 ms first call, microseconds thereafter."""
from __future__ import annotations
import logging
import re
import shutil
import subprocess
from pathlib import Path

from korder.actions.base import Action, register
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_QDBUS_TIMEOUT_S = 2.0
_LAUNCH_TIMEOUT_S = 5.0

# Standard XDG application directories. ~/.local takes precedence
# implicitly because we preserve order during the scan and later
# entries with the same stem overwrite earlier ones — same precedence
# the freedesktop spec gives.
_XDG_APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".var/app",
]

_DESKTOP_CACHE: list[tuple[str, str, set[str]]] | None = None


def _tokens(text: str) -> set[str]:
    return {t for t in (m.lower() for m in _TOKEN_RE.findall(text or "")) if t}


def _scan_desktop_files() -> list[tuple[str, str, set[str]]]:
    """Walk the standard XDG application directories. Returns a list
    of (stem, display_name, search_tokens) for each .desktop file
    that's not hidden or NoDisplay-flagged.

    `stem` is the .desktop filename without extension — the argument
    `gtk-launch` accepts. `display_name` is the human-readable Name=
    value (or the stem if Name= is missing). `search_tokens` is a
    lowercase token set covering the stem, Name, GenericName,
    Keywords, Comment, and the Exec basename.

    Cached for the process lifetime — apps install/uninstall rarely
    enough that re-scanning per voice command is wasted work."""
    global _DESKTOP_CACHE
    if _DESKTOP_CACHE is not None:
        return _DESKTOP_CACHE

    by_stem: dict[str, tuple[str, set[str]]] = {}
    for d in _XDG_APP_DIRS:
        if not d.is_dir():
            continue
        for path in d.glob("**/*.desktop"):
            try:
                hidden = False
                no_display = False
                display = path.stem
                fields_for_tokens: list[str] = [path.stem]
                with path.open(encoding="utf-8", errors="replace") as f:
                    in_main_section = False
                    for line in f:
                        line = line.rstrip("\n")
                        if line.startswith("["):
                            # Only read the main [Desktop Entry] section.
                            # Action / Subentry sections come after; their
                            # Name= values aren't what the user types.
                            in_main_section = (line.strip() == "[Desktop Entry]")
                            continue
                        if not in_main_section:
                            continue
                        if line.startswith("Hidden=") and line.split("=", 1)[1].strip().lower() == "true":
                            hidden = True
                        elif line.startswith("NoDisplay=") and line.split("=", 1)[1].strip().lower() == "true":
                            no_display = True
                        elif line.startswith("Name="):
                            value = line.split("=", 1)[1].strip()
                            display = value
                            fields_for_tokens.append(value)
                        elif line.startswith("GenericName="):
                            fields_for_tokens.append(line.split("=", 1)[1].strip())
                        elif line.startswith("Keywords="):
                            fields_for_tokens.append(line.split("=", 1)[1].strip())
                        elif line.startswith("Comment="):
                            fields_for_tokens.append(line.split("=", 1)[1].strip())
                        elif line.startswith("Exec="):
                            cmd = line.split("=", 1)[1].split()
                            if cmd:
                                fields_for_tokens.append(Path(cmd[0]).name)
                if hidden or no_display:
                    continue
                tokens: set[str] = set()
                for field in fields_for_tokens:
                    tokens.update(_tokens(field))
                if tokens:
                    by_stem[path.stem] = (display, tokens)
            except OSError:
                continue
    _DESKTOP_CACHE = [
        (stem, display, tokens) for stem, (display, tokens) in by_stem.items()
    ]
    log.info("launcher: indexed %d .desktop files", len(_DESKTOP_CACHE))
    return _DESKTOP_CACHE


def _invalidate_desktop_cache() -> None:
    """Test hook — clear the scan cache so the next call rebuilds."""
    global _DESKTOP_CACHE
    _DESKTOP_CACHE = None


def _resolve_app(query: str) -> tuple[str, str] | None:
    """Find the .desktop file that best matches `query` via token
    overlap. Returns (stem, display_name) or None if no entry passes
    the confidence floor.

    Confidence floor:
    - A query token matches a STEM token (best signal — users say
      the app's common name like 'firefox' / 'konsole', which is
      exactly what the stem encodes), OR
    - The query has 2+ tokens AND total overlap is 2+ (multi-word
      query with broad overlap, e.g. 'visual studio code' matching
      Code's Name=, GenericName=, and Categories=).

    Single weak overlaps (one query token like 'app' matching one
    field token in something unrelated like 'kdeconnect.app') don't
    pass — those would silently launch the wrong thing. Falling
    through to the KRunner fallback is the right behavior there."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return None
    candidates: list[tuple[int, int, int, str, str]] = []
    for stem, display, tokens in _scan_desktop_files():
        stem_tokens = _tokens(stem)
        # Stem-match counts only with tokens >= 4 chars. Generic
        # prefix/suffix words ('app', 'kde', 'org', 'io', 'ui')
        # appear in many .desktop stems and would otherwise cause
        # false positives — e.g. 'krunner-not-an-app' matching the
        # 'app' suffix in 'org.kde.kdeconnect.app'. Real app names
        # the user speaks ('firefox', 'konsole', 'spotify') are
        # always >= 4 chars.
        meaningful_stem_overlap = sum(
            1 for t in (query_tokens & stem_tokens) if len(t) >= 4
        )
        all_overlap = len(query_tokens & tokens)
        score = 2 * meaningful_stem_overlap + all_overlap
        if score == 0:
            continue
        candidates.append(
            (score, meaningful_stem_overlap, all_overlap, stem, display)
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    score, stem_overlap, all_overlap, stem, display = candidates[0]
    if stem_overlap >= 1:
        return (stem, display)
    if len(query_tokens) >= 2 and all_overlap >= 2:
        return (stem, display)
    return None


def _launch_via_desktop(stem: str, display: str) -> bool:
    """gtk-launch is the cross-distro standard for .desktop launching;
    it forks the app properly with desktop-entry semantics (Categories,
    StartupNotify, etc.) instead of just exec-ing the Exec= line.
    Falls back to `gio launch` if gtk-launch is missing."""
    cmd = None
    if shutil.which("gtk-launch"):
        cmd = ["gtk-launch", stem]
    elif shutil.which("gio"):
        # gio launch wants the path to the .desktop, not the stem.
        for d in _XDG_APP_DIRS:
            candidate = d / f"{stem}.desktop"
            if candidate.is_file():
                cmd = ["gio", "launch", str(candidate)]
                break
    if cmd is None:
        log.warning("launcher: no gtk-launch or gio available")
        return False
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("launcher: launched %s (%s) via %s", stem, display, cmd[0])
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("launcher: launch failed for %s: %s", stem, e)
        return False


def _open_via_krunner(query: str) -> bool:
    """Fall back to KRunner's fuzzy launcher when the .desktop scan
    couldn't pin down a match. KRunner opens with the query pre-
    filled — Plasma's familiar UI; the user confirms with Enter.
    This handles aliases / paths / Plasma plugins our simple scan
    doesn't cover."""
    if not shutil.which("qdbus6"):
        return False
    try:
        result = subprocess.run(
            [
                "qdbus6", "org.kde.krunner", "/App",
                "org.kde.krunner.App.query", query,
            ],
            capture_output=True, timeout=_QDBUS_TIMEOUT_S, check=False,
        )
        if result.returncode != 0:
            return False
        log.info("launcher: opened KRunner with query %r", query)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("launcher: krunner D-Bus failed: %s", e)
        return False


def _do_launch(app_name: str) -> None:
    app_name = app_name.strip()
    if not app_name:
        emit_progress("No app named")
        return
    resolved = _resolve_app(app_name)
    if resolved is not None:
        stem, display = resolved
        if _launch_via_desktop(stem, display):
            emit_progress(f"Opening {display}")
            return
    # Fall back to KRunner UI — user disambiguates / confirms there.
    if _open_via_krunner(app_name):
        emit_progress(f"Searching {app_name!r}")
        return
    emit_progress(f"Couldn't open {app_name!r}")
    log.warning("launcher: no resolution path for %r", app_name)


def _app_launcher_op(args: dict) -> tuple | None:
    raw = (args or {}).get("app_name", "")
    if not isinstance(raw, str):
        raw = ""
    raw = raw.strip()
    if not raw:
        return None
    return ("callable", lambda name=raw: _do_launch(name))


register(Action(
    name="app_launcher",
    description=(
        "Open / launch a desktop application by name. Use for ANY "
        "imperative meaning 'start this app for me' — 'open Firefox', "
        "'launch Konsole', 'start Krita', 'otwórz Spotify', 'uruchom "
        "Kalkulator'. Extract the app name into params.app_name. "
        "Distinct from focus_window: this action LAUNCHES a fresh "
        "instance (or activates an already-running one through the "
        "system's normal launch path), while focus_window just shifts "
        "keyboard focus to a window that's already open. Prefer this "
        "for 'open' / 'launch' / 'start' / 'otwórz' / 'uruchom' verbs."
    ),
    triggers={
        "en": ["open", "launch", "start"],
        "pl": ["otwórz", "uruchom"],
    },
    op_factory=_app_launcher_op,
    parameters={
        "app_name": {
            "type": "string",
            "required": True,
            "description": (
                "Free-form application name as the user spoke it. "
                "Examples: 'Firefox', 'Konsole', 'Spotify', 'Krita', "
                "'Kalkulator'. Korder fuzzy-matches this against "
                "installed .desktop files; if no confident match, "
                "falls back to opening KRunner with the query "
                "pre-filled so the user can confirm. Required."
            ),
        },
    },
))
