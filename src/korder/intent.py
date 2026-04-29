"""LLM-based intent parser for inline voice actions.

Architecture:
- Gemma is asked to *identify* trigger phrases in the transcript (where they
  start, what action they map to). Gemma does NOT reproduce text content.
- We then find each phrase in the original input and segment around it
  ourselves. Text op values come from the original input verbatim, so the
  model can't corrupt user speech.

Falls back to inject's regex parser if ollama is unreachable, the model
returns malformed output, or any phrase isn't found in the input — so
enabling LLM mode never breaks the dictation path.
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request

from korder.inject import (
    NAMED_SHORTCUTS,
    _KEY_BACKSPACE,
    _KEY_ENTER,
    _KEY_ESCAPE,
    _KEY_TAB,
    _split_into_ops,
)

_OLLAMA_URL = "http://localhost:11434/api/generate"

_KEY_NAMES_TO_CODES = {
    "enter": _KEY_ENTER,
    "return": _KEY_ENTER,
    "tab": _KEY_TAB,
    "escape": _KEY_ESCAPE,
    "esc": _KEY_ESCAPE,
    "backspace": _KEY_BACKSPACE,
}

_PUNCT_TO_STRIP = " \t.!?,;:\n"

_PROMPT_TEMPLATE = """You are an inline action detector for a voice dictation tool. Identify imperative key-press phrases in a transcript and return JSON describing them. You do NOT reproduce text content — just identify where the action phrases are.

Return a JSON object with one field "actions" containing an array of detected actions:
{
  "actions": [
    {"phrase": "<exact phrase as it appears in input>", "type": "key|char|shortcut", "value": "<value>"}
  ]
}

The "phrase" field must be a contiguous substring that appears VERBATIM in the input (case-sensitive).
The "type" determines what gets executed:
- "key": single key press; value is one of: enter, return, tab, escape, backspace
- "char": insert character; value is a literal string like "\\n"
- "shortcut": named keyboard shortcut; value is one of: delete_word, delete_word_forward, select_all, undo, select_to_line_start, select_to_line_end

Action triggers (English and Polish):
- "press enter", "wciśnij enter", "naciśnij enter", "submit", "wyślij", "potem enter", "and enter" → type=key, value=enter
- "press tab", "tabuluj" → type=key, value=tab
- "press escape" → type=key, value=escape
- "press backspace", "skasuj" → type=key, value=backspace
- "new line", "nowa linia", "nowy wiersz" → type=char, value=\\n
- "delete word", "usuń słowo", "skasuj słowo" → type=shortcut, value=delete_word
- "select all", "zaznacz wszystko" → type=shortcut, value=select_all
- "select line", "zaznacz linię" → type=shortcut, value=select_to_line_start
- "select to end", "zaznacz do końca" → type=shortcut, value=select_to_line_end
- "undo", "cofnij" → type=shortcut, value=undo

Rules:
- If no action triggers are present, return {"actions": []}.
- Descriptive prose like "she pressed enter on the keyboard" is NOT an action — it's narrative text.
- Return phrases in the order they appear. Multiple actions per input are fine.
- The phrase must match the input EXACTLY (preserve case, accents, etc.) so we can find it.

Examples:
Input: "hello world"
Output: {"actions": []}

Input: "Naciśnij Enter."
Output: {"actions": [{"phrase": "Naciśnij Enter", "type": "key", "value": "enter"}]}

Input: "Co tu się stało? Naciśnij Enter."
Output: {"actions": [{"phrase": "Naciśnij Enter", "type": "key", "value": "enter"}]}

Input: "Usuń słowo"
Output: {"actions": [{"phrase": "Usuń słowo", "type": "shortcut", "value": "delete_word"}]}

Input: "ten kod jest zły usuń słowo"
Output: {"actions": [{"phrase": "usuń słowo", "type": "shortcut", "value": "delete_word"}]}

Input: "Zaznacz linię"
Output: {"actions": [{"phrase": "Zaznacz linię", "type": "shortcut", "value": "select_to_line_start"}]}

Input: "Skasuj"
Output: {"actions": [{"phrase": "Skasuj", "type": "key", "value": "backspace"}]}

Input: "press enter and run it"
Output: {"actions": [{"phrase": "press enter", "type": "key", "value": "enter"}]}

