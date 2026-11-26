#!/bin/sh
# =============================================================================
# kiosX - Chromium Kiosk Watchdog-Schleife (POSIX sh-kompatibel)
# =============================================================================
# Wird von Openbox autostart oder 'kiosx start' gestartet.
# Respektiert die Pause-Flag-Datei ($XDG_RUNTIME_DIR/kiosx-browser.paused),
# sodass 'kiosx stop' den Browser dauerhaft anhalten kann (Wallpaper sichtbar).
# =============================================================================

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CONFIG_FILE="${KIOSX_CONFIG:-/etc/kiosk/config.json}"
WALLPAPER_FILE="${KIOSX_WALLPAPER:-/etc/kiosk/wallpaper.jpg}"
KIOSK_PORT="${KIOSX_PORT:-8080}"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_RUNTIME_PATH="${PULSE_RUNTIME_PATH:-${XDG_RUNTIME_DIR}/pulse}"
export PATH="/snap/bin:$PATH"

RUNTIME_DIR="${XDG_RUNTIME_DIR}"
if [ ! -d "${RUNTIME_DIR}" ] || [ ! -w "${RUNTIME_DIR}" ]; then
    RUNTIME_DIR="/tmp"
fi

PAUSE_FLAG="${RUNTIME_DIR}/kiosx-browser.paused"
PID_FILE="${RUNTIME_DIR}/kiosx-watchdog.pid"

# Verhindern, dass mehrere Watchdog-Instanzen gleichzeitig laufen
if [ -f "${PID_FILE}" ]; then
    OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD_PID}" ] && [ "${OLD_PID}" != "$$" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
        exit 0
    fi
fi
printf '%s\n' "$$" > "${PID_FILE}"
trap 'rm -f "${PID_FILE}"' EXIT INT TERM

resolve_chromium() {
    for bin in chromium /snap/bin/chromium chromium-browser; do
        if command -v "${bin}" >/dev/null 2>&1; then
            command -v "${bin}"
            return 0
        elif [ -x "${bin}" ]; then
            printf '%s\n' "${bin}"
            return 0
        fi
    done
    return 1
}

is_snap_chromium() {
    bin="$1"
    real_bin="$(readlink -f "${bin}" 2>/dev/null || printf '%s' "${bin}")"
    case "${bin}:${real_bin}" in
        *"/snap/"*|*"/bin/snap"*) return 0 ;;
    esac
    if [ -f "${bin}" ] && grep -qs "snap" "${bin}" 2>/dev/null; then
        return 0
    fi
    return 1
}

while true; do
    # Falls per 'kiosx stop' pausiert, warten ohne Browser neu zu starten
    if [ -f "${PAUSE_FLAG}" ]; then
        sleep 1
        continue
    fi

    CHROME_BIN="$(resolve_chromium || true)"
    if [ -z "${CHROME_BIN}" ]; then
        sleep 5
        continue
    fi

    # Hintergrundbild vor jedem Browser-Start sicherstellen
    if [ -s "${WALLPAPER_FILE}" ] && command -v feh >/dev/null 2>&1; then
        feh --no-fehbg --bg-fill "${WALLPAPER_FILE}" >/dev/null 2>&1 || true
    fi

    KIOSK_URL="$(jq -r '.url // "https://example.com"' "${CONFIG_FILE}" 2>/dev/null || printf '%s' "https://example.com")"
    if [ -z "${KIOSK_URL}" ] || [ "${KIOSK_URL}" = "null" ]; then
        KIOSK_URL="https://example.com"
    fi

    SPLASH_SEC="$(jq -r '.splash_seconds // 15' "${CONFIG_FILE}" 2>/dev/null || printf '%s' "15")"
    LAUNCH_URL="${KIOSK_URL}"

    # Bei aktivierter Start-Anzeige (/splash) kurz auf lokalen Flask-Dienst pruefen
    if [ "${SPLASH_SEC}" != "0" ]; then
        i=0
        while [ "${i}" -lt 6 ]; do
            if curl -fsS --max-time 1 "http://127.0.0.1:${KIOSK_PORT}/api/info" >/dev/null 2>&1; then
                LAUNCH_URL="http://127.0.0.1:${KIOSK_PORT}/splash"
                break
            fi
            i=$((i + 1))
            sleep 0.5
        done
    fi

    # Profil-Pfad kompatibel zu apt- und Snap-Chromium waehlen
    if is_snap_chromium "${CHROME_BIN}"; then
        PROFILE_DIR="$HOME/snap/chromium/common/kiosx-profile"
    else
        PROFILE_DIR="$HOME/.config/kiosx-chromium"
    fi
    mkdir -p "${PROFILE_DIR}/Default"

    # Absturz-Dialoge ("Seiten wiederherstellen") verhindern
    PREFS_FILE="${PROFILE_DIR}/Default/Preferences"
    if [ -f "${PREFS_FILE}" ]; then
        sed -i 's/"exited_cleanly":false/"exited_cleanly":true/g; s/"exit_type":"Crashed"/"exit_type":"Normal"/g' "${PREFS_FILE}" 2>/dev/null || true
    fi

    "${CHROME_BIN}" \
        --kiosk \
        --user-data-dir="${PROFILE_DIR}" \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        --no-first-run \
        --overscroll-history-navigation=0 \
        --disable-pinch \
        --disable-translate \
        --disable-features=Translate,TranslateUI \
        --autoplay-policy=no-user-gesture-required \
        --use-fake-ui-for-media-stream \
        --check-for-update-interval=31536000 \
        --disable-component-update \
        --password-store=basic \
        --start-fullscreen \
        --start-maximized \
        "${LAUNCH_URL}" >/tmp/kiosx-chromium.log 2>&1 || true

    sleep 2
done
