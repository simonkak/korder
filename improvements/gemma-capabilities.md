# Gemma Capabilities — Improvement Opportunities

> **Status:** all five "ship soon" items landed on `feat/gemma-capabilities` (commit `292b3ed`). Bench delta on the 21-case suite: 16/21 → 17/21 (76% → 81%); median latency 802 → 808 ms; schema mode adds 0 ms vs bare json. Streaming saves ~140 ms TTFT on conversational answers. KV-cache reuse on `/api/generate` did not show a measurable warm-call win (see annotated item below). The "evaluate with measurements" and "reject" buckets are unchanged.

## What Korder uses today

Korder runs a single non-streaming `/api/generate` call per transcript at
`http://localhost:11434` (`src/korder/intent.py:55`, `_call_ollama` at
`intent.py:457`). The default model is `gemma4:e4b` (the README's `[inject]
llm_model` default; the `IntentParser` constructor at `intent.py:256`
incorrectly defaults to `gemma4:e2b` — likely an oversight). The payload
sets `format: "json"`, `temperature: 0.0`, `num_predict: 256`, and
`keep_alive` (default 300 s) so the model stays resident across calls. A
fixed system prompt (`_SYSTEM_PROMPT`, `intent.py:97`) and a per-call user
prompt (`_build_user_prompt`, `intent.py:207`) carry the action catalogue
plus the four-turn rolling history (`_MAX_HISTORY_TURNS = 4`, `intent.py:53`)
into the model. The model returns a JSON object with `actions`, `response`,
and `context` fields; `_extract_json_object` strips ``` fences when
thinking mode is on (which is mutually exclusive with `format=json` on
`/api/generate`). A `_scrub_hallucinated_confirm` band-aid
(`intent.py:529`) post-validates that LLM-injected `confirm` values
actually appear in the transcript, because E2B sometimes invents
confirmations on destructive actions. Warm-up (`intent.py:291`) fires a
zero-token `/api/generate` on the hotkey-press path so the model is
resident before the transcript arrives. Function calling (`/api/chat` with
`tools`) was implemented and benchmarked head-to-head; it lost on every
dimension that matters and is now archived on the `function-calling`
branch (`docs/intent-architecture.md`). Vision input via `box_2d` already
exists on the `feat/at-spi-and-vision-actions` branch (`_vision_click.py`)
as the fallback path for `click_by_label` when AT-SPI returns no match.

## Gemma 4 capabilities not yet leveraged

- **Streaming** — `stream: true` returns partial tokens; Korder waits for the full body.
- **JSON Schema** — `format` accepts a JSON Schema, not just `"json"`; structure could be enforced at the model level.
- **Audio input** — Gemma 4 ingests audio directly; the Whisper → Gemma pipeline could collapse to one call.
- **Multi-image input** — vision branch sends one screenshot; pre/post screenshots or active-window crop + full screen could combine.
- **Vision (already on a branch)** — `feat/at-spi-and-vision-actions` uses it for `click_by_label`; not yet on `main`.
- **128k context window** — Korder uses 4 turns of history.
- **Per-request thinking with structured output** via `/api/chat` (`/api/generate` makes them mutually exclusive — the reason `_extract_json_object` exists).
- **KV-cache prefix reuse** — ollama's KV cache persists across calls when the prompt prefix matches; Korder doesn't structure its prompt to maximize this.
- **Per-call temperature** — Korder sends 0.0 for everything; conversational answers might benefit from non-zero.

## Recommendations

### High value, ship soon

- ✅ **~~JSON Schema instead of `format: "json"`~~** *Done in `292b3ed`.*
  *Where:* `intent.py:487` — replace `payload["format"] = "json"` with a
  full JSON Schema describing the response shape:
  `{"type": "object", "properties": {"actions": {...}, "response": {"type": "string"}, "context": {"type": "string"}}, "required": ["actions"]}`,
  with `actions` items having `phrase` (string), `name` (enum of registered
  action names from `all_actions()`), and `params` (object with per-action
  shapes — at least for the destructive actions, `confirm` can be omitted
  entirely from the schema for non-confirmable actions so the model can't
  attach it).
  *User gain:* `_scrub_hallucinated_confirm` becomes unnecessary —
  hallucinated `confirm` values can't appear because the schema doesn't
  declare them on actions where they don't belong; `name` enum constrains
  the action to the registered set so the legacy-shape recovery in
  `segment_input_by_actions` (`intent.py:580`) sheds another failure mode;
  malformed nested-actions runaway (`{"actions": [{"actions": [...]`) can't
  occur. The `legacy_type_value_to_name` shim could be deleted entirely
  once we're past one release cycle on schema mode.
  *Latency:* schema-constrained sampling is roughly the same speed as
  `format=json` on ollama (the constraint engine runs at token-emit time
  either way); expect ~0 ms delta.
  *Trade-off vs current:* per-action params schemas have to be derived
  from the `Action.parameters` dict (which already has `type`, `enum`,
  `description`). This is mechanical generation in `_build_action_schema()`
  — one new helper of ~30 lines. Risk: if Gemma's schema-mode is buggier
  than its bare-JSON mode (it's been measured stable on Llama; less
  measured on Gemma 4 specifically), keep the old code path behind a
  config flag for one release.

- ✅ **~~Stream the conversational `response` to the OSD~~** *Done in `292b3ed`.*
  *Where:* `_call_ollama` (`intent.py:457`) becomes a generator over
  `stream: true` chunks; `MainWindow` (`ui/main_window.py:115`) connects to
  a new `parse_response_partial(text)` signal alongside the existing
  `parse_response`.
  *User gain:* For factual questions and small-talk where `response` is
  the user-facing payload, first token in ~150–250 ms (typical cold for
  Gemma E4B on a 7800 XT) instead of full-completion ~600–1,800 ms — the
  OSD is reading aloud / showing prose much sooner. Hard to overstate
  for the conversational mode where the user's eye is on the OSD waiting.
  *Latency impact:* time-to-first-token ≈ 200 ms for E4B (vs ~620 ms
  median to full-completion today). Net saving on perceived latency for
  conversational answers: ~400 ms.
  *Trade-off:* streaming JSON is parseable only after `}` arrives, so we
  can't stream the structured `actions` field — but we can detect early
  (after the first ~30 tokens) whether `"response": "` has appeared
  before any non-empty `actions` array, and start streaming that string
  to the OSD. If `actions` arrives non-empty, abort the streamed-prose
  display. Implementation is fiddlier than schema mode but well-bounded.
  Action-only utterances (the majority) see no change. Pre-empt: don't
  stream the `actions` parse — wait for full body and parse normally,
  because action latency is dominated by execution (xdg-open, D-Bus) not
  by waiting another 300 ms for the JSON tail.

- ✅ **~~Move thinking to `/api/chat` and stop manually stripping fences~~** *Done in `292b3ed`.*
  *Where:* `intent.py:478` — when `thinking_mode` is on, switch to
  `/api/chat` with `messages: [{role: system, content: …}, {role: user, content: …}]`
  and `format: <schema>`, `think: true`. Drop the entire markdown-fence
  branch in `_extract_json_object` (`intent.py:76-88`).
  *User gain:* removes a hand-rolled parser and a known reliability hazard.
  Today the thinking-mode path can't enforce JSON because format=json is
  ignored alongside `think:true` on `/api/generate`; on `/api/chat`,
  `format: <schema>` and `think: true` compose cleanly (verified in
  `docs/intent-architecture.md` for the function-calling branch).
  *Latency:* nil. Same model call, different endpoint.
  *Trade-off:* requires reshaping the payload from `prompt`+`system` to
  `messages: [...]` only on the thinking-mode code path. Non-thinking
  (`format: "json"` on `/api/generate`) stays as is so the measured 95%
  pass-rate / 610 ms median doesn't move.

- ✅ **~~Maximize KV-cache prefix reuse by re-ordering the prompt~~** *Prompt re-ordered in `292b3ed`. The example block now lives in the system prompt and the per-call user message is catalogue + history + transcript only. The expected second-call latency win on `/api/generate` did not materialize in measurements (within noise across repeated calls), consistent with the doc's own open question on whether ollama prefix-matches generate-mode. The structural change still has value when the thinking-mode `/api/chat` path is used (per-message hashing) and as a foundation for future audio-input experiments.*
  *Where:* `_build_user_prompt` (`intent.py:207`) builds catalogue +
  history + examples + transcript. The catalogue and examples are fixed
  per-startup; history changes per turn; transcript is per-call. Today
  the catalogue is rendered first in the user message — good — but
  history is interpolated between catalogue and examples (`intent.py:221`),
  meaning the example block's KV-cache prefix is invalidated on every
  turn that adds history.
  *User gain:* with the example block's KV-cached, the variable suffix
  shrinks from ~2,000 tokens (catalogue + history + examples) to
  ~200 tokens (history + transcript only). Per-call prompt processing on
  E4B at 7800 XT runs ~400 tok/s prefill, so an extra 1,800 cached
  tokens means ~+450 ms avoided on each subsequent call within a
  session.
  *Latency:* second-call-onward win of ~300–500 ms on warm sessions.
  Cold-start unchanged.
  *Trade-off:* the example block becomes a part of the system prompt
  instead of the user prompt; semantically equivalent. Need to verify
  ollama actually does prefix-match KV-cache for `/api/generate` (it
  does for `/api/chat` via the messages array; for `/api/generate` the
  full prompt string is hashed so reordering helps only if everything
  before the variable suffix is byte-identical). Add a benchmark hook
  (`korder.intent_bench --warm`) that runs each case twice and reports
  the second-call latency so the gain is measured before shipping.

- ✅ **~~Drop `IntentParser` constructor default from `gemma4:e2b` to `gemma4:e4b`~~** *Done in `292b3ed`.*
  *Where:* `intent.py:256` — the constructor default disagrees with the
  README's `[inject] llm_model = gemma4:e4b` and the documented
  benchmarking. Anyone instantiating `IntentParser()` directly (notably
  `tests/`) gets the worse model.
  *User gain:* aligns code default with documented recommendation. Tests
  exercising real ollama hit the model the README tells users to use.
  *Latency:* ~100 ms slower median (E4B vs E2B), gains 4 cases on the
  21-case suite. Worth it.
  *Trade-off:* breaks any caller that depends on the smaller default
  without naming it explicitly. Grep the codebase for direct
  `IntentParser(` constructions; pass `model=` explicitly where E2B is
  intentional.

### Worth evaluating with measurements

- **Bring the `feat/at-spi-and-vision-actions` branch onto `main`**
  Already implements Gemma vision via `box_2d`; the architectural
  question is whether vision should be *only* an AT-SPI fallback (current
  branch design) or a peer path the user can opt into directly. Gemma's
  vision latency on the 7800 XT in the branch's measurements is 1.5–4 s,
  which is high enough that it's right to keep as a fallback. Trigger the
  merge by closing issues #12 and #13.

- **Audio input as a Whisper-replacement experiment for short commands**
  Gemma 4 ingests audio. For 1-second commands like "press enter",
  the Whisper → Gemma pipeline pays Whisper's load + decode cost.
  Could a Gemma-only path skip Whisper for explicit commands? Concrete
  experiment: add `--audio` to `intent_bench`, feed each test case's
  audio through `format: <schema>` with the audio attached, measure
  end-to-end latency vs the current Whisper → Gemma. Hypothesis: Gemma's
  audio input is multilingual but worse than tuned Whisper on Polish
  diacritics, especially in noisy conditions; Whisper's medium model
  has a very small latency advantage. Risk: bench shows the gap is
  large, we lose the comparison cheaply. If gap is small, the path
  forward is "Whisper for dictation, Gemma-audio for commands" but only
  if a low-cost classifier can route audio between the two paths
  pre-transcription, which is itself a research project. Recommend: time-box
  to a 1-day experiment, decide based on numbers.

- **Use 128k context for full-session conversation memory**
  Today `_MAX_HISTORY_TURNS = 4` (`intent.py:53`). On a typical session
  with mixed dictation + commands, useful conversation context (where
  follow-ups bind) is the recent 4–6 turns; 100+ turns is excessive
  and costs prompt-prefill latency proportionally (see KV-cache item).
  Concrete experiment: run an extended session (recorded transcripts
  for the integration test set chained together, ~20 turns) with
  `_MAX_HISTORY_TURNS` at 4, 8, 16, and report whether follow-up
  resolution improves or just gets slower. Hypothesis: 8 turns is the
  sweet spot for typical dictation flows where the user dictates a
  paragraph, asks a question, dictates more — keeping 4 means the
  question is dropped after a paragraph, but going above 8 just costs
  latency without measurable resolution improvement.

- **Per-call temperature variation**
  Today `temperature: 0.0` for all calls (`intent.py:475`). Action
  classification needs determinism; conversational `response` does not.
  Concrete experiment: when the prior turn's `actions` was empty (so
  this turn is plausibly conversational continuation) or the parser
  identifies a high-confidence factual question, send `temperature: 0.3`
  alongside a request for slight variation in phrasing. Measure whether
  `intent_bench` regresses on accuracy (it shouldn't — bench cases test
  action dispatch, not response prose). User-facing benefit is
  qualitative: less robotic small-talk. The measured win is whether
  users prefer the varied prose; that's a self-report study not a
  benchmark. Risk: if `actions` ever fires under non-zero temperature,
  determinism in tests collapses; mitigation is to use a *function* of
  the prompt to decide temperature rather than a stateful one (e.g.
  set 0.3 only when the previous turn's `last_response` was non-empty
  AND `last_context` was set, signalling we're in a conversational flow).

- **Embedding-based first-pass for explicit triggers**
  Korder already has a regex parser for explicit triggers and uses it as
  a supplement when the LLM misses obvious matches. An embedding-based
  parser (`nomic-embed-text` or similar via ollama, ~50 ms per call)
  could route commands like "press enter" / "naciśnij enter" against a
  pre-computed catalogue of trigger phrasings without an LLM call at all.
  Experiment: add an `EmbeddingParser` mode behind `[inject]
  action_parser = embed`, benchmark vs LLM mode. Hypothesis: on
  `intent_bench`'s 21 cases, embeddings would catch ~14 (the explicit
  triggers) at ~80 ms each, falling back to Gemma for the 7 ambiguous
  ones. Net: 14 × (80 - 620) ms = -7,500 ms saved on the bench, with
  Gemma still active for the hard cases. The cost is one more model to
  manage and another optional dep group. Don't ship without
  measurements; the payoff hinges on whether explicit triggers are
  actually the latency-critical workload or whether the conversational
  case (already 600 ms+) dominates user perception.

