#!/usr/bin/env bash
# =============================================================================
# kiosX - Live-Konfigurations-Applier (Display, Wallpaper, Touch, Audio)
# =============================================================================
# Liest /etc/kiosk/config.json per jq aus und wendet alle Einstellungen ohne
# Neustart auf die laufende X11- und PipeWire-Session an.
# =============================================================================

set -u

CONFIG_FILE="${KIOSX_CONFIG:-/etc/kiosk/config.json}"
WALLPAPER_FILE="${KIOSX_WALLPAPER:-/etc/kiosk/wallpaper.jpg}"
export LC_ALL=C
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_RUNTIME_PATH="${PULSE_RUNTIME_PATH:-${XDG_RUNTIME_DIR}/pulse}"

WARNINGS=0

log()  { printf '[kiosX] %s\n' "$*"; }
warn() { printf '[kiosX WARN] %s\n' "$*" >&2; WARNINGS=$((WARNINGS + 1)); }
err()  { printf '[kiosX ERROR] %s\n' "$*" >&2; }

# -----------------------------------------------------------------------------
# 1. Voraussetzungen & Konfiguration pruefen
# -----------------------------------------------------------------------------
if ! command -v jq >/dev/null 2>&1; then
    err "'jq' ist nicht installiert."
    exit 1
fi

if [[ ! -r "${CONFIG_FILE}" ]]; then
    err "Konfigurationsdatei nicht lesbar: ${CONFIG_FILE}"
    exit 1
fi

if ! jq empty "${CONFIG_FILE}" >/dev/null 2>&1; then
    err "Ungueltiges JSON in ${CONFIG_FILE}"
    exit 1
fi

PRIMARY_OUTPUT="$(jq -r '.display.primary_output // ""' "${CONFIG_FILE}")"
RESOLUTION="$(jq -r '.display.resolution // "auto"' "${CONFIG_FILE}")"
ROTATION="$(jq -r '.display.rotation // "normal"' "${CONFIG_FILE}")"
SECONDARY_OUTPUT="$(jq -r '.display.secondary_output // ""' "${CONFIG_FILE}")"
SECONDARY_MODE="$(jq -r '.display.secondary_mode // "off"' "${CONFIG_FILE}")"

TOUCH_DEVICE="$(jq -r '.touch.device // ""' "${CONFIG_FILE}")"
TOUCH_ROTATION="$(jq -r '.touch.rotation // "auto"' "${CONFIG_FILE}")"

AUDIO_CARD="$(jq -r '.audio.card // ""' "${CONFIG_FILE}")"
AUDIO_PROFILE="$(jq -r '.audio.profile // ""' "${CONFIG_FILE}")"
AUDIO_VOLUME="$(jq -r '.audio.volume // 75' "${CONFIG_FILE}")"

# Werte validieren / normalisieren
case "${ROTATION}" in
    normal|right|inverted|left) ;;
    *)
        warn "Ungueltige Display-Rotation '${ROTATION}', verwende 'normal'."
        ROTATION="normal"
        ;;
esac

case "${SECONDARY_MODE}" in
    off|right|left|mirror) ;;
    *)
        warn "Ungueltiger Zweitdisplay-Modus '${SECONDARY_MODE}', verwende 'off'."
        SECONDARY_MODE="off"
        ;;
esac

case "${TOUCH_ROTATION}" in
    auto|0|90|180|270) ;;
    *)
        warn "Ungueltige Touch-Korrektur '${TOUCH_ROTATION}', verwende 'auto'."
        TOUCH_ROTATION="auto"
        ;;
esac

if ! [[ "${AUDIO_VOLUME}" =~ ^[0-9]+$ ]]; then
    AUDIO_VOLUME=75
elif (( AUDIO_VOLUME > 100 )); then
    AUDIO_VOLUME=100
fi

# -----------------------------------------------------------------------------
# 2. X11-Verbindung & Display-Konfiguration (xrandr)
# -----------------------------------------------------------------------------
ACTIVE_PRIMARY=""
X11_AVAILABLE=0

if command -v xrandr >/dev/null 2>&1 && XRANDR_OUT="$(xrandr --query 2>/dev/null)"; then
    X11_AVAILABLE=1
else
    warn "Keine X11-Session unter DISPLAY=${DISPLAY} erreichbar – Display- und Touch-Konfiguration uebersprungen."
