#!/usr/bin/env bash
# =============================================================================
# kiosX - Idempotentes Installations-Skript fuer Ubuntu Server (minimized)
# =============================================================================
# Verwendung:
#   sudo bash /opt/kiosX/install.sh
#   sudo bash /opt/kiosX/install.sh --user kiosk --port 8080
#   sudo bash /opt/kiosX/install.sh --skip-netplan
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/etc/kiosk"
CONFIG_FILE="${CONFIG_DIR}/config.json"
WALLPAPER_FILE="${CONFIG_DIR}/wallpaper.jpg"
DEFAULT_WALLPAPER_URL="https://images.unsplash.com/photo-1493606278519-11aa9f86e40a?auto=format&fit=crop&w=1920&q=85"
WIFI_CONF="${REPO_DIR}/wifi.conf"
SERVICE_NAME="kiosk-ui.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
KIOSK_PORT="${KIOSX_PORT:-8080}"
KIOSK_USER="${KIOSK_USER:-}"
GIT_TOKEN="${KIOSX_GIT_TOKEN:-}"
GIT_USER="${KIOSX_GIT_USER:-teletom}"
SKIP_NETPLAN=0

log()  { printf '\033[1;34m[kiosX]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[kiosX]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[kiosX WARN]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[kiosX ERROR]\033[0m %s\n' "$*" >&2; }

usage() {
    cat <<EOF
Verwendung: sudo bash $0 [--user <benutzername>] [--port <port>] [--git-token <pat>] [--skip-netplan]

Optionen:
  --user <name>     Linux-Benutzer fuer Kiosk-Session und Web-UI
                    (Standard: \$SUDO_USER oder 'kiosk')
  --port <port>     Port der Flask-Weboberflaeche (Standard: 8080)
  --git-token <pat> Read-Only GitHub PAT fuer automatische Web-UI-Updates
                    (alternativ Env KIOSX_GIT_TOKEN oder direkt in Clone-URL)
  --git-user <name> GitHub-Benutzername fuer --git-token (Standard: teletom)
  --wifi-conf <pf>  Pfad zur WLAN-Konfigurationsdatei (Standard: ${REPO_DIR}/wifi.conf)
  --skip-netplan    Bestehende Netplan-Konfiguration nicht veraendern
  -h, --help        Diese Hilfe anzeigen
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
        --git-token)
            GIT_TOKEN="${2:-}"
            shift 2
            ;;
        --git-user)
            GIT_USER="${2:-teletom}"
            shift 2
            ;;
        --wifi-conf)
            WIFI_CONF="${2:-}"
            shift 2
            ;;
        --skip-netplan)
            SKIP_NETPLAN=1
            shift
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

if [[ -z "${KIOSK_HOME}" || ! -d "${KIOSK_HOME}" ]]; then
    err "Home-Verzeichnis fuer Benutzer '${KIOSK_USER}' nicht gefunden."
    exit 1
fi

log "Installiere kiosX aus ${REPO_DIR} fuer Benutzer '${KIOSK_USER}' (UID=${KIOSK_UID}, Home=${KIOSK_HOME})"

# 3. Systempakete installieren (vollstaendig fuer minimales Ubuntu Server)
log "Pruefe Paketquellen (inkl. universe) und installiere Systempakete..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

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
    curl
    dbus-user-session
    libpam-systemd
    wpasupplicant
    avahi-daemon
    avahi-utils
    xorg
    xinit
    xauth
    xserver-xorg-input-libinput
    openbox
    x11-xserver-utils
    xinput
    unclutter
    feh
    pipewire
    wireplumber
    pipewire-pulse
    pipewire-alsa
    pulseaudio-utils
    alsa-utils
    python3
    python3-flask
    python3-tk
    qrencode
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

# Bei Snap-Chromium Kamera- & Mikrofon-Interfaces explizit verbinden (idempotent)
if command -v snap &>/dev/null && snap list chromium &>/dev/null; then
    snap connect chromium:camera :camera 2>/dev/null || true
    snap connect chromium:audio-record :audio-record 2>/dev/null || true
fi

# 4. Gruppenmitgliedschaften und systemd-Linger fuer PipeWire/X11 einrichten
log "Konfiguriere Gruppen fuer '${KIOSK_USER}'..."
for grp in audio video input render tty; do
    if getent group "${grp}" &>/dev/null; then
        usermod -aG "${grp}" "${KIOSK_USER}"
    fi
