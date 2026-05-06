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
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from korder.actions.base import all_actions, get_action
from korder.actions.parser import split_into_ops
from korder.tools.base import all_tools, get_tool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Turn:
    """One past exchange in the current dictation session: what the
    user said, what action (if any) fired, what natural-language
    reply the LLM produced, and what the LLM identified as the
    conversation's current subject. Used as conversation context
    for follow-up resolution — e.g. 'A Polski?' after asking about
    France's capital. The `context` field is the structured
    counterpart to free-text `response`: where response is the
    LLM's reply prose, context is the topic it's reasoning about
    ('Francja', 'Linkin Park'). Subsequent prompts surface it as a
    'Current topic:' line so follow-ups have an explicit subject
    to bind to."""
    user_text: str
    action_name: str  # main action name fired, "" if none
    response: str  # last_response from that turn, "" if none
    context: str  # last_context — short topic phrase, "" if none


# Number of past turns to feed into the parser as context. 4 covers
# typical Q&A → follow-up → follow-up patterns without bloating the
# prompt enough to risk a meaningful latency or accuracy regression.
_MAX_HISTORY_TURNS = 4

# Hard cap on the LLM tool-call loop within a single parse call. The
# LLM iterates: emit tool_calls → Korder runs them → results fed back
# → repeat until LLM emits actions / response. The cap stops a runaway
# (Gemma chains tools indefinitely) at a worst-case latency of
# MAX_TOOL_ITERATIONS × per-call latency. Combined with the
# repetition guard (same tool + same args twice → break), this caps
# real-world worst-case latency around ~15 s on the local stack and
# ~5–6 s in the typical 1-tool-call case. Beyond the cap, parse falls
# back to whatever's been gathered or the regex parser.
_MAX_TOOL_ITERATIONS = 5

_OLLAMA_URL = "http://localhost:11434/api/generate"
_OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
_OLLAMA_PS_URL = "http://localhost:11434/api/ps"

_PUNCT_TO_STRIP = " \t.!?,;:\n"


def _extract_json_object(text: str):
    """Parse a JSON object from the LLM response.

    With format=<schema> (or format=json) on both /api/generate and
    /api/chat, ollama returns bare JSON. The fence-stripping branch
    that used to live here was a workaround for thinking-mode on
    /api/generate (where format=json was ignored alongside think:true,
    so Gemma sometimes wrapped output in ``` fences). Thinking now
    runs on /api/chat where format and think compose, so fenced
    output shouldn't appear — but a `{...}` slice fallback stays as
    defense in case the model emits trailing prose."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return json.loads(stripped[start : end + 1])
    raise ValueError(f"no JSON object in LLM response: {text!r}")


def _decode_partial_string(text: str, value_start: int, emit_from: int) -> tuple[str, int, bool]:
    """Decode the JSON-string body that started at value_start and yield
    any characters between emit_from and the current end of `text`,
    stopping at the unescaped closing quote (which marks `finished=True`).

    Returns (newly_decoded_chars, new_emit_cursor, finished).

    Used by the streaming response path: once the model has committed
    to writing `"response": "..."`, this peels decoded characters off
    the tail as the bytes arrive, so the OSD can render prose without
    waiting for the full JSON body.

    Conservative: if a backslash escape is split across token-emit
    boundaries, the cursor advances only to the position before the
    backslash; the next call resumes from there with more bytes."""
    n = len(text)
    out: list[str] = []
    i = max(emit_from, value_start)
    finished = False
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                # split escape — wait for more
                break
            nxt = text[i + 1]
            if nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            elif nxt == "/":
                out.append("/")
            elif nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "b":
                out.append("\b")
            elif nxt == "f":
                out.append("\f")
            elif nxt == "u":
                if i + 6 > n:
                    break
                try:
                    out.append(chr(int(text[i + 2 : i + 6], 16)))
                except ValueError:
                    out.append(text[i : i + 6])
                i += 6
                continue
            else:
                out.append(nxt)
            i += 2
            continue
        if c == '"':
            finished = True
            i += 1
            break
        out.append(c)
        i += 1
    return "".join(out), i, finished


def _object_schema_from_property_defs(defs: dict) -> dict:
    """Convert a {name: {type, enum, description}} dict (the shape used
    by both Action.parameters and Tool.args_schema) into a JSON Schema
    object with those properties.

    additionalProperties=False so the model can only emit keys that
    are declared — stray fields are rejected at sampling time. Empty
    input → empty properties → matches `{}` exactly (zero-arg case).

    The "required": True marker that some action params carry is read
    but NOT propagated to schema-level `required` here. Field log:
    propagating it to schema caused Gemma E4B to fabricate values
    (target="media") to satisfy the constraint rather than pick a
    target-less alternative action (play_pause). Description-level
    guidance turned out to be more reliable for required-ness."""
    properties: dict = {}
    for name, definition in defs.items():
        if not isinstance(definition, dict):
            continue
        prop: dict = {}
        if "type" in definition:
            prop["type"] = definition["type"]
        else:
            prop["type"] = "string"
        if "enum" in definition:
            prop["enum"] = list(definition["enum"])
        if "description" in definition:
            prop["description"] = definition["description"]
        properties[name] = prop
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def _params_schema_for_action(action) -> dict:
    """Render the params sub-schema for an Action's declared parameters."""
    return _object_schema_from_property_defs(action.parameters)


def _args_schema_for_tool(tool) -> dict:
    """Render the args sub-schema for a Tool's declared args. Used by
    the per-tool branches in tool_calls.items so parametric tools
    constrain the LLM to emit valid arg shapes."""
    return _object_schema_from_property_defs(tool.args_schema)


