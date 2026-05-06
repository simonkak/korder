# Feature Improvements

Audit of Korder's action vocabulary, drawing inspiration from Talon Voice,
Rhasspy, macOS Voice Control / Shortcuts, and household voice-assistant
patterns. Recommendations are scoped to **user-spoken commands** — KDE-system
internals, UI, and audio-pipeline tuning are explicitly out of scope.

The codebase is in good shape. The vocabulary is dense around media + Spotify
+ system power, but has clear gaps in **clipboard handling, screenshots,
window/app control, productivity primitives (timers, capture), and dictation
verbs (case-shifting, punctuation, repeat-last)**. Several gaps are sub-2-hour
trivial copies of `keys.py` / `shortcuts.py` patterns.

The biggest leverage comes from two larger initiatives at the bottom:
**user-defined custom actions via config** and **timers/notifications as
first-class state**. Without those, every common-but-personal command
("open my work playlist", "remind me in 10 minutes") needs a Python edit.

---

## High value, low effort (≤2h each — ship these soon)

### Action: `screenshot`
- Phrase EN: "take a screenshot", "screenshot", "screenshot region", "screenshot window"
- Phrase PL: "zrób zrzut ekranu", "screenshot", "zrzut okna", "zrzut zaznaczenia"
- Op kind: `subprocess` — `["spectacle", "-r"]` for region (default), `-f` full screen, `-a` active window. `--background --copy-image` saves to clipboard.
- Parameters: `mode` (enum: `region` | `fullscreen` | `window`, default `region`)
- Why an action vs Gemma chat: capture is an operation, not a question. A common need; would get used multiple times a day.
- Model file on: `src/korder/actions/system.py` (Spectacle is part of KDE so it's safe to assume) — single-shot, no confirmation gate.
- New file: `src/korder/actions/screenshot.py`

### Action: `copy_selection` / `paste` / `cut_selection`
- Phrase EN: "copy that", "paste", "cut that"
- Phrase PL: "skopiuj", "wklej", "wytnij"
- Op kind: `combo` — `[KEY_LCTRL, KEY_C]`, `[KEY_LCTRL, KEY_V]`, `[KEY_LCTRL, KEY_X]` (need new keycodes `KEY_C=46`, `KEY_X=45` in `codes.py`)
- Parameters: none
- Why an action: extremely common dictation companion. The current vocabulary already has `select_all`/`undo`; copy/cut/paste are the obvious siblings missing.
- Model on: existing `shortcuts.py`. Two-hour drop-in.

### Action: `find_in_page`
- Phrase EN: "find", "find in page", "search in page", "search this page for X"
- Phrase PL: "szukaj na stronie", "znajdź", "wyszukaj tu"
- Op kind: `combo` `[KEY_LCTRL, KEY_F]` — and if the user named a query, follow up with a `text` op typing the query (multi-op return).
- Parameters: `query` (string, optional)
- Why an action: the LLM is great at extracting the search term. Keeps dictation flow when user wants to scan a doc.
- Model on: `web.py`'s "extract query, return op" pattern. Would emit a list of ops — first the combo, then the text. Will need the parser to accept list-of-ops returns from `op_factory` (see "tuple-or-list ops" follow-up below).

### Action: `redo`
- Phrase EN: "redo", "redo that"
- Phrase PL: "ponów", "powtórz akcję"
- Op kind: `combo` `[KEY_LCTRL, KEY_LSHIFT, KEY_Z]` (or `KEY_Y`, distro-dependent — `Ctrl+Shift+Z` is the safer cross-app default).
- Why an action: pairs with existing `undo`. Trivial to add and noticeably absent.
- Model on: `shortcuts.py`.

### Action: `save_file`
- Phrase EN: "save", "save file", "save this"
- Phrase PL: "zapisz", "zapisz plik"
- Op kind: `combo` `[KEY_LCTRL, KEY_S]`
- Why an action: ubiquitous. Easy win.
- Model on: `shortcuts.py`. Add `KEY_S` to `codes.py`.

### ✅ ~~Action: `close_window` / `close_tab`~~ — *partial: `close_window` shipped via KWin script bridge (`src/korder/actions/window.py`); `close_tab` not yet (would still be a `Ctrl+W` combo since per-app tab semantics live inside the focused app, not KWin).*
- Phrase EN: "close window", "close tab"
- Phrase PL: "zamknij okno", "zamknij kartę"
- Op kind: `combo` `[KEY_LALT, KEY_F4]` for window, `[KEY_LCTRL, KEY_W]` for tab. Need new `KEY_F4`, `KEY_W`.
- Why an action: high-frequency operations. Two distinct phrases prevent the LLM from confusing window-close with tab-close.
- Model on: `shortcuts.py`.

### Action: `move_cursor` (line/word navigation by voice)
- Phrase EN: "go to start of line", "go to end of line", "next word", "previous word"
- Phrase PL: "początek linii", "koniec linii", "następne słowo", "poprzednie słowo"
- Op kind: `key` (Home/End) and `combo` (Ctrl+Right/Left — need `KEY_LEFT=105`, `KEY_RIGHT=106`)
- Why an action: dictation users need to navigate. Currently they can `select_to_line_start` but not just cursor-move there. Pairs naturally with new `redo`/`save_file`.
- Model on: `shortcuts.py`. Four small registrations.

### Action: `type_punctuation`
- Phrase EN (within dictation): "comma", "period", "question mark", "exclamation mark", "colon", "semicolon", "open paren", "close paren", "quote", "dash"
- Phrase PL: "przecinek", "kropka", "znak zapytania", "wykrzyknik", "dwukropek", "średnik", "otwórz nawias", "zamknij nawias", "cudzysłów", "myślnik"
- Op kind: `text` / `char` — emit literal punctuation
- Why an action: Whisper already inserts punctuation reasonably, but precise dictation (code, structured text, formal writing) wants explicit control. macOS Voice Control's biggest dictation lever. Currently Korder relies entirely on Whisper's punctuation guess.
- Model on: `text_chars.py` — same shape as `new_line` / `new_paragraph`. About 10 small registrations.
- Caveat: registering all these as triggers risks false positives ("I told her, period.") — gate behind an explicit prefix word ("punctuation comma") OR limit to the LLM path so context disambiguates.

### Action: `volume_set` (absolute level)
- Phrase EN: "set volume to 30", "volume to 50 percent", "volume 25"
- Phrase PL: "ustaw głośność na 30", "głośność na 50 procent"
- Op kind: extend `system_volume` payload — `("set", pct)` in addition to existing `("up"/"down"/"mute_toggle", step)`. Plumb a new branch in `inject._run_system_volume` that calls `wpctl set-volume @DEFAULT_AUDIO_SINK@ <pct>%`.
- Parameters: `level_pct` (int 0–100)
- Why an action: relative volume already exists. Absolute is the obvious sibling and matches how users actually think ("set it to 30").
- Model on: `media.py:volume_up`. ~1h.

### Action: `microphone_mute_toggle`
- Phrase EN: "mute mic", "mute microphone", "unmute mic"
- Phrase PL: "wycisz mikrofon", "włącz mikrofon"
- Op kind: `subprocess` — `wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle`
- Why an action: meeting workflow staple. Distinct from output-mute (`volume_mute`) which already exists.
- Model on: `media.py`.

### Action: `clipboard_read` (read clipboard aloud)
- Phrase EN: "what's on the clipboard", "read clipboard"
- Phrase PL: "co jest w schowku", "przeczytaj schowek"
- Op kind: `callable` — read `wl-paste`, push to `emit_progress_speak`, also notify-send.
- Why an action: pairs with TTS. Useful for eyes-busy contexts. Emit-speak path already wired.
- Model on: `now_playing.py` (similar shape — read state, emit_progress_speak).

### Action: `dnd_toggle` (do-not-disturb)
- Phrase EN: "do not disturb", "enable do not disturb", "silence notifications", "turn off do not disturb"
- Phrase PL: "tryb nie przeszkadzać", "wycisz powiadomienia", "wyłącz tryb nie przeszkadzać"
- Op kind: `subprocess` — KDE Plasma uses `qdbus6 org.kde.plasmashell /org/kde/osdService org.kde.osdService.kbdLayoutChanged` style; the cleaner path is `qdbus6 org.kde.plasmashell /KNotify org.kde.kdeglobals` or `kwriteconfig6 --file plasmanotifyrc --group DoNotDisturb --key Until <time>` then signal Plasma to reload. Plasma 6's interactive-notify-state flag is `org.kde.notificationmanager` — `qdbus6 org.kde.plasmashell /org/kde/plasmashell org.kde.plasmashell.toggleDoNotDisturbMode` works on current Plasma.
- Why an action: focus tooling that pairs naturally with voice ("Hey Jarvis, do not disturb").
- Model on: `system.py:lock_screen`. No confirmation needed.

### Action: `repeat_last_command`
- Phrase EN: "do that again", "repeat", "again"
- Phrase PL: "powtórz", "jeszcze raz", "ponownie"
- Op kind: `callable` — pulls last executed op-list from a small in-memory ring (size 1 is enough for "again"). Lives in MainWindow / SessionState.
- Why an action: Talon ships this; users discover the value once they've used it twice. Implementation needs a tiny last-ops cache and a contract: don't repeat session-meta ops (cancel/end/write_mode toggles), don't repeat "again" itself.
- Model on: needs a new module `src/korder/actions/meta.py`. ~2h once you decide where the history lives.

---

## High value, medium effort (4–12h each)

### ✅ ~~Action: `app_launcher` — "open Firefox", "open Konsole", "open my notes"~~ — *Shipped in `src/korder/actions/launcher.py`. Hybrid resolution: scan XDG `.desktop` files (cached), token-overlap match against stem + Name + GenericName + Keywords + Comment + Exec basename, with stem matches weighted 2× and gated to ≥4-char tokens to avoid `app`/`kde`/`org` false positives. Confident match → `gtk-launch <stem>` (or `gio launch` fallback). Unresolved → KRunner D-Bus `query()` opens the launcher UI with the term pre-filled for user confirmation. Common case ('open Firefox') launches without UI flash; ambiguous queries fall through to Plasma's launcher.*
- Phrase EN: "open Firefox", "launch Konsole", "open Spotify", "start Krita"
- Phrase PL: "otwórz Firefoxa", "uruchom Konsolę", "otwórz Spotify"
- Op kind: `subprocess` — call `gtk-launch` / `kioclient5 exec` / `gio launch` against the resolved `.desktop` file. Or use `qdbus6 org.kde.krunner` (the Plasma launcher) which already does fuzzy app-name matching.
- Parameters: `app_name` (string, free-form)
- Why an action: bedrock voice-assistant capability missing today. Can't say "open Firefox" — high embarrassment factor when demoing.
- Implementation outline:
  1. Discover apps once at process start: scan `XDG_DATA_DIRS` for `applications/*.desktop`, parse Name + Exec, build a fuzzy index.
  2. The action's `op_factory` receives `app_name`, returns a callable that does the lookup → `subprocess.run(["gtk-launch", desktop_id])`.
  3. Bilingual matching: index `Name[pl]=` and `Name[en]=` entries when present.
- Files: new `src/korder/actions/launch.py` + a tiny `desktop_index.py` in `src/korder/system/`. ~6h with tests.

### ✅ ~~Action: `window_switcher` — "switch to Firefox", "focus my terminal"~~ — *Shipped as `focus_window` in `src/korder/actions/window.py`. Token-overlap fuzzy match against window caption + resourceClass runs server-side in a KWin script (no Python round-trip), so multi-word fragments work too ('focus Firefox How I Chose a Linux Distro' picks the right tab among multiple firefoxes). Companion actions also shipped: `close_window`, `minimize_window`, `tile_window`, `next_desktop`, `previous_desktop`, `send_window_to_desktop`, `send_window_to_screen`, `show_overview`, `show_desktop`.*
- Phrase EN: "switch to Firefox", "focus terminal", "go to Spotify"
- Phrase PL: "przełącz na Firefoxa", "pokaż terminal", "przejdź do Spotify"
- Op kind: `subprocess` — `qdbus6 org.kde.kglobalaccel` or KWin's window-list API. `kdotool` (a recent Wayland-friendly clone of xdotool for KWin) provides `kdotool search --class firefox windowactivate`.
- Parameters: `target` (string, app name or window-title fragment)
- Why an action: pairs with `app_launcher` — launch when not running, switch when it is. The opening of any voice workflow.
- Caveat: Wayland makes this awkward. Best path is a KWin script bound to a global shortcut, or accept `kdotool` as an optional dep.
- Files: new `src/korder/actions/windows.py`. ~8h including the kdotool detection / fallback messaging.

### Action: `timer_set` — "remind me in 10 minutes", "set a timer for 5 minutes"
- Phrase EN: "set a timer for 10 minutes", "remind me in 5 minutes to take the bread out"
- Phrase PL: "ustaw timer na 10 minut", "przypomnij mi za 5 minut o piekarniku"
- Op kind: `callable` (registers the timer) — uses `threading.Timer` or asyncio. On expiry, call `notify-send` and `emit_progress_speak`.
- Parameters: `duration` (string — "10 minutes", "1 hour 30") and `label` (string, optional)
- Why an action: cooking, pomodoro, "wait for the dryer" — all things where eyes/hands aren't free. Explicit op (not chat) because the user actually wants the timer registered, not described.
- Implementation outline:
  - New `src/korder/timers.py` — small registry `{id: (deadline, label, lang)}` with one shared scheduler thread. Persist to `~/.local/state/korder/timers.json` so a Korder restart doesn't drop pending timers (medium-effort but worth it).
  - On fire: notify-send + speak + emit a Plasma OSD blip. Optional: ring an audible bell (use the existing chime infra in `audio/`).
  - Companion actions: `timer_list` ("what timers are running"), `timer_cancel` ("cancel my 10-minute timer"). Both small.
- Files: `src/korder/actions/timers.py` + `src/korder/timers.py`. ~10h with persistence + tests.

### Action: `time_query` — "what time is it"
- Phrase EN: "what time is it", "what's the time", "time in Tokyo"
- Phrase PL: "która godzina", "która jest", "która godzina w Tokio"
- Op kind: `callable` — Python `datetime` + `zoneinfo` for the timezone branch; emit_progress_speak the answer.
- Parameters: `tz` (string, optional — IANA name extracted by LLM from "Tokyo")
- Why an action vs Gemma chat: Gemma can't answer reliably (the model has no clock). Today users get either silence or hallucination. README explicitly says live-data questions aren't answered — adding this fills that gap properly.
- Files: `src/korder/actions/clock.py`. ~2h.

### Action: `date_query` / `weekday_query`
- Phrase EN: "what's today's date", "what day is it"
- Phrase PL: "jaka jest dzisiejsza data", "jaki dzisiaj dzień"
- Op kind: `callable` — locale-aware date formatting via `babel.dates.format_date`.
- Why an action: same rationale as `time_query` — Gemma can't know.
- Files: same module as `time_query`. ~1h once the time module exists.

### Action: `unit_convert` / `calculator`
- Phrase EN: "convert 200 grams to ounces", "what's 17 times 23"
- Phrase PL: "przelicz 200 gramów na uncje", "ile to siedemnaście razy dwadzieścia trzy"
- Op kind: `callable` — `pint` for unit conversion, `ast.literal_eval` (or `simpleeval`) for arithmetic. Emit_progress_speak the result.
- Parameters: `expression` (string)
- Why an action vs Gemma chat: Gemma already handles arithmetic (and the README mentions "ile to siedem razy osiem" works). BUT — the LLM gets bigger numbers / unit conversions wrong. Routing to a deterministic evaluator is more robust and faster. Also a great teaching example for users that some intents are computational.
- Files: `src/korder/actions/calc.py`. New optional dep on `pint` (or write a tiny conversion table for the dozen common units).
- ~6h including the LLM intent prompt update so it knows when to route here vs answer conversationally.

### Action: `quick_capture` — "note that…", "remember that…"
- Phrase EN: "note that the wifi password is X", "remember that I parked in spot 12B", "add eggs to my shopping list"
- Phrase PL: "zapamiętaj że hasło to X", "dopisz jajka do listy zakupów"
- Op kind: `callable` — append a line to `~/.local/state/korder/notes.md` (or per-list files keyed by `list_name`). Speak/notify confirmation.
- Parameters: `content` (string, required), `list_name` (string, optional — "shopping", "todo", default "notes")
- Why an action: GTD / Obsidian-quick-capture pattern. The LLM today would just answer "OK, noted" but nothing actually persists. A real capture sink earns its keep instantly.
- Files: `src/korder/actions/capture.py`. ~4h.

### ✅ ~~Per-app context for the LLM intent prompt~~ — *Shipped, broader than the original spec. The intent prompt now carries a 'Currently open windows' block listing every normal window with its resourceClass + caption + active flag (`src/korder/intent.py:_render_window_list`, fed by `korder.kwin_bridge`). The active window is marked `(active)` so the LLM can use it as the focused-app signal AND answer 'which window' questions verbatim from the same block. Plumbed via a QtDBus bridge service Korder hosts on the session bus so KWin scripts callDBus the list back synchronously — no per-call shelling out to qdbus.*
- Detect the focused window's app id (via `qdbus6 org.kde.KWin /KWin org.kde.KWin.activeWindow` or KWin's `getWindowInfo`) and inject it into the intent prompt as `Focused app: <name>`.
- Why: lets the LLM bias its choices — `select line` is a text-editing intent in a code editor but might mean something different in a media app; `play` in Spotify is unambiguous.
- Implementation:
  1. New `src/korder/system/focus.py` — single function returning `(app_id, window_title)` with a 200ms cache.
  2. Plumb into `intent.py` prompt builder; add a `Focused app:` line before `Current topic:`.
  3. No action changes needed — the prompt already controls disambiguation.
