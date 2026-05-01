"""LLM-based intent parser for inline voice actions.

Architecture:
- The action registry (korder.actions) is the single source of truth for
  what triggers exist. The LLM prompt is auto-generated from it; adding
  a new action means writing one module under actions/ and never editing
  the prompt by hand.
- Gemma is asked to *identify* trigger phrases in the transcript and the
  action name — it does NOT reproduce text content. We slice the input
  around action positions ourselves, so text op contents come from the
  original transcript verbatim.

Fall back to the regex parser if ollama is unreachable, output is malformed,
or any phrase isn't found in the input.
"""
from __future__ import annotations
import json
import sys
import threading
import time
import urllib.error
import urllib.request

from korder.actions.base import all_actions, get_action
from korder.actions.parser import split_into_ops

_OLLAMA_URL = "http://localhost:11434/api/generate"
_OLLAMA_PS_URL = "http://localhost:11434/api/ps"

_PUNCT_TO_STRIP = " \t.!?,;:\n"


def _extract_json_object(text: str):
    """Parse a JSON object from a response that may include markdown fences
    or surrounding prose.

    With format=json forced, ollama returns bare JSON we can json.loads
    directly. With thinking mode (which is incompatible with format=json
    on /api/generate), Gemma sometimes wraps the answer in ``` or ```json
    fences, so we have to strip those before parsing. Last resort: pull
    the first balanced {...} slice out of the response.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Markdown fence (``` or ```json … ```)
    if stripped.startswith("```"):
        body = stripped.lstrip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.lstrip("\n").rstrip()
        end_fence = body.rfind("```")
        if end_fence >= 0:
            body = body[:end_fence]
        try:
            return json.loads(body.strip())
        except json.JSONDecodeError:
            pass
    # Last-resort: first {...} slice (greedy, balanced enough for our shape).
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return json.loads(stripped[start : end + 1])
    raise ValueError(f"no JSON object in LLM response: {text!r}")


_SYSTEM_PROMPT = (
    "You are an inline action detector for a voice dictation tool. The user "
    "dictates speech that may include imperative commands (key presses, media "
    "control, app actions). Your job is to identify which commands are present "
    "and classify them by *intent*, not by literal string match. The user may "
    "speak in any language Whisper transcribed — match meaning, not surface form.\n"
    "\n"
    "Return a single JSON object with this exact shape:\n"
    '  {"actions": [{"phrase": "<exact substring of input>", "name": "<action_name>", "params": {...}}]}\n'
    "\n"
    "Rules you must always follow:\n"
    "- If the transcript is plain dictation (description, prose, narrative, no imperative command), "
    'return {"actions": []}.\n'
    "- The `phrase` field must be a substring that appears verbatim in the user input.\n"
    "- The `name` field must be one of the action names listed in the user message.\n"
    "- Multiple actions in one input are allowed; return them in the order they appear.\n"
    "- Descriptive prose about a command (e.g., 'she pressed enter on the keyboard') is NOT an action.\n"
    "- For parameterized actions, extract relevant fields into `params`. If a required parameter "
    "is not present in the input, leave `params` empty or omit it — do NOT invent values."
)


def _render_action_catalogue(*, show_triggers: bool) -> str:
    """Render the per-call action list. With show_triggers=False (default), "
    "the LLM only sees name + description + params and reasons by intent. "
    "With show_triggers=True, full trigger phrase lists are appended."""
    lines = []
    for action in all_actions():
        line = f'- {action.name}: {action.description}'
        if action.parameters:
            param_keys = ", ".join(action.parameters.keys())
            line += f' [params: {param_keys}]'
        if show_triggers:
            triggers_flat = ", ".join(f'"{t}"' for t in action.all_triggers())
            line += f' (example phrasings: {triggers_flat})'
        lines.append(line)
    return "\n".join(lines)


def _build_user_prompt(transcript: str, *, show_triggers: bool) -> str:
    """Per-call user message: action catalogue + transcript + reminder of
    the JSON output format."""
    catalogue = _render_action_catalogue(show_triggers=show_triggers)
    return (
        "Available actions:\n"
        f"{catalogue}\n"
        "\n"
        "A few examples to anchor your output shape:\n"
        '  "hello world" → {"actions": []}\n'
        '  "Naciśnij Enter." → {"actions": [{"phrase": "Naciśnij Enter", "name": "press_enter"}]}\n'
        '  "Spotify zagraj Linkin Park" → {"actions": [{"phrase": "Spotify zagraj Linkin Park", "name": "spotify_search", "params": {"query": "Linkin Park", "kind": "album"}}]}\n'
        '  "press enter and run it" → {"actions": [{"phrase": "press enter", "name": "press_enter"}]}\n'
        '  "she pressed enter on the keyboard" → {"actions": []}\n'
        "\n"
        f"Now analyze this transcript and return ONLY the JSON object:\n"
        f"Input: {json.dumps(transcript, ensure_ascii=False)}\n"
        "Output:"
    )


