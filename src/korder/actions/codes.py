"""Linux evdev keycode constants. Source of truth for keycodes used across
the inject backend and action definitions."""

KEY_ESCAPE = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LCTRL = 29
KEY_A = 30
KEY_LSHIFT = 42
KEY_Z = 44
KEY_V = 47
KEY_LALT = 56
KEY_HOME = 102
KEY_END = 107
KEY_DELETE = 111
KEY_LMETA = 125

# Media keys — KDE Plasma (and most desktop environments) listens for these
# at the kernel input layer and routes them to the active MPRIS player /
# PipeWire mixer. No external CLI needed.
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
KEY_NEXTSONG = 163
KEY_PLAYPAUSE = 164
KEY_PREVIOUSSONG = 165
KEY_STOPCD = 166