- ~6h. The big win: opens the door to per-app vocabularies (see initiative below).

---

## Larger initiatives (1d+)

### Initiative: User-defined custom actions via config
**Problem.** Every personal voice command ("open my project Y", "play my workout playlist") today requires editing Python and re-running `uv sync`. The action registration pattern is clean but it's still code.

**Sketch.** Add a `[custom_actions]` section to `~/.config/korderrc` (or a sibling `~/.config/korder/actions.toml` to avoid escaping pain), declarative form:
```toml
[[action]]
name = "open_work_project"
triggers_en = ["open my work project", "open work"]
triggers_pl = ["otwórz projekt służbowy"]
description = "Opens the work project in VS Code."
type = "shell"
command = "code /home/szymon/work/project"

[[action]]
name = "play_workout"
triggers_en = ["play workout", "workout playlist"]
triggers_pl = ["puść trening"]
description = "Play the gym playlist on Spotify."
type = "spotify"
query = "Workout 2024"
kind = "playlist"

[[action]]
name = "morning_routine"
triggers_en = ["morning routine", "good morning"]
triggers_pl = ["dzień dobry"]
description = "Open mail, calendar, and Spotify."
type = "chain"
steps = [
  { type = "shell", command = "kioclient5 exec mailto:" },
  { type = "shell", command = "xdg-open https://calendar.google.com" },
  { type = "spotify", query = "Morning Coffee", kind = "playlist" },
]
```

