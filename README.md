# Korder

Voice transcription + voice-controlled actions for KDE Plasma (Wayland).
Push a hotkey, dictate Polish or English, and have your speech either typed
into the focused app or interpreted as commands (media control, Spotify,
keyboard shortcuts) routed through a small local Gemma model.

Built for one user — me — on KDE Plasma 6 / Wayland. Nothing here is
production-grade, but everything works on a quiet desk mic with a 7800 XT.

## Features

- **Live transcription** via [whisper.cpp](https://github.com/ggerganov/whisper.cpp) with the Vulkan backend (GPU-accelerated on AMD/Nvidia/Intel)
- **Layer-shell OSD** (org.kde.layershell) — overlay never steals focus, stays above all windows
- **System tray app** with global hotkey (KGlobalAccel via the IPC `korder toggle` command)
- **Write mode toggle** — say *"Pisz"* to start typing into the focused app, *"Przestań"* to stop. Default is preview-only.
- **Action vocabulary** dispatched via either a regex parser or a local LLM (Gemma via ollama):
    - Keys: Enter, Tab, Escape, Backspace
    - Shortcuts: Ctrl+Backspace (delete word), Ctrl+A (select all), Ctrl+Z (undo), Shift+Home/End (select line)
    - Media: volume up/down/mute, play/pause, next/previous track (via kernel media keycodes — KDE routes to MPRIS + PipeWire automatically)
    - Spotify: search and play albums or tracks via the Web API (free Client Credentials flow, no Premium required for search)
- **Pending parameter handling** — say *"Spotify play"* … pause to think … *"Linkin Park"* and the second utterance becomes the search query for the first action
- **Polish + English** trigger phrases for every action

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

## Configuration

`~/.config/korderrc` is auto-created on first run with reasonable defaults.
Some interesting knobs:

```ini
[whisper]
model = medium          # tiny | base | small | medium | large-v3
language = pl           # whisper language hint; "pl"/"en"/etc. or empty for auto
n_threads = 4

[audio]
gain = 0.7              # software gain on captured audio; lower if mic too hot

[inject]
action_parser = regex   # "regex" (fast, deterministic) or "llm" (smarter, slower)
llm_model = gemma4:e4b  # which ollama tag to use when action_parser = llm
paste_mode = auto       # auto | always | never — clipboard-paste vs direct type

# Optional — Spotify Web API credentials. Without these, Spotify actions
# fall back to opening search results (user clicks). Get them at
# https://developer.spotify.com/dashboard (free, no Premium needed).
[spotify]
client_id =
client_secret =
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
| Play/pause        | "play music", "pause"             | "puść muzykę", "pauza"                |
| Next/prev track   | "next song" / "previous song"     | "następna piosenka" / "poprzednia"    |
| Spotify album     | "spotify play Pink Floyd"         | "spotify zagraj Pink Floyd"           |
| Spotify track     | "spotify play track Numb"         | "spotify zagraj utwór Numb"           |
| Write mode on     | "start writing"                   | "pisz"                                |
| Write mode off    | "stop writing"                    | "przestań"                            |

## Development

```bash
uv run pytest    # 78 tests, no external services required
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

## License

MIT — see [LICENSE](LICENSE).