### Reject (with reasoning)

- **Function calling / tools** — already evaluated head-to-head and
  rejected. Cited measurements from `docs/intent-architecture.md`:
  function calling forces Gemma's reasoning loop on every call (no way
  to disable in ollama 0.22), regressing latency from 610 ms median to
  1,842 ms median, and pass rate from 95% to 90% (drops "wstrzymaj" /
  "Przestań" / "ciszej" — Polish loose-meaning matches that JSON mode
  handles correctly). The doc lists trigger conditions for revisiting
  (ollama exposes a tools-without-thinking flag, Gemma 5 ships with
  looser tool semantics, or Korder grows agentic flows). None apply
  today. Keep the branch as a reference; don't propose this path.

- **Per-action-context system prompts (prompt routing)**
  Idea: detect "Spotify-flavored" transcripts up-front and use a smaller
  Spotify-specific system prompt. Pre-empt: doesn't pay off. Routing
  needs its own classifier (LLM or embedding), which adds 50–200 ms
  upfront. The full prompt today is ~1,500 tokens and prefills in ~400 ms
  on E4B; a Spotify-only prompt might be ~400 tokens, saving ~300 ms.
  Net: zero or worse, and you've added a new failure mode (the router
  picks the wrong specialized prompt and Gemma can't see the right
  action). The KV-cache reuse recommendation above achieves the same
  prefill saving without a router. Skip.

