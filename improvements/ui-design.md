# UI Design Improvements

Audit performed against `src/korder/ui/osd.py`, `src/korder/ui/qml/osd.qml`,
`src/korder/ui/settings_dialog.py`, `src/korder/ui/main_window.py`,
`src/korder/ui/icons/tray.svg`, `src/korder/app.py` (tray plumbing),
`src/korder/ui/i18n.py`, and the screenshots in `assets/screens/`. Notes are
limited to what the user actually sees and interacts with — voice vocabulary,
audio pipeline, LLM and KDE-system integration are owned by other audits.

## Quick wins (≤1h each)

- **Pin the OSD pill width while it is visible.** In
  `src/korder/ui/qml/osd.qml:93-94` the window's `width` is bound to
  `contentColumn.implicitWidth + hPad*2`, clamped to `minW=720..maxW=Math.min(1200, Screen.width-80)`.
  Each new partial transcription changes that implicit width, so the bottom-anchored
  pill grows from the centre outward on every Whisper revision — perceptually
  the entire OSD jitters horizontally while the user reads. Snap the width when
  `osdState.visible` flips true (latch the max width attained for the current
  state, reset on every state transition or hide). The user-visible problem is
  motion at the edges of the pill that has nothing to do with the words being
  transcribed; the fix removes that motion without losing the dynamic width
  for short utterances.

- ✅ **~~Tighten the state-dot pulse during Thinking and Loading.~~** *Done in `6d8d79f`.*
  `src/korder/ui/qml/osd.qml:196-201` runs the inner-dot opacity pulse at
  `from: 1.0 to: 0.35 duration: 600ms` (so a full cycle is 1.2 s). At that rate
  the dot reads as "idle / heartbeat" rather than "work happening" — users on
  the LLM cold-start path (Loading takes ~3 s, Thinking another 0.6–2 s) see
  only one or two pulses. Drop to `duration: 380ms` for Thinking and 480 ms
  for Loading, and lift the trough to 0.45 so it never blinks fully off. The
  outer-ring pulse at `duration: 1100ms` (lines 177-184) is correctly slow
  for "open mic, take your time" — leave it.

- **Make the "Press ESC to cancel" hint recognisably a key.**
  `src/korder/ui/qml/osd.qml:329-341` renders the hint as a single faded text
  line. Plasma's own dialog convention uses a chip / kbd-styled glyph for the
  key. Wrap "ESC" in a small rounded-rect with `palette.windowText * 0.10`
  fill and 1px 0.30 border so it reads as a key-cap; keep "Press ... to cancel"
  flanking it. Cheap markup-only change (RichText already supported), bumps
  affordance from "small grey text" to "you can hit this key now".

- **Stop using the i18n key as visible fallback when a translation misses.**
  `src/korder/ui/i18n.py:174-178` returns the bare key (`pending_prompt_shutdown`)
  when no translation exists in either the user's bundle or English. That string
  hits the OSD center and the TTS engine. For a one-user app this is fine while
  developing, but the fallback should still be localized text — return the
  English string for unknown locales (already done) and a `?` glyph for
  unknown keys, with an explicit `log.warning("missing i18n key %r")`. User
  problem: a stray `pending_prompt_foo` text appearing in the OSD looks like a
  bug rather than a missing translation.

- **Surface `Inject failed: …` more prominently than the history-window status
  bar.** `src/korder/ui/main_window.py:1080-1082` routes inject failures to
  `self.statusBar().showMessage(...)`. The history window is hidden by default
  (tray-first; `closeEvent` line 779-783 just hides it), so the user never sees
  the message — they only see the OSD ending in `Done` while nothing actually
  happened. Push the same string through `set_executing_progress("✗ " + msg)`
  and hold the OSD on a red-tinted Done state for ~2 s before fading. Same for
  `_on_wake_error` (line 387-388) and the wake-start error at line 358-359.
  Pre-empts the obvious "this should be a libnotify popup" objection: the OSD
  is already the canonical surface for everything voice-flow related, and
  routing through libnotify duplicates Plasma's own per-action notifications.

- **Localize the history-window title.** `src/korder/ui/main_window.py:197`
  hardcodes `"Korder — transcript history"` and `src/korder/ui/settings_dialog.py:142`
  hardcodes `"Korder Settings"`. The whole rest of the chrome uses `t(...)`
  including the tray menu. Add `window_title_history` / `window_title_settings`
  i18n keys; matters for a Polish user since every other surface is translated
  and these are the only window titles they see.