fi

if (( X11_AVAILABLE == 1 )); then
    # Bildschirmschoner & DPMS deaktivieren
    if command -v xset >/dev/null 2>&1; then
        xset s off >/dev/null 2>&1 || true
        xset s noblank >/dev/null 2>&1 || true
        xset -dpms >/dev/null 2>&1 || true
    fi

    mapfile -t CONNECTED_OUTPUTS < <(awk '/ connected/ {print $1}' <<< "${XRANDR_OUT}")

    if (( ${#CONNECTED_OUTPUTS[@]} == 0 )); then
        warn "Keine verbundenen Monitore erkannt – Display-Konfiguration uebersprungen."
    else
        is_connected() {
            local target="$1"
            local out
            for out in "${CONNECTED_OUTPUTS[@]}"; do
                if [[ "${out}" == "${target}" ]]; then
                    return 0
                fi
            done
            return 1
        }

        mode_supported() {
            local output="$1"
            local mode="$2"
            awk -v out="${output}" -v m="${mode}" '
                $1 == out && $2 == "connected" { in_out = 1; next }
                /^[^ \t]/ { in_out = 0 }
                in_out && $1 == m { found = 1; exit }
                END { exit(found ? 0 : 1) }
            ' <<< "${XRANDR_OUT}"
        }

        # Primaeren Monitor bestimmen (Fallback auf ersten verbundenen Monitor)
        if [[ -n "${PRIMARY_OUTPUT}" ]] && is_connected "${PRIMARY_OUTPUT}"; then
            ACTIVE_PRIMARY="${PRIMARY_OUTPUT}"
        else
            if [[ -n "${PRIMARY_OUTPUT}" ]]; then
                warn "Konfiguriertes Primaer-Display '${PRIMARY_OUTPUT}' nicht verbunden – Fallback auf '${CONNECTED_OUTPUTS[0]}'."
            fi
            ACTIVE_PRIMARY="${CONNECTED_OUTPUTS[0]}"
        fi

        XRANDR_ARGS=(--output "${ACTIVE_PRIMARY}" --primary)
        if [[ -z "${RESOLUTION}" || "${RESOLUTION}" == "auto" ]]; then
            XRANDR_ARGS+=(--auto)
        elif mode_supported "${ACTIVE_PRIMARY}" "${RESOLUTION}"; then
            XRANDR_ARGS+=(--mode "${RESOLUTION}")
        else
            warn "Aufloesung '${RESOLUTION}' fuer '${ACTIVE_PRIMARY}' nicht verfuegbar – verwende 'auto'."
            XRANDR_ARGS+=(--auto)
        fi
        XRANDR_ARGS+=(--rotate "${ROTATION}")

        # Optionales zweites Display konfigurieren
        ACTIVE_SECONDARY=""
        if [[ -n "${SECONDARY_OUTPUT}" && "${SECONDARY_OUTPUT}" != "${ACTIVE_PRIMARY}" ]]; then
            if is_connected "${SECONDARY_OUTPUT}"; then
                ACTIVE_SECONDARY="${SECONDARY_OUTPUT}"
                case "${SECONDARY_MODE}" in
                    off)
                        XRANDR_ARGS+=(--output "${ACTIVE_SECONDARY}" --off)
                        ;;
                    right)
                        XRANDR_ARGS+=(--output "${ACTIVE_SECONDARY}" --auto --rotate normal --right-of "${ACTIVE_PRIMARY}")
                        ;;
                    left)
                        XRANDR_ARGS+=(--output "${ACTIVE_SECONDARY}" --auto --rotate normal --left-of "${ACTIVE_PRIMARY}")
                        ;;
                    mirror)
                        XRANDR_ARGS+=(--output "${ACTIVE_SECONDARY}" --auto --rotate "${ROTATION}" --same-as "${ACTIVE_PRIMARY}")
                        ;;
                esac
            else
                warn "Konfiguriertes Zweit-Display '${SECONDARY_OUTPUT}' nicht verbunden."
            fi
        elif [[ "${SECONDARY_MODE}" == "off" ]]; then
            for out in "${CONNECTED_OUTPUTS[@]}"; do
                if [[ "${out}" != "${ACTIVE_PRIMARY}" ]]; then
                    XRANDR_ARGS+=(--output "${out}" --off)
                fi
            done
        fi

        log "Wende Display-Konfiguration an: xrandr ${XRANDR_ARGS[*]}"
        if ! xrandr "${XRANDR_ARGS[@]}" 2>&1; then
            warn "xrandr meldete einen Fehler beim Anwenden der Display-Konfiguration."
        fi
    fi

    # Hintergrundbild passend zur aktuellen Aufloesung/Drehung setzen
    if [[ -s "${WALLPAPER_FILE}" ]] && command -v feh >/dev/null 2>&1; then
        log "Setze Hintergrundbild: ${WALLPAPER_FILE}"
        feh --no-fehbg --bg-fill "${WALLPAPER_FILE}" >/dev/null 2>&1 || warn "feh konnte Hintergrundbild nicht setzen."
    fi
