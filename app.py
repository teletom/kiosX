#!/usr/bin/env python3
"""
kiosX - Flask Backend & Web-UI zur Kiosk-Konfiguration fuer Ubuntu Server Mini-PCs.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CONFIG_PATH = Path(os.environ.get("KIOSX_CONFIG", "/etc/kiosk/config.json"))
APPLY_SCRIPT = Path(os.environ.get("KIOSX_APPLY_SCRIPT", "/opt/kiosX/kiosk-apply.sh"))
LISTEN_HOST = os.environ.get("KIOSX_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("KIOSX_PORT", "8080"))

VALID_DISPLAY_ROTATIONS = ("normal", "right", "inverted", "left")
VALID_SECONDARY_MODES = ("off", "right", "left", "mirror")
VALID_TOUCH_ROTATIONS = ("0", "90", "180", "270")

TOUCH_NAME_REGEX = re.compile(
    r"(touch|goodix|elan|egalax|ilitek|wacom|ft54|gt9|elo|3m|sis|atmel|"
    r"weida|multitouch|digitizer|pen|eeti|sitronix|novatek|chicony|hid-over-i2c)",
    re.IGNORECASE,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "url": "https://example.com",
    "display": {
        "primary_output": "",
        "resolution": "auto",
        "rotation": "normal",
        "secondary_output": "",
        "secondary_mode": "off",
    },
    "touch": {
        "device": "",
        "rotation": "0",
    },
    "audio": {
        "card": "",
        "profile": "",
        "volume": 75,
    },
}


# ---------------------------------------------------------------------------
# Umgebungs- und Prozess-Helfer
# ---------------------------------------------------------------------------
def build_runtime_env() -> dict[str, str]:
    """Erstellt die Prozessumgebung fuer X11- und PipeWire-Kommandos."""
    env = os.environ.copy()
    uid = os.getuid()
    home = str(Path.home())
    xdg_runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"

    env["LC_ALL"] = "C"
    env["DISPLAY"] = env.get("DISPLAY") or ":0"
    env["XAUTHORITY"] = env.get("XAUTHORITY") or str(Path(home) / ".Xauthority")
    env["XDG_RUNTIME_DIR"] = xdg_runtime
    env["PULSE_RUNTIME_PATH"] = env.get("PULSE_RUNTIME_PATH") or str(Path(xdg_runtime) / "pulse")
    return env


def run_cmd(cmd: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    """Fuehrt einen Systembefehl mit X11/PipeWire-Umgebung sicher aus."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=build_runtime_env(),
        check=False,
    )


def get_runtime_dir() -> Path:
    """Gibt ein beschreibbares Laufzeitverzeichnis fuer PID-Dateien zurueck."""
    xdg = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if xdg.is_dir() and os.access(xdg, os.W_OK):
        return xdg
    return Path(tempfile.gettempdir())


def get_touch_test_pid_file() -> Path:
    return get_runtime_dir() / "kiosx-touch-test.pid"