- **Replace the `Start dictating` / `Stop` history-window button labels with
  i18n + an icon.** `src/korder/ui/main_window.py:318` and `:773` set raw
  English strings. Reuse the tray-state idea: when recording, the button reads
  *"Stop"* with a square stop glyph; idle, *"Słucham"* / *"Listen"* with a
  mic glyph. Same i18n + visual contract as the tray. The current text-only
  toggle is the loudest English fragment in an otherwise-Polish UI on a Polish
  locale.

- **Consolidate or remove the `Inject into focused app` checkbox.**
  `src/korder/ui/main_window.py:323-330` exposes the same concept that
  *"start writing" / "stop writing"* (write_mode) already covers via voice,
  and which is also auto-managed by parsed action ops. Two truths sources
  for the same boolean → `_write_mode` is reset to False at session end
  (`main_window.py:598`) but the checkbox state isn't observed anywhere
  (it just gates the inject-attempt branch at line 834). Either remove the
  checkbox (write-mode voice toggle wins) or make it the canonical source
  and drop the voice toggle's main-thread bookkeeping. Today's split is
  exactly the bug factory comments line 596-598 are guarding against.

- **Show a compact accent stripe on the history window's status bar to mirror
  OSD state.** `src/korder/ui/main_window.py:309-310, 333-334` give the
  history window a plain `QLabel` and a `QStatusBar` that say "Listening…",
  "Idle.", etc. Add a 4-pixel coloured strip on the left of the status bar
  using the same `accentForState` mapping the OSD uses — so the history window,
  if it's open during a session, mirrors the OSD's state at a glance.

- **Use a busy/spinner overlay on the OSD while the BenchmarkDialog runs.**
  `src/korder/ui/settings_dialog.py:775-784` opens BenchmarkDialog modally;
  during the run the user has no in-app feedback in the OSD if they triggered
  it from the settings dialog (the dialog itself blocks the settings UI but
  not the rest of the app). At minimum, disable the *"Run benchmark…"*
  button (`bench_btn.setEnabled(False)`) for the duration so a double-click
  doesn't fire two parallel runs. Currently the only protection is the
  modal exec hiding behind another dialog.

## Medium-effort (2-6h)