fi

# -----------------------------------------------------------------------------
# 3. Touch-Kalibrierung (Option A: auto via xinput map-to-output + optionaler Offset)
# -----------------------------------------------------------------------------
if (( X11_AVAILABLE == 1 )) && [[ -n "${TOUCH_DEVICE}" ]]; then
    if ! command -v xinput >/dev/null 2>&1; then
        warn "'xinput' nicht verfuegbar – Touch-Kalibrierung uebersprungen."
    else
        TOUCH_IDS=()
        if [[ "${TOUCH_DEVICE}" =~ ^[0-9]+$ ]]; then
            if xinput list --id-only "${TOUCH_DEVICE}" >/dev/null 2>&1; then
                TOUCH_IDS=("${TOUCH_DEVICE}")
            fi
        else
            while IFS= read -r id; do
                [[ -n "${id}" ]] && TOUCH_IDS+=("${id}")
            done < <(
                xinput list 2>/dev/null | awk -v name="${TOUCH_DEVICE}" '
                    /slave[[:space:]]+pointer/ {
                        line = $0
                        sub(/^[[:space:]↳⎜⎢⎡⎣│├└─]+/, "", line)
                        sub(/[[:space:]]+id=[0-9]+.*$/, "", line)
                        if (line == name) {
                            if (match($0, /id=[0-9]+/)) {
                                print substr($0, RSTART + 3, RLENGTH - 3)
                            }
                        }
                    }
                '
            )
            if (( ${#TOUCH_IDS[@]} == 0 )); then
                if fallback_id="$(xinput list --id-only "pointer:${TOUCH_DEVICE}" 2>/dev/null)"; then
                    [[ -n "${fallback_id}" ]] && TOUCH_IDS=("${fallback_id}")
                fi
            fi
        fi

        if (( ${#TOUCH_IDS[@]} == 0 )); then
            warn "Touch-Geraet '${TOUCH_DEVICE}' nicht gefunden – uebersprungen."
        else
            for dev_id in "${TOUCH_IDS[@]}"; do
                # 1) Touch-Geraet an Primaer-Monitor binden
                #    Hinweis: xinput map-to-output uebernimmt die aktuelle xrandr-Drehung automatisch!
                if [[ -n "${ACTIVE_PRIMARY}" ]]; then
                    log "Binde Touch-Geraet ID=${dev_id} ('${TOUCH_DEVICE}') an Monitor '${ACTIVE_PRIMARY}'..."
                    if ! xinput map-to-output "${dev_id}" "${ACTIVE_PRIMARY}" >/dev/null 2>&1; then
                        warn "xinput map-to-output fuer ID=${dev_id} auf '${ACTIVE_PRIMARY}' fehlgeschlagen."
                    fi
                fi

                # 2) Bei 'auto' oder '0' keine zusaetzliche Rotation aufmultiplizieren
                if [[ "${TOUCH_ROTATION}" == "auto" || "${TOUCH_ROTATION}" == "0" ]]; then
                    log "Touch-Ausrichtung fuer ID=${dev_id}: automatisch (folgt Display-Drehung)."
                    continue
                fi

                # 3) Optionale manuelle Korrektur fuer physisch verdreht eingebaute Touch-Panels
                case "${TOUCH_ROTATION}" in
                    90)  ROT_MAT="0 1 0 -1 0 1 0 0 1" ;;
                    180) ROT_MAT="-1 0 1 0 -1 1 0 0 1" ;;
                    270) ROT_MAT="0 -1 1 1 0 0 0 0 1" ;;
                esac

                CURRENT_MAT="$(xinput list-props "${dev_id}" 2>/dev/null | awk -F: '
                    /Coordinate Transformation Matrix/ {
                        gsub(/,/, " ", $2)
                        gsub(/^[ \t]+|[ \t]+$/, "", $2)
                        print $2
                        exit
                    }
                ')"

                if [[ -n "${CURRENT_MAT}" ]]; then
                    FINAL_MAT="$(awk -v m="${CURRENT_MAT}" -v r="${ROT_MAT}" '
                        BEGIN {
                            n1 = split(m, A, /[ \t]+/)
                            n2 = split(r, B, /[ \t]+/)
                            if (n1 < 9 || n2 < 9) {
                                print r
                                exit
                            }
                            for (i = 0; i < 3; i++) {
                                for (j = 0; j < 3; j++) {
                                    sum = 0
                                    for (k = 0; k < 3; k++) {
                                        sum += A[i*3 + k + 1] * B[k*3 + j + 1]
                                    }
                                    if (sum > -0.000001 && sum < 0.000001) sum = 0
                                    printf "%.6g%s", sum, (i==2 && j==2 ? "\n" : " ")
                                }
                            }
                        }
                    ')"
                else
                    FINAL_MAT="${ROT_MAT}"
                fi

                log "Wende manuelle Touch-Korrektur (+${TOUCH_ROTATION} deg) fuer ID=${dev_id} an: ${FINAL_MAT}"
                # shellcheck disable=SC2086
                if ! xinput set-prop "${dev_id}" "Coordinate Transformation Matrix" ${FINAL_MAT} 2>&1; then
                    warn "Konnte Coordinate Transformation Matrix fuer ID=${dev_id} nicht setzen."
                fi
            done
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 4. Audio-Konfiguration (PipeWire / pactl)
# -----------------------------------------------------------------------------
if ! command -v pactl >/dev/null 2>&1; then
    warn "'pactl' nicht installiert – Audio-Konfiguration uebersprungen."
elif ! pactl info >/dev/null 2>&1; then
    warn "PipeWire/PulseAudio nicht erreichbar (XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR}) – Audio uebersprungen."
else
    TARGET_SINK="@DEFAULT_SINK@"

    if [[ -n "${AUDIO_CARD}" && -n "${AUDIO_PROFILE}" ]]; then
        log "Setze Audio-Profil '${AUDIO_PROFILE}' auf Karte '${AUDIO_CARD}'..."
        if pactl set-card-profile "${AUDIO_CARD}" "${AUDIO_PROFILE}" 2>&1; then
            sleep 0.35

            CARD_SHORT="${AUDIO_CARD#alsa_card.}"
            FOUND_SINK="$(pactl list short sinks 2>/dev/null | awk -v key="${CARD_SHORT}" '
                index($2, key) > 0 { print $2; exit }
            ')"
            if [[ -n "${FOUND_SINK}" ]]; then
                TARGET_SINK="${FOUND_SINK}"
                pactl set-default-sink "${TARGET_SINK}" >/dev/null 2>&1 || true
                log "Standard-Audioausgang gesetzt: ${TARGET_SINK}"
            fi
        else
            warn "Audio-Profil '${AUDIO_PROFILE}' konnte auf Karte '${AUDIO_CARD}' nicht gesetzt werden."
        fi
    fi

    log "Setze Lautstaerke auf ${AUDIO_VOLUME}% und hebe Stummschaltung auf (${TARGET_SINK})..."
    if ! pactl set-sink-mute "${TARGET_SINK}" 0 2>&1; then
        warn "Stummschaltung fuer '${TARGET_SINK}' konnte nicht aufgehoben werden."
    fi
    if ! pactl set-sink-volume "${TARGET_SINK}" "${AUDIO_VOLUME}%" 2>&1; then
        warn "Lautstaerke fuer '${TARGET_SINK}' konnte nicht auf ${AUDIO_VOLUME}% gesetzt werden."
    fi
fi

if (( WARNINGS > 0 )); then
    log "Konfiguration mit ${WARNINGS} Hinweis(en)/Warnung(en) abgeschlossen."
else
    log "Konfiguration erfolgreich angewendet."
fi
exit 0