def _build_response_schema(question_mode: bool = False) -> dict:
    """Build a JSON Schema constraining Gemma's response to the exact
    shape Korder expects:
      - actions: array of {phrase, name, params} where `name` is one of
        the registered action names (enum) and `params` matches the
        action's declared parameter shape (additionalProperties=False).
      - response: optional natural-language reply.
      - context: optional short topic phrase.

    Replaces `format: "json"` — schema-constrained sampling is roughly
    the same speed as bare JSON mode but eliminates whole classes of
    failure: hallucinated `confirm` on non-confirmable actions, unknown
    action names, nested-actions runaway, stray top-level keys.

    `question_mode=True` swaps in a tighter schema for inputs that
    arrive with a trailing '?' — actions array is forced empty
    (maxItems: 0) and response is forced non-empty (minLength: 1).
    Schema-level enforcement is language-agnostic and works where
    prose hints failed: even when E4B's prior is to fabricate an
    action, the constraint engine rejects every token sequence that
    would put a non-empty array there."""
    if question_mode:
        return {
            "type": "object",
            "properties": {
                "actions": {
                    "const": [],
                    "description": "Empty — the input is a question.",
                },
                "response": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Conversational answer in the same language "
                        "as the input. If the question needs current "
                        "system state, call the appropriate tool "
                        "first (e.g. list_open_windows for 'which "
                        "window is active', list_active_mpris_players "
                        "for 'what's playing')."
                    ),
                },
                "context": {"type": "string"},
            },
            "required": ["actions", "response"],
            "additionalProperties": False,
        }

    action_branches = []
    for action in all_actions():
        branch = {
            "type": "object",
            "properties": {
                "phrase": {"type": "string"},
                "name": {"const": action.name},
                "params": _params_schema_for_action(action),
            },
            "required": ["phrase", "name"],
            "additionalProperties": False,
        }
        action_branches.append(branch)

    if action_branches:
        actions_items: dict = {"oneOf": action_branches}
    else:
        # Defensive: empty registry shouldn't happen at runtime, but
        # ollama rejects an empty oneOf.
        actions_items = {"type": "object"}

    # Tool-call branch: per-tool oneOf so the args schema is constrained
    # PER tool. Without this every tool would accept the same loose
    # `{}` args object — fine for zero-arg tools but unsafe once
    # parametric tools (search_spotify(query), screenshot_window(target))
    # land, where the LLM must emit specific kwargs and the runtime
    # has to validate them at sampling time.
    tool_branches = []
    for tool in all_tools():
        branch = {
            "type": "object",
            "properties": {
                "name": {"const": tool.name},
                "args": _args_schema_for_tool(tool),
            },
            "required": ["name"],
            "additionalProperties": False,
        }
        tool_branches.append(branch)
    if tool_branches:
        tool_calls_items: dict = {"oneOf": tool_branches}
    else:
        # Defensive: empty registry shouldn't happen at runtime, but
        # ollama rejects an empty oneOf.
        tool_calls_items = {"type": "object"}

    return {
        "type": "object",
        "properties": {
            "tool_calls": {
                "type": "array",
                "items": tool_calls_items,
                "description": (
                    "Calls to read-only context tools BEFORE deciding on "
                    "an action. Use when filling action params would "
                    "benefit from current system state (available audio "
                    "sinks, paired Bluetooth devices, active media "
                    "players, etc.). Their results will be appended to "
                    "the next turn's prompt; you can iterate. Leave "
                    "EMPTY when the input alone gives you everything "
                    "needed to either dispatch an action or answer "
                    "directly."
                ),
            },
            "actions": {
                "type": "array",
                "items": actions_items,
                "description": (
                    "Imperative commands the user is telling the system "
                    "to execute. EMPTY ARRAY for plain dictation, factual "
                    "questions, small-talk, and questions ABOUT desktop "
                    "state ('which window is active', 'what's playing', "
                    "'co jest otwarte') — those go in `response`, not "
                    "here. Also EMPTY when this turn is a tool-gathering "
                    "step (tool_calls is non-empty)."
                ),
            },
            "response": {"type": "string"},
            "context": {
                "type": "string",
                # Cap kept tight so the LLM can't use this field as a
                # reasoning scratchpad. Field log: 'Odtwórz Troy Sivan'
                # produced context='The user wants to play music. The
                # song/artist is "Tame Impala"...' — a full reasoning
                # trace and a hallucinated artist name. Without the
                # cap, that string becomes 'Current topic: ...' in the
                # next turn's prompt and pollutes follow-up resolution.
                # 80 chars is enough for proper nouns and short topics
                # ('Linkin Park', 'Bohemian Rhapsody', 'Budapeszt').
                "maxLength": 80,
            },
        },
        "required": ["actions"],
        "additionalProperties": False,
    }


# Static example block. Moved into the system prompt (was in the
# per-call user prompt) so the entire system message stays
# byte-identical across calls in a session — ollama's KV cache
# can then reuse the prefill for it. Variable suffix shrinks to
# the catalogue + history + transcript only.
_EXAMPLES_BLOCK = (
    "Examples:\n"
    '  "hello world" → {"actions": []}\n'
    '  "Naciśnij Enter." → {"actions": [{"phrase": "Naciśnij Enter", "name": "press_enter"}]}\n'
    "  Bare media verbs (no named player) → play_pause / stop_playback, NEVER pause_player / resume_player:\n"
    '    "Wznów" → {"actions": [{"phrase": "Wznów", "name": "play_pause"}]}\n'
    '    "wstrzymaj" → {"actions": [{"phrase": "wstrzymaj", "name": "play_pause"}]}\n'
    '    "Pauzuj" → {"actions": [{"phrase": "Pauzuj", "name": "play_pause"}]}\n'
    '    "Zatrzymaj odtwarzanie" → {"actions": [{"phrase": "Zatrzymaj odtwarzanie", "name": "stop_playback"}]}\n'
    '    "play music" → {"actions": [{"phrase": "play music", "name": "play_pause"}]}\n'
    '    "Pause Spotify" → {"actions": [{"phrase": "Pause Spotify", "name": "pause_player", "params": {"target": "Spotify"}}]}\n'
    '  "press enter and run it" → {"actions": [{"phrase": "press enter", "name": "press_enter"}]}\n'
    '  "Spotify zagraj Linkin Park" → {"actions": [{"phrase": "Spotify zagraj Linkin Park", "name": "spotify_play", "params": {"query": "Linkin Park"}}]}\n'
    '  "Odtwórz Lose Yourself w Spotify" → {"actions": [{"phrase": "Odtwórz Lose Yourself w Spotify", "name": "spotify_play", "params": {"query": "Lose Yourself"}}]}\n'
    '  "Odtwórz utwór all the things she said w Spotify" → {"actions": [{"phrase": "Odtwórz utwór all the things she said w Spotify", "name": "spotify_play", "params": {"query": "all the things she said", "kind": "track"}}]}\n'
    '  "Spotify zagraj" → {"actions": [{"phrase": "Spotify zagraj", "name": "spotify_play"}], "response": "Co chcesz odtworzyć w Spotify?"}\n'
    '  "shutdown computer" → {"actions": [{"phrase": "shutdown computer", "name": "shutdown"}], "response": "Are you sure you want to shut down? Say yes or no."}\n'
    '  "shutdown computer yes" → {"actions": [{"phrase": "shutdown computer yes", "name": "shutdown", "params": {"confirm": "yes"}}]}\n'
    '  "what is the capital of France" → {"actions": [], "response": "Paris.", "context": "France"}\n'
    "  Window / desktop questions are conversational — empty actions, answer in response.\n"
    "  Call list_open_windows first when current state matters; the example below shows\n"
    "  the final-turn shape the user sees, after tool results came back:\n"
    '    "które okno jest aktywne?" → {"actions": [], "response": "Aktualnie aktywne jest Firefox."}\n'
    '    "what windows are open?" → {"actions": [], "response": "Firefox, Konsole, and Spotify are open."}\n'
    '  "co powiesz o Budapeszcie?" → {"actions": [], "response": "Budapeszt to piękne miasto...", "context": "Budapeszt"}\n'
    '  "ile to siedem razy osiem" → {"actions": [], "response": "Pięćdziesiąt sześć."}\n'
    '  "czy lubisz kotki?" → {"actions": [], "response": "Tak, lubię kotki — są urocze."}\n'
    '  "wikipedia, Paryż" → {"actions": [{"phrase": "wikipedia, Paryż", "name": "wikipedia_search", "params": {"query": "Paryż"}}]}\n'
    '  "Zakończę." → {"actions": [{"phrase": "Zakończę.", "name": "cancel_session"}]}\n'
    "  Follow-ups (use 'Current topic:' from history block as the implicit subject;\n"
    "  the TOPIC value below is illustrative only — bind to whatever the actual prior turn established):\n"
    '    Current topic: Francja\n'
    '    now:   "A Polski?" → {"actions": [], "response": "Warszawa.", "context": "Polska"}\n'
    '    Current topic: Gdańsk\n'
    '    now:   "Pokaż stronę na Wikipedii" → {"actions": [{"phrase": "Pokaż stronę na Wikipedii", "name": "wikipedia_search", "params": {"query": "Gdańsk"}}], "context": "Gdańsk"}\n'
    '    Current topic: Bohemian Rhapsody\n'
    '    now:   "play it on Spotify" → {"actions": [{"phrase": "play it on Spotify", "name": "spotify_play", "params": {"query": "Bohemian Rhapsody"}}], "context": "Bohemian Rhapsody"}'
)