Input: "napisz coś potem wyślij"
Output: {"actions": [{"phrase": "potem wyślij", "type": "key", "value": "enter"}]}

Input: "she pressed enter on the keyboard"
Output: {"actions": []}

Input: "Myślę, że to jest OK."
Output: {"actions": []}

Now analyze this transcript and return ONLY the JSON object, no other text.
Input: %s
Output:"""


class IntentParser:
    def __init__(self, model: str = "gemma4:e2b", timeout_s: float = 5.0):
        # Gemma 4 ships in E2B (2.3B effective) / E4B (4.5B) / 26B-A4B / 31B
        # variants per huggingface.co/blog/gemma4. E4B is recommended for
        # JSON tasks; E2B is faster but more prone to text mangling — which
        # is why this parser only uses the LLM for *classification*, not
        # for reproducing text. Adjust llm_model in config to match the
        # actual ollama tag.
        self.model = model
        self.timeout_s = timeout_s

    def parse(self, transcript: str) -> list[tuple]:
        """Returns ops in inject's format. Text ops always come from the
        original transcript verbatim — LLM never touches text content."""
        if not transcript:
            return []
        try:
            actions = self._call_ollama(transcript)
        except Exception as e:
            print(f"[korder] intent LLM failed, falling back to regex: {e}", flush=True)
            return _split_into_ops(transcript)

        print(f"[korder] LLM actions for {transcript!r}: {actions!r}", flush=True)

        ops = _segment_input_by_actions(transcript, actions)
        if ops is None:
            print(f"[korder] LLM action phrase not found in input, falling back to regex", flush=True)
            return _split_into_ops(transcript)
        print(f"[korder] segmented ops: {ops!r}", flush=True)
        return ops

    def _call_ollama(self, transcript: str) -> list:
        prompt = _PROMPT_TEMPLATE % json.dumps(transcript)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 256},
        }
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        raw = body.get("response", "").strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list):
            return parsed["actions"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "phrase" in parsed:
            # Single action collapsed to a bare object
            return [parsed]
        raise ValueError(f"unexpected LLM output shape: {type(parsed).__name__}: {parsed!r}")


def _segment_input_by_actions(transcript: str, actions: list) -> list[tuple] | None:
    """Slice the original transcript at action phrase boundaries, returning
    ops whose text values come straight from the input. Returns None if any
    phrase can't be located in the input (signals fallback)."""
    if not isinstance(actions, list):
        return None

    # Find each phrase's position in the transcript (case-insensitive,
    # left-to-right, advancing past previous matches).
    found: list[tuple[int, int, dict]] = []
    cursor = 0
    lower = transcript.lower()
    for a in actions:
        if not isinstance(a, dict):
            return None
        phrase = a.get("phrase")
        if not isinstance(phrase, str) or not phrase:
            continue
        idx = lower.find(phrase.lower(), cursor)
        if idx == -1:
            # Try without leading word boundary cursor advance — phrase might
            # appear anywhere; if still not found, the model hallucinated it.
            idx = lower.find(phrase.lower())
            if idx == -1 or idx < cursor:
                return None
        found.append((idx, idx + len(phrase), a))
        cursor = idx + len(phrase)

    ops: list[tuple] = []
    pos = 0
    for start, end, a in found:
        if start > pos:
            seg = transcript[pos:start].strip(_PUNCT_TO_STRIP)
            if seg:
                ops.append(("text", seg))
        op = _action_to_op(a)
        if op is not None:
            ops.append(op)
        pos = end
    if pos < len(transcript):
        seg = transcript[pos:].strip(_PUNCT_TO_STRIP)
        if seg:
            ops.append(("text", seg))

    return ops


def _action_to_op(action: dict) -> tuple | None:
    type_ = action.get("type")
    value = action.get("value")
    if not isinstance(value, str):
        return None
    if type_ == "key":
        keycode = _KEY_NAMES_TO_CODES.get(value.lower())
        if keycode is None:
            return None
        return ("key", keycode)
    if type_ == "shortcut":
        keycodes = NAMED_SHORTCUTS.get(value.lower())
        if keycodes is None:
            return None
        return ("combo", keycodes)
    if type_ == "char":
        if not value:
            return None
        return ("char", value)
    return None