- **Chain-of-thought toggle for hard conversational answers**
  Korder already exposes `[intent] thinking_mode` as a config flag.
  Auto-detecting "is this a hard question" so we toggle thinking on for
  hard ones and off for easy ones requires a pre-classifier — see the
  prompt-routing rejection. Easier to ship the thinking-mode-on-`/api/chat`
  cleanup (high-value list) and let users flip the existing flag when
  they want it. Adding auto-detection is meta-reasoning Gemma already
  does internally when `think: true` is passed; trying to second-guess
  it adds latency for marginal gain.

- **Smaller fast models (Gemma 3 Nano / Phi-3.5 / Qwen 2.5)**
  Already covered by E2B in the README's model picker. The measured
  data (`README.md` "Picking your Gemma model") says E2B drops 4 cases
  and saves ~120 ms median — mostly net negative for a hotkey-driven
  voice UX. A smaller third-party model would face the same measurement
  question; introducing Phi/Qwen brings a non-Gemma family with its own
  prompt-format requirements (chat templates, system-message behavior)
  and would need the entire prompt re-tested. Not worth it as a
  precaution; if VRAM pressure rises in the field, E2B is the stocked
  answer.

## Out of scope

- Whisper changes (model selection, language, Polish quality) — separate agent.
- OSD / UI rendering changes — separate agent.
- New action implementations beyond the existing vision-click branch — separate agent.
- Cloud APIs (Anthropic, OpenAI, Google AI Studio) — local-first ethos.
- Wake-word model selection (`openWakeWord`) — orthogonal to Gemma usage.
- TTS engine choice and voice models — orthogonal to Gemma usage.
- pywhispercpp build tuning, ROCm version — infrastructure not Gemma.