_SYSTEM_PROMPT = (
    "You are an inline action detector for dictated speech. Identify "
    "commands by INTENT, not literal string match. Match meaning across "
    "any language Whisper transcribed.\n"
    "\n"
    "Return ONE JSON object with this exact shape:\n"
    '  {"tool_calls": [{"name": "<tool_name>", "args": {...}}],\n'
    '   "actions": [{"phrase": "<verbatim substring of input>", "name": "<action_name>", "params": {...}}],\n'
    '   "response": "<optional natural-language reply, same language as input>",\n'
    '   "context": "<short topic phrase, or empty>"}\n'
    "\n"
    "Tool-call loop (when an action lists `[tools: ...]`):\n"
    "- If the action you're about to emit lists `[tools: ...]` in the "
    "catalogue below, you MUST call those tools first. Emit "
    "`tool_calls` (one per listed tool, leaving `args: {}` for zero-"
    "arg tools), keep `actions` EMPTY, and the next turn will show the "
    "results. THEN, with the canonical names visible, emit the action.\n"
    "- The user's words for sink / device / player names are NOT "
    "canonical — only tool results are. Saying \"głośnik monitora\" "
    "doesn't mean PipeWire's sink is named \"głośnik monitora\"; the "
    "real name might be \"Głośniki monitora\" or anything else the "
    "system advertises. Don't guess; let the tool tell you.\n"
    "- If you skip a required tool call, Korder will force the "
    "discovery and re-prompt you anyway, costing an extra round-trip. "
    "Emit tool_calls upfront to keep dispatch fast.\n"
    "- NEVER report tool RESULTS in `response` unless you actually "
    "called the tool this parse and saw the data. Don't say \"I found "
    "X\", \"the available outputs are Y\", \"there are 3 paired "
    "devices\" etc. without first emitting tool_calls and reading "
    "the response back. If you're unsure, leave response empty and "
    "either call the tool or dispatch the action.\n"
    "- For actions WITHOUT `[tools: ...]`, dispatch immediately. The "
    "loop is bounded — don't repeat the same tool with the same args "
    "twice.\n"
    "\n"
    "Rules:\n"
    "- Questions are NOT commands. Interrogative inputs ('what's X?', "
    "'which Y?', 'is Z on?', 'co/jak/które/jakie/czy ...?') return "
    "`{\"actions\": []}` and answer in `response`. Even questions about "
    "what the user could do or what's currently happening (windows "
    "open, music playing, time, weather) take the empty-actions path. "
    "Commands are imperatives that tell the system to do something "
    "('press X', 'open Y', 'pause Z', 'naciśnij X', 'pokaż Y').\n"
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
    "as the prior turn.\n"
    "When the user asks ABOUT windows ('which window is active', "
    "'what's open', 'co jest aktywne', 'jakie okna mam otwarte', 'które "
    "okno jest otwarte'), this is a QUESTION not a command. Call "
    "list_open_windows in tool_calls first; on the next turn answer in "
    "`response` using the literal names from the result — the entry "
    "with `active: true` is the focused window. Do NOT dispatch ANY "
    "window action (focus_window, close_window, show_desktop, "
    "show_overview, tile_window, etc.) for questions. Reserve those "
    "actions for explicit imperatives that command the system to do "
    "something — 'focus X', 'close this', 'show desktop', 'przełącz "
    "na X', 'zminimalizuj wszystko'.\n"
    "\n"
    "`context` field: populate with the primary subject of THIS turn — "
    "a short phrase (proper noun / place / topic), NOT a sentence. "
    "Examples: 'Budapeszt', 'Linkin Park', 'Bohemian Rhapsody'. When "
    "the input doesn't introduce a new subject, CARRY FORWARD the prior "
    "turn's topic (visible above as 'Current topic:'). For pure "
    "commands ('press enter', 'shutdown computer') and dictation with "
    "no clear subject, leave `context` empty. The 'Current topic:' "
    "line is your authoritative reference — when the input is a bare "
    "follow-up ('Ile ma mieszkańców?', 'A Polski?'), treat it as a "
    "question ABOUT that topic and fill `response` with the answer "
    "for that subject.\n"
    "\n"
    + _EXAMPLES_BLOCK
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
        if action.tools:
            tools_flat = ", ".join(action.tools)
            line += f' [tools: {tools_flat}]'
        if show_triggers:
            triggers_flat = ", ".join(f'"{t}"' for t in action.all_triggers())
            line += f' (example phrasings: {triggers_flat})'
        lines.append(line)
    return "\n".join(lines)


def _render_tool_catalogue() -> str:
    """Render the read-only tool catalog the LLM may call to gather
    context. Tools are advertised in the system prompt (cached prefix)
    so KV-cache reuse is preserved across turns. Empty string when no
    tools are registered.

    The format mirrors _render_action_catalogue so the LLM sees both
    surfaces in a familiar shape: name, description, args (when any).
    Each tool prints as one line."""
    tools = all_tools()
    if not tools:
        return ""
    lines = ["Available tools (read-only, optional — call before deciding actions when current system state matters):"]
    for tool in tools:
        line = f"- {tool.name}: {tool.description}"
        if tool.args_schema:
            arg_keys = ", ".join(tool.args_schema.keys())
            line += f" [args: {arg_keys}]"
        lines.append(line)
    return "\n".join(lines)


def _render_history(history: list[Turn]) -> str:
    """Format prior turns as a 'Recent conversation' block to prepend to
    the analysis section of the user prompt. Empty string when there's
    no history. Each turn is one User: + Assistant: pair, trimmed of
    empty fields so action-only turns don't print blank Assistant lines.

    Adds an explicit 'Current topic:' line (when the most recent turn
    populated `context`) AFTER the per-turn block. This is the
    structured signal that complements raw history — gives bare
    follow-ups like 'Ile ma mieszkańców?' an unambiguous subject to
    bind to, instead of relying on the LLM to extract topic from
    free-text assistant prose."""
    if not history:
        return ""
    lines = ["Recent conversation in this session (oldest first):"]
    for turn in history:
        lines.append(f"  User: {turn.user_text!r}")
        if turn.response:
            lines.append(f"  Assistant: {turn.response!r}")
        elif turn.action_name:
            lines.append(f"  (Korder fired action: {turn.action_name})")
    last_context = history[-1].context if history else ""
    if last_context:
        lines.append(f"Current topic: {last_context}")
    lines.append("")
    return "\n".join(lines)


def _looks_interrogative(transcript: str) -> bool:
    """Universal '?'-only question detection. Language-agnostic: any
    Whisper-supported language preserves '?' on rising-intonation
    speech (verified PL, EN; documented for the other languages
    Whisper transcribes). When Whisper drops '?' for short / low-
    intoned utterances, the question falls through to the normal
    path and the LLM does its best — no degradation vs not having
    this check at all.

    Earlier versions also matched language-specific question-word
    starters (PL + EN) but those silently broke for any third
    language, which is the wrong way to add coverage."""
    t = transcript.strip().rstrip(" .,!;:")
    return t.endswith("?")


def _build_user_prompt(
    transcript: str,
    history: list[Turn] | None = None,
    *,
    show_triggers: bool = False,  # accepted for backwards-compat; no longer used here
    tool_history_block: str = "",
) -> str:
    """Per-call user message — only the parts that change per call:
    history (per turn) + tool-call results (per loop iteration) +
    transcript. The action catalogue and tool catalogue moved into
    the system prompt (see _build_system_prompt) so they're a byte-
    stable prefix Gemma's KV cache can reuse across turns instead
    of being rebuilt + re-prefilled every time.

    Interrogative inputs (input ends with '?') get a short hint
    prepended that nudges the LLM toward the empty-actions / response
    path. The catalogue stays in the cached system prompt either way
    — only this small per-call hint changes — so KV-cache reuse is
    preserved.

    tool_history_block: pre-rendered "Previous tool calls and
    results" section appended on iterations 2+ of the tool-call
    loop. Empty on the first iteration (no prior calls yet).

    Window-list context that used to live here moved out — when the
    LLM needs the current windows (focus_window targeting, or to
    answer 'which is open?') it calls list_open_windows in
    tool_calls and the tool dispatcher feeds the result back via
    tool_history_block on the next iteration.

    show_triggers is accepted but unused at this layer; it controls
    catalogue rendering, which now happens in _build_system_prompt."""
    history_block = _render_history(history or [])
    hint = ""
    if _looks_interrogative(transcript):
        hint = (
            "Note: input ends with '?'. This is a QUESTION — emit "
            "`{\"actions\": []}` and answer in `response` (same "
            "language as input). If the answer needs current system "
            "state (open windows, active player, etc.), call the "
            "appropriate tool first.\n"
            "\n"
        )
    return (
        hint
        + history_block
        + tool_history_block
        + f"Now analyze this transcript and return ONLY the JSON object:\n"
        f"Input: {json.dumps(transcript, ensure_ascii=False)}\n"
        "Output:"
    )


# Cached system prompt with action catalogue baked in. Built lazily on
# first call so the registry has had time to populate (actions/__init__
# registers at import time but the order of imports during testing
# isn't always stable). Keyed on `show_triggers` because that's the
# only knob that changes catalogue rendering.
_SYSTEM_PROMPT_CACHE: dict[bool, str] = {}


def _build_system_prompt(show_triggers: bool = False) -> str:
    """Return the full system prompt: rules + examples + action
    catalogue + tool catalogue. Cached per-show_triggers so calls
    within a session return the byte-identical string Gemma's KV
    cache fingerprints.

    Used to be just _SYSTEM_PROMPT (rules + examples), with the
    catalogue rebuilt into the user prompt every call. That meant
    the LLM re-prefilled ~3k tokens of catalogue on every turn —
    same content, fresh hash. Folding the catalogue into the system
    prompt makes the entire 'tools and rules' surface a stable
    prefix the cache can reuse, leaving only history + windows +
    tool-results + transcript as the per-call suffix."""
    cached = _SYSTEM_PROMPT_CACHE.get(show_triggers)
    if cached is not None:
        return cached
    action_catalogue = _render_action_catalogue(show_triggers=show_triggers)
    tool_catalogue = _render_tool_catalogue()
    parts = [
        _SYSTEM_PROMPT,
        "",
        "Available actions:",
        action_catalogue,
    ]
    if tool_catalogue:
        parts.extend(["", tool_catalogue])
    full = "\n".join(parts)
    _SYSTEM_PROMPT_CACHE[show_triggers] = full
    return full


def _invalidate_system_prompt_cache() -> None:
    """Test hook — call after registering / unregistering actions
    so the next _build_system_prompt rebuilds with the new catalogue."""
    _SYSTEM_PROMPT_CACHE.clear()


class IntentParser:
    def __init__(
        self,
        model: str = "gemma4:e4b",
        timeout_s: float = 8.0,
        thinking_mode: bool = False,
        show_triggers_in_prompt: bool = False,
        keep_alive_s: float = 300.0,
        schema_mode: bool = True,
    ):
        self.model = model
        self.timeout_s = timeout_s
        self.thinking_mode = thinking_mode
        self.show_triggers_in_prompt = show_triggers_in_prompt
        self.keep_alive_s = keep_alive_s
        # When True, constrain Gemma's output with a per-action JSON
        # Schema (the registered action enum + per-action params shape).
        # When False, fall back to bare format=json. Schema mode is
        # the safer default — kept toggleable for one release in case
        # a specific Gemma build has issues with schema-constrained
        # sampling that bare JSON mode handles.
        self.schema_mode = schema_mode
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
        # Structured topic the LLM identified as the conversation's
        # current subject ('Francja', 'Linkin Park'). Surfaced back
        # into the next prompt as a 'Current topic:' line — gives
        # follow-ups like 'A Polski?' or 'Ilu ma członków?' an
        # explicit subject to bind to, instead of relying on the
        # LLM to extract topic from raw assistant prose.
        self.last_context: str = ""
        # Tool calls the LLM emitted on the most recent turn — list of
        # {name, args} dicts. Populated by _call_ollama from the parsed
        # JSON; consumed by _run_intent_loop to drive iterations.
        # Empty list when the LLM didn't request any tools (the common
        # case — fast-path single-call parses).
        self.last_tool_calls: list[dict] = []
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
            log.info("warm-up: %s already resident — keep_alive bumped", self.model)
        else:
            log.info("warm-up: %s not resident — kicking off background load", self.model)
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
                log.warning("warm-up of %s failed: %s", self.model, e)
                return
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            verb = "bumped" if already_loaded else "loaded"
            log.info("warm-up: %s %s in %.0f ms", self.model, verb, elapsed_ms)
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
            log.info("history cleared (%d turns)", len(self._history))
        self._history = []

    def _push_turn(
        self,
        transcript: str,
        actions: list,
        response: str,
        context: str,
    ) -> None:
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
            context=context,
        ))
        if len(self._history) > _MAX_HISTORY_TURNS:
            self._history = self._history[-_MAX_HISTORY_TURNS:]

    def parse(
        self,
        transcript: str,
        on_partial_response=None,
    ) -> list[tuple]:
        if not transcript:
            return []
        try:
            actions = self._run_intent_loop(
                transcript,
                on_partial_response=on_partial_response,
            )
        except Exception as e:
            log.warning("intent LLM failed, falling back to regex: %s", e)
            return split_into_ops(transcript)
        # Record this turn BEFORE the regex-supplement / segmentation
        # path forks — history is about what the LLM saw and produced,
        # which is the input to the next round's resolution.
        self._push_turn(transcript, actions, self.last_response, self.last_context)

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

        log.info("LLM actions for %r: %r", transcript, actions)

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
                log.info(
                    "LLM found no actions; regex caught triggers, using regex: %r",
                    regex_ops,
                )
                return regex_ops

        ops = segment_input_by_actions(transcript, actions)
        if ops is None:
            # Phrase positioning failed — Gemma sometimes emits an
            # English `phrase` ("minimize") for a Polish input
            # ("Zminimalizuj Firefox") even though name + params are
            # correct. Don't immediately go to regex (which discards
            # the LLM's params and runs the action with empty params,
            # yielding wrong behavior on parametric actions like
            # minimize_window{target} or focus_window{target}).
            # Instead try a relaxed dispatch: trust the schema-
            # constrained action names, run their op_factories with
            # the LLM's params, drop the surrounding text. The
            # transcript itself doesn't get typed, which is the
            # correct behavior for command utterances.
            relaxed_ops = _dispatch_actions_by_name(actions)
            if relaxed_ops:
                log.info(
                    "LLM phrase mismatch — dispatching %d action(s) by name+params: %r",
                    len(relaxed_ops), relaxed_ops,
                )
                return relaxed_ops
            log.info("LLM action phrase not found in input, falling back to regex")
            return split_into_ops(transcript)

        # Second-chance regex backstop. Three trigger conditions, all
        # signalling "the LLM didn't fully resolve the action and
        # regex might do better":
        #
        # 1. ALL ops are text — happens when LLM emits malformed shapes
        #    like `{name: 'actions', args: []}` or every entry lacks a
        #    `phrase`. Original case the backstop covered.
        # 2. Ops contain a `pending_action` — happens when LLM emits an
        #    action with empty params and the trailing-text-fill couldn't
        #    rescue (e.g. LLM picked the subject as the phrase, leaving
        #    trailing punctuation only). Field log: 'Spotify Play Numb'
        #    → LLM phrase='Numb' → segmenter ops = [text 'Spotify Play',
        #    pending spotify_play]. Regex matches 'spotify play' at
        #    position 0 and pulls 'Numb' from trailing text — strictly
        #    better than going pending.
        #
        # In either case, prefer regex ONLY when it produces strictly
        # more callable ops than the LLM segmenter and doesn't itself
        # add new pending actions. Conservative: doesn't swap when both
        # parsers tied, doesn't swap when regex would also be pending.
        ops_has_text_only = bool(ops) and all(op[0] == "text" for op in ops)
        ops_has_pending = any(op[0] == "pending_action" for op in (ops or []))
        if actions and (ops_has_text_only or ops_has_pending):
            regex_ops = split_into_ops(transcript)
            llm_callables = sum(
                1 for op in (ops or [])
                if op[0] not in ("text", "pending_action")
            )
            regex_callables = sum(
                1 for op in regex_ops
                if op[0] not in ("text", "pending_action")
            )
            regex_pendings = sum(
                1 for op in regex_ops if op[0] == "pending_action"
            )
            llm_pendings = sum(
                1 for op in (ops or []) if op[0] == "pending_action"
            )
            if regex_callables > llm_callables and regex_pendings <= llm_pendings:
                log.info(
                    "regex produced more callables than LLM segmenter "
                    "(%d vs %d); using regex: %r",
                    regex_callables, llm_callables, regex_ops,
                )
                return regex_ops

        log.info("segmented ops: %r", ops)
        return ops

    def _format_constraint(self, question_mode: bool = False):
        """Pick the format constraint for this call. Schema mode wins
        when enabled — bare format=json is the fallback path so we can
        toggle it off if a specific Gemma build chokes on schema-
        constrained sampling. `question_mode=True` returns the tighter
        question-only schema (empty actions, non-empty response).
        Returns the value that should go in payload["format"]."""
        if self.schema_mode:
            return _build_response_schema(question_mode=question_mode)
        return "json"

    def _call_ollama(
        self,
        transcript: str,
        *,
        on_partial_response=None,
        tool_history_block: str = "",
    ) -> list:
        """Call Gemma and return the parsed actions array.

        on_partial_response: optional callback. When provided, the call
        runs with stream=true; the callback fires with the cumulative
        `response` field text as it streams in (only after the model has
        committed to a non-empty response — i.e. after `actions` arrived
        empty, so we know this turn is conversational, not action-
        dispatch). Net perceived-latency saving on conversational
        answers is the time-to-first-token vs full-completion delta.
        For action-only turns the callback never fires.

        tool_history_block: rendered "Previous tool calls and results"
        section appended to the user prompt on iterations 2+ of the
        tool-call loop. Empty on iteration 1 (no prior calls). Driven
        by ``_run_intent_loop`` — direct callers / test mocks can
        leave this empty.

        Routing:
        - thinking_mode on  → /api/chat with messages + format:<schema>
          + think:true. Format and think compose cleanly on /api/chat
          (they're mutually exclusive on /api/generate).
        - thinking_mode off → /api/generate with format:<schema>. Same
          path the bench has used since v1; preserves the measured
          latency profile for the action-dispatch hot path."""
        user_prompt = _build_user_prompt(
            transcript,
            self._history,
            show_triggers=self.show_triggers_in_prompt,
            tool_history_block=tool_history_block,
        )
        # num_predict capped at 256 — well above any legitimate JSON
        # output (the longest valid response we've seen is ~120 tokens,
        # including a multi-action segment + a Polish response field)
        # but tight enough that runaway recursion gets truncated in ~3s
        # rather than ~10s, so the regex fallback path engages quickly
        # when the model degrades.
        options = {"temperature": 0.0, "num_predict": 256}
        # Tighten the schema when '?' marks the input as a question:
        # actions array forced empty, response forced non-empty. Schema
        # enforcement does what prose hints couldn't — even E4B's bias
        # toward action selection can't fabricate a non-empty array
        # when the constraint engine refuses every token of one.
        question_mode = _looks_interrogative(transcript)
        format_value = self._format_constraint(question_mode=question_mode)
        stream = on_partial_response is not None

        system_prompt = _build_system_prompt(self.show_triggers_in_prompt)
        if self.thinking_mode:
            # /api/chat lets format:<schema> compose with think:true.
            # On /api/generate they're mutually exclusive — the reason
            # the old path had to choose between strict JSON and
            # thinking, and why _extract_json_object had a fence-
            # stripping branch.
            url = _OLLAMA_CHAT_URL
            payload: dict = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": stream,
                "format": format_value,
                "think": True,
                "options": options,
                "keep_alive": self.keep_alive_s,
            }
        else:
            url = _OLLAMA_URL
            payload = {
                "model": self.model,
                "system": system_prompt,
                "prompt": user_prompt,
                "stream": stream,
                "format": format_value,
                "options": options,
                "keep_alive": self.keep_alive_s,
            }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        if stream:
            raw, thinking = self._consume_stream(
                req, on_partial_response=on_partial_response
            )
        else:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            thinking = (body.get("thinking") or "").strip()
            if self.thinking_mode and "message" in body:
                # /api/chat shape: {message: {role, content, thinking?}}
                msg = body.get("message") or {}
                raw = (msg.get("content") or "").strip()
                if not thinking:
                    thinking = (msg.get("thinking") or "").strip()
            else:
                raw = body.get("response", "").strip()

        self.last_thinking = thinking
        if thinking:
            log.info("gemma thinking for %r:\n  %s", transcript, thinking)
        parsed = _extract_json_object(raw)
        # Reset last_response — if the new parse omits it, we don't
        # want to surface a stale message from the previous turn.
        self.last_response = ""
        # Reset last_context too. New parses with no context field
        # default to empty; only an explicit non-empty value carries
        # forward (via _push_turn → next render's 'Current topic:').
        self.last_context = ""
        # Reset last_tool_calls — the loop driver re-checks this after
        # every _call_ollama, so stale calls from prior turns must not
        # leak in.
        self.last_tool_calls = []
        if isinstance(parsed, dict):
            response_field = parsed.get("response")
            if isinstance(response_field, str) and response_field.strip():
                self.last_response = response_field.strip()
                log.info("LLM response for %r: %r", transcript, self.last_response)
            context_field = parsed.get("context")
            if isinstance(context_field, str) and context_field.strip():
                # Defensive trim: schema caps at 80 chars but a future
                # ollama / model that ignores maxLength would otherwise
                # let a reasoning-trace context propagate as 'Current
                # topic: ...' into the next prompt's history block,
                # poisoning follow-up resolution. Drop sentences (any
                # internal '.') and clamp length.
                ctx = context_field.strip().split(".")[0].strip()
                if len(ctx) > 80:
                    ctx = ctx[:80].rstrip() + "…"
                self.last_context = ctx
                log.info("LLM context for %r: %r", transcript, self.last_context)
            tool_calls_field = parsed.get("tool_calls")
            if isinstance(tool_calls_field, list):
                # Filter to well-formed entries — name must be a string,
                # args must be a dict (default to empty when omitted).
                clean: list[dict] = []
                for call in tool_calls_field:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    args = call.get("args")
                    if not isinstance(args, dict):
                        args = {}
                    clean.append({"name": name, "args": args})
                self.last_tool_calls = clean
                if clean:
                    log.info(
                        "LLM tool_calls for %r: %r",
                        transcript,
                        [(c["name"], c["args"]) for c in clean],
                    )
            if isinstance(parsed.get("actions"), list):
                return parsed["actions"]
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "phrase" in parsed:
            return [parsed]
        raise ValueError(f"unexpected LLM output shape: {type(parsed).__name__}: {parsed!r}")

    def _run_intent_loop(
        self,
        transcript: str,
        on_partial_response=None,
    ) -> list:
        """Iterative LLM call: emit tool_calls → run them → feed results
        back → repeat until terminal (LLM emits actions / response /
        nothing).

        Iteration 1 uses ``_call_ollama`` directly so existing test
        mocks continue to work. Iterations 2+ also go through
        ``_call_ollama`` but with a populated ``tool_history_block``
        so the LLM sees prior calls + their results.

        Termination conditions (any one ends the loop):
          1. LLM emits ``actions`` non-empty → dispatch them.
          2. LLM emits ``response`` non-empty → conversational answer.
          3. LLM emits no further tool_calls → all-empty response,
             let parse() fall through to its existing fallbacks.
          4. Repetition guard fires (same tool name + same args asked
             twice in one parse) — break with whatever we have.
          5. ``_MAX_TOOL_ITERATIONS`` reached — break.

        Tool failures (unknown name, executor raises) don't abort the
        loop. Their result is recorded as ``{"error": "..."}`` so the
        LLM sees the failure and can choose differently next iteration.
        """
        seen: set[tuple] = set()
        tool_history: list[tuple[str, dict, object]] = []
        actions: list = []
        for iteration in range(_MAX_TOOL_ITERATIONS):
            history_block = (
                self._render_tool_history(tool_history) if tool_history else ""
            )
            # Streaming partial-response is only useful on the FINAL
            # iteration (when the LLM commits to a conversational
            # answer). On intermediate iterations the LLM is still in
            # tool-gathering mode; streaming bytes there would just be
            # JSON we'd discard. Wire the callback only on iteration 1
            # to preserve the existing time-to-first-token behavior;
            # if the loop iterates, intermediate iterations don't
            # stream but the final iteration's response is short
            # enough that it doesn't matter much.
            cb = on_partial_response if iteration == 0 else None
            actions = self._call_ollama(
                transcript,
                on_partial_response=cb,
                tool_history_block=history_block,
            )
            tool_calls = list(self.last_tool_calls)
            # Force-discovery on skip: if the LLM emitted actions that
            # declare tools but didn't call those tools, synthesize
            # the missing tool_calls and discard the LLM's first-pass
            # action params. The next iteration will see the canonical
            # data and re-emit. This is the safety net for Gemma E4B's
            # tendency to commit to a guess even when an enumeration
            # would be definitive — Field log: "głośnik monitora" went
            # to sink_name="monitor speaker" without consulting
            # list_audio_sinks. The substring resolver rescued that
            # particular case but the design relies on the LLM seeing
            # real names, not a fuzzy match downstream.
            if actions and not tool_calls:
                forced = self._compute_forced_tool_calls(actions, seen)
                if forced:
                    log.info(
                        "intent loop: LLM emitted action(s) without consulting "
                        "tools — forcing %d discovery call(s)",
                        len(forced),
                    )
                    tool_calls = forced
                    actions = []
            if actions or self.last_response or not tool_calls:
                # Terminal state — nothing more to gather.
                if iteration > 0:
                    log.info(
                        "intent loop: terminated after %d tool iteration(s)",
                        iteration,
                    )
                return actions
            # Repetition guard — protects against Gemma re-asking the
            # same question in a stuck loop.
            for call in tool_calls:
                sig = (call["name"], tuple(sorted(call["args"].items())))
                if sig in seen:
                    log.warning(
                        "intent loop: repeated tool call %s — breaking", sig,
                    )
                    return actions
                seen.add(sig)
            # Run each tool, append to history
            for call in tool_calls:
                name = call["name"]
                args = call["args"]
                tool = get_tool(name)
                if tool is None:
                    log.warning("intent loop: unknown tool %r", name)
                    tool_history.append((name, args, {"error": f"unknown tool {name!r}"}))
                    continue
                try:
                    result = tool.executor(**args)
                except Exception as e:
                    log.warning("intent loop: tool %s raised: %s", name, e)
                    result = {"error": str(e)}
                tool_history.append((name, args, result))
            log.info(
                "intent loop: iteration %d ran %d tool(s); continuing",
                iteration + 1, len(tool_calls),
            )
        log.warning(
            "intent loop: hit MAX_TOOL_ITERATIONS=%d, breaking with current actions",
            _MAX_TOOL_ITERATIONS,
        )
        return actions

    def _render_tool_history(
        self,
        history: list[tuple[str, dict, object]],
    ) -> str:
        """Format prior tool calls + results as a per-iteration suffix
        in the user prompt. Bounded length per result so a tool that
        returns lots of data (e.g. a future screenshot describer)
        doesn't blow the LLM's context budget.

        Format mirrors the casual call → result shape — the LLM has
        already seen tool descriptions in the system prompt, so this
        block just shows what was tried and what came back."""
        if not history:
            return ""
        lines = ["Previous tool calls and results in this parse:"]
        for name, args, result in history:
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = str(args)
            try:
                result_str = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                result_str = str(result)
            # Truncate long results to keep the prompt bounded —
            # 2000 chars is enough for a typical sink / device list,
            # which is at most a few dozen entries.
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "…"
            lines.append(f"  Tool: {name}({args_str})")
            lines.append(f"  Result: {result_str}")
        lines.extend([
            "",
            "Now decide based on the results above:",
            "- emit `actions` to dispatch (use literal names from the results)",
            "- emit `tool_calls` for additional info you still need",
            "- emit `response` to answer the user conversationally",
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _compute_forced_tool_calls(
        actions: list,
        already_seen: set[tuple],
    ) -> list[dict]:
        """For each action in ``actions`` that declares ``tools`` but
        whose tools haven't been called yet this parse, build a
        synthetic ``tool_calls`` list that runs each missing tool.
        ``already_seen`` is the loop's repetition-guard set; tools
        already in it are skipped (they've run in a prior iteration).

        Two synthesis strategies depending on the tool:

        1. **Zero-arg tools** (empty args_schema) are forced with
           ``args: {}``. Used by list_audio_sinks, list_open_windows,
           etc. — pure state enumerators with no input.

        2. **Parametric tools** synthesize args by name-matching
           against the action's params. If the action emitted
           ``params.query`` and the tool declares an ``query`` arg,
           the value is forwarded. Mirrors the common pattern where
           an action's discriminator (query, target) is also the
           tool's primary arg. When no matching keys carry usable
           values, the parametric tool is skipped — the LLM has to
           call deliberately or the action's own fallback handles it.

        DOES NOT mutate ``already_seen`` — the loop's main repetition
        guard adds sigs as it actually executes them. Pre-adding here
        would cause that guard to immediately reject every forced
        tool.

        Returns an empty list when every action either has no tools
        or has already had its tools called."""
        forced: list[dict] = []
        for entry in actions:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            action = get_action(name)
            if action is None or not action.tools:
                continue
            action_params = (
                entry.get("params") if isinstance(entry.get("params"), dict) else {}
            )
            for tool_name in action.tools:
                tool = get_tool(tool_name)
                if tool is None:
                    continue
                if tool.args_schema:
                    # Parametric — synthesize args from action params
                    # via name matching.
                    synthesized: dict = {}
                    for arg_name in tool.args_schema:
                        if arg_name in action_params:
                            value = action_params[arg_name]
                            if isinstance(value, str) and value.strip():
                                synthesized[arg_name] = value.strip()
                            elif isinstance(value, (int, float, bool)):
                                synthesized[arg_name] = value
                    if not synthesized:
                        # No mappable args — runtime can't help here.
                        continue
                    sig = (
                        tool_name,
                        tuple(sorted(synthesized.items())),
                    )
                    if sig in already_seen:
                        continue
                    if any(
                        c["name"] == tool_name and c["args"] == synthesized
                        for c in forced
                    ):
                        continue
                    forced.append({"name": tool_name, "args": synthesized})
                    continue
                # Zero-arg path
                sig = (tool_name, ())
                if sig in already_seen:
                    continue
                if any(c["name"] == tool_name for c in forced):
                    continue
                forced.append({"name": tool_name, "args": {}})
        return forced

    def _consume_stream(self, req, *, on_partial_response) -> tuple[str, str]:
        """Read a streaming ollama response line by line (NDJSON).
        Calls on_partial_response with new chunks of the JSON `response`
        field once it's clear the model is in conversational mode
        (i.e. `actions` arrived as `[]`). Returns (full_raw_output,
        thinking_text).

        Streaming JSON parsing is intentionally light: we scan the
        accumulating output for `"actions"\\s*:\\s*\\[\\s*\\]` to detect
        the empty-actions branch; once that fires AND the
        `"response": "..."` string opens, we yield the decoded characters
        of that string as they arrive (stopping at the unescaped close
        quote). For action-only turns the callback never fires —
        action dispatch latency is dominated by execution, not the JSON
        tail, so streaming would be wasted work."""
        accumulated = ""
        thinking_parts: list[str] = []
        actions_decided: bool | None = None  # None unknown, False empty, True non-empty
        response_state = "before"  # before / streaming / done
        response_value_start = -1
        response_emit_idx = 0

        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            for raw_line in resp:
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if chunk.get("done"):
                    break
                if self.thinking_mode and "message" in chunk:
                    msg = chunk.get("message") or {}
                    delta = msg.get("content", "") or ""
                    th = msg.get("thinking", "") or ""
                    if th:
                        thinking_parts.append(th)
                else:
                    delta = chunk.get("response", "") or ""
                    th = chunk.get("thinking", "") or ""
                    if th:
                        thinking_parts.append(th)
                if not delta:
                    continue
                accumulated += delta

                if actions_decided is None:
                    actions_decided = self._scan_actions_decision(accumulated)

                if (
                    actions_decided is False
                    and response_state == "before"
                ):
                    idx = self._scan_response_open(accumulated)
                    if idx >= 0:
                        response_value_start = idx
                        response_emit_idx = idx
                        response_state = "streaming"

                if response_state == "streaming":
                    decoded, consumed_to, finished = _decode_partial_string(
                        accumulated, response_value_start, response_emit_idx,
                    )
                    if decoded:
                        try:
                            on_partial_response(decoded)
                        except Exception as e:
                            log.warning("partial-response callback failed: %s", e)
                    response_emit_idx = consumed_to
                    if finished:
                        response_state = "done"

        return accumulated, "".join(thinking_parts).strip()

    @staticmethod
    def _scan_actions_decision(text: str) -> bool | None:
        """Return False if `"actions": [ ... ]` is closed empty so far,
        True if it's closed non-empty, None if the array hasn't closed
        yet. Conservative: only commits when the bracket structure is
        unambiguous."""
        # Find `"actions"` key
        key_idx = text.find('"actions"')
        if key_idx < 0:
            return None
        # Find the opening bracket after the colon
        rest = text[key_idx:]
        bracket_open = rest.find("[")
        if bracket_open < 0:
            return None
        # Walk forward tracking [] depth and string-state until depth back to 0
        depth = 0
        in_string = False
        escape = False
        had_content = False
        i = key_idx + bracket_open
        while i < len(text):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                i += 1
                continue
            if c == '"':
                in_string = True
                if depth >= 1:
                    had_content = True
                i += 1
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return had_content
            elif depth >= 1 and c not in " \t\r\n":
                had_content = True
            i += 1
        return None

    @staticmethod
    def _scan_response_open(text: str) -> int:
        """Return the index right after the opening quote of
        `"response": "..."` once that opening sequence has fully
        arrived; -1 otherwise."""
        key_idx = text.find('"response"')
        if key_idx < 0:
            return -1
        # Walk past colon + whitespace to find the opening quote.
        i = key_idx + len('"response"')
        n = len(text)
        # skip whitespace
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != ":":
            return -1
        i += 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != '"':
            return -1
        return i + 1


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
            log.warning(
                "cleared hallucinated confirm=%r for action %r — not present in transcript",
                confirm, action.get("name"),
            )
            params["confirm"] = ""


def _dispatch_actions_by_name(actions: list) -> list[tuple]:
    """Build an ops list directly from the LLM's actions, ignoring the
    `phrase` field entirely. Used as a relaxed fallback when phrase
    positioning failed (e.g. Gemma emitted an English phrase for a
    Polish input) but the action name + params are still trustworthy.

    The schema constrains `name` to a const enum from the registry,
    so an unknown name shouldn't appear; we still defend against it
    to handle the legacy `{type, value}` shape and any future schema
    drift. Skips entries with no resolvable action.

    Returns [] when no entry produced a usable op — caller falls
    further down to the regex parser."""
    if not isinstance(actions, list):
        return []
    out: list[tuple] = []
    for entry in actions:
        if not isinstance(entry, dict):
            continue
        action_name = entry.get("name")
        # Backward compat with old {type, value} shape.
        if action_name is None and "type" in entry and "value" in entry:
            action_name = _legacy_type_value_to_name(entry["type"], entry["value"])
        if not isinstance(action_name, str) or not action_name:
            continue
        action = get_action(action_name)
        if action is None:
            # Try the trigger-phrase remap that segment_input_by_actions
            # uses, in case Gemma emitted a trigger as the name.
            from korder.actions.parser import _compile_trigger_regex
            _, phrase_map = _compile_trigger_regex()
            mapped = phrase_map.get(action_name.lower())
            if mapped:
                action = get_action(mapped)
                action_name = mapped
            if action is None:
                continue
        params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
        op = action.op_factory(params)
        if op is None and action.parameters:
            out.append(("pending_action", action_name))
        elif op is not None:
            out.append(op)
    return out


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
        # Reject suspiciously short phrases. Field log: LLM emitted
        # `pause_player` with phrase='ok' against input 'które okno
        # jest' — 'ok' is a 2-char substring of 'okno' but isn't
        # meaningfully a trigger for anything. Such phrases have a
        # high false-match rate against arbitrary words in the input.
        # 3-char minimum lets through real short triggers ('cofnij',
        # 'tak', 'nie', 'yes') while killing the substring noise.
        if len(phrase.strip()) < 3:
            log.warning(
                "rejecting LLM action with too-short phrase %r (name=%r)",
                phrase, action_name,
            )
            return None
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
    for i, (start, end, action_name, params) in enumerate(found):
        if start > pos:
            seg = transcript[pos:start]
            if pos > 0:
                seg = seg.lstrip(_PUNCT_TO_STRIP)
            seg = seg.rstrip(_PUNCT_TO_STRIP)
            if seg:
                ops.append(("text", seg))
        action = get_action(action_name)
        next_start = found[i + 1][0] if i + 1 < len(found) else len(transcript)
        if action is not None:
            op = action.op_factory(params)
            if op is None and action.parameters and not params:
                # LLM emitted an action without params but parameters
                # are declared. Same trick the regex parser does
                # (actions/parser.py): take the trailing text between
                # this phrase and the next action (or end of input)
                # and use it to fill the action's first declared
                # parameter. Catches the "Spotify, Play, Numb" case
                # where Gemma emitted phrase='Spotify' with empty
                # params; trailing 'Play, Numb.' becomes query='Numb'
                # after stripping the verb. Skips when params is
                # non-empty — respects the LLM's deliberate choice.
                trailing = (
                    transcript[end:next_start]
                    .lstrip(_PUNCT_TO_STRIP)
                    .rstrip(_PUNCT_TO_STRIP)
                    .strip()
                )
                if trailing:
                    first_param = next(iter(action.parameters.keys()))
                    trailing_op = action.op_factory({first_param: trailing})
                    if trailing_op is not None:
                        ops.append(trailing_op)
                        pos = next_start
                        continue
                # Nothing usable in trailing text — go pending.
                ops.append(("pending_action", action_name))
            elif op is None and action.parameters:
                # Params were given but rejected by op_factory (e.g.
                # confirm value not yes/no). Original behavior:
                # pending_action so MainWindow grabs the next commit.
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