**Files affected.**
- New `src/korder/actions/_custom.py` — loader that reads the TOML, validates, and calls `register(Action(...))` for each entry at import time. Lives alongside `__init__.py`'s static imports.
- `src/korder/config.py` — add the loader hook.
- `src/korder/actions/__init__.py:21` — call the custom loader after the built-in modules so user actions can override builtins (collision check exists already).
- Reload-on-edit: hook into the existing config-watch mechanism if there is one, or accept restart-required for v1.

**Prerequisites.** None hard. The action protocol is already factory-based, the trigger collision check already raises on overlap.

**Out of v1.** Per-app filters (defer to "per-app vocabularies" initiative). Custom Python (security risk; users can write a Python module if they need it).

**Impact.** Removes the largest single friction point in scaling Korder's vocabulary. ~1.5–2 days incl. validation, error messages for bad TOML, tests covering load + collision + reload.

---

### Initiative: Per-app voice vocabularies
**Problem.** "Select line" means one thing in Konsole, another in Kate, irrelevant in Firefox. "New tab" means Ctrl+T everywhere except in IDEs where a different chord opens a project switcher. A single global vocabulary forces compromises.

**Sketch.** Two changes in tandem:
1. Action registration accepts an `app_filter` field — a list of substrings that the focused-app id must contain. `app_filter=["firefox", "chromium"]` means "only suggest this action when a browser is focused".
2. The LLM intent prompt receives the focused app (see medium-effort item above) AND filters the available-action list to: globally-applicable actions ∪ actions whose `app_filter` matches.

