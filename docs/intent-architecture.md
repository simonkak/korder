# Intent parser architecture

## TL;DR

Korder's voice-action parser sends each transcript to a local Gemma model via
ollama and asks it to identify any imperative commands. Two implementations
were measured head-to-head:

- **JSON output** (`/api/generate` with `format: "json"`) — currently on `main`.
  The model returns `{"actions": [{"phrase", "name", "params"}, …]}` and we
  slice the original transcript ourselves around the identified phrases to
  preserve verbatim user text.
- **Function calling** (`/api/chat` with `tools`) — on the `function-calling`
  branch. Each registered action becomes a tool schema; the model invokes
  tools by name and we translate `tool_calls` back into the same internal
  shape the slicer consumes.

**JSON output won on every dimension that matters for our use case.** The
function-calling branch is preserved as a reference; this document explains
why we picked the path we did so future-us doesn't relitigate it.

## The use case shapes the answer

Function calling shines when each call is **independent and self-contained**:
"get_weather(city='Berlin')", "send_email(to=…)". The agent layer composes
results from many such calls into a useful answer.

Korder's job is different. We're parsing a transcript that **interleaves
text and commands**:

> "remind me to call mom press enter and add bread to the list"
> → text("remind me to call mom") + key(enter) + text("add bread to the list")

We need:

1. Each command identified
2. Verbatim text segments between commands preserved (no model rewriting)
3. Multi-action ordering preserved
4. Polish diacritics intact across the whole pipeline

This is closer to **structured parsing** than agentic tool use. Both
implementations support it — but JSON output mode does so with less ceremony,
and Gemma 4 E4B happens to behave better in that mode.

## Numbers (gemma4:e4b, flash attention enabled)

Measured against the same 21-case benchmark suite + 34-case integration
test set on the same hardware (AMD 7800 XT, ROCm).

| Metric                              | JSON (main)              | function-calling          | Δ        |
|-------------------------------------|--------------------------|---------------------------|----------|
| Benchmark, thinking off — passes    | **20/21 (95%)**          | 19/21 (90%)               | −1 case  |
| Benchmark, thinking off — avg ms    | **610 ms**               | 2,497 ms                  | +1,887   |
| Benchmark, thinking off — median ms | **621 ms**               | 1,842 ms                  | +1,221   |
| Benchmark, thinking on  — passes    | **20/21 (95%)**          | 19/21 (90%)               | −1 case  |
| Benchmark, thinking on  — avg ms    | 1,845 ms                 | 2,499 ms                  | +654     |
| Integration tests — passes          | **33/34 (97%)**          | 31/34 (91%)               | −2 cases |
| Engages reasoning                   | only when think=true     | **every call** (forced)   |          |
| `_extract_json_object` needed?      | yes (thinking-mode mode) | no (tools envelope clean) |          |

## What's the same in both branches

- **Verbatim text fidelity**. Function calling preserves Polish diacritics
  via a synthetic `_phrase` argument required on every tool. The slicer
  (`segment_input_by_actions`) is shared, so multi-action utterances and
  user-typed text around commands round-trip cleanly in both.
- **Regex-supplement safety net**. Both branches still fall back to the
  regex parser when the LLM misses a registered trigger.
- **Test framework + benchmark**. Both consume the same headless
  `korder.intent_bench` runner and the same integration test set.

## Where function calling loses

### 1. Latency — 4× slower at no-thinking baseline

`format=json` lets ollama go straight to structured output. Tools mode in
ollama 0.22 appears to **always engage Gemma's reasoning loop**, even when
`think:true` isn't set. Every tools call in the benchmark produced a
`thinking` field, regardless of the request flag.

Concretely, on an "easy" case:

```
JSON, thinking off:        "press enter"   →    496 ms
function-calling:          "press enter"   →  1,200 ms (+700 ms baseline)
```

JSON output scales latency to case difficulty (easy → fast, hard → reasons
and slows). Tools mode pays a flat ~1.5 s per call regardless. For a
voice-driven UX where 70% of calls are simple keypresses, this is a real
ergonomic regression.

### 2. Conservatism — more false negatives on bare imperatives

