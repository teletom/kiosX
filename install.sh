#!/usr/bin/env bash
# =============================================================================
# kiosX - Idempotentes Installations-Skript fuer Ubuntu Server (minimized)
# =============================================================================
# Verwendung:
#   sudo bash /opt/kiosX/install.sh
#   sudo bash /opt/kiosX/install.sh --user kiosk
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/etc/kiosk"
CONFIG_FILE="${CONFIG_DIR}/config.json"
SERVICE_NAME="kiosk-ui.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
KIOSK_PORT="${KIOSX_PORT:-8080}"
KIOSK_USER="${KIOSK_USER:-}"

log()  { printf '\033[1;34m[kiosX]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[kiosX]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[kiosX WARN]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[kiosX ERROR]\033[0m %s\n' "$*" >&2; }

usage() {
    cat <<EOF
Verwendung: sudo bash $0 [--user <benutzername>] [--port <port>]

Optionen:
  --user <name>   Linux-Benutzer fuer Kiosk-Session und Web-UI
                  (Standard: \$SUDO_USER oder 'kiosk')
  --port <port>   Port der Flask-Weboberflaeche (Standard: 8080)
  -h, --help      Diese Hilfe anzeigen
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            KIOSK_USER="${2:-}"
            shift 2
            ;;
        --port)
            KIOSK_PORT="${2:-8080}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            err "Unbekannte Option: $1"
            usage
            exit 1
            ;;
    esac
done

# 1. Root-Rechte pruefen
if [[ "${EUID}" -ne 0 ]]; then
    err "Bitte mit Root-Rechten ausfuehren: sudo bash $0"
    exit 1
fi

# 2. Kiosk-Benutzer bestimmen bzw. anlegen
if [[ -z "${KIOSK_USER}" ]]; then
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        KIOSK_USER="${SUDO_USER}"
    else
        KIOSK_USER="kiosk"
    fi
fi

if ! id "${KIOSK_USER}" &>/dev/null; then
    log "Lege Kiosk-Benutzer '${KIOSK_USER}' an..."
    useradd -m -s /bin/bash "${KIOSK_USER}"
fi

KIOSK_HOME="$(getent passwd "${KIOSK_USER}" | cut -d: -f6)"
KIOSK_UID="$(id -u "${KIOSK_USER}")"
KIOSK_GID="$(id -g "${KIOSK_USER}")"

if [[ -z "${KIOSK_HOME}" || ! -d "${KIOSK_HOME}" ]]; then
    err "Home-Verzeichnis fuer Benutzer '${KIOSK_USER}' nicht gefunden."
    exit 1
fi

log "Installiere kiosX aus ${REPO_DIR} fuer Benutzer '${KIOSK_USER}' (UID=${KIOSK_UID}, Home=${KIOSK_HOME})"

# 3. Systempakete installieren (vollstaendig fuer minimales Ubuntu Server)
log "Pruefe Paketquellen (inkl. universe) und installiere Systempakete..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Sicherstellen, dass das Ubuntu 'universe'-Repository aktiviert ist
if ! apt-cache show openbox &>/dev/null; then
    log "Aktiviere Ubuntu 'universe'-Repository..."
    if [[ -f /etc/apt/sources.list.d/ubuntu.sources ]]; then
        sed -i '/^Components:/ { /universe/! s/$/ universe/ }' /etc/apt/sources.list.d/ubuntu.sources
    elif command -v add-apt-repository &>/dev/null; then
        add-apt-repository -y universe
    else
        apt-get install -y --no-install-recommends software-properties-common
        add-apt-repository -y universe
    fi
    apt-get update -qq
fi

BASE_PACKAGES=(
    ca-certificates
    dbus-user-session
    libpam-systemd
    xorg
    xinit
    xauth
    xserver-xorg-input-libinput
    openbox
    x11-xserver-utils
    xinput
    unclutter
    pipewire
    wireplumber
    pipewire-pulse
    pipewire-alsa
    pulseaudio-utils
    alsa-utils
    python3
    python3-flask
    jq
    procps
    fonts-dejavu-core
)

# Hilfsfunktion fuer robuste apt-Installation (faengt kurzzeitige Mirror-Sync-404s ab)
apt_install_retry() {
    local attempt=1
    local max_attempts=3
    while (( attempt <= max_attempts )); do
        if apt-get install -y -o Acquire::Retries=3 "$@"; then
            return 0
        fi
        if (( attempt < max_attempts )); then
            warn "apt-get install fehlgeschlagen (Versuch ${attempt}/${max_attempts}) – aktualisiere Paketlisten und wiederhole..."
            sleep 3
            apt-get update -qq || true
        fi
        attempt=$((attempt + 1))
    done
    return 1
}

