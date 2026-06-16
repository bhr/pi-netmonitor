#!/usr/bin/env bash
# setup.sh — idempotent installer for the netmon Pi monitor.
# Run on the Pi with: sudo bash setup.sh
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
    echo "setup.sh: must run as root (use sudo)" >&2
    exit 1
fi

echo "==> Installing apt packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl ca-certificates gnupg jq python3 \
    msmtp msmtp-mta iputils-ping coreutils

echo "==> Installing Ookla speedtest CLI"
if ! command -v speedtest >/dev/null 2>&1; then
    curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash
    DEBIAN_FRONTEND=noninteractive apt-get install -y speedtest
else
    echo "    speedtest already installed; skipping repo step"
fi

echo "==> Creating netmon system user"
if ! id netmon >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /var/lib/netmon \
        --shell /usr/sbin/nologin netmon
fi

echo "==> Creating directories"
install -d -m 0755 -o root   -g root   /opt/netmon /opt/netmon/bin
install -d -m 0750 -o root   -g netmon /etc/netmon
install -d -m 0750 -o netmon -g netmon /var/lib/netmon \
                                       /var/lib/netmon/raw \
                                       /var/lib/netmon/aggregates
install -d -m 0750 -o netmon -g netmon /var/log/netmon

echo "==> Installing scripts to /opt/netmon/bin"
install -m 0755 -o root -g root "$SRC/bin/run_speedtest.sh"      /opt/netmon/bin/run_speedtest.sh
install -m 0755 -o root -g root "$SRC/bin/aggregate_daily.py"    /opt/netmon/bin/aggregate_daily.py
install -m 0755 -o root -g root "$SRC/bin/aggregate_trailing.py" /opt/netmon/bin/aggregate_trailing.py
install -m 0755 -o root -g root "$SRC/bin/send_daily_report.py"  /opt/netmon/bin/send_daily_report.py

echo "==> Installing config (only on first install)"
if [[ ! -e /etc/netmon/netmon.conf ]]; then
    install -m 0640 -o root -g netmon "$SRC/etc/netmon.conf.template" /etc/netmon/netmon.conf
    echo "    wrote /etc/netmon/netmon.conf — edit it to set SMTP credentials and recipient"
else
    echo "    /etc/netmon/netmon.conf already exists; not overwriting"
fi

echo "==> Accepting Ookla EULA as netmon user"
sudo -u netmon -- speedtest --accept-license --accept-gdpr --format=json >/dev/null 2>&1 || \
    echo "    warning: initial speedtest probe failed; the timer will retry"

echo "==> Generating ~netmon/.msmtprc from netmon.conf"
SMTP_HOST=""; SMTP_PORT=""; SMTP_USER=""; SMTP_PASS=""; SMTP_FROM=""
# shellcheck disable=SC1091
set -a; source /etc/netmon/netmon.conf; set +a
MSMTPRC=/var/lib/netmon/.msmtprc
if [[ -z "${SMTP_PASS:-}" ]]; then
    echo "    !! SMTP_PASS is empty in /etc/netmon/netmon.conf — $MSMTPRC NOT written"
    echo "    !! Edit the conf and re-run: sudo bash $0"
elif [[ "$SMTP_PASS" == REPLACE_* ]]; then
    echo "    !! SMTP_PASS still has placeholder value 'REPLACE_*' — $MSMTPRC NOT written"
    echo "    !! Edit /etc/netmon/netmon.conf (set a real password) and re-run: sudo bash $0"
else
    umask 077
    cat > "$MSMTPRC" <<EOF
defaults
auth           on
tls            on
tls_starttls   on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/netmon/msmtp.log

account        netmon
host           ${SMTP_HOST}
port           ${SMTP_PORT}
from           ${SMTP_FROM:-$SMTP_USER}
user           ${SMTP_USER}
password       ${SMTP_PASS}

account default : netmon
EOF
    chown netmon:netmon "$MSMTPRC"
    chmod 0600 "$MSMTPRC"
    touch /var/log/netmon/msmtp.log
    chown netmon:netmon /var/log/netmon/msmtp.log
    chmod 0640 /var/log/netmon/msmtp.log
    # Remove any stale /etc/msmtprc that an earlier version of this script wrote;
    # msmtp would otherwise read it (or fail to read it because of mode 0600 root:root).
    if [[ -e /etc/msmtprc ]]; then
        rm -f /etc/msmtprc
        echo "    removed stale /etc/msmtprc (msmtp now reads $MSMTPRC)"
    fi
    echo "    wrote $MSMTPRC (host=${SMTP_HOST}, user=${SMTP_USER})"
fi

echo "==> Installing systemd units"
install -m 0644 -o root -g root "$SRC/systemd/netmon-speedtest.service" /etc/systemd/system/
install -m 0644 -o root -g root "$SRC/systemd/netmon-speedtest.timer"   /etc/systemd/system/
install -m 0644 -o root -g root "$SRC/systemd/netmon-daily.service"     /etc/systemd/system/
install -m 0644 -o root -g root "$SRC/systemd/netmon-daily.timer"       /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now netmon-speedtest.timer netmon-daily.timer

cat <<MSG

==============================================================================
netmon installed.

  Scripts:    /opt/netmon/bin/
  Data:       /var/lib/netmon/{raw,aggregates}/
  Config:     /etc/netmon/netmon.conf
  Logs:       journalctl -u netmon-speedtest -f
              journalctl -u netmon-daily -n 100

Next steps:
  1. If SMTP_PASS is still a placeholder, edit /etc/netmon/netmon.conf and
     re-run this script to write /etc/msmtprc.
  2. Verify a run:    sudo systemctl start netmon-speedtest.service
                      tail -n 1 /var/lib/netmon/raw/speedtests-$(date -u +%Y-%m).csv
  3. Force a daily:   sudo systemctl start netmon-daily.service

Timers active:
MSG
systemctl list-timers --all 'netmon-*' --no-pager 2>/dev/null || true