**Files affected.**
- `src/korder/actions/base.py` — add `app_filter: list[str] = field(default_factory=list)` and helper `applicable_for(app_id)`.
- `src/korder/intent.py` — filter the action list passed into the prompt by focused app. Keeps the prompt smaller too — speed win.
- `src/korder/actions/parser.py` — same filter for regex mode.
- Built-in actions get tagged where useful (`find_in_page` is universal, `terminal_clear_screen` is Konsole-only, `git_amend` is for git-aware terminals).

**Prerequisites.** Focused-app detection (medium-effort item above).

**Impact.** ~1 day for the plumbing + tagging existing actions. The real win is enabling power users to add Konsole-specific or IDE-specific commands in their custom-action TOML without polluting the global vocabulary.

---

### Initiative: First-class multi-step / chained intents
**Problem today.** The LLM is told to extract one action per turn. "Play my workout playlist on Spotify and lock the screen" probably resolves to one action and drops the other (or vice versa). Whether this works for a given prompt is currently undocumented.

**Sketch.** Two parts:
1. **LLM contract change.** Allow the model to return a list of actions, not just one — `{"actions": [{name, params}, ...], "response": "..."}`. The execution loop runs them in order, narrating each.
2. **Inter-step cancellation.** If an action emits an error (e.g. Spotify search returned nothing), the chain pauses and asks the user "continue with the rest?" or auto-aborts (configurable).