apt_install_retry --no-install-recommends "${BASE_PACKAGES[@]}"

# Chromium installieren (Ubuntu verwendet haeufig chromium-browser/Snap, Debian natives chromium)
install_chromium() {
    if command -v chromium &>/dev/null || command -v chromium-browser &>/dev/null || [[ -x /snap/bin/chromium ]]; then
        ok "Chromium ist bereits installiert."
        return 0
    fi

    local cand
    cand="$(apt-cache policy chromium 2>/dev/null | awk '/Candidate:/ {print $2}')"
    if [[ -n "${cand}" && "${cand}" != "(none)" ]]; then
        log "Installiere natives apt-Paket 'chromium'..."
        apt_install_retry chromium
        return 0
    fi

    log "Kein natives apt-Paket 'chromium' verfuegbar; installiere ueber Snap / 'chromium-browser'..."
    if ! command -v snap &>/dev/null; then
        log "Installiere snapd..."
        apt_install_retry snapd
    fi
    systemctl enable --now snapd.socket snapd.service || true
    log "Warte auf Initialisierung von snapd..."
    snap wait system seed.loaded || sleep 5

    if apt-cache show chromium-browser &>/dev/null; then
        apt_install_retry chromium-browser
    else
        snap install chromium
    fi
}

install_chromium

# 4. Gruppenmitgliedschaften und systemd-Linger fuer PipeWire/X11 einrichten
log "Konfiguriere Gruppen fuer '${KIOSK_USER}'..."
for grp in audio video input render tty; do
    if getent group "${grp}" &>/dev/null; then
        usermod -aG "${grp}" "${KIOSK_USER}"
    fi
done

# Linger aktivieren, damit /run/user/<UID> und PipeWire User-Services bereitstehen
if command -v loginctl &>/dev/null; then
    loginctl enable-linger "${KIOSK_USER}" || warn "loginctl enable-linger fehlgeschlagen (ignoriert)."
fi

# PipeWire User-Services & Sockets fuer den Kiosk-Benutzer aktivieren
USER_SYSTEMD_DIR="${KIOSK_HOME}/.config/systemd/user"
mkdir -p "${USER_SYSTEMD_DIR}/default.target.wants" "${USER_SYSTEMD_DIR}/sockets.target.wants"
for unit in pipewire.service pipewire-pulse.service wireplumber.service; do
    if [[ -f "/usr/lib/systemd/user/${unit}" ]]; then
        ln -sfn "/usr/lib/systemd/user/${unit}" "${USER_SYSTEMD_DIR}/default.target.wants/${unit}"
    fi
done
for sock in pipewire.socket pipewire-pulse.socket; do
    if [[ -f "/usr/lib/systemd/user/${sock}" ]]; then
        ln -sfn "/usr/lib/systemd/user/${sock}" "${USER_SYSTEMD_DIR}/sockets.target.wants/${sock}"
    fi
done
chown -R "${KIOSK_USER}:${KIOSK_USER}" "${KIOSK_HOME}/.config"

# 5. Skript-Berechtigungen im Repository setzen
chmod 0755 "${REPO_DIR}/install.sh" "${REPO_DIR}/kiosk-apply.sh"
if [[ -f "${REPO_DIR}/app.py" ]]; then
    chmod 0644 "${REPO_DIR}/app.py"
fi

# 6. Persistente Konfiguration unter /etc/kiosk/config.json anlegen
log "Richte Konfigurationsverzeichnis ${CONFIG_DIR} ein..."
mkdir -p "${CONFIG_DIR}"

if [[ ! -f "${CONFIG_FILE}" ]] || ! jq empty "${CONFIG_FILE}" &>/dev/null; then
    if [[ -f "${CONFIG_FILE}" ]]; then
        cp "${CONFIG_FILE}" "${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
        warn "Bestehende ungueltige ${CONFIG_FILE} gesichert und neu erstellt."
    fi
    cat > "${CONFIG_FILE}" <<'EOF'
{
  "url": "https://example.com",
  "display": {
    "primary_output": "",
    "resolution": "auto",
    "rotation": "normal",
    "secondary_output": "",
    "secondary_mode": "off"
  },
  "touch": {
    "device": "",
    "rotation": "0"
  },
  "audio": {
    "card": "",
    "profile": "",
    "volume": 75
  }
}
EOF
    ok "Standard-Konfiguration unter ${CONFIG_FILE} erstellt."