def resolve_chromium_binary() -> str | None:
    """Ermittelt das installierte Chromium-Binary (apt oder Snap)."""
    for candidate in ("chromium", "chromium-browser", "/snap/bin/chromium"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def get_touch_test_profile_dir(chrome_bin: str) -> Path:
    """Liefert ein Snap-kompatibles Profilverzeichnis fuer das Touch-Test-Fenster."""
    real_bin = os.path.realpath(chrome_bin)
    if "/snap/" in chrome_bin or "/snap/" in real_bin:
        profile = Path.home() / "snap" / "chromium" / "common" / "kiosx-touch-test"
    else:
        profile = get_runtime_dir() / "kiosx-touch-test-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


# ---------------------------------------------------------------------------
# Konfiguration laden, validieren & atomar speichern
# ---------------------------------------------------------------------------
def merge_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    if isinstance(raw.get("url"), str) and raw["url"].strip():
        cfg["url"] = raw["url"].strip()

    for section in ("display", "touch", "audio"):
        sub = raw.get(section)
        if isinstance(sub, dict):
            for key in cfg[section]:
                if key in sub and sub[key] is not None:
                    cfg[section][key] = sub[key]

    # Typen normalisieren
    cfg["touch"]["rotation"] = str(cfg["touch"]["rotation"])
    try:
        cfg["audio"]["volume"] = max(0, min(100, int(cfg["audio"]["volume"])))
    except (TypeError, ValueError):
        cfg["audio"]["volume"] = 75

    return cfg


def load_config() -> dict[str, Any]:
    try:
        if CONFIG_PATH.is_file():
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                return merge_defaults(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    return copy.deepcopy(DEFAULT_CONFIG)


def validate_and_normalize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Ungueltiges Anfrageformat (JSON-Objekt erwartet)."

    cfg = merge_defaults(payload)

    url = cfg["url"].strip()
    if not url:
        return None, "Bitte eine gueltige Kiosk-URL angeben."
    if not re.match(r"^(https?://|file://)", url, re.IGNORECASE):
        if "://" in url or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d+(/|$))", url):
            return None, "Nur http://, https:// oder file:// URLs sind zulaessig."
        url = f"https://{url}"
    cfg["url"] = url

    disp = cfg["display"]
    disp["primary_output"] = str(disp.get("primary_output", "")).strip()
    disp["resolution"] = str(disp.get("resolution", "auto")).strip() or "auto"
    if disp["resolution"] != "auto" and not re.match(r"^\d+x\d+$", disp["resolution"]):
        return None, f"Ungueltige Aufloesung: {disp['resolution']}"

    if disp["rotation"] not in VALID_DISPLAY_ROTATIONS:
        return None, f"Ungueltige Display-Drehung: {disp['rotation']}"

    disp["secondary_output"] = str(disp.get("secondary_output", "")).strip()
    if disp["secondary_mode"] not in VALID_SECONDARY_MODES:
        return None, f"Ungueltiger Zweitdisplay-Modus: {disp['secondary_mode']}"

    if disp["secondary_output"] and disp["secondary_output"] == disp["primary_output"]:
        disp["secondary_output"] = ""
        disp["secondary_mode"] = "off"

    touch = cfg["touch"]
    touch["device"] = str(touch.get("device", "")).strip()
    touch["rotation"] = str(touch.get("rotation", "0")).strip()
    if touch["rotation"] not in VALID_TOUCH_ROTATIONS:
        return None, f"Ungueltige Touch-Ausrichtung: {touch['rotation']}"

    audio = cfg["audio"]
    audio["card"] = str(audio.get("card", "")).strip()
    audio["profile"] = str(audio.get("profile", "")).strip()
    try:
        audio["volume"] = max(0, min(100, int(audio.get("volume", 75))))
    except (TypeError, ValueError):
        return None, "Lautstaerke muss eine Zahl zwischen 0 und 100 sein."

    return cfg, None


def save_config_atomic(cfg: dict[str, Any]) -> None:
    """Schreibt /etc/kiosk/config.json atomar ueber eine temporaere Datei im selben Verzeichnis."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(CONFIG_PATH.parent),
            prefix=".config.json.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            json.dump(cfg, tmp_file, indent=2, ensure_ascii=False)
            tmp_file.write("\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        try:
            os.chmod(tmp_path, 0o664)
        except OSError:
            pass
        os.replace(tmp_path, CONFIG_PATH)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Dynamische Hardware-Erkennung (xrandr, xinput, pactl)
# ---------------------------------------------------------------------------
def get_displays() -> dict[str, Any]:
    """Liest verbundene Monitore und unterstuetzte Aufloesungen via xrandr --query."""
    if not shutil.which("xrandr"):
        return {"available": False, "error": "xrandr ist nicht installiert.", "displays": []}

    try:
        res = run_cmd(["xrandr", "--query"], timeout=6)
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "Zeitueberschreitung bei xrandr.", "displays": []}
    except OSError as exc:
        return {"available": False, "error": str(exc), "displays": []}

    if res.returncode != 0:
        err_msg = (res.stderr or res.stdout).strip() or "X11-Session nicht erreichbar."
        return {"available": False, "error": err_msg, "displays": []}

    displays: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    header_re = re.compile(r"^(\S+)\s+(connected|disconnected)\b(.*)$")
    mode_re = re.compile(r"^\s+(\d+x\d+)\s+(.*)$")
    geom_rot_re = re.compile(
        r"\b(\d+x\d+)\+\d+\+\d+\s*(normal|left|right|inverted)?\s*\("
    )

    for line in res.stdout.splitlines():
        m_header = header_re.match(line)
        if m_header:
            name, state, rest = m_header.groups()
            if state == "connected":
                is_primary = " primary " in f" {rest} "
                cur_mode = ""
                cur_rot = "normal"
                m_gr = geom_rot_re.search(rest)
                if m_gr:
                    cur_mode = m_gr.group(1) or ""
                    cur_rot = m_gr.group(2) or "normal"
                current = {
                    "name": name,
                    "primary": is_primary,
                    "current_mode": cur_mode,
                    "current_rotation": cur_rot,
                    "resolutions": [],
                }
                displays.append(current)
            else:
                current = None
            continue

        if current is not None:
            m_mode = mode_re.match(line)
            if m_mode:
                mode_str, flags = m_mode.groups()
                if mode_str not in current["resolutions"]:
                    current["resolutions"].append(mode_str)
                if "*" in flags and not current["current_mode"]:
                    current["current_mode"] = mode_str
            elif line and not line[0].isspace():
                current = None

    return {"available": True, "error": None, "displays": displays}


def get_touch_devices() -> dict[str, Any]:
    """Liest verfuegbare Pointer-/Touch-Eingabegeraete via xinput list."""
    if not shutil.which("xinput"):
        return {"available": False, "error": "xinput ist nicht installiert.", "devices": []}

    try:
        res = run_cmd(["xinput", "list"], timeout=6)
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "Zeitueberschreitung bei xinput.", "devices": []}
    except OSError as exc:
        return {"available": False, "error": str(exc), "devices": []}

    if res.returncode != 0:
        err_msg = (res.stderr or res.stdout).strip() or "X11-Session fuer xinput nicht erreichbar."
        return {"available": False, "error": err_msg, "devices": []}

    devices: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    line_re = re.compile(r"^(.*?)\s+id=(\d+)\s+\[slave\s+pointer\b")

    for raw_line in res.stdout.splitlines():
        m = line_re.search(raw_line)
        if not m:
            continue
        raw_name, dev_id = m.group(1), int(m.group(2))
        name = re.sub(r"^[\s\u239c\u23a1\u23a2\u23a3\u21b3│├└─]+", "", raw_name).strip()
        if not name or "XTEST" in name.upper():
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        devices.append(
            {
                "id": dev_id,
                "name": name,
                "is_touch": bool(TOUCH_NAME_REGEX.search(name)),
            }
        )

    devices.sort(key=lambda d: (not d["is_touch"], d["name"].lower()))
    return {"available": True, "error": None, "devices": devices}


def _classify_audio_profile(name: str, description: str) -> str:
    combined = f"{name} {description}".lower()
    if "hdmi" in combined or "displayport" in combined:
        return "HDMI / DisplayPort"
    if "analog" in combined or "line" in combined or "headphone" in combined or "speaker" in combined:
        return "Analog / Klinke"
    if "iec958" in combined or "spdif" in combined:
        return "Digital (S/PDIF)"
    return "Ausgang"


def get_audio_cards() -> dict[str, Any]:
    """Liest Soundkarten und verfuegbare Ausgabe-Profile (HDMI vs. Analog) via pactl list cards."""
    if not shutil.which("pactl"):
        return {"available": False, "error": "pactl ist nicht installiert.", "cards": []}

    try:
        res = run_cmd(["pactl", "list", "cards"], timeout=6)
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "Zeitueberschreitung bei pactl.", "cards": []}
    except OSError as exc:
        return {"available": False, "error": str(exc), "cards": []}

    if res.returncode != 0:
        err_msg = (res.stderr or res.stdout).strip() or "PipeWire/PulseAudio nicht erreichbar."
        return {"available": False, "error": err_msg, "cards": []}

    cards: list[dict[str, Any]] = []
    blocks = re.split(r"(?m)^Card #\d+\s*$", res.stdout)

    for block in blocks:
        if not block.strip():
            continue

        name_m = re.search(r"(?m)^\s*Name:\s*(.+)$", block)
        if not name_m:
            continue
        card_name = name_m.group(1).strip()

        desc_m = re.search(r'(?m)^\s*device\.description\s*=\s*"([^"]+)"', block)
        alsa_m = re.search(r'(?m)^\s*alsa\.card_name\s*=\s*"([^"]+)"', block)
        description = (
            (desc_m.group(1).strip() if desc_m else "")
            or (alsa_m.group(1).strip() if alsa_m else "")
            or card_name
        )

        active_m = re.search(r"(?m)^\s*Active Profile:\s*(.+)$", block)
        active_profile = active_m.group(1).strip() if active_m else ""

        profiles: list[dict[str, Any]] = []
        in_profiles = False
        for line in block.splitlines():
            stripped = line.strip()
            if re.match(r"^\tProfiles:\s*$", line) or stripped == "Profiles:":
                in_profiles = True
                continue
            if in_profiles:
                if line.startswith("\t") and not line.startswith("\t\t") and stripped.endswith(":"):
                    break
                if stripped.startswith("Active Profile:") or stripped.startswith("Ports:"):
                    break
                if ": " not in stripped:
                    continue

                prof_key, rest = stripped.split(": ", 1)
                prof_key = prof_key.strip()
                if prof_key == "off":
                    continue

                sinks_m = re.search(r"sinks:\s*(\d+)", rest)
                sinks_count = int(sinks_m.group(1)) if sinks_m else 0
                if sinks_count == 0 and "output:" not in prof_key:
                    continue

                avail_m = re.search(r"available:\s*(yes|no|unknown)", rest, re.IGNORECASE)
                available = (avail_m.group(1).lower() != "no") if avail_m else True

                prof_desc = re.sub(r"\s*\(sinks:.*\)$", "", rest).strip() or prof_key
                kind = _classify_audio_profile(prof_key, prof_desc)
                profiles.append(
                    {
                        "name": prof_key,
                        "description": prof_desc,
                        "kind": kind,
                        "available": available,
                    }
                )

        cards.append(
            {
                "name": card_name,
                "description": description,
                "active_profile": active_profile,
                "profiles": profiles,
            }
        )

    return {"available": True, "error": None, "cards": cards}


# ---------------------------------------------------------------------------
# Touch-Test & Browser-Steuerung
# ---------------------------------------------------------------------------
def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        # Zombie-Prozesse ausschliessen
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.is_file():
            content = stat_path.read_text(encoding="utf-8", errors="ignore")
            rparen = content.rfind(")")
            if rparen != -1:
                state = content[rparen + 2 : rparen + 3]
                if state == "Z":
                    return False
        return True
    except OSError:
        return False


def get_touch_test_pid() -> int | None:
    pid_file = get_touch_test_pid_file()
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return None

    if is_pid_alive(pid):
        return pid
    pid_file.unlink(missing_ok=True)
    return None


def stop_touch_test() -> bool:
    """Beendet das aktive Touch-Test-Vollbildfenster sauber."""
    pid_file = get_touch_test_pid_file()
    pid = get_touch_test_pid()
    stopped = False

    if pid is not None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        for _ in range(15):
            if not is_pid_alive(pid):
                break
            time.sleep(0.1)

        if is_pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        stopped = True

    # Sicherheitshalber eventuelle Restprozesse mit dem Touch-Test-Profil beenden
    try:
        run_cmd(["pkill", "-f", "kiosx-touch-test"], timeout=3)
    except OSError:
        pass

    pid_file.unlink(missing_ok=True)
    return stopped


def start_touch_test() -> tuple[bool, str]:
    """Oeffnet die Touch-Test-Vollbildansicht auf dem Kiosk-Bildschirm."""
    chrome_bin = resolve_chromium_binary()
    if not chrome_bin:
        return False, "Chromium ist nicht installiert."

    # Pruefen, ob X11 erreichbar ist
    if shutil.which("xset"):
        check = run_cmd(["xset", "q"], timeout=4)
        if check.returncode != 0:
            return False, "Keine aktive X11-Session unter DISPLAY=:0 erreichbar."

    stop_touch_test()
    profile_dir = get_touch_test_profile_dir(chrome_bin)
    test_url = f"http://127.0.0.1:{LISTEN_PORT}/touch-test-view"

    cmd = [
        chrome_bin,
        "--kiosk",
        f"--user-data-dir={profile_dir}",
        "--noerrdialogs",
        "--disable-infobars",
        "--no-first-run",
        "--overscroll-history-navigation=0",
        "--disable-pinch",
        "--start-fullscreen",
        "--new-window",
        test_url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            env=build_runtime_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"Touch-Test konnte nicht gestartet werden: {exc}"

    get_touch_test_pid_file().write_text(str(proc.pid), encoding="utf-8")
    return True, "Touch-Testansicht wurde im Vollbild geoeffnet."


def is_browser_running() -> bool:
    """Prueft, ob ein Chromium-Kiosk-Prozess aktiv ist."""
    try:
        res = run_cmd(["pgrep", "-f", "kiosx-(profile|chromium)"], timeout=3)
        if res.returncode == 0:
            return True
        res2 = run_cmd(["pgrep", "-x", "chromium|chrome"], timeout=3)
        return res2.returncode == 0
    except OSError:
        return False


def get_system_info() -> dict[str, Any]:
    hostname = socket.gethostname()
    ip_addr = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip_addr = s.getsockname()[0]
    except OSError:
        ip_addr = "127.0.0.1"

    return {
        "hostname": hostname,
        "ip": ip_addr,
        "port": LISTEN_PORT,
        "config_path": str(CONFIG_PATH),
        "touch_test_active": get_touch_test_pid() is not None,
        "browser_running": is_browser_running(),
    }


def collect_hardware() -> dict[str, Any]:
    return {
        "displays": get_displays(),
        "touch": get_touch_devices(),
        "audio": get_audio_cards(),
        "system": get_system_info(),
    }


# ---------------------------------------------------------------------------
# Stereo-Testton erzeugen & abspielen
# ---------------------------------------------------------------------------
def generate_stereo_test_wav(path: str) -> None:
    """Erzeugt eine kurze 1,2s Stereo-Testton-WAV-Datei (Links 440Hz, Rechts 554Hz, Stereo)."""
    sample_rate = 44100
    duration_sec = 1.2
    total_frames = int(sample_rate * duration_sec)
    amplitude = 0.35 * 32767

    frames = bytearray()
    for i in range(total_frames):
        t = i / sample_rate
        left = 0.0
        right = 0.0

        if 0.0 <= t < 0.38:
            # Linker Kanal (440 Hz)
            env = min(1.0, t / 0.02, max(0.0, (0.38 - t) / 0.03))
            left = math.sin(2.0 * math.pi * 440.0 * t) * env
        elif 0.42 <= t < 0.80:
            # Rechter Kanal (554.37 Hz)
            dt = t - 0.42
            env = min(1.0, dt / 0.02, max(0.0, (0.80 - t) / 0.03))
            right = math.sin(2.0 * math.pi * 554.37 * t) * env
        elif 0.84 <= t < 1.18:
            # Beide Kanaele (659.25 Hz)
            dt = t - 0.84
            env = min(1.0, dt / 0.02, max(0.0, (1.18 - t) / 0.05))
            tone = math.sin(2.0 * math.pi * 659.25 * t) * env
            left = tone
            right = tone

        l_val = max(-32767, min(32767, int(left * amplitude)))
        r_val = max(-32767, min(32767, int(right * amplitude)))
        frames.extend(struct.pack("<hh", l_val, r_val))

    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))


# ---------------------------------------------------------------------------
# Flask-Routen
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    config = load_config()
    hardware = collect_hardware()
    return render_template("index.html", config=config, hardware=hardware)


@app.route("/api/hardware", methods=["GET"])
def api_hardware():
    return jsonify({"ok": True, "hardware": collect_hardware(), "config": load_config()})


@app.route("/save", methods=["POST"])
def save_settings():
    payload = request.get_json(silent=True)
    if payload is None:
        # Fallback fuer klassische Formular-Uebermittlung
        form = request.form
        payload = {
            "url": form.get("url", ""),
            "display": {
                "primary_output": form.get("primary_output", ""),
                "resolution": form.get("resolution", "auto"),
                "rotation": form.get("rotation", "normal"),
                "secondary_output": form.get("secondary_output", ""),
                "secondary_mode": form.get("secondary_mode", "off"),
            },
            "touch": {
                "device": form.get("touch_device", ""),
                "rotation": form.get("touch_rotation", "0"),
            },
            "audio": {
                "card": form.get("audio_card", ""),
                "profile": form.get("audio_profile", ""),
                "volume": form.get("audio_volume", 75),
            },
        }

    cfg, err_msg = validate_and_normalize_payload(payload)
    if err_msg or cfg is None:
        return jsonify({"ok": False, "message": err_msg}), 400

    try:
        save_config_atomic(cfg)
    except OSError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": f"Konnte {CONFIG_PATH} nicht schreiben: {exc}",
                }
            ),
            500,
        )

    apply_output = ""
    warnings: list[str] = []
    apply_ok = True

    if APPLY_SCRIPT.is_file():
        try:
            res = run_cmd(["/usr/bin/env", "bash", str(APPLY_SCRIPT)], timeout=15)
            combined = "\n".join(part for part in (res.stdout.strip(), res.stderr.strip()) if part)
            apply_output = combined
            warnings = [
                line for line in combined.splitlines() if "[kiosX WARN]" in line or "[kiosX ERROR]" in line
            ]
            if res.returncode != 0:
                apply_ok = False
        except subprocess.TimeoutExpired:
            apply_ok = False
            warnings.append("Zeitueberschreitung beim Ausfuehren von kiosk-apply.sh.")
        except OSError as exc:
            apply_ok = False
            warnings.append(f"Fehler beim Starten von kiosk-apply.sh: {exc}")
    else:
        warnings.append(f"Apply-Skript nicht gefunden: {APPLY_SCRIPT}")

    if not apply_ok:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Gespeichert, aber beim Anwenden traten Fehler auf.",
                    "warnings": warnings,
                    "output": apply_output,
                    "config": cfg,
                }
            ),
            500,
        )

    msg = (
        "Konfiguration gespeichert und angewendet (mit Hinweisen)."
        if warnings
        else "Konfiguration gespeichert und sofort angewendet."
    )
    return jsonify(
        {
            "ok": True,
            "message": msg,
            "warnings": warnings,
            "output": apply_output,
            "config": cfg,
        }
    )


@app.route("/test-audio", methods=["POST"])
def test_audio():
    tmp_wav: str | None = None
    try:
        # 1. Bevorzugt: Stereo-Testton per paplay direkt ueber den aktiven PipeWire-Sink
        if shutil.which("paplay"):
            fd, tmp_wav = tempfile.mkstemp(prefix="kiosx-audio-test-", suffix=".wav")
            os.close(fd)
            generate_stereo_test_wav(tmp_wav)
            res = run_cmd(["paplay", tmp_wav], timeout=8)
            if res.returncode == 0:
                return jsonify(
                    {
                        "ok": True,
                        "method": "paplay",
                        "message": "Stereo-Testton (Links / Rechts / Stereo) abgespielt.",
                    }
                )
            paplay_err = (res.stderr or res.stdout).strip()
        else:
            paplay_err = "paplay nicht installiert"

        # 2. Fallback: speaker-test -t wav -c 2 -l 1
        if shutil.which("speaker-test"):
            res = run_cmd(["speaker-test", "-t", "wav", "-c", "2", "-l", "1"], timeout=10)
            if res.returncode == 0:
                return jsonify(
                    {
                        "ok": True,
                        "method": "speaker-test",
                        "message": "Stereo-Testton via speaker-test abgespielt.",
                    }
                )
            st_err = (res.stderr or res.stdout).strip()
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": f"Testton fehlgeschlagen (paplay: {paplay_err}; speaker-test: {st_err})",
                    }
                ),
                500,
            )

        return (
            jsonify(
                {
                    "ok": False,
                    "message": f"Kein Audio-Testtool erfolgreich ({paplay_err}).",
                }
            ),
            500,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "Zeitueberschreitung beim Abspielen des Testtons."}), 504
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Audio-Test fehlgeschlagen: {exc}"}), 500
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass


@app.route("/touch-test-view", methods=["GET"])
def touch_test_view():
    return render_template("touch_test.html", config=load_config())


@app.route("/touch-test", methods=["POST"])
def touch_test():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "toggle")).strip().lower()
    active_now = get_touch_test_pid() is not None

    if action == "stop" or (action == "toggle" and active_now):
        stop_touch_test()
        return jsonify(
            {
                "ok": True,
                "active": False,
                "message": "Touch-Testansicht wurde geschlossen.",
            }
        )

    if action in ("start", "toggle"):
        ok, msg = start_touch_test()
        status = 200 if ok else 500
        return jsonify({"ok": ok, "active": ok, "message": msg}), status

    return jsonify({"ok": False, "message": f"Unbekannte Aktion: {action}"}), 400


@app.route("/restart-browser", methods=["POST"])
def restart_browser():
    """Beendet den laufenden Chromium-Kiosk-Prozess, sodass der Watchdog ihn sauber neu startet."""
    # Falls Touch-Test offen ist, ebenfalls schliessen
    stop_touch_test()

    patterns = [
        "kiosx-profile",
        "kiosx-chromium",
    ]
    signalled = False
    for pattern in patterns:
        res = run_cmd(["pkill", "-TERM", "-f", pattern], timeout=4)
        if res.returncode == 0:
            signalled = True

    if not signalled:
        # Fallback: regulaeren Chromium-Prozess des aktuellen Benutzers beenden
        uid = str(os.getuid())
        for proc_name in ("chromium", "chromium-browser", "chrome"):
            res = run_cmd(["pkill", "-TERM", "-u", uid, "-x", proc_name], timeout=4)
            if res.returncode == 0:
                signalled = True

    if signalled:
        time.sleep(1.2)
        for pattern in patterns:
            run_cmd(["pkill", "-KILL", "-f", pattern], timeout=3)
        return jsonify(
            {
                "ok": True,
                "message": "Chromium-Kiosk-Browser wird sauber neu gestartet (Openbox-Watchdog).",
            }
        )

    return jsonify(
        {
            "ok": True,
            "message": "Kein laufender Chromium-Prozess gefunden (Watchdog startet automatisch in X11).",
        }
    )


if __name__ == "__main__":
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
