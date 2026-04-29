"""LLM-based intent parser for inline voice actions.

Splits a Whisper transcript into a list of (text, key, char) ops the same way
inject._split_into_ops does, but using a small local LLM via ollama instead of
regex. Handles Polish, English, and natural phrasing variations that regex
can't generalize over.

Falls back to inject's regex parser if ollama is unreachable, the model returns
malformed output, or anything else goes wrong — so enabling LLM mode never
breaks the dictation path.
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request

from korder.inject import _KEY_BACKSPACE, _KEY_ENTER, _KEY_ESCAPE, _KEY_TAB, _split_into_ops

_OLLAMA_URL = "http://localhost:11434/api/generate"

_KEY_NAMES_TO_CODES = {
    "enter": _KEY_ENTER,
    "return": _KEY_ENTER,
    "tab": _KEY_TAB,
    "escape": _KEY_ESCAPE,
    "esc": _KEY_ESCAPE,
    "backspace": _KEY_BACKSPACE,
}

_PROMPT_TEMPLATE = """You are an inline action parser for a voice dictation tool. Given a transcript that mixes free dictation text with imperative key-press commands, return a JSON array of operations.

Each operation is one of:
- {"op": "text", "value": "..."}  — text to type literally
- {"op": "key", "value": "enter|return|tab|escape|backspace"}  — press a special key
- {"op": "char", "value": "\\n"}  — insert a newline within typed text

Rules:
- Plain dictation with no key-press intent → one text op containing the whole input.
- Phrases like "press enter", "wciśnij enter", "naciśnij enter", "submit", "wyślij", "potem enter", "and submit", "and enter" → key op for enter.
- Phrases like "press tab" or "tabuluj" → key op for tab.
- "new line" / "nowa linia" / "nowy wiersz" → char op with "\\n".
- Strip surrounding punctuation around action triggers ("Press Enter." → just key op, no trailing dot).
- Language is auto-detected; same rules apply for English and Polish.

Examples:
Input: "hello world"
Output: [{"op": "text", "value": "hello world"}]

Input: "please add this press enter and run it"
Output: [{"op": "text", "value": "please add this"}, {"op": "key", "value": "enter"}, {"op": "text", "value": "and run it"}]

Input: "Zwiększ rozmiar fontu wciśnij enter"
Output: [{"op": "text", "value": "Zwiększ rozmiar fontu"}, {"op": "key", "value": "enter"}]

Input: "she pressed enter on the keyboard"
Output: [{"op": "text", "value": "she pressed enter on the keyboard"}]

Now parse this transcript and return ONLY the JSON array, no other text.
Input: %s
Output:"""


class IntentParser:
    def __init__(self, model: str = "gemma4:e4b", timeout_s: float = 5.0):
        # Gemma 4 ships in E2B (2.3B effective) / E4B (4.5B) / 26B-A4B / 31B
        # variants per huggingface.co/blog/gemma4. E4B is the
        # recommended-for-JSON-tasks size — fits comfortably in 7800 XT
        # VRAM with room for whisper, and benchmarks show good structured
        # output on instruction-tuned variants. Adjust llm_model in config
        # to match the actual ollama tag if it differs.
        self.model = model
        self.timeout_s = timeout_s

    def parse(self, transcript: str) -> list[tuple]:
        """Returns ops in the same shape as inject._split_into_ops:
        list of ("text", str), ("key", int_keycode), ("char", str)."""
        if not transcript:
            return []
        try:
            llm_ops = self._call_ollama(transcript)
        except Exception as e:
            print(f"[korder] intent LLM failed, falling back to regex: {e}", flush=True)
            return _split_into_ops(transcript)

        normalized = _normalize_llm_ops(llm_ops)
        if normalized is None:
            print(f"[korder] intent LLM returned malformed output, falling back to regex", flush=True)
            return _split_into_ops(transcript)
        return normalized

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
        # ollama wraps the model output in `response` (a string)
        raw = body.get("response", "").strip()
        # The model is asked for a JSON array; format=json may wrap it in
        # an object. Try both shapes.
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "ops" in parsed:
            return parsed["ops"]
        if isinstance(parsed, dict) and len(parsed) == 1:
            only = next(iter(parsed.values()))
            if isinstance(only, list):
                return only
        raise ValueError(f"unexpected LLM output shape: {type(parsed).__name__}")


def _normalize_llm_ops(llm_ops: list) -> list[tuple] | None:
    """Translate LLM output (list of {op, value} dicts) to inject's tuple format.
    Returns None if anything is malformed."""
    out: list[tuple] = []
    if not isinstance(llm_ops, list):
        return None
    for entry in llm_ops:
        if not isinstance(entry, dict):
            return None
        op = entry.get("op")
        val = entry.get("value")
        if op == "text":
            if not isinstance(val, str):
                return None
            if val:
                out.append(("text", val))
        elif op == "char":
            if not isinstance(val, str):
                return None
            if val:
                out.append(("char", val))
        elif op == "key":
            if not isinstance(val, str):
                return None
            keycode = _KEY_NAMES_TO_CODES.get(val.lower())
            if keycode is None:
                return None
            out.append(("key", keycode))
        else:
            return None
    return out