- **Audit OSD contrast across the four built-in Plasma colour schemes.**
  `src/korder/ui/qml/osd.qml:35-69` derives all colours from `palette.windowText`,
  `palette.window`, `palette.placeholderText`, and `palette.highlight`. The
  background fill is `palette.window` at 86% alpha (`osd.qml:111`); on Breeze
  Light (#EFF0F1) at 86% over a bright wallpaper the contrast against
  `palette.windowText` (#232629) is fine, but the *flux* colour
  `_blend(promptColor, palette.window, 0.45)` lands at ~3.1:1 which is below
  WCAG AA for body text. Run the four stock schemes (Breeze Light, Breeze Dark,
  Breeze High Contrast, Oxygen) through a contrast check, lift the flux blend
  to 0.55–0.60 if it falls under 4.5:1, and consider switching to a
  background-alpha bump (e.g. 0.92) on themes where `palette.window` is light.
  The `feedbackColor` blend at line 56 (`_blend(palette.windowText,
  accentColor, 0.55)`) is itself fine on Breeze Dark but still risky on
  custom user schemes — codify a minimum-contrast clamp using the
  WCAG luminance formula in QML.

- ✅ **~~Reorganise the Settings dialog into fewer tabs and group bands.~~** *Done in `520f307` (path (a)).*
  `src/korder/ui/settings_dialog.py:155-162` adds six tabs in a row:
  *Mic & Whisper, Actions & Output, Spotify, Wake word, Speech, General*.
  At six tabs the row already wraps on a 620-wide dialog (`_init_ui` resize
  line 143) on `0.9` font scaling, and Plasma's HIG calls for KPageWidget
  (sidebar) once tab count crosses 4–5. Either: (a) collapse Spotify into
  *Actions & Output* under a section header so the *Credentials* form sits
  under "Spotify" inside the same scrollable panel; or (b) port to a
  KPageWidget-style left-rail layout (PySide6 has `QPageWidget` via
  KConfigWidgets bindings on Plasma; falls back to a custom QListWidget +
  QStackedWidget). Keep *General* last but rename to *Behaviour* — almost
  every checkbox in there is a behaviour toggle, "General" reads as
  "miscellaneous". User-visible win: settings discovery improves; `Speech`
  and `Wake word` no longer compete with `General` for tab attention.

- **Make the "Most settings take effect after restarting Korder" hint
  actionable.** `src/korder/ui/settings_dialog.py:164-165` shows a flat
  helper label after Apply. Add a `Restart Korder now` button next to it,
  enabled only when at least one restart-required field has changed, that
  spawns a short confirmation and re-execs the process via
  `QProcess.startDetached(sys.executable, sys.argv)`. The list of
  fields-that-actually-need-restart is small — `audio.sample_rate`,
  `whisper.model`, `whisper.n_threads`, `wake.engine`, `wake.phrase`,
  `tts.voice_*` — the rest live-update via the `settings_saved` signal at
  line 138. Today the user has to know *which* fields require restart;
  with the button you can also tag the restart-required fields with a
  small `(restart)` suffix like Plasma's own kcms do.

- **Add an in-app voice-vocabulary cheat sheet.** No file currently exposes
  what phrases Korder responds to (the README is the only source). Add a
  *"Voice commands"* tab (or modal accessible from the tray menu's
  *Settings…* peer) that lists the trigger phrases per action — content
  is already in `src/korder/actions/*` (`feature` agent owns vocabulary
  itself, but rendering the same data into a UI surface is squarely a UI
  concern). Group by category (Media, Spotify, System, Web, Dictation,
  Cancel/End), show triggers in both PL/EN with the example invocations
  from each action's docstring. Fall-back render for actions without
  triggers ("LLM-only intent recognition: speak naturally") so it doesn't
  imply the user must memorise phrasings. New-user learnability is the
  single largest UX gap right now.

- **Render long OSD answers with a maximum height + scroll fade rather than wrap-only.** `src/korder/ui/qml/osd.qml:228` sets
  `Layout.preferredHeight: Math.max(40, promptText.implicitHeight + 16)` —
  the pill grows tall as the conversational answer text expands. A 4-line
  Polish reply about Budapest's history can shove the pill 200 px up over
  any focused window. Cap at ~3 lines (~108 px), enable
  `wrapMode: Text.WordWrap` (already on) plus `maximumLineCount: 3`,
  and add a soft fade on the final line so the user knows there's
  more — paired with the existing TTS read-out the truncation is benign,
  and it stops the OSD from competing with the focused app for vertical
  space. Multi-monitor ramification: the pill's `Screen.width` clamp at
  line 26 binds to whichever screen Qt chose, but the layer-shell anchor
  doesn't currently pin a screen — a separate fix would be to expose
  `Screen.virtualX/Y` per the screen the user's pointer is on.

- **Animate the locked-prefix → flux boundary instead of swapping it instantly.** `src/korder/ui/qml/osd.qml:271-293` rebuilds the RichText
  string each time `osdState.prompt` / `osdState.flux` changes. Whisper
  partials revise the flux several times a second, so the colour boundary
  jumps abruptly each revision. Wrap `promptText.text` updates in a short
  cross-fade (e.g. opacity to 0.85 over 80 ms then back to 1.0) when the
  *length* of the prompt portion changes; or, more robustly, keep two
  stacked Text nodes — one rendering only the locked range, one the
  flux — and animate `font.color` of the flux range toward `promptColor`
  when it gets locked. Subtle, but on a 250 ms partial throttle
  (`main_window.py:470`) the user reads ~4 partial revisions per second and
  the current hard swap reads as flicker on long words.

- **Replace the QPlainTextEdit transcript view with a wrapping list of
  per-utterance entries with timestamps.**
  `src/korder/ui/main_window.py:312-315` currently appends each commit as
  a plain line into a single read-only text edit. The history loses
  structure: you can't tell which utterance was a command vs a write,
  whether it succeeded, or when. Swap for a `QListWidget` (or
  `QTreeView` on a small model) with rows like
  `[14:32:08] [✓ command] "spotify play darude sandstorm"`,
  `[14:32:14] [— dictation] "thanks for the help"`. The
  `command_executed` / `cancel_requested` / `pending_action` signals in
  `_InjectWorker` already emit the metadata; the UI just doesn't render
  it. Pre-empts "this is a feature request": the data is already produced
  and discarded — surfacing it costs ~50 lines.

## Larger initiatives (1d+)

- **Adopt KDE Frameworks (KSvg / KConfigWidgets / KMessageWidget) where
  hand-rolled equivalents exist.** Today's settings dialog hand-rolls a
  `_InfoBanner` (`src/korder/ui/settings_dialog.py:49-105`) that reproduces
  KMessageWidget's information-tint behaviour, and `_hint_label`
  (`:108-122`) reaches into `QPalette` to access placeholder colour. Both
  exist in upstream KF6. A dependency on PyQt-KF6 bindings is heavy for
  a single-user app, but the payoff is automatic theme + colour-scheme
  re-rendering on `kdeglobals` change (today's `changeEvent` plumbing
  re-tints once), proper KMessageWidget animation, and KPageWidget for
  the sidebar settings layout above. Pre-empts "more deps to install":
  every Plasma 6 box already has the binaries; the binding wheels are
  small; we keep the existing widgets as fallback.

- **Build a high-DPI-aware tray icon set with proper Plasma-monochrome
  glyph treatment.** The current tray SVG (`src/korder/ui/icons/tray.svg`,
  6 lines, fixed 22×22 viewBox with 6 rounded rectangles) is fine at
  16/22/24 px but coarse at 32+ where the equalizer bars become rectangles
  with visible aliasing. The recolour pass in `_tray_icon` at
  `src/korder/app.py:515-538` paints `currentColor` via
  `CompositionMode_SourceIn` which works, but Plasma's tray convention is
  *monochrome paths designed at 22×22, painted with the system text colour
  for idle and a single accent for active states*. Recommendations:
  (a) re-cut the SVG with snap-to-22 path data, plus a separate 16×16
  variant for size-rendering at small sizes; (b) drop the multi-size
  pixmap loop in favour of `QIcon::addFile(":/scalable/...", QSize(0,0),
  QIcon::Normal)` and let Qt's SVG icon engine handle scaling — more
  consistent across HiDPI fractional scales (1.25, 1.5); (c) consider three
  glyph variants (idle waveform, idle filled circle for wake-listening,
  filled mic for dictating) instead of recolouring the same shape, since
  tray icons in Plasma's clock / network / battery applets all use shape
  changes for state, not colour. Pre-empts "the colour change is enough":
  on the BreezeAccentColor=blue theme today, both `idle` (theme text) and
  `wake_listening` (`#66b3f2`) read as similar blues against a dark panel.

- **Provide a single Korder branded SVG with proper iconography for both
  app launcher and tray, replacing the same waveform glyph at every
  surface.** `assets/icon.svg` (referenced from README) is the launcher
  icon; the tray uses a different SVG (`src/korder/ui/icons/tray.svg`).
  They are visually unrelated. Plasma users expect the launcher icon and
  the tray icon to share family — the tray version stripped to monochrome,
  the launcher full-colour. Today's launcher icon also doesn't include
  Korder's identity — it's a generic mic. A short identity pass: pick one
  primary mark (e.g. mic-with-waveform-tail), produce three SVGs (full
  colour 256×256, monochrome 22×22, monochrome 16×16) and wire them
  through `_install_tray_icon_in_theme` (`src/korder/app.py:467-478`) plus
  the existing `.desktop` entry. Doubles as the OSD's own branding (tiny
  16×16 mark in the leading section can replace today's pulse-dot when
  the mic is closed and only the post-Done frame is showing — gives the
  user a non-state-bearing identity glyph to anchor on).

- **Full accessibility pass: focus rings, tab order, screen-reader names,
  and font-scale honour.** Multiple problems:
  - The `_OSDState` QObject (`src/korder/ui/osd.py:27-156`) exposes no
    accessible-text annotations to AT-SPI. A user running Orca will hear
    nothing when the OSD updates. QML `Accessible.role` / `Accessible.name`
    bindings on the `bg` Rectangle and the prompt Text are zero-cost.
  - The settings dialog is form-only with `setLabelAlignment` (right-align)
    but no explicit `setBuddy` / mnemonics, so Alt-key navigation through
    the form is broken. Sweep all `addRow("Foo:", widget)` calls and add
    `&` mnemonics on the labels (e.g. `"Mic &gain:"`).
  - Font scaling: every `font.pixelSize` in `osd.qml` is a fixed integer
    (14 for state label line 209, 16 for promptText line 309, 10 for the
    cancel hint line 338). Plasma offers a global font scale; today's OSD
    ignores it. Bind to `Theme.smallFont` / `Theme.defaultFont` from
    KIRIGAMI / Plasma components, or compute scale from
    `Screen.devicePixelRatio` plus the system font's pixelSize.
  - Tab order in the settings dialog isn't explicit — Qt computes it from
    insertion order, which mostly works for the form rows, but the
    benchmark button (line 359-371) sits after the LLM keep-alive spinner
    and ends up reachable only after every other inject field. Add
    `setTabOrder(...)` calls or use `QWidget::setFocusProxy` on group
    boxes.
  - Korder ITSELF needs to pass an accessibility audit (the user's own
    instruction) — Qt's accessibility plugin (`qt6-virtualkeyboard` /
    `accessibility-qt5-bridge`) needs to be a hard runtime requirement,
    not implicit; document it in the README's *Required* dependencies.

