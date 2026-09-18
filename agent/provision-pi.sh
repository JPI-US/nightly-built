#!/usr/bin/env bash
# provision-pi.sh <DEVICE_ID>
#
# Everything a fresh Raspberry Pi needs to become a capture node, minus the
# parts that genuinely require a human (Imager settings, Tailscale auth, the
# secrets). Idempotent - safe to re-run after a partial failure.
#
#   ./provision-pi.sh 9000
#
# Encodes the things that cost hours the first time round: cargo on tmpfs,
# ModemManager stealing the serial port, netplan discarding the Wi-Fi profile,
# NetworkManager power save, unattended reboots mid-capture.
set -euo pipefail

DEVICE_ID="${1:-}"
SCHEDULER_DIR="${NB_SCHEDULER_DIR:-/opt/scheduler}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die()  { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }
ok()   { echo "  ok: $*"; }

[ -n "$DEVICE_ID" ] || die "usage: $0 <DEVICE_ID>    e.g. $0 9000"
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root - it sudos where it needs to."

step "1. System packages"
sudo apt-get update -qq
# git/curl/unzip for this script; the rest are ESP-IDF build deps, harmless if
# you never build on the Pi and needed the moment you try.
sudo apt-get install -y -qq git curl unzip tmux python3 \
    ca-certificates >/dev/null
ok "base packages"

step "2. Serial access"
if ! id -nG "$USER" | grep -qw dialout; then
    sudo usermod -aG dialout "$USER"
    echo "  added $USER to dialout - REBOOT REQUIRED before the port is usable"
else
    ok "$USER already in dialout"
fi
# Both of these grab USB serial devices out from under espflash. Neither is
# installed on Lite by default, but they arrive with desktop-ish metapackages.
for pkg in modemmanager brltty; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        sudo apt-get purge -y -qq "$pkg" >/dev/null
        echo "  purged $pkg (it steals /dev/ttyACM*)"
    fi
done
ok "no serial-grabbing daemons"

step "3. Wi-Fi power save off"
# A NetworkManager drop-in, NOT `nmcli connection modify`: the Imager writes
# Wi-Fi into netplan, and anything set on a netplan-managed connection is
# discarded the next time netplan regenerates. This applies to every profile
# and survives that.
if [ ! -f /etc/NetworkManager/conf.d/wifi-powersave-off.conf ]; then
    sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
    sudo systemctl restart NetworkManager
    sleep 5
fi
ok "power save: $(iw wlan0 get power_save 2>/dev/null | awk '{print $NF}' || echo 'n/a')"

step "4. No unattended reboots"
# An automatic reboot mid-capture is the main way an unattended box silently
# loses a night of data.
if ! grep -qs 'Automatic-Reboot "false"' /etc/apt/apt.conf.d/52-no-auto-reboot; then
    sudo tee /etc/apt/apt.conf.d/52-no-auto-reboot >/dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
EOF
fi
ok "automatic reboot disabled"

step "5. Keep cargo off tmpfs"
# /tmp is a tmpfs sized from RAM - 453 MB on a 1 GB Pi. Any cargo build that
# vendors C sources fills it and dies with what looks like a compiler error.
if ! grep -qs 'TMPDIR=/var/tmp' "$HOME/.bashrc"; then
    echo 'export TMPDIR=/var/tmp' >> "$HOME/.bashrc"
fi
ok "TMPDIR -> /var/tmp in .bashrc"

step "6. espflash"
if command -v espflash >/dev/null; then
    ok "already installed: $(espflash --version)"
else
    arch="$(uname -m)"
    [ "$arch" = "aarch64" ] || die "expected aarch64, got $arch - use the 64-bit Pi OS image."
    tmp="$(mktemp -d -p /var/tmp)"
    curl -fsSL -o "$tmp/e.zip" \
        https://github.com/esp-rs/espflash/releases/latest/download/espflash-aarch64-unknown-linux-gnu.zip
    ( cd "$tmp" && unzip -oq e.zip )
    sudo install -m755 "$(find "$tmp" -maxdepth 2 -type f -name espflash | head -1)" /usr/local/bin/
    rm -rf "$tmp"
    ok "installed: $(espflash --version)"
fi

step "7. Scheduler layout"
sudo mkdir -p "$SCHEDULER_DIR"/{logs,public/reports,nightly}
sudo chown -R "$USER:$USER" "$SCHEDULER_DIR"
ok "$SCHEDULER_DIR"

step "8. Report pipeline"
# Lives in the other repo; the supervisor shells out to it at the 23:00
# rollover. Without it you capture beautifully and analyse nothing.
if [ ! -f "$SCHEDULER_DIR/report_and_retain.py" ]; then
    rm -rf "$SCHEDULER_DIR/dps"
    git clone -q --depth 1 \
        https://github.com/JPI-US/data-processor-scheduler.git "$SCHEDULER_DIR/dps"
    cp "$SCHEDULER_DIR"/dps/{process_log.py,report_and_retain.py,signatures.py,axum_nightly_analysis_prompt.md} \
       "$SCHEDULER_DIR/"
fi
python3 -c "import sys; sys.path.insert(0,'$SCHEDULER_DIR'); import report_and_retain" \
    && ok "report pipeline imports cleanly"

step "9. Serial device"
PORT="$(ls /dev/serial/by-id/* 2>/dev/null | head -1 || true)"
if [ -n "$PORT" ]; then
    ok "$PORT"
else
    echo "  none found. Either the board isn't plugged in, or - far more likely -"
    echo "  the USB cable is charge-only. That symptom cost us hours: the board"
    echo "  lights up and is completely invisible to the host. Try a cable you"
    echo "  have actually moved files with, then re-run this script."
fi

step "10. .env"
ENV_FILE="$SCHEDULER_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    ok "already exists, left alone"
else
    sed -e "s|^NB_DEVICE_ID=.*|NB_DEVICE_ID=$DEVICE_ID|" \
        -e "s|^AXUM_PORT=.*|AXUM_PORT=${PORT:-/dev/serial/by-id/REPLACE_ME}|" \
        "$HERE/env.tower-9001.pi.example" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "wrote $ENV_FILE (NB_DEVICE_ID=$DEVICE_ID)"
fi

step "11. Power"
throttled="$(vcgencmd get_throttled 2>/dev/null || echo 'throttled=?')"
echo "  $throttled   (0x0 = clean; bit 16 set = it has browned out since boot)"
echo "  $(cat /proc/device-tree/model 2>/dev/null | tr -d '\0')"

cat <<EOF

========================================================================
Provisioned for tower $DEVICE_ID.

Still manual, because each needs a human or a secret:

  1. Tailscale
       curl -fsSL https://tailscale.com/install.sh | sh
       sudo tailscale up --ssh --advertise-tags=tag:axum
     The tailnet policy already covers tag:axum - no ACL change needed.
     Then disable key expiry for this node in the admin console.

  2. Secrets in $ENV_FILE
       NB_GITHUB_TOKEN    fine-grained PAT, Actions: Read on nightly-built
       ANTHROPIC_API_KEY  same key the other nodes use
     Confirm ANTHROPIC_MODEL=claude-sonnet-5 and AXUM_PORT is a real path.

  3. Services (only once the .env is filled in)
       sudo cp $HERE/systemd/axum-*.service /etc/systemd/system/
       sudo sed -i "s/%USER%/\$USER/" /etc/systemd/system/axum-*.service
       sudo systemctl daemon-reload
       sudo systemctl enable --now axum-capture
       journalctl -u axum-capture -f

  4. Reboot if this run added you to dialout.
========================================================================
EOF