**Files affected.**
- `src/korder/intent.py` — JSON schema, prompt updates with examples ("X and Y", "X then Y", and Polish equivalents — "X, a potem Y", "zrób X i Y").
- `src/korder/inject.py` — already iterates an op-list, so the engine side mostly works. The change is in the parser returning multiple action ops vs one.
- New `src/korder/actions/_chain.py` for the orchestration, error-handling, and progress narration ("Step 1 of 3: …").
- Bench: add 4–5 multi-step cases to `intent_bench` (single-language, mixed-language, mid-step error).

**Prerequisites.** None. Mostly prompt work + a small dispatcher change.

**Impact.** ~1.5 days. Unlocks the "morning routine" custom-action chains too (those can compile to multi-step at registration time).

---

### Initiative: Op-factory returns list-of-ops (currently only one)
**Problem.** `op_factory` returns a single op tuple. Several proposed actions naturally want to return multiple ops (e.g. `find_in_page` does Ctrl+F **then** types the query; `clipboard_read` runs a callable but the answer wants TTS). Today this is hacked by stuffing the multi-step inside a `callable`.

**Sketch.**
- Allow `op_factory` to return either `Op` or `list[Op]`. `inject._run_ops` already iterates a list, so the change is one helper that flattens.
- Update `parser.py` and the LLM dispatcher to match.
- Document on `Action`: "Return a single op for atomic actions, or a list when the action is naturally a sequence of typing/keys/calls."

