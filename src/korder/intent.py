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
from dataclasses import dataclass

from korder.actions.base import all_actions, get_action
from korder.actions.parser import split_into_ops


@dataclass(frozen=True)
class Turn:
    """One past exchange in the current dictation session: what the
    user said, what action (if any) fired, and what natural-language
    reply the LLM produced. Used as conversation context for
    follow-up resolution — e.g. 'A Polski?' after asking about
    France's capital."""
    user_text: str
    action_name: str  # main action name fired, "" if none
    response: str  # last_response from that turn, "" if none


# Number of past turns to feed into the parser as context. 4 covers
# typical Q&A → follow-up → follow-up patterns without bloating the
# prompt enough to risk a meaningful latency or accuracy regression.
_MAX_HISTORY_TURNS = 4

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
    "You are an inline action detector for dictated speech. Identify "
    "commands by INTENT, not literal string match. Match meaning across "
    "any language Whisper transcribed.\n"
    "\n"
    "Return ONE JSON object with this exact shape:\n"
    '  {"actions": [{"phrase": "<verbatim substring of input>", "name": "<action_name>", "params": {...}}],\n'
    '   "response": "<optional natural-language reply, same language as input>"}\n'
    "\n"
    "Rules:\n"
    "- Plain dictation with no command → `{\"actions\": []}`.\n"
    "- `phrase` must appear verbatim in the input. `name` must be one of "
    "the listed action names. Multiple actions allowed, in order.\n"
    "- For parameterized actions, extract values into `params`. The "
    "value is the SUBJECT of the search/play — a proper noun, name, "
    "or topic, NOT structural nouns describing what to do with it "
    "('strona/page', 'artykuł/article', 'miasto/city', 'utwór/track', "
    "'wynik/result' etc., when used like 'show the page', 'play the "
    "track'). When the input has only structural nouns or a bare "
    "action verb ('show its page', 'pokaż stronę miasta', 'play it on "
    "Spotify') AND a prior turn named a specific topic, USE THE PRIOR "
    "TOPIC as the value — don't make the user repeat themselves, and "
    "don't grab a structural noun as if it were the subject. Leave "
    "the value out only when neither input nor context names a real "
    "subject. Do NOT invent.\n"
    "- A song / album / artist name in a query is query content, NOT "
    "narrative — even if it contains pronouns or verbs.\n"
    "\n"
    "`response` cases (optional, same language as input):\n"
    "- Confirmation needed (`confirm` param missing): 'are you sure?' "
    "with yes/no hint.\n"
    "- Other parameter missing (`query`, etc.) AND no prior turn names "
    "the topic to infer from: ask what the user wants. If the prior "
    "turn DOES name the topic, fill the param from context instead — "
    "don't ask.\n"
    "- Factual question, no action match, you know the answer from "
    "training (math, geography, definitions, etc.): answer directly, "
    "briefly. Don't answer about live data (time, today's weather). "
    "If unsure, say so plainly — don't invent.\n"
    "- Small-talk / opinion ('do you like cats?', 'how are you?'): brief, "
    "friendly, in-character (≤ 2 sentences).\n"
    "- Otherwise leave `response` empty.\n"
    "\n"
    "Don't emit BOTH a direct factual answer AND a search action — pick "
    "one. Reserve `web_search` / `wikipedia_search` for when the user "
    "explicitly says to open a browser / look something up. Use prior "
    "turns (when shown) to resolve follow-ups like 'and Poland?' against "
    "the prior question — answer in the same mode (response vs action) "
    "as the prior turn."
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


def _render_history(history: list[Turn]) -> str:
    """Format prior turns as a 'Recent conversation' block to prepend to
    the analysis section of the user prompt. Empty string when there's
    no history. Each turn is one User: + Assistant: pair, trimmed of
    empty fields so action-only turns don't print blank Assistant lines."""
    if not history:
        return ""
    lines = ["Recent conversation in this session (oldest first):"]
    for turn in history:
        lines.append(f"  User: {turn.user_text!r}")
        if turn.response:
            lines.append(f"  Assistant: {turn.response!r}")
        elif turn.action_name:
            lines.append(f"  (Korder fired action: {turn.action_name})")
    lines.append("")
    return "\n".join(lines)