- **Multi-monitor and per-screen anchoring for the OSD.** The
  layer-shell scene in `src/korder/ui/qml/osd.qml:96-101` anchors to
  `LayerShell.Window.AnchorBottom` without specifying which screen.
  org.kde.layershell defaults to the focused output, so on a multi-monitor
  setup with the focused window on a side monitor the OSD pops on the
  primary, away from the user's eye. Accept a screen handle on
  `OSDWindow.__init__`, query the focused output via KWindowSystem
  (`KX11Extras.activeWindow().screen()` on X11; `kwin --output-info` /
  `wlr-randr` parsing on Wayland), and re-emit when the user moves their
  pointer/focus to another screen. Concretely: a separate `PointerWatcher`
  in `src/korder/app.py` that listens for monitor changes and calls
  `OSDWindow.set_screen(idx)`. Hard problem on Wayland (no global pointer
  position API by spec) — pragmatic compromise is to sample the screen
  containing the focused window at the moment the mic opens and pin the
  OSD there for the session.

## Out of scope / not worth doing

- **Adding desktop notifications for OSD state transitions.** Plasma's
  notification stream is already noisy; doubling every OSD frame onto
  KNotifications would be hostile. The OSD itself is the intended
  notification surface — it's just that *errors* leak past it
  (covered above as "surface inject failures in the OSD").