done

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

# 4b. Zeitzone pruefen (Ubuntu Server minimized startet oft mit Etc/UTC)
if command -v timedatectl &>/dev/null; then
    CUR_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    if [[ -z "${CUR_TZ}" || "${CUR_TZ}" == "Etc/UTC" || "${CUR_TZ}" == "UTC" ]]; then
        log "Setze System-Zeitzone von '${CUR_TZ:-unbekannt}' auf 'Europe/Vienna'..."
        timedatectl set-timezone Europe/Vienna 2>/dev/null || warn "Zeitzone konnte nicht gesetzt werden."
    fi
fi

# 5. Skript-Berechtigungen, CLI 'kiosx' & minimale sudoers fuer Web-UI-Updates einrichten
chmod 0755 "${REPO_DIR}/install.sh" "${REPO_DIR}/kiosk-apply.sh" "${REPO_DIR}/kiosk-watchdog.sh" "${REPO_DIR}/kiosk-overlay.py" "${REPO_DIR}/kiosx"
if [[ -f "${REPO_DIR}/app.py" ]]; then
    chmod 0644 "${REPO_DIR}/app.py"
fi
ln -sfn "${REPO_DIR}/kiosx" /usr/local/bin/kiosx
ok "CLI-Befehl 'kiosx' unter /usr/local/bin/kiosx verknuepft."

# Minimal-Rechte fuer Web-UI-Updates (nur genau 2 definierte Befehle, kein generelles sudo)
if [[ -d /etc/sudoers.d ]]; then
    SUDOERS_TMP="$(mktemp)"
    cat > "${SUDOERS_TMP}" <<EOF
