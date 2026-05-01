<p align="center">
  <img src="assets/icon.svg" width="96" alt="Korder" />
</p>

<h1 align="center">Korder</h1>

<p align="center">
  Voice transcription + voice-controlled actions for KDE Plasma (Wayland).<br>
  Push a hotkey, dictate Polish or English, and have your speech either typed<br>
  into the focused app or interpreted as commands (media control, Spotify,<br>
  keyboard shortcuts) routed through a small local Gemma model.
</p>

<p align="center">
  <img src="assets/screens/command_invocation_centered.png" alt="Listening — your spoken command" width="720" />
  <br><em>Listening — your spoken command, with locked-prefix highlighting</em>
</p>

<p align="center">
  <img src="assets/screens/command_thinking_centered.png" alt="Thinking — Gemma parses the command" width="720" />
  <br><em>Thinking — Gemma parses the command into structured ops</em>
</p>

<p align="center">
  <img src="assets/screens/command_execution_feedback_centered.png" alt="Executing — action narrates progress" width="720" />
  <br><em>Executing — action narrates its progress in the Plasma accent color</em>
</p>

<p align="center">
  <img src="assets/screens/command_pending_action_centered.png" alt="Awaiting — waiting for follow-up parameter" width="720" />
  <br><em>Awaiting — pending parameter, waiting for your follow-up utterance</em>
</p>

Built for one user — me — on KDE Plasma 6 / Wayland. Nothing here is
production-grade, but everything works on a quiet desk mic with a 7800 XT.

## Features