else
    ok "Bestehende Konfiguration unter ${CONFIG_FILE} bleibt erhalten."
fi

# Schreibrechte fuer den Kiosk-User (inkl. atomarem Rename im Verzeichnis /etc/kiosk)
chown -R "${KIOSK_USER}:${KIOSK_USER}" "${CONFIG_DIR}"
chmod 0775 "${CONFIG_DIR}"
chmod 0664 "${CONFIG_FILE}"

# 7. Autologin auf tty1 einrichten
log "Konfiguriere Autologin auf tty1 fuer '${KIOSK_USER}'..."
GETTY_DROPIN_DIR="/etc/systemd/system/getty@tty1.service.d"
mkdir -p "${GETTY_DROPIN_DIR}"
cat > "${GETTY_DROPIN_DIR}/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF

# 8. .bash_profile (automatischer startx-Start auf tty1) und .xinitrc einrichten
log "Konfiguriere automatischen X11-Start in ${KIOSK_HOME}/.bash_profile..."
BASH_PROFILE="${KIOSK_HOME}/.bash_profile"
touch "${BASH_PROFILE}"

# Bestehenden kiosX-Block entfernen (fuer Idempotenz)
tmp_profile="$(mktemp)"
awk '
    /^# >>> kiosX startx >>>$/ { skip=1; next }
    /^# <<< kiosX startx <<<$/ { skip=0; next }
    !skip { print }
' "${BASH_PROFILE}" > "${tmp_profile}"

cat >> "${tmp_profile}" <<'EOF'
# >>> kiosX startx >>>
if [ -f "$HOME/.bashrc" ]; then
    . "$HOME/.bashrc"
fi

if [ -z "${DISPLAY:-}" ] && [ "$(tty 2>/dev/null || true)" = "/dev/tty1" ]; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    exec startx
fi
# <<< kiosX startx <<<
EOF

mv "${tmp_profile}" "${BASH_PROFILE}"
chown "${KIOSK_USER}:${KIOSK_USER}" "${BASH_PROFILE}"
chmod 0644 "${BASH_PROFILE}"

# .xinitrc fuer Openbox-Session
XINITRC="${KIOSK_HOME}/.xinitrc"
cat > "${XINITRC}" <<'EOF'
#!/usr/bin/env bash
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# PipeWire-Session sicherstellen (falls User-systemd nicht bereits aktiv ist)
if command -v systemctl &>/dev/null; then
    systemctl --user start pipewire.service pipewire-pulse.service wireplumber.service 2>/dev/null || true
fi

exec openbox-session
EOF
chown "${KIOSK_USER}:${KIOSK_USER}" "${XINITRC}"
chmod 0755 "${XINITRC}"

# 9. Openbox Autostart konfigurieren (kiosk-apply.sh + Chromium Watchdog-Loop)
log "Konfiguriere Openbox Autostart unter ${KIOSK_HOME}/.config/openbox/autostart..."
OPENBOX_DIR="${KIOSK_HOME}/.config/openbox"
mkdir -p "${OPENBOX_DIR}"

cat > "${OPENBOX_DIR}/autostart" <<EOF
#!/usr/bin/env bash
# kiosX Openbox Autostart

export DISPLAY="\${DISPLAY:-:0}"
export XAUTHORITY="\${XAUTHORITY:-\$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}"

# Lokalen Zugriff fuer den Kiosk-Benutzer (z. B. systemd kiosk-ui.service) erlauben
xhost +SI:localuser:"\$(id -un)" >/dev/null 2>&1 || true

# Bildschirmschoner und DPMS dauerhaft deaktivieren
xset s off >/dev/null 2>&1 || true
xset s noblank >/dev/null 2>&1 || true
xset -dpms >/dev/null 2>&1 || true

# Mauszeiger bei Inaktivitaet ausblenden
unclutter -idle 2 -root >/dev/null 2>&1 &

# Gespeicherte Hardware-Konfiguration (Display, Touch, Audio) anwenden
"${REPO_DIR}/kiosk-apply.sh" >/tmp/kiosx-apply.log 2>&1 || true

