#!/usr/bin/env bash
# One-time setup so ydotool can synthesize input events into the focused app.
# Grants the 'input' group access to /dev/uinput, ensures the module loads at
# boot, and adds the current user to the group. Re-run is safe (idempotent).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Re-running with sudo..." >&2
    exec sudo -E bash "$0" "$@"
fi

USER_NAME="${SUDO_USER:-$USER}"
RULE_FILE="/etc/udev/rules.d/60-uinput.rules"

echo "uinput" > /etc/modules-load.d/uinput.conf
modprobe uinput || true

getent group input >/dev/null || groupadd -r input
usermod -aG input "$USER_NAME"

cat > "$RULE_FILE" <<'EOF'
KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
EOF

udevadm control --reload-rules
udevadm trigger /dev/uinput || true

cat <<EOF

Setup complete. Next steps:
  1. Log out and back in (or reboot) so '$USER_NAME' picks up the 'input' group.
     Verify with:  groups | grep -q input && echo OK
  2. Start the ydotoold daemon. Two options:
       a) systemctl --user enable --now ydotool       # if a unit ships with the package
       b) ydotoold &                                  # ad-hoc, for testing
  3. Smoke test:
       ydotool type -- "hello from ydotool"
     should type into whatever window currently has focus.
EOF