- **Live transcription** via [whisper.cpp](https://github.com/ggerganov/whisper.cpp) with the Vulkan backend (GPU-accelerated on AMD/Nvidia/Intel)
- **Pill-shaped layer-shell OSD** (org.kde.layershell) — overlay never steals focus, stays above all windows. Anchored to the bottom of the screen, semi-transparent so KWin's Blur effect picks it up automatically. Three sections:
    - leading: animated accent dot + state label (*Słucham → Myślę → Wykonuję → Gotowe*)
    - center: your transcription, with **locked-prefix highlighting** — the longest word-aligned common prefix between successive Whisper partials renders bright while the still-revising tail fades, so the eye knows what's settled. Action progress narration (*"Searching Spotify for Linkin Park… → Found album: Linkin Park → Playing Linkin Park"*) renders inline in your Plasma accent color, italic, distinct from your spoken command.
    - "Press ESC to cancel" hint below the pill while listening.
- **System tray app** with global hotkeys (KGlobalAccel via the IPC `korder toggle` and `korder cancel` commands) and a Settings… dialog covering every config key — no hand-editing required
- **Write mode toggle** — say *"Pisz"* to start typing into the focused app, *"Przestań"* to stop. Default is preview-only.
- **Action vocabulary** dispatched via either a regex parser or a local LLM (Gemma via ollama):
    - Keys: Enter, Tab, Escape, Backspace
    - Shortcuts: Ctrl+Backspace (delete word), Ctrl+A (select all), Ctrl+Z (undo), Shift+Home/End (select line)
    - Media: volume up/down/mute, play/pause, stop, next/previous track (via kernel media keycodes — KDE routes to MPRIS + PipeWire automatically)
    - Now playing: ask *"what's playing"* / *"co teraz gra"* and Korder reads MPRIS metadata from the active player (Spotify, Firefox, mpv, …) and pops a desktop notification with track + artist
    - Spotify: search and play albums, tracks, artists, or playlists via the Web API (free Client Credentials flow, no Premium required for search). When you don't say what kind ("Spotify play *Pink Floyd*"), one request fans out across all four types and the closest name match wins — artist > album > track > playlist within each match tier.
    - Web actions (xdg-routed, opens default browser): web search (DuckDuckGo / Google / Bing / Startpage / Ecosia), YouTube search, Wikipedia (auto-picks language from system locale), Maps
    - System: lock screen via `xdg-screensaver lock`
- **Pending parameter handling** — say *"Spotify play"* … pause to think … *"Linkin Park"* and the second utterance becomes the search query for the first action
- **Polish + English** trigger phrases for every action
- **Optional Gemma thinking step** — slower (~1–2 s vs ~500 ms) but resolves ambiguous phrasings without hand-coded triggers; toggle via `[intent] thinking_mode`
- **Auto-stop after a command** — fires once an action lands so you don't have to hit the hotkey twice; pure dictation and mode toggles keep the session open
- **Auto-duck system volume while listening** (default on) — drops the default PipeWire sink to 30 % when the mic opens and restores the original level on stop, so speaker bleed stops confusing Whisper. Skipped if you're already quieter than the target; restored on crash via `atexit`. Requires `wpctl`.

## OS dependencies

Tested on Arch Linux / CachyOS with KDE Plasma 6.6+. Other distros' package
names will differ but the binaries are the same.

### Required

```bash
# Input synthesis (Wayland-friendly)
sudo pacman -S ydotool wl-clipboard

# Build deps for whisper.cpp Vulkan backend (pywhispercpp builds from source)
sudo pacman -S cmake gcc vulkan-headers vulkan-icd-loader shaderc

# Local LLM serving for action parsing (optional — falls back to regex)
sudo pacman -S ollama

# Python package manager
sudo pacman -S uv
```

KDE Plasma 6 ships `org.kde.layershell` QML module, KSvg, and KWindowSystem
out of the box — no extra install if you're already on Plasma.

### Setup steps

```bash
# 1. ydotool needs /dev/uinput access. Run the script once:
./scripts/setup-uinput.sh
# Then log out + back in so the 'input' group membership applies.

# 2. Start the ydotoold daemon
systemctl --user enable --now ydotool

# 3. Start ollama (only if you want LLM action parsing)
sudo systemctl enable --now ollama
ollama pull gemma4:e4b   # or gemma4:e2b — smaller/faster but lower accuracy

# 4. Sync Python deps
uv sync

# 5. Build pywhispercpp from source with Vulkan
CMAKE_ARGS="-DGGML_VULKAN=on" \
  uv pip install --force-reinstall --no-binary=pywhispercpp pywhispercpp
```

## Run

```bash
uv run korder
```

A microphone icon appears in your system tray. Left-click to toggle recording;
right-click for the context menu.

For a global hotkey, bind `/home/YOU/priv/korder/.venv/bin/korder toggle` in
**System Settings → Shortcuts → Custom Shortcuts → Edit → New → Global
Shortcut → Command/URL**. Common pick: `Ctrl+Space`.

To cancel a recording mid-flight without committing what you've said, bind
`/home/YOU/priv/korder/.venv/bin/korder cancel` to a second hotkey (the
OSD shows a "Press ESC to cancel" hint while listening — common pick is
`Esc`, though it has to go through KGlobalAccel since the OSD is a
focusless overlay).

## Configuration

The tray menu's **Settings…** entry exposes every key in a tabbed dialog —
that's the recommended path. The underlying file is `~/.config/korderrc`,
auto-created on first run with reasonable defaults; edit by hand if you
prefer. Some interesting knobs:

```ini
[whisper]
model = medium          # tiny | base | small | medium | large-v3 | large-v3-turbo
language = pl           # whisper language hint; "pl"/"en"/etc. or empty for auto
n_threads = 4

[audio]
gain = 0.7                      # software gain on captured audio; lower if mic too hot
duck_during_recording = true    # lower system playback volume while listening
duck_volume_pct = 30            # target volume (% of full) while ducked; no-op if
                                # you're already quieter than this. Requires wpctl.

[inject]
action_parser = regex   # "regex" (fast, deterministic) or "llm" (smarter, slower)
llm_model = gemma4:e4b  # which ollama tag to use when action_parser = llm
paste_mode = auto       # auto | always | never — clipboard-paste vs direct type

# LLM intent-parser tuning (only applies when inject.action_parser = "llm").
[intent]
thinking_mode = false           # engage Gemma's reasoning loop before answering
show_triggers_in_prompt = false # include trigger phrases in the prompt catalog
timeout_s = 20                  # per-call timeout; generous so a slow think
                                # doesn't trip the regex fallback

[ui]
show_history_on_start = false
auto_stop_after_action = true   # stop recording once a command-style action
                                # fires (dictation/mode toggles keep going)

# Optional — Spotify Web API credentials. Without these, Spotify actions
# fall back to opening search results (user clicks). Get them at
# https://developer.spotify.com/dashboard (free, no Premium needed).
[spotify]
client_id =
client_secret =

# Engine for the "search the web for X" / "google X" / "wyszukaj X" voice
# action. Supported: duckduckgo (default), google, bing, startpage, ecosia.
# YouTube/Wikipedia/Maps actions don't read this — they go to their
# canonical URL.
[web]
search_engine = duckduckgo
```

## Voice commands (when LLM mode is on)

Say these in normal speech; phrasing variations work because Gemma understands
intent.

| Action            | English                          | Polish                                |
|-------------------|----------------------------------|---------------------------------------|
| Press Enter       | "press enter", "submit"           | "naciśnij enter", "wyślij"            |
| Delete word       | "delete word"                     | "usuń słowo", "skasuj słowo"          |
| Select line       | "select line"                     | "zaznacz linię"                       |
| Volume up/down    | "louder" / "quieter"              | "głośniej" / "ciszej"                 |
| Play/pause        | "play music", "pause"             | "puść muzykę", "pauza", "wznów"       |
| Stop playback     | "stop playback"                   | "zatrzymaj odtwarzanie"               |
| Next/prev track   | "next song" / "previous song"     | "następna piosenka" / "poprzednia"    |
| Now playing       | "what's playing", "what song is this" | "co gra", "co teraz leci"           |
| Spotify (any)     | "spotify play Pink Floyd"         | "spotify zagraj Pink Floyd"           |
| Spotify album     | "spotify play album Meteora"      | "spotify zagraj album Meteora"        |
| Spotify track     | "spotify play track Numb"         | "spotify zagraj utwór Numb"           |
| Spotify artist    | "spotify play artist Pink Floyd"  | "spotify zagraj wykonawcę Pink Floyd" |
| Spotify playlist  | "spotify play playlist workout"   | "spotify zagraj playlistę workout"    |
| Web search        | "search for X", "google X"        | "wyszukaj X", "wygoogluj X"           |
| YouTube           | "play X on YouTube"               | "puść X na YouTube"                   |
| Wikipedia         | "wikipedia X", "tell me about X"  | "co to jest X", "kim jest X"          |
| Maps              | "navigate to X", "where is X"     | "nawiguj do X", "gdzie jest X"        |
| Lock screen       | "lock screen"                     | "zablokuj ekran"                      |
| Write mode on     | "start writing"                   | "pisz"                                |
| Write mode off    | "stop writing"                    | "przestań"                            |

## Development

```bash
uv run pytest                                    # 141 tests, no external services required
uv run pytest -m ollama                          # +39 integration tests against a live Gemma
uv run python -m korder.intent_bench             # 21-case headless benchmark vs the current model
uv run python -m korder.intent_bench --thinking  # …with Gemma's thinking step engaged
```

Adding a new action is one file in `src/korder/actions/`:

```python
from korder.actions.base import Action, register

register(Action(
    name="my_action",
    description="Short English description for the LLM prompt",
    triggers={
        "en": ["do the thing"],
        "pl": ["zrób to"],
    },
    op_factory=lambda _args: ("subprocess", ["my-cli", "--flag"]),
))
```

Then add the module to `src/korder/actions/__init__.py`'s import list. The
LLM prompt and regex parser pick it up automatically.

### Architecture notes

- [`docs/intent-architecture.md`](docs/intent-architecture.md) — why the
  intent parser uses JSON output rather than function calling, with
  measured numbers from a head-to-head against the `function-calling`
  branch.

## License

MIT — see [LICENSE](LICENSE).