class IntentParser:
    def __init__(
        self,
        model: str = "gemma4:e2b",
        timeout_s: float = 8.0,
        thinking_mode: bool = False,
        show_triggers_in_prompt: bool = False,
        keep_alive_s: float = 300.0,
    ):
        self.model = model
        self.timeout_s = timeout_s
        self.thinking_mode = thinking_mode
        self.show_triggers_in_prompt = show_triggers_in_prompt
        self.keep_alive_s = keep_alive_s
        # Reasoning trace from the most recent _call_ollama. Populated only
        # when thinking_mode is on. Surfaced for diagnostics (logged to
        # stderr in _call_ollama; readable by the benchmark dialog).
        self.last_thinking: str = ""
        # Cache of LLM-generated user-facing strings, keyed on
        # (kind, action_name, locale). Each entry is generated once
        # per app session via _call_ollama_completion; subsequent
        # lookups hit instantly. Sized small (≤20 actions × 2 locales
        # × handful of kinds) so no eviction logic needed.
        self._feedback_cache: dict[tuple[str, str, str], str] = {}

    def warm_up(self) -> None:
        """Fire-and-forget: tell ollama to page the model into VRAM,
        without generating anything. Used by the UI to start the cold
        load while the user is still dictating, so the actual intent
        call doesn't pay the load cost on the critical path. Safe to
        call repeatedly — an already-resident model just bumps its
        keep_alive timer. Returns immediately; the actual HTTP call
        runs on a daemon thread."""
        already_loaded = self.is_model_loaded()
        if already_loaded:
            print(
                f"[korder] warm-up: {self.model} already resident — "
                f"keep_alive bumped",
                flush=True, file=sys.stderr,
            )
        else:
            print(
                f"[korder] warm-up: {self.model} not resident — "
                f"kicking off background load",
                flush=True, file=sys.stderr,
            )
        payload = {
            "model": self.model,
            "prompt": "",
            "stream": False,
            "keep_alive": self.keep_alive_s,
        }
        def _send() -> None:
            t0 = time.perf_counter()
            try:
                req = urllib.request.Request(
                    _OLLAMA_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30.0) as resp:
                    resp.read()
            except Exception as e:
                # Warm-up is opportunistic; failure here is fine —
                # the actual parse call will surface a real error.
                print(
                    f"[korder] warm-up of {self.model} failed: {e}",
                    flush=True, file=sys.stderr,
                )
                return
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            verb = "bumped" if already_loaded else "loaded"
            print(
                f"[korder] warm-up: {self.model} {verb} in {elapsed_ms:.0f} ms",
                flush=True, file=sys.stderr,
            )
        threading.Thread(target=_send, daemon=True).start()

    def is_model_loaded(self) -> bool:
        """Quick check (≈20 ms) of whether ollama already has self.model
        resident in VRAM. Used by the UI to show a 'Loading' state on
        the cold-start path (when keep_alive_s expired). Fails open —
        any error returns True so a transient ollama hiccup doesn't
        falsely flash 'Loading' on every command.
        """
        try:
            req = urllib.request.Request(_OLLAMA_PS_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return True
        for m in body.get("models", []) or []:
            if m.get("name") == self.model or m.get("model") == self.model:
                return True
        return False

    def warm_feedback_cache(self, locale: str = "en") -> None:
        """Pre-generate confirmation prompts for every action that has a
        'confirm' parameter, in the given locale, on a background
        thread. Idempotent — skips cached entries. Called once at app
        boot so the first time the user triggers a confirmable action,
        the cached prompt is already there and the OSD update is
        instant.

        Generation is serial (not parallel) so we don't overload the
        ollama queue — each completion is ~300-500ms and there are
        only a handful of confirmable actions, so total is ~1-2s
        running alongside whatever else happens at startup.
        """
        confirmable = [a for a in all_actions() if "confirm" in a.parameters]
        if not confirmable:
            return
        def _populate() -> None:
            for action in confirmable:
                if ("confirm", action.name, locale) in self._feedback_cache:
                    continue
                # Side-effect of generate_confirm_prompt is to populate
                # the cache; we ignore the return.
                self.generate_confirm_prompt(action.name, locale)
        threading.Thread(target=_populate, daemon=True).start()

    def generate_confirm_prompt(
        self,
        action_name: str,
        locale: str = "en",
    ) -> str | None:
        """Ask the LLM to phrase a natural confirmation question for a
        destructive action, in the user's locale. Returns None if the
        LLM call fails or the action isn't registered — caller should
        fall back to the i18n template chain.

        Cached per (action, locale) for the lifetime of the parser.
        First call ~300-500ms, subsequent calls instant.
        """
        cached = self._feedback_cache.get(("confirm", action_name, locale))
        if cached is not None:
            return cached

        action = get_action(action_name)
        if action is None:
            return None

        prompt = (
            "You are Korder, a voice assistant. The user just asked you "
            "to perform an action that is irreversible or disruptive — "
            "you must confirm before running it.\n\n"
            f"Action description: {action.description}\n\n"
            f"Locale: {locale} ({'Polish' if locale == 'pl' else 'English'})\n\n"
            "Generate a brief confirmation question (max 12 words) in the "
            "user's locale. Phrase it naturally and clearly — make it "
            "obvious what is about to happen. End with a hint that the "
            "user should say 'yes' or 'no' (or the locale equivalent).\n\n"
            "Output ONLY the question, no explanation, no quotes, no prefix."
        )
        text = self._call_ollama_completion(prompt, max_tokens=40)
        if not text:
            return None
        # Trim quotes/punctuation noise the model sometimes adds.
        text = text.strip().strip('"').strip("'").strip()
        if not text:
            return None
        self._feedback_cache[("confirm", action_name, locale)] = text
        print(
            f"[korder] feedback: confirm/{action_name}/{locale} → {text!r}",
            flush=True, file=sys.stderr,
        )
        return text

    def _call_ollama_completion(self, prompt: str, max_tokens: int = 80) -> str | None:
        """Free-form text completion (no JSON mode, no structured
        output). Used for generating user-facing strings on the fly.
        Returns None on any error so callers can fall back gracefully.

        ``think: false`` is critical: gemma4 is a thinking-capable
        model and defaults to emitting reasoning tokens before the
        actual answer. With our short num_predict budget, the
        response field comes back empty (whole budget eaten by
        thinking tokens that go to a separate field). Setting
        ``think: false`` bypasses the reasoning step and writes
        directly to the response field.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive_s,
            "options": {
                "temperature": 0.3,
                "num_predict": max_tokens,
            },
        }
        try:
            req = urllib.request.Request(
                _OLLAMA_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[korder] feedback completion failed: {e}", flush=True, file=sys.stderr)
            return None
        return (body.get("response") or "").strip() or None

    def parse(self, transcript: str) -> list[tuple]:
        if not transcript:
            return []
        try:
            actions = self._call_ollama(transcript)
        except Exception as e:
            print(f"[korder] intent LLM failed, falling back to regex: {e}", flush=True, file=sys.stderr)
            return split_into_ops(transcript)

        print(f"[korder] LLM actions for {transcript!r}: {actions!r}", flush=True, file=sys.stderr)

        # Supplement: if LLM came back empty but the regex parser sees an
        # actual registered trigger phrase, use the regex result. Catches
        # cases where E2B-class models miss obvious triggers (Polish exit
        # mode toggles in particular). LLM still wins for "she pressed
        # enter on the keyboard" because regex would also classify that
        # as text-only thanks to the word-boundary trigger phrasing.
        if not actions:
            regex_ops = split_into_ops(transcript)
            if any(op[0] != "text" for op in regex_ops):
                print(
                    f"[korder] LLM found no actions; regex caught triggers, using regex: {regex_ops!r}",
                    flush=True,
                    file=sys.stderr,
                )
                return regex_ops

        ops = segment_input_by_actions(transcript, actions)
        if ops is None:
            print(f"[korder] LLM action phrase not found in input, falling back to regex", flush=True, file=sys.stderr)
            return split_into_ops(transcript)
        print(f"[korder] segmented ops: {ops!r}", flush=True, file=sys.stderr)
        return ops

    def _call_ollama(self, transcript: str) -> list:
        user_prompt = _build_user_prompt(
            transcript, show_triggers=self.show_triggers_in_prompt
        )
        payload: dict = {
            "model": self.model,
            "system": _SYSTEM_PROMPT,
            "prompt": user_prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 512},
            "keep_alive": self.keep_alive_s,
        }
        if self.thinking_mode:
            # Ollama's /api/generate suppresses the "thinking" field
            # whenever format=json is set, so when reasoning is requested
            # we drop the strict JSON constraint and rely on the system
            # prompt to keep output well-formed. _extract_json_object
            # handles markdown fences that Gemma tends to add in this
            # mode.
            payload["think"] = True
        else:
            payload["format"] = "json"
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # Capture and log Gemma's reasoning trace when thinking mode is on.
        # Ollama returns it in a separate "thinking" field so the JSON
        # response stays parseable; we keep it for diagnostics.
        thinking = (body.get("thinking") or "").strip()
        self.last_thinking = thinking
        if thinking:
            print(
                f"[korder] gemma thinking for {transcript!r}:\n  {thinking}",
                flush=True,
                file=sys.stderr,
            )
        raw = body.get("response", "").strip()
        parsed = _extract_json_object(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list):
            return parsed["actions"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "phrase" in parsed:
            return [parsed]
        raise ValueError(f"unexpected LLM output shape: {type(parsed).__name__}: {parsed!r}")


def segment_input_by_actions(transcript: str, actions: list) -> list[tuple] | None:
    """Slice the original transcript at action phrase boundaries, returning
    ops whose text values come straight from the input. Returns None if any
    phrase can't be located in the input or names an unknown action
    (signals fallback to regex)."""
    if not isinstance(actions, list):
        return None

    found: list[tuple[int, int, str, dict]] = []
    cursor = 0
    lower = transcript.lower()
    for entry in actions:
        if not isinstance(entry, dict):
            return None
        phrase = entry.get("phrase")
        action_name = entry.get("name")
        # Backward compat with old {type, value} shape — translate to a name.
        if action_name is None and "type" in entry and "value" in entry:
            action_name = _legacy_type_value_to_name(entry["type"], entry["value"])
        if not isinstance(phrase, str) or not phrase or not isinstance(action_name, str):
            continue
        action = get_action(action_name)
        if action is None:
            # Gemma sometimes returns a trigger phrase ("resume", "pauza")
            # as the action name instead of the proper name ("play_pause").
            # Look it up in the trigger phrase map and recover gracefully.
            from korder.actions.parser import _compile_trigger_regex
            _, phrase_map = _compile_trigger_regex()
            mapped = phrase_map.get(action_name.lower())
            if mapped:
                action = get_action(mapped)
                action_name = mapped
            if action is None:
                return None
        params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
        idx = lower.find(phrase.lower(), cursor)
        if idx == -1:
            idx = lower.find(phrase.lower())
            if idx == -1 or idx < cursor:
                return None
        found.append((idx, idx + len(phrase), action_name, params))
        cursor = idx + len(phrase)

    ops: list[tuple] = []
    pos = 0
    for start, end, action_name, params in found:
        if start > pos:
            seg = transcript[pos:start]
            if pos > 0:
                seg = seg.lstrip(_PUNCT_TO_STRIP)
            seg = seg.rstrip(_PUNCT_TO_STRIP)
            if seg:
                ops.append(("text", seg))
        action = get_action(action_name)
        if action is not None:
            op = action.op_factory(params)
            if op is None and action.parameters:
                # Action has declared params but the LLM didn't supply
                # them — emit a pending marker so MainWindow can grab the
                # next commit as the parameter.
                ops.append(("pending_action", action_name))
            elif op is not None:
                ops.append(op)
        pos = end
    if pos < len(transcript):
        seg = transcript[pos:]
        if pos > 0:
            seg = seg.lstrip(_PUNCT_TO_STRIP)
        if seg:
            ops.append(("text", seg))
    return ops


def _legacy_type_value_to_name(type_: str, value: str) -> str | None:
    """Backward-compat: old prompt used {type: key|char|shortcut, value: enter|...}.
    Some existing models still emit that shape; map to action names."""
    if not isinstance(value, str):
        return None
    v = value.lower()
    if type_ == "key":
        return {
            "enter": "press_enter",
            "return": "press_enter",
            "tab": "press_tab",
            "escape": "press_escape",
            "esc": "press_escape",
            "backspace": "press_backspace",
        }.get(v)
    if type_ == "shortcut":
        # Shortcut value is already an action name.
        return v
    if type_ == "char":
        if value == "\n":
            return "new_line"
        if value == "\n\n":
            return "new_paragraph"
    return None