Gemma is **more careful about tool selection** than free-form JSON
generation. With JSON output it confidently emitted `play_pause` for
"wstrzymaj" (Polish: pause/halt). With tools it reasoned through the
schema, decided "wstrzymaj means stop", and called `stop_playback`
instead — wrong for our domain.

The same pattern hit:

- `Przestań` (Polish "stop", but in our domain that's the
  exit-write-mode toggle, not media-stop). Tools mode picked
  `stop_playback`; JSON mode correctly picked `exit_write_mode`.
- `ciszej` (Polish "quieter"). Tools mode classified it as "descriptive
  speech, not imperative" and called nothing. JSON mode correctly called
  `volume_down`. (The regex supplement caught it so the user experience
  wasn't broken, but the LLM-only correctness dropped.)

### 3. Subtle behaviour shifts in `show_triggers_in_prompt`

The legacy "include trigger phrases in the prompt" toggle appends phrasings
to a tool's `description` field in tools mode. Gemma weights description
text differently than catalog text in JSON mode, so the toggle's behaviour
shifts subtly. Functional, but harder to reason about.

## Where function calling wins

### 1. Cleaner ollama integration

No `_extract_json_object` markdown-fence stripping. Tool calls arrive as
proper structured data:

```json
{
  "function": {
    "name": "spotify_search",
    "arguments": {"_phrase": "...", "query": "...", "kind": "album"}
  }
}
```

vs. the JSON path which has to defensively handle ` ```json … ``` ` fences
that Gemma sometimes emits in thinking mode (because `format=json` and
`think:true` are mutually exclusive on `/api/generate`).

### 2. Thinking + structured output composes

`/api/chat` with `tools` and `think:true` returns both `message.thinking`
and `message.tool_calls` cleanly. The JSON path forces a choice (the two
are mutually exclusive on `/api/generate`, hence the fence-handling
workaround).

### 3. Multi-action support is native

Tool calling returns an array of calls in invocation order. We don't lean
on this much in benchmarks but it's structurally cleaner than the JSON
`actions: [...]` array — the array semantics are part of the tool-calling
contract rather than something we ask Gemma to please get right.

## Why the win-conditions don't outweigh the losses

The cleanest path forward would be one where:

1. Gemma can do tool calling without forced reasoning on every call (so
   easy cases stay sub-second), and
2. The model treats tool descriptions as *guides* rather than
   *contracts* (so loose-meaning matches like "wstrzymaj → play_pause"
   still work).

Neither condition holds in ollama 0.22 + gemma4:e4b today. Maybe a future
ollama release fixes (1) — at which point the latency gap may close
materially. (2) is more likely a Gemma-itself behaviour, harder to undo.

The plan that initially deferred function calling to "a future change"
was right that the mechanism is more elegant; it was wrong that it would
be a strict improvement. **Our problem shape is structured parsing, not
agentic tool use, and JSON output mode is the right tool for it.**

## How to reproduce

```bash
# main, JSON output:
git checkout main
uv run python -m korder.intent_bench --json > main-off.json
uv run python -m korder.intent_bench --thinking --json > main-on.json
uv run pytest tests/test_intent_integration.py

# function-calling:
git checkout function-calling
uv run python -m korder.intent_bench --json > fc-off.json
uv run python -m korder.intent_bench --thinking --json > fc-on.json
uv run pytest tests/test_intent_integration.py
```

The headless `korder.intent_bench` runner emits per-case results + summary
as JSON when `--json` is passed, suitable for `jq`-based diffing across
branches or future re-runs.

## When to revisit this

Trigger conditions for re-running this comparison:

- ollama releases a version where tools mode does **not** force reasoning
  by default (check `journalctl -u ollama -g 'thinking'` after a tools
  call: if no `enabling thinking attention` line appears, tools mode is
  no longer always-on-thinking).
- Gemma releases a successor that's known to be more permissive on tool
  selection (e.g. Gemma 5 release notes mention "looser tool semantics"
  or similar).
- We start adding **agentic** flows to Korder (e.g. an action that fetches
  data from one source and feeds it to another), at which point function
  calling becomes structurally more appropriate regardless of latency.