def _build_user_prompt(
    transcript: str,
    history: list[Turn] | None = None,
    *,
    show_triggers: bool,
) -> str:
    """Per-call user message: action catalogue + optional conversation
    history + transcript + reminder of the JSON output format."""
    catalogue = _render_action_catalogue(show_triggers=show_triggers)
    history_block = _render_history(history or [])
    return (
        "Available actions:\n"
        f"{catalogue}\n"
        "\n"
        + history_block +
        "Examples:\n"
        '  "hello world" → {"actions": []}\n'
        '  "Naciśnij Enter." → {"actions": [{"phrase": "Naciśnij Enter", "name": "press_enter"}]}\n'
        '  "press enter and run it" → {"actions": [{"phrase": "press enter", "name": "press_enter"}]}\n'
        '  "Spotify zagraj Linkin Park" → {"actions": [{"phrase": "Spotify zagraj Linkin Park", "name": "spotify_search", "params": {"query": "Linkin Park"}}]}\n'
        '  "Odtwórz Lose Yourself w Spotify" → {"actions": [{"phrase": "Odtwórz Lose Yourself w Spotify", "name": "spotify_search", "params": {"query": "Lose Yourself"}}]}\n'
        '  "Odtwórz utwór all the things she said w Spotify" → {"actions": [{"phrase": "Odtwórz utwór all the things she said w Spotify", "name": "spotify_search", "params": {"query": "all the things she said", "kind": "track"}}]}\n'
        '  "Spotify zagraj" → {"actions": [{"phrase": "Spotify zagraj", "name": "spotify_search"}], "response": "Co chcesz odtworzyć w Spotify?"}\n'
        '  "shutdown computer" → {"actions": [{"phrase": "shutdown computer", "name": "shutdown"}], "response": "Are you sure you want to shut down? Say yes or no."}\n'
        '  "shutdown computer yes" → {"actions": [{"phrase": "shutdown computer yes", "name": "shutdown", "params": {"confirm": "yes"}}]}\n'
        '  "what is the capital of France" → {"actions": [], "response": "Paris."}\n'
        '  "ile to siedem razy osiem" → {"actions": [], "response": "Pięćdziesiąt sześć."}\n'
        '  "czy lubisz kotki?" → {"actions": [], "response": "Tak, lubię kotki — są urocze."}\n'
        '  "wikipedia, Paryż" → {"actions": [{"phrase": "wikipedia, Paryż", "name": "wikipedia_search", "params": {"query": "Paryż"}}]}\n'
        '  "Zakończę." → {"actions": [{"phrase": "Zakończę.", "name": "cancel_session"}]}\n'
        "  Follow-ups (use prior turn's topic — for both factual answers AND action params,\n"
        "  do NOT grab structural nouns like 'strona', 'miasto', 'page'):\n"
        '    prior: User "Co powiesz o mieście Warszawa?" / Assistant "Warszawa to duże miasto..."\n'
        '    now:   "ile ma mieszkańców?" → {"actions": [], "response": "Warszawa ma ponad 1,8 miliona mieszkańców."}\n'
        '    prior: User "Jaka jest stolica Francji?" / Assistant "Paryż."\n'
        '    now:   "A Polski?" → {"actions": [], "response": "Warszawa."}\n'
        '    prior: User "Co możesz powiedzieć o Gdańsku?" / Assistant "Gdańsk to piękne miasto..."\n'
        '    now:   "Pokaż stronę na Wikipedii" → {"actions": [{"phrase": "Pokaż stronę na Wikipedii", "name": "wikipedia_search", "params": {"query": "Gdańsk"}}]}\n'
        '    prior: User "Co powiesz o Warszawie?" / Assistant "Warszawa to duże miasto..."\n'
        '    now:   "Pokaż stronę miasta w wikipedii" → {"actions": [{"phrase": "Pokaż stronę miasta w wikipedii", "name": "wikipedia_search", "params": {"query": "Warszawa"}}]}\n'
        '           (NOT query="miasta" — \'miasta\' is structural, the subject from the prior turn is Warszawa)\n'
        '    prior: User "what is Bohemian Rhapsody?" / Assistant "It\'s a 1975 Queen song..."\n'
        '    now:   "play it on Spotify" → {"actions": [{"phrase": "play it on Spotify", "name": "spotify_search", "params": {"query": "Bohemian Rhapsody"}}]}\n'
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
        # Free-form natural-language reply from the LLM for the most
        # recent parse, extracted from the JSON `response` field.
        # Used today for confirmation prompts on destructive actions;
        # ground for future conversational/TTS features. Empty string
        # when the LLM didn't include a response (the common case).
        self.last_response: str = ""
        # Rolling conversation history, fed back into the parser prompt
        # so follow-up questions ('A Polski?' after the France-capital
        # question) resolve against prior turns. Cleared by
        # clear_history() on dictation-session boundaries
        # (auto-stop, cancel, hotkey toggle-off).
        self._history: list[Turn] = []

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

    def clear_history(self) -> None:
        """Drop the conversation context. Called by main_window at
        dictation-session boundaries (recorder stops, cancel) so the
        next session starts fresh — yesterday's 'and Polish?' shouldn't
        resolve against last week's France question."""
        if self._history:
            print(f"[korder] history cleared ({len(self._history)} turns)", flush=True, file=sys.stderr)
        self._history = []

    def _push_turn(self, transcript: str, actions: list, response: str) -> None:
        """Append a Turn for this parse to history, trimmed to the most
        recent _MAX_HISTORY_TURNS entries. Action-only turns (no
        response) still get recorded for context — 'press enter' →
        'and now backspace?' should plausibly work."""
        action_name = ""
        if isinstance(actions, list) and actions:
            first = actions[0]
            if isinstance(first, dict):
                action_name = first.get("name") or ""
        self._history.append(Turn(
            user_text=transcript,
            action_name=action_name,
            response=response,
        ))
        if len(self._history) > _MAX_HISTORY_TURNS:
            self._history = self._history[-_MAX_HISTORY_TURNS:]

    def parse(self, transcript: str) -> list[tuple]:
        if not transcript:
            return []
        try:
            actions = self._call_ollama(transcript)
        except Exception as e:
            print(f"[korder] intent LLM failed, falling back to regex: {e}", flush=True, file=sys.stderr)
            return split_into_ops(transcript)
        # Record this turn BEFORE the regex-supplement / segmentation
        # path forks — history is about what the LLM saw and produced,
        # which is the input to the next round's resolution.
        self._push_turn(transcript, actions, self.last_response)

        # Safety net: clear hallucinated `confirm` params. Smaller
        # models (e2b) sometimes invent a confirm value even when the
        # user didn't say a confirmation word in the transcript — the
        # most likely failure mode is also the most dangerous (LLM
        # writes confirm="yes" → systemctl poweroff fires without
        # actual user consent). The rule: the confirm value must
        # appear literally in the input. Anything else is rejected
        # and the action goes pending so the user has a real chance
        # to confirm or cancel.
        _scrub_hallucinated_confirm(transcript, actions)

        print(f"[korder] LLM actions for {transcript!r}: {actions!r}", flush=True, file=sys.stderr)

        # Supplement: if LLM came back empty but the regex parser sees an
        # actual registered trigger phrase, use the regex result. Catches
        # cases where E2B-class models miss obvious triggers (Polish exit
        # mode toggles in particular). LLM still wins for "she pressed
        # enter on the keyboard" because regex would also classify that
        # as text-only thanks to the word-boundary trigger phrasing.
        #
        # Exception: when last_response is populated, the LLM intentionally
        # chose to answer the user conversationally rather than dispatch
        # an action. Skipping the regex supplement preserves that choice
        # — otherwise a fuzzy trigger match like "what is" → wikipedia
        # would overshadow Gemma's direct answer.
        if not actions:
            if self.last_response:
                return [("text", transcript)] if transcript else []
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

        # Second-chance regex backstop: LLM emitted SOMETHING in actions
        # (so the empty-actions branch above didn't fire), but every
        # entry got skipped by segmentation — typically because it
        # lacked a `phrase` field or used a legacy schema like
        # `{name: 'actions', args: [...]}`. Result is a text-only
        # ops list, which is wrong for "Odtwórz w Spotify Linkin Park"
        # and similar utterances where regex would clearly catch the
        # trigger. Try regex; if it finds something actionable, prefer
        # that over silently typing the user's command.
        if actions and ops and all(op[0] == "text" for op in ops):
            regex_ops = split_into_ops(transcript)
            if any(op[0] != "text" for op in regex_ops):
                print(
                    f"[korder] LLM emitted malformed actions; regex caught triggers, using regex: {regex_ops!r}",
                    flush=True,
                    file=sys.stderr,
                )
                return regex_ops

        print(f"[korder] segmented ops: {ops!r}", flush=True, file=sys.stderr)
        return ops

    def _call_ollama(self, transcript: str) -> list:
        user_prompt = _build_user_prompt(
            transcript,
            self._history,
            show_triggers=self.show_triggers_in_prompt,
        )
        payload: dict = {
            "model": self.model,
            "system": _SYSTEM_PROMPT,
            "prompt": user_prompt,
            "stream": False,
            # num_predict capped at 256 — well above any legitimate
            # JSON output (the longest valid response we've seen is
            # ~120 tokens, including a multi-action segment + a Polish
            # response field) but tight enough that runaway recursion
            # ('{"actions": [{"actions": [{"actions": [...') gets
            # truncated in ~3s rather than ~10s, so the regex
            # fallback path engages quickly when the model degrades.
            "options": {"temperature": 0.0, "num_predict": 256},
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
        # Reset last_response — if the new parse omits it, we don't
        # want to surface a stale message from the previous turn.
        self.last_response = ""
        if isinstance(parsed, dict):
            response_field = parsed.get("response")
            if isinstance(response_field, str) and response_field.strip():
                self.last_response = response_field.strip()
                print(
                    f"[korder] LLM response for {transcript!r}: {self.last_response!r}",
                    flush=True, file=sys.stderr,
                )
            if isinstance(parsed.get("actions"), list):
                return parsed["actions"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "phrase" in parsed:
            return [parsed]
        raise ValueError(f"unexpected LLM output shape: {type(parsed).__name__}: {parsed!r}")


def _scrub_hallucinated_confirm(transcript: str, actions: list) -> None:
    """Clear `confirm` params whose value doesn't actually appear in the
    transcript. Safety net against LLM-side hallucinations on
    destructive actions (shutdown / reboot / sleep etc.) — the model
    sometimes invents a confirm value mid-parse even when the user
    didn't say a yes/no word, which would fire the action without
    real consent. Mutates the actions list in place."""
    lower = transcript.lower()
    for action in actions:
        if not isinstance(action, dict):
            continue
        params = action.get("params")
        if not isinstance(params, dict):
            continue
        confirm = params.get("confirm")
        if not confirm or not isinstance(confirm, str):
            continue
        if confirm.strip().lower() not in lower:
            print(
                f"[korder] cleared hallucinated confirm={confirm!r} for "
                f"action {action.get('name')!r} — not present in transcript",
                flush=True, file=sys.stderr,
            )
            params["confirm"] = ""


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