**Files affected.** `actions/base.py`, `actions/parser.py`, `inject.py`. Tests for both single + list returns.

**Impact.** ~4h (small initiative). Unblocks `find_in_page`, makes future actions cleaner.

---

## Out of scope / not worth doing

- **Mouse control by voice ("click that link")**. Talon does this with a number-grid overlay on screen elements; it requires accessibility-tree introspection (KDE has KAccessibility but it's spotty on Wayland) plus an OSD overlay system. **Outside Korder's "voice → keystrokes/dispatcher" scope and not feasible without significant Wayland work.** Recommend documenting that "navigation by voice" is what the keyboard cursor-move actions are for.

- **Light/IoT control (Rhasspy-style smart-home).** Korder is single-user, single-machine. Adding MQTT/Home-Assistant integration is a different product.

- **Cooking-step / recipe-walkthrough mode.** Tempting (timers + "next step") but it requires structured recipe storage, a UI, and a state machine. The timer action above is the 80% solution; "next step" can be a normal `quick_capture`-style note.

- **App-specific code-dictation grammars (Talon's biggest selling point).** Very high implementation cost (a parser per language/IDE), and the LLM already does ~good-enough English-to-code via dictation when prompted. Not worth a custom DSL today; revisit if Korder grows a developer audience.

- **Brightness / screen rotation.** Korder runs on a desktop (per the README's "AMD 7800 XT" reference). Brightness on a desktop is the monitor's hardware buttons; rotation is a one-keystroke KDE shortcut already. Add only if a handheld port becomes a real goal.

- **Streamy live-data answers (weather, news, calendar).** Each is its own integration with API keys, error modes, locale-specific data sources. The README explicitly drew the line. Hold until there's a clear single user need; revisit then with one specific integration (likely calendar via Google's API).

- **Capitalize-last-word, format-as-title.** Tempting but every dictation context has different conventions and Whisper does most of this already. The marginal win for a polished dictation experience is large effort for small payoff vs the productivity items above. Add later if `quick_capture` and `find_in_page` are landed and the pain still ranks.

- **Macro-record-and-replay ("record my next 3 actions and bind them to 'do the morning thing'").** Huge UX surface, niche. The custom-actions TOML already covers the static-chain case.