# Chromium im Kiosk-Modus mit Watchdog-Endlosschleife starten
(
    resolve_chromium() {
        for bin in chromium chromium-browser /snap/bin/chromium; do
            if command -v "\${bin}" >/dev/null 2>&1; then
                command -v "\${bin}"
                return 0
            elif [[ -x "\${bin}" ]]; then
                echo "\${bin}"
                return 0
            fi
        done
        return 1
    }

    while true; do
        CHROME_BIN="\$(resolve_chromium || true)"
        if [[ -z "\${CHROME_BIN}" ]]; then
            sleep 5
            continue
        fi

        KIOSK_URL="\$(jq -r '.url // "https://example.com"' "${CONFIG_FILE}" 2>/dev/null || echo "https://example.com")"
        if [[ -z "\${KIOSK_URL}" || "\${KIOSK_URL}" == "null" ]]; then
            KIOSK_URL="https://example.com"
        fi

        # Profil-Pfad kompatibel zu apt- und Snap-Chromium waehlen
        if [[ "\${CHROME_BIN}" == *"/snap/"* ]] || readlink -f "\${CHROME_BIN}" 2>/dev/null | grep -q "/snap/"; then
            PROFILE_DIR="\$HOME/snap/chromium/common/kiosx-profile"
        else
            PROFILE_DIR="\$HOME/.config/kiosx-chromium"
        fi
        mkdir -p "\${PROFILE_DIR}/Default"

        # Absturz-Dialoge ("Seiten wiederherstellen") verhindern
        PREFS_FILE="\${PROFILE_DIR}/Default/Preferences"
        if [[ -f "\${PREFS_FILE}" ]]; then
            sed -i 's/"exited_cleanly":false/"exited_cleanly":true/g; s/"exit_type":"Crashed"/"exit_type":"Normal"/g' "\${PREFS_FILE}" 2>/dev/null || true
        fi

        "\${CHROME_BIN}" \\
            --kiosk \\
            --user-data-dir="\${PROFILE_DIR}" \\
            --noerrdialogs \\
            --disable-infobars \\
            --disable-session-crashed-bubble \\
            --no-first-run \\
            --overscroll-history-navigation=0 \\
            --disable-pinch \\
            --disable-translate \\
            --disable-features=Translate,TranslateUI \\
            --autoplay-policy=no-user-gesture-required \\
            --check-for-update-interval=31536000 \\
            --disable-component-update \\
            --password-store=basic \\
            --start-fullscreen \\
            --start-maximized \\
            "\${KIOSK_URL}" >/dev/null 2>&1 || true

        sleep 2
    done
) &
EOF

chown -R "${KIOSK_USER}:${KIOSK_USER}" "${OPENBOX_DIR}"
chmod 0755 "${OPENBOX_DIR}/autostart"

# 10. systemd-Service fuer Flask Web-UI registrieren
log "Registriere systemd-Service ${SERVICE_NAME} (Port ${KIOSK_PORT})..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=kiosX Web-Konfigurationsoberflaeche
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${KIOSK_USER}
Group=${KIOSK_USER}
WorkingDirectory=${REPO_DIR}
Environment=DISPLAY=:0
Environment=XAUTHORITY=${KIOSK_HOME}/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/${KIOSK_UID}
Environment=PULSE_RUNTIME_PATH=/run/user/${KIOSK_UID}/pulse
Environment=KIOSX_CONFIG=${CONFIG_FILE}
Environment=KIOSX_APPLY_SCRIPT=${REPO_DIR}/kiosk-apply.sh
Environment=KIOSX_PORT=${KIOSK_PORT}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 ${REPO_DIR}/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

# 11. Abschlussmeldung
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP_ADDR="${IP_ADDR:-<kiosk-ip>}"

echo ""
ok "============================================================"
ok "kiosX Installation erfolgreich abgeschlossen!"
ok "============================================================"
log "Kiosk-Benutzer : ${KIOSK_USER} (UID=${KIOSK_UID})"
log "Konfiguration  : ${CONFIG_FILE}"
log "Web-UI Service : ${SERVICE_NAME} ($(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || echo unknown))"
log "Web-UI URL     : http://${IP_ADDR}:${KIOSK_PORT}"
echo ""
log "Naechster Schritt: System neu starten ('sudo reboot'), damit"
log "Autologin auf tty1, X11/Openbox und Chromium Kiosk starten."