# kiosX Web-UI Update-Berechtigungen (automatisch erstellt von install.sh)
${KIOSK_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/bash ${REPO_DIR}/install.sh --skip-netplan
EOF
    chmod 0440 "${SUDOERS_TMP}"
    if ! command -v visudo &>/dev/null || visudo -cf "${SUDOERS_TMP}" >/dev/null 2>&1; then
        install -m 0440 -o root -g root "${SUDOERS_TMP}" /etc/sudoers.d/kiosx-updater
        ok "Update-Berechtigungen unter /etc/sudoers.d/kiosx-updater eingerichtet."
    else
        warn "visudo-Pruefung fehlgeschlagen – /etc/sudoers.d/kiosx-updater wurde nicht geschrieben."
    fi
    rm -f "${SUDOERS_TMP}"
fi

# 5b. Automatische GitHub-Authentifizierung fuer Web-UI-Updates einrichten
configure_git_updater_auth() {
    if ! command -v git &>/dev/null || [[ ! -d "${REPO_DIR}/.git" ]]; then
        return 0
    fi

    local cred_file="${KIOSK_HOME}/.git-credentials"
    local origin_url
    origin_url="$(runuser -u "${KIOSK_USER}" -- git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"
    [[ -z "${origin_url}" ]] && return 0

    # Repository explizit an die geschuetzte Credential-Datei des Kiosk-Benutzers binden
    runuser -u "${KIOSK_USER}" -- git -C "${REPO_DIR}" config --local credential.helper "store --file=${cred_file}" || true

    save_git_cred() {
        local host="$1"
        local user="$2"
        local token="$3"
        python3 - "${cred_file}" "${host}" "${user}" "${token}" <<'PYCRED'
import os, sys, urllib.parse
cred_path, host, user, token = sys.argv[1:5]
entry = f"https://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(token, safe='')}@{host}"
lines = []
if os.path.isfile(cred_path):
    with open(cred_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            s = line.strip()
            if not s or f"@{host}" in s:
                continue
            lines.append(s)
lines.append(entry)
with open(cred_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PYCRED
        chown "${KIOSK_USER}:${KIOSK_USER}" "${cred_file}"
        chmod 0600 "${cred_file}"
    }

    # 1) Falls Token direkt in der Clone-URL stand (https://user:TOKEN@github.com/...),
    #    sicher nach ~/.git-credentials (0600) verschieben und Remote-URL bereinigen
    if [[ "${origin_url}" =~ ^https?://[^/]+@ ]]; then
        local parsed
        parsed="$(python3 - "${origin_url}" <<'PYPARSE'
import sys, urllib.parse
u = urllib.parse.urlsplit(sys.argv[1])
host = u.hostname or "github.com"
user = urllib.parse.unquote(u.username or "teletom")
pwd = urllib.parse.unquote(u.password or "")
if not pwd and u.username:
    pwd = user
    user = "x-access-token"
clean = urllib.parse.urlunsplit((u.scheme, host + (f":{u.port}" if u.port else ""), u.path, u.query, u.fragment))
print(f"{host}\t{user}\t{pwd}\t{clean}")
PYPARSE
)"
        local p_host p_user p_pwd p_clean
        IFS=$'\t' read -r p_host p_user p_pwd p_clean <<< "${parsed}"
        if [[ -n "${p_pwd}" ]]; then
            save_git_cred "${p_host}" "${p_user}" "${p_pwd}"
            runuser -u "${KIOSK_USER}" -- git -C "${REPO_DIR}" remote set-url origin "${p_clean}"
            ok "Git-Zugangsdaten aus Clone-URL sicher nach ${cred_file} (0600) uebernommen und Remote-URL bereinigt."
            origin_url="${p_clean}"
        fi
    fi

    # 2) Falls --git-token / KIOSX_GIT_TOKEN uebergeben wurde, direkt speichern
    if [[ -n "${GIT_TOKEN}" ]]; then
        local host
        host="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "github.com")' "${origin_url}")"
        save_git_cred "${host}" "${GIT_USER}" "${GIT_TOKEN}"
        ok "Git-Token sicher in ${cred_file} (0600) gespeichert."
    fi

    # 3) Testen, ob nicht-interaktives git fetch bereits funktioniert
    if runuser -u "${KIOSK_USER}" -- env GIT_TERMINAL_PROMPT=0 git -C "${REPO_DIR}" fetch --quiet origin 2>/dev/null; then
        ok "GitHub-Update-Zugriff verifiziert (automatische Web-UI-Updates aktiv)."
        return 0
    fi

    # 4) Bei interaktiver SSH-Installation einmalig optional nach dem Read-Only Token fragen
    if [[ -t 0 && "${origin_url}" == https://* ]]; then
        echo ""
        log "Fuer automatische Updates im Web-UI benoetigt dieses private Repo ein Read-Only GitHub Token (PAT)."
        printf 'Read-Only GitHub Token eingeben (Enter zum Ueberspringen): '
        local input_token=""
        read -r -s input_token || true
        echo ""
        if [[ -n "${input_token}" ]]; then
            local host
            host="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.argv[1]).hostname or "github.com")' "${origin_url}")"
            save_git_cred "${host}" "${GIT_USER}" "${input_token}"
            if runuser -u "${KIOSK_USER}" -- env GIT_TERMINAL_PROMPT=0 git -C "${REPO_DIR}" fetch --quiet origin 2>/dev/null; then
                ok "GitHub-Token verifiziert – automatische Web-UI-Updates sind ab sofort aktiv!"
            else
                warn "Token gespeichert, aber 'git fetch origin' war nicht erfolgreich (Token-Rechte/Netzwerk pruefen)."
            fi
        else
            log "Uebersprungen – kann jederzeit per 'sudo bash ${REPO_DIR}/install.sh --skip-netplan --git-token <PAT>' nachgeholt werden."
        fi
    else
        warn "Automatischer git fetch ohne Zugangsdaten nicht moeglich (fuer Web-UI-Updates: --git-token <PAT> uebergeben)."
    fi
}

configure_git_updater_auth

# 6. Persistente Konfiguration (/etc/kiosk/config.json) & 4-stellige Kiosk-ID
log "Richte Konfigurationsverzeichnis ${CONFIG_DIR} ein..."
mkdir -p "${CONFIG_DIR}"

generate_device_id() {
    python3 -c '
import secrets
alphabet = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
print("".join(secrets.choice(alphabet) for _ in range(4)))
'
}

if [[ ! -f "${CONFIG_FILE}" ]] || ! jq empty "${CONFIG_FILE}" &>/dev/null; then
    if [[ -f "${CONFIG_FILE}" ]]; then
        cp "${CONFIG_FILE}" "${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
        warn "Bestehende ungueltige ${CONFIG_FILE} gesichert und neu erstellt."
    fi
    NEW_ID="$(generate_device_id)"
    cat > "${CONFIG_FILE}" <<EOF
{
  "device_id": "${NEW_ID}",
  "url": "https://example.com",
  "splash_seconds": 15,
  "wallpaper_url": "${DEFAULT_WALLPAPER_URL}",
  "gesture_enabled": true,
  "auth": {
    "lan_pin": "050477"
  },
  "display": {
    "primary_output": "",
    "resolution": "auto",
    "rotation": "normal",
    "secondary_output": "",
    "secondary_mode": "off"
  },
  "touch": {
    "device": "",
    "rotation": "auto"
  },
  "audio": {
    "card": "",
    "profile": "",
    "volume": 75
  },
  "schedule": {
    "enabled": false,
    "timezone": "Europe/Vienna",
    "scope": "both",
    "primary": {
      "preset": "inhouse",
      "days": {
        "mon": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "tue": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "wed": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "thu": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "fri": { "enabled": true,  "start": "07:15", "end": "12:15" },
        "sat": { "enabled": false, "start": "08:00", "end": "18:00" },
        "sun": { "enabled": false, "start": "08:00", "end": "18:00" }
      }
    },
    "secondary": {
      "preset": "inhouse",
      "days": {
        "mon": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "tue": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "wed": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "thu": { "enabled": true,  "start": "07:15", "end": "16:45" },
        "fri": { "enabled": true,  "start": "07:15", "end": "12:15" },
        "sat": { "enabled": false, "start": "08:00", "end": "18:00" },
        "sun": { "enabled": false, "start": "08:00", "end": "18:00" }
      }
    }
  }
}
EOF
    ok "Standard-Konfiguration unter ${CONFIG_FILE} erstellt (Kiosk-ID: ${NEW_ID})."
else
    # Bestehende Konfiguration sanft um neue Felder ergaenzen (idempotent)
    EXISTING_ID="$(jq -r '.device_id // ""' "${CONFIG_FILE}")"
    if [[ -z "${EXISTING_ID}" ]]; then
        EXISTING_ID="$(generate_device_id)"
    fi
    tmp_cfg="$(mktemp)"
    jq \
        --arg id "${EXISTING_ID}" \
        --arg wp "${DEFAULT_WALLPAPER_URL}" '
        .device_id = (if (.device_id // "") == "" then $id else .device_id end) |
        .splash_seconds = (if has("splash_seconds") then .splash_seconds else 15 end) |
        .wallpaper_url = (if has("wallpaper_url") then .wallpaper_url else $wp end) |
        .gesture_enabled = (if has("gesture_enabled") then .gesture_enabled else true end) |
        .auth = (if has("auth") then .auth else {"lan_pin": "050477"} end) |
        .touch.rotation = (if (.touch.rotation // "auto") == "0" then "auto" else (.touch.rotation // "auto") end) |
        .schedule = (if has("schedule") then .schedule else {
          "enabled": false,
          "timezone": "Europe/Vienna",
          "scope": "both",
          "primary": {
            "preset": "inhouse",
            "days": {
              "mon": {"enabled": true, "start": "07:15", "end": "16:45"},
              "tue": {"enabled": true, "start": "07:15", "end": "16:45"},
              "wed": {"enabled": true, "start": "07:15", "end": "16:45"},
              "thu": {"enabled": true, "start": "07:15", "end": "16:45"},
              "fri": {"enabled": true, "start": "07:15", "end": "12:15"},
              "sat": {"enabled": false, "start": "08:00", "end": "18:00"},
              "sun": {"enabled": false, "start": "08:00", "end": "18:00"}
            }
          },
          "secondary": {
            "preset": "inhouse",
            "days": {
              "mon": {"enabled": true, "start": "07:15", "end": "16:45"},
              "tue": {"enabled": true, "start": "07:15", "end": "16:45"},
              "wed": {"enabled": true, "start": "07:15", "end": "16:45"},
              "thu": {"enabled": true, "start": "07:15", "end": "16:45"},
              "fri": {"enabled": true, "start": "07:15", "end": "12:15"},
              "sat": {"enabled": false, "start": "08:00", "end": "18:00"},
              "sun": {"enabled": false, "start": "08:00", "end": "18:00"}
            }
          }
        } end)
    ' "${CONFIG_FILE}" > "${tmp_cfg}"
    mv "${tmp_cfg}" "${CONFIG_FILE}"
    ok "Bestehende Konfiguration unter ${CONFIG_FILE} aktualisiert/beibehalten (Kiosk-ID: ${EXISTING_ID})."
fi

DEVICE_ID="$(jq -r '.device_id' "${CONFIG_FILE}")"
WALLPAPER_URL="$(jq -r '.wallpaper_url // ""' "${CONFIG_FILE}")"

# Standard-Hintergrundbild lokal herunterladen (offline-faehig fuer Boot/Fallback)
if [[ ! -s "${WALLPAPER_FILE}" && -n "${WALLPAPER_URL}" ]]; then
    log "Lade Hintergrundbild nach ${WALLPAPER_FILE}..."
    if curl -fsSL --connect-timeout 10 --max-time 25 "${WALLPAPER_URL}" -o "${WALLPAPER_FILE}.tmp"; then
        mv "${WALLPAPER_FILE}.tmp" "${WALLPAPER_FILE}"
        ok "Hintergrundbild gespeichert: ${WALLPAPER_FILE}"
    else
        rm -f "${WALLPAPER_FILE}.tmp"
        warn "Hintergrundbild konnte nicht heruntergeladen werden (kein Internet?) – wird uebersprungen."
    fi
fi

# Schreibrechte fuer den Kiosk-User (inkl. atomarem Rename im Verzeichnis /etc/kiosk)
chown -R "${KIOSK_USER}:${KIOSK_USER}" "${CONFIG_DIR}"
chmod 0775 "${CONFIG_DIR}"
chmod 0664 "${CONFIG_FILE}"
[[ -f "${WALLPAPER_FILE}" ]] && chmod 0664 "${WALLPAPER_FILE}"

# 7. Avahi / mDNS fuer LAN-Erreichbarkeit (<hostname>.local & kiosx-<id>.local)
log "Konfiguriere avahi-daemon (mDNS)..."
mkdir -p /etc/avahi/services
cat > /etc/avahi/services/kiosx.service <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">kiosX (%h - ${DEVICE_ID})</name>
  <service>
    <type>_http._tcp</type>
    <port>${KIOSK_PORT}</port>
    <txt-record>app=kiosX</txt-record>
    <txt-record>id=${DEVICE_ID}</txt-record>
  </service>
</service-group>
EOF
systemctl enable --now avahi-daemon.service || warn "avahi-daemon konnte nicht gestartet werden."

# 8. Hardware-unabhaengiges Netplan konfigurieren (mit Backup & Vorab-Validierung)
configure_netplan() {
    if (( SKIP_NETPLAN == 1 )); then
        log "Netplan-Konfiguration uebersprungen (--skip-netplan)."
        return 0
    fi
    if ! command -v netplan &>/dev/null; then
        warn "'netplan' nicht gefunden – Netzwerkkonfiguration uebersprungen."
        return 0
    fi

    log "Erstelle hardware-unabhaengige Netplan-Konfiguration..."
    local tmp_yaml
    tmp_yaml="$(mktemp)"

    # WLAN-Interfaces dynamisch erkennen (vermeidet Netplan-Wildcard-Bug bei wifis:)
    local wifi_ifaces=()
    local net_dir iface
    for net_dir in /sys/class/net/*; do
        [[ -e "${net_dir}" ]] || continue
        iface="$(basename "${net_dir}")"
        if [[ -d "${net_dir}/wireless" || -d "${net_dir}/phy80211" ]]; then
            wifi_ifaces+=("${iface}")
        fi
    done

    # WLAN-Zugangsdaten aus wifi.conf einlesen
    local wifi_ssids=()
    local wifi_pwds=()
    if [[ -f "${WIFI_CONF}" ]]; then
        while IFS= read -r line || [[ -n "${line}" ]]; do
            # Kommentare und Leerzeilen ueberspringen
            [[ "${line}" =~ ^[[:space:]]*# ]] && continue
            [[ "${line}" =~ ^[[:space:]]*$ ]] && continue
            if [[ "${line}" == *"="* ]]; then
                local ssid="${line%%=*}"
                local pwd="${line#*=}"
                wifi_ssids+=("${ssid}")
                wifi_pwds+=("${pwd}")
            fi
        done < "${WIFI_CONF}"
    fi

    yaml_escape() {
        printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
    }

    cat > "${tmp_yaml}" <<'EOF'
# Automatisch generiert von kiosX install.sh (hardware-unabhaengig)
network:
  version: 2
  renderer: networkd
  ethernets:
    all-en:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: true
      optional: true
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
    all-eth:
      match:
        name: "eth*"
      dhcp4: true
      dhcp6: true
      optional: true
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
EOF

    if (( ${#wifi_ifaces[@]} > 0 && ${#wifi_ssids[@]} > 0 )); then
        echo "  wifis:" >> "${tmp_yaml}"
        for iface in "${wifi_ifaces[@]}"; do
            cat >> "${tmp_yaml}" <<EOF
    ${iface}:
      dhcp4: true
      optional: true
      dhcp4-overrides:
        route-metric: 200
      access-points:
EOF
            local idx
            for (( idx=0; idx<${#wifi_ssids[@]}; idx++ )); do
                local esc_ssid esc_pwd
                esc_ssid="$(yaml_escape "${wifi_ssids[$idx]}")"
                esc_pwd="$(yaml_escape "${wifi_pwds[$idx]}")"
                printf '        "%s":\n          password: "%s"\n' "${esc_ssid}" "${esc_pwd}" >> "${tmp_yaml}"
            done
        done
        log "Erkannte WLAN-Adapter fuer Netplan: ${wifi_ifaces[*]}"
    else
        log "Kein WLAN-Adapter oder keine WLAN-Konfiguration gefunden – konfiguriere nur Ethernet."
    fi

    local target_yaml="/etc/netplan/50-kiosx-network.yaml"
    local other_yamls=()
    local f
    for f in /etc/netplan/*.yaml /etc/netplan/*.yml; do
        [[ -e "$f" ]] || continue
        if [[ "$f" != "${target_yaml}" ]]; then
            other_yamls+=("$f")
        fi
    done

    # Idempotenz: nichts aendern, wenn Konfiguration bereits identisch ist
    if [[ -f "${target_yaml}" && ${#other_yamls[@]} -eq 0 ]] && cmp -s "${tmp_yaml}" "${target_yaml}"; then
        rm -f "${tmp_yaml}"
        ok "Netplan-Konfiguration ist bereits aktuell."
        return 0
    fi

    # Backup bestehender Netplan-Dateien ausserhalb von /etc/netplan/
    local backup_dir="${CONFIG_DIR}/netplan-backup-$(date +%Y%m%d%H%M%S)"
    mkdir -p "${backup_dir}"
    for f in /etc/netplan/*.yaml /etc/netplan/*.yml; do
        [[ -e "$f" ]] || continue
        cp -a "$f" "${backup_dir}/"
    done
    chmod 0700 "${backup_dir}"

    # Alte Dateien entfernen, damit keine MAC-gebundenen Regeln kollidieren
    for f in "${other_yamls[@]}"; do
        rm -f "$f"
    done

    install -m 0600 -o root -g root "${tmp_yaml}" "${target_yaml}"
    rm -f "${tmp_yaml}"

    # Vorab-Validierung mit automatischem Rollback im Fehlerfall
    if ! netplan generate; then
        err "netplan generate fehlgeschlagen – stelle Backup aus ${backup_dir} wieder her!"
        rm -f "${target_yaml}"
        cp -a "${backup_dir}/." /etc/netplan/ 2>/dev/null || true
        chmod 0600 /etc/netplan/*.yaml /etc/netplan/*.yml 2>/dev/null || true
        netplan generate || true
        return 1
    fi

    log "Wende validierte Netplan-Konfiguration an..."
    netplan apply || warn "netplan apply meldete eine Warnung (Backup liegt unter ${backup_dir})."
    ok "Netplan erfolgreich eingerichtet (Backup: ${backup_dir})."
}

configure_netplan

# 9. Autologin auf tty1 einrichten
log "Konfiguriere Autologin auf tty1 fuer '${KIOSK_USER}'..."
GETTY_DROPIN_DIR="/etc/systemd/system/getty@tty1.service.d"
mkdir -p "${GETTY_DROPIN_DIR}"
cat > "${GETTY_DROPIN_DIR}/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF

# 10. .bash_profile (automatischer startx-Start auf tty1) und .xinitrc einrichten
log "Konfiguriere automatischen X11-Start in ${KIOSK_HOME}/.bash_profile..."
BASH_PROFILE="${KIOSK_HOME}/.bash_profile"
touch "${BASH_PROFILE}"

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

XINITRC="${KIOSK_HOME}/.xinitrc"
cat > "${XINITRC}" <<'EOF'
#!/usr/bin/env bash
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

if command -v systemctl &>/dev/null; then
    systemctl --user start pipewire.service pipewire-pulse.service wireplumber.service 2>/dev/null || true
fi

exec openbox-session
EOF
chown "${KIOSK_USER}:${KIOSK_USER}" "${XINITRC}"
chmod 0755 "${XINITRC}"

# 11. Openbox Autostart konfigurieren (POSIX sh-kompatibel fuer openbox-autostart)
log "Konfiguriere Openbox Autostart unter ${KIOSK_HOME}/.config/openbox/autostart..."
OPENBOX_DIR="${KIOSK_HOME}/.config/openbox"
mkdir -p "${OPENBOX_DIR}"

cat > "${OPENBOX_DIR}/autostart" <<EOF
#!/bin/sh
# kiosX Openbox Autostart (POSIX sh-kompatibel fuer openbox-autostart / dash)

export DISPLAY="\${DISPLAY:-:0}"
export XAUTHORITY="\${XAUTHORITY:-\$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}"
export PATH="/snap/bin:\$PATH"

# Lokalen Zugriff fuer den Kiosk-Benutzer (z. B. systemd kiosk-ui.service) erlauben
xhost +SI:localuser:"\$(id -un)" >/dev/null 2>&1 || true

# Bildschirmschoner und DPMS dauerhaft deaktivieren
xset s off >/dev/null 2>&1 || true
xset s noblank >/dev/null 2>&1 || true
xset -dpms >/dev/null 2>&1 || true

# Hintergrundbild sofort anzeigen, solange Browser startet oder geschlossen ist
if [ -f "${WALLPAPER_FILE}" ] && command -v feh >/dev/null 2>&1; then
    feh --no-fehbg --bg-fill "${WALLPAPER_FILE}" >/dev/null 2>&1 &
fi

# Mauszeiger bei Inaktivitaet ausblenden
unclutter -idle 2 -root >/dev/null 2>&1 &

# Gespeicherte Hardware-Konfiguration (Display, Touch, Audio, Wallpaper) anwenden
"${REPO_DIR}/kiosk-apply.sh" >/tmp/kiosx-apply.log 2>&1 || true

# Chromium im Kiosk-Modus ueber Watchdog-Skript starten (unterstuetzt 'kiosx stop/start')
KIOSX_CONFIG="${CONFIG_FILE}" \
KIOSX_WALLPAPER="${WALLPAPER_FILE}" \
KIOSX_PORT="${KIOSK_PORT}" \
"${REPO_DIR}/kiosk-watchdog.sh" &
EOF

chown -R "${KIOSK_USER}:${KIOSK_USER}" "${OPENBOX_DIR}"
chmod 0755 "${OPENBOX_DIR}/autostart"

# 12. systemd-Service fuer Flask Web-UI registrieren
log "Registriere systemd-Service ${SERVICE_NAME} (Port ${KIOSK_PORT})..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=kiosX Web-Konfigurationsoberflaeche
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service

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
Environment=KIOSX_WALLPAPER=${WALLPAPER_FILE}
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
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# 13. Abschlussmeldung
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP_ADDR="${IP_ADDR:-<kiosk-ip>}"
HOST_NAME="$(hostname)"
DEVICE_ID_LOWER="$(printf '%s' "${DEVICE_ID}" | tr '[:upper:]' '[:lower:]')"

echo ""
ok "============================================================"
ok "kiosX Installation erfolgreich abgeschlossen!"
ok "============================================================"
log "Kiosk-Benutzer : ${KIOSK_USER} (UID=${KIOSK_UID})"
log "Kiosk-ID       : ${DEVICE_ID}"
log "Konfiguration  : ${CONFIG_FILE}"
log "Web-UI Service : ${SERVICE_NAME} ($(systemctl is-active "${SERVICE_NAME}" 2>/dev/null || echo unknown))"
log "Web-UI (IP)    : http://${IP_ADDR}:${KIOSK_PORT}"
log "Web-UI (mDNS)  : http://${HOST_NAME}.local:${KIOSK_PORT}"
log "Web-UI (Alias) : http://kiosx-${DEVICE_ID_LOWER}.local:${KIOSK_PORT}"
echo ""
log "Naechster Schritt: System neu starten ('sudo reboot'), damit"
log "Autologin auf tty1, X11/Openbox und Chromium Kiosk starten."