- **Replacing the QML pill with a Plasma applet / panel widget.** The
  layer-shell overlay is the right primitive for "non-focus-stealing
  full-screen overlay"; reframing as a panel applet would either pin
  the OSD into a tiny corner of one panel or duplicate today's
  layer-shell anchoring inside a Plasmoid. The README explicitly takes
  the layer-shell route for good reasons (no focus stealing, blur
  inheritance) — overhauling that is wasted effort.

- **Adding light/dark mode auto-toggle to the OSD beyond what the
  Plasma palette already drives.** All OSD colours are derived from
  `palette.window` / `palette.windowText` (`osd.qml:35-44`); the
  `SystemPalette { colorGroup: SystemPalette.Active }` at line 103
  re-evaluates when the user switches theme. The only manual colour
  values (the per-state accents at lines 62-66) are tuned to be legible
  on both light and dark — they could be perfected but the cost-benefit
  doesn't hit even the quick-win bar.

- **Drag-to-reposition the OSD pill.** Layer-shell overlay + bottom-anchor
  is the right default. Users who hate the bottom can ask; until then,
  exposing a draggable handle adds a discoverability problem (visible
  drag affordance) that only fires for power users.

- **Custom QSS/QML theme for the settings dialog.** The current dialog
  uses native widgets (`QFormLayout`, `QGroupBox`, `QTabWidget`) which
  inherit Breeze automatically. A custom QSS sheet would either fight
  the platform style or add maintenance debt for marginal aesthetics.
  The *contents* (layout grouping, mnemonics, KPageWidget swap) are
  worth doing; the *chrome* is fine.

- **Animated transitions between tabs in the settings dialog.** Native
  QTabWidget tab switching is instantaneous and that's correct for a
  config panel — slide/cross-fade animations there feel like consumer-
  app cosplay. The OSD's animations are load-bearing (state feedback);
  the settings dialog's would be ornament.
