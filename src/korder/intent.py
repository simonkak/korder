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
import urllib.error
import urllib.request

from korder.actions.base import all_actions, get_action
from korder.actions.parser import split_into_ops

_OLLAMA_URL = "http://localhost:11434/api/generate"

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
