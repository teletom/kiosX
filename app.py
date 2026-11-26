#!/usr/bin/env python3
"""
kiosX - Flask Backend & Web-UI zur Kiosk-Konfiguration fuer Ubuntu Server Mini-PCs.
"""

from __future__ import annotations

import atexit
import copy
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

app = Flask(__name__)

CONFIG_PATH = Path(os.environ.get("KIOSX_CONFIG", "/etc/kiosk/config.json"))
WALLPAPER_PATH = Path(os.environ.get("KIOSX_WALLPAPER", "/etc/kiosk/wallpaper.jpg"))
APPLY_SCRIPT = Path(os.environ.get("KIOSX_APPLY_SCRIPT", "/opt/kiosX/kiosk-apply.sh"))
LISTEN_HOST = os.environ.get("KIOSX_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("KIOSX_PORT", "8080"))

DEFAULT_WALLPAPER_URL = (
    "https://images.unsplash.com/photo-1493606278519-11aa9f86e40a"
    "?auto=format&fit=crop&w=1920&q=85"
)
DEVICE_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
DEVICE_ID_LENGTH = 4
MAX_WALLPAPER_BYTES = 15 * 1024 * 1024  # 15 MB

VALID_DISPLAY_ROTATIONS = ("normal", "right", "inverted", "left")
VALID_SECONDARY_MODES = ("off", "right", "left", "mirror")
VALID_TOUCH_ROTATIONS = ("auto", "90", "180", "270")

TOUCH_NAME_REGEX = re.compile(
    r"(touch|goodix|elan|egalax|ilitek|wacom|ft54|gt9|elo|3m|sis|atmel|"
    r"weida|multitouch|digitizer|pen|eeti|sitronix|novatek|chicony|hid-over-i2c)",
    re.IGNORECASE,
)

DEFAULT_CONFIG: dict[str, Any] = {
    "device_id": "",
    "url": "https://example.com",
    "splash_seconds": 15,
    "wallpaper_url": DEFAULT_WALLPAPER_URL,
    "display": {
        "primary_output": "",
        "resolution": "auto",
        "rotation": "normal",
        "secondary_output": "",
        "secondary_mode": "off",
    },
    "touch": {
        "device": "",
        "rotation": "auto",
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
    for candidate in ("chromium", "/snap/bin/chromium", "chromium-browser"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def get_touch_test_profile_dir(chrome_bin: str) -> Path:
    """Liefert ein Snap-kompatibles Profilverzeichnis fuer das Touch-Test-Fenster."""
    real_bin = os.path.realpath(chrome_bin)
    is_snap = "/snap/" in chrome_bin or "/snap/" in real_bin or real_bin.endswith("/snap")
    if not is_snap and os.path.isfile(chrome_bin):
        try:
            with open(chrome_bin, "rb") as fh:
                head = fh.read(2048)
            if head.startswith(b"#!") and b"snap" in head:
                is_snap = True
        except OSError:
            pass

    if is_snap:
        profile = Path.home() / "snap" / "chromium" / "common" / "kiosx-touch-test"
    else:
        profile = get_runtime_dir() / "kiosx-touch-test-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def generate_device_id(length: int = DEVICE_ID_LENGTH) -> str:
    """Erzeugt eine kurze, verwechslungssichere Kiosk-ID (ohne 0/O/1/I/L/U)."""
    return "".join(secrets.choice(DEVICE_ID_ALPHABET) for _ in range(length))


def normalize_device_id(raw: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if 2 <= len(cleaned) <= 12:
        return cleaned
    return ""


# ---------------------------------------------------------------------------
# Netzwerk-Erkennung (LAN, Tailscale, mDNS-Alias)
# ---------------------------------------------------------------------------
def get_network_addresses() -> dict[str, str]:
    """Ermittelt LAN-IP und Tailscale-IP ueber 'ip -4 addr' mit Socket-Fallback."""
    lan_ip = ""
    tailscale_ip = ""

    if shutil.which("ip"):
        try:
            res = run_cmd(["ip", "-4", "-o", "addr", "show", "scope", "global"], timeout=3)
            if res.returncode == 0:
                cgnat = ipaddress.ip_network("100.64.0.0/10")
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    iface = parts[1]
                    addr = parts[3].split("/")[0]
                    try:
                        ip_obj = ipaddress.ip_address(addr)
                    except ValueError:
                        continue
                    if iface.startswith("tailscale") or ip_obj in cgnat:
                        if not tailscale_ip:
                            tailscale_ip = addr
                    elif not iface.startswith(("docker", "br-", "veth", "virbr", "lxd")):
                        if not lan_ip:
                            lan_ip = addr
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not lan_ip:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                cand = s.getsockname()[0]
                if not cand.startswith("100."):
                    lan_ip = cand
                elif not tailscale_ip:
                    tailscale_ip = cand
        except OSError:
            pass

    return {"lan_ip": lan_ip, "tailscale_ip": tailscale_ip}


class MdnsAliasManager:
    """Veroeffentlicht den kurzen mDNS-Alias kiosx-<id>.local ueber avahi-publish."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._current_alias: str = ""
        self._current_ip: str = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not shutil.which("avahi-publish"):
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="kiosx-mdns", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def sync_now(self, device_id: str | None = None) -> None:
        if not shutil.which("avahi-publish"):
            return
        with self._lock:
            dev_id = normalize_device_id(device_id) if device_id else normalize_device_id(load_config().get("device_id"))
            if not dev_id:
                return
            alias = f"kiosx-{dev_id.lower()}.local"
            addrs = get_network_addresses()
            ip_addr = addrs["lan_ip"]
            if not ip_addr:
                return

            if (
                self._proc is not None
                and self._proc.poll() is None
                and self._current_alias == alias
                and self._current_ip == ip_addr
            ):
                return

            self._terminate_locked()
            try:
                self._proc = subprocess.Popen(
                    ["avahi-publish", "-a", "-R", alias, ip_addr],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._current_alias = alias
                self._current_ip = ip_addr
            except OSError:
                self._proc = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.sync_now()
            except Exception:
                pass
            self._stop_event.wait(30.0)

    def _terminate_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._current_alias = ""
            self._current_ip = ""

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._terminate_locked()


mdns_manager = MdnsAliasManager()


# ---------------------------------------------------------------------------
# Konfiguration laden, validieren & atomar speichern
# ---------------------------------------------------------------------------
def merge_defaults(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg

    if isinstance(raw.get("device_id"), str):
        cfg["device_id"] = normalize_device_id(raw["device_id"])

    if isinstance(raw.get("url"), str) and raw["url"].strip():
        cfg["url"] = raw["url"].strip()

    if "splash_seconds" in raw and raw["splash_seconds"] is not None:
        try:
            cfg["splash_seconds"] = max(0, min(120, int(raw["splash_seconds"])))
        except (TypeError, ValueError):
            cfg["splash_seconds"] = 15

    if "wallpaper_url" in raw and raw["wallpaper_url"] is not None:
        cfg["wallpaper_url"] = str(raw["wallpaper_url"]).strip()

    for section in ("display", "touch", "audio"):
        sub = raw.get(section)
        if isinstance(sub, dict):
            for key in cfg[section]:
                if key in sub and sub[key] is not None:
                    cfg[section][key] = sub[key]

    # Legacy touch.rotation == "0" sauber auf "auto" mappen
    touch_rot = str(cfg["touch"]["rotation"]).strip().lower()
    cfg["touch"]["rotation"] = "auto" if touch_rot in ("", "0", "auto") else touch_rot

    try:
        cfg["audio"]["volume"] = max(0, min(100, int(cfg["audio"]["volume"])))
    except (TypeError, ValueError):
        cfg["audio"]["volume"] = 75

    return cfg


def load_config(ensure_id: bool = True) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    loaded_from_disk = False
    try:
        if CONFIG_PATH.is_file():
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                cfg = merge_defaults(json.load(fh))
                loaded_from_disk = True
    except (OSError, json.JSONDecodeError):
        pass

    if ensure_id and not cfg["device_id"]:
        cfg["device_id"] = generate_device_id()
        if loaded_from_disk or os.access(CONFIG_PATH.parent, os.W_OK):
            try:
                save_config_atomic(cfg)
            except OSError:
                pass
    return cfg


def _validate_http_url(url: str, allow_file: bool = True) -> tuple[str | None, str | None]:
    url = url.strip()
    if not url:
        return "", None
    allowed_re = r"^(https?://|file://)" if allow_file else r"^https?://"
    if not re.match(allowed_re, url, re.IGNORECASE):
        if "://" in url or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d+(/|$))", url):
            allowed_txt = "http://, https:// oder file://" if allow_file else "http:// oder https://"
            return None, f"Nur {allowed_txt} URLs sind zulaessig."
        url = f"https://{url}"
    return url, None


def validate_and_normalize_payload(
    payload: dict[str, Any],
    existing_config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Ungueltiges Anfrageformat (JSON-Objekt erwartet)."

    cfg = merge_defaults(payload)

    # Kiosk-ID validieren oder bestehende behalten / generieren
    raw_id = payload.get("device_id")
    if raw_id is not None and str(raw_id).strip():
        norm_id = normalize_device_id(raw_id)
        if not norm_id:
            return None, "Kiosk-ID muss aus 2 bis 12 Buchstaben/Zahlen bestehen."
        cfg["device_id"] = norm_id
    else:
        fallback_id = (existing_config or {}).get("device_id") or generate_device_id()
        cfg["device_id"] = normalize_device_id(fallback_id) or generate_device_id()

    url, url_err = _validate_http_url(cfg["url"], allow_file=True)
    if url_err or not url:
        return None, url_err or "Bitte eine gueltige Kiosk-URL angeben."
    cfg["url"] = url

    # Start-Anzeige Dauer (0 = deaktiviert, 1..120 Sekunden)
    try:
        splash_sec = int(payload.get("splash_seconds", cfg["splash_seconds"]))
        cfg["splash_seconds"] = max(0, min(120, splash_sec))
    except (TypeError, ValueError):
        return None, "Start-Anzeige-Dauer muss eine Zahl zwischen 0 und 120 Sekunden sein."

    # Optionales Hintergrundbild (leer = Standard / kein Download)
    wp_raw = str(payload.get("wallpaper_url", cfg["wallpaper_url"]) or "").strip()
    if wp_raw:
        wp_url, wp_err = _validate_http_url(wp_raw, allow_file=False)
        if wp_err:
            return None, f"Hintergrundbild-URL: {wp_err}"
        cfg["wallpaper_url"] = wp_url or ""
    else:
        cfg["wallpaper_url"] = ""

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
    touch_rot = str(touch.get("rotation", "auto")).strip().lower()
    if touch_rot == "0":
        touch_rot = "auto"
    if touch_rot not in VALID_TOUCH_ROTATIONS:
        return None, f"Ungueltige Touch-Korrektur: {touch_rot}"
    touch["rotation"] = touch_rot

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


def download_wallpaper_atomic(url: str) -> tuple[bool, str | None]:
    """Laedt das Hintergrundbild sicher herunter und speichert es atomar nach WALLPAPER_PATH."""
    if not url:
        return True, None

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "kiosX/1.0 (+https://github.com/teletom/kiosX)"},
    )
    tmp_path: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read(MAX_WALLPAPER_BYTES + 1)
            if len(data) > MAX_WALLPAPER_BYTES:
                return False, "Hintergrundbild ist groesser als 15 MB."
            if len(data) < 32:
                return False, "Heruntergeladene Datei ist leer oder ungueltig."

            # Einfache Magic-Byte-Pruefung fuer JPEG / PNG / WebP / GIF
            is_image = (
                data.startswith(b"\xff\xd8\xff")
                or data.startswith(b"\x89PNG\r\n\x1a\n")
                or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
                or data.startswith((b"GIF87a", b"GIF89a"))
            )
            if not is_image:
                return False, "URL liefert kein gueltiges Bildformat (JPEG, PNG oder WebP erwartet)."

        WALLPAPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(WALLPAPER_PATH.parent),
            prefix=".wallpaper.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        try:
            os.chmod(tmp_path, 0o664)
        except OSError:
            pass
        os.replace(tmp_path, WALLPAPER_PATH)
        tmp_path = None
        return True, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Hintergrundbild konnte nicht geladen werden: {exc}"
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
# Touch-Test, Browser-Steuerung & QR-Code
# ---------------------------------------------------------------------------
def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
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


def build_device_info(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    hostname = socket.gethostname()
    device_id = cfg.get("device_id") or ""
    addrs = get_network_addresses()
    lan_ip = addrs["lan_ip"]
    tailscale_ip = addrs["tailscale_ip"]

    mdns_host = f"{hostname}.local"
    mdns_alias = f"kiosx-{device_id.lower()}.local" if device_id else mdns_host

    lan_url = f"http://{lan_ip}:{LISTEN_PORT}" if lan_ip else ""
    tailscale_url = f"http://{tailscale_ip}:{LISTEN_PORT}" if tailscale_ip else ""
    mdns_url = f"http://{mdns_alias}:{LISTEN_PORT}"
    primary_config_url = lan_url or tailscale_url or mdns_url

    return {
        "device_id": device_id,
        "hostname": hostname,
        "lan_ip": lan_ip,
        "tailscale_ip": tailscale_ip,
        "ip": lan_ip or tailscale_ip or "127.0.0.1",
        "mdns_host": mdns_host,
        "mdns_alias": mdns_alias,
        "port": LISTEN_PORT,
        "config_url": primary_config_url,
        "lan_url": lan_url,
        "tailscale_url": tailscale_url,
        "mdns_url": mdns_url,
        "target_url": cfg.get("url", "https://example.com"),
        "splash_seconds": cfg.get("splash_seconds", 15),
        "has_wallpaper": WALLPAPER_PATH.is_file() and WALLPAPER_PATH.stat().st_size > 0,
        "config_path": str(CONFIG_PATH),
        "touch_test_active": get_touch_test_pid() is not None,
        "browser_running": is_browser_running(),
    }


def collect_hardware(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "displays": get_displays(),
        "touch": get_touch_devices(),
        "audio": get_audio_cards(),
        "system": build_device_info(cfg),
    }


def generate_qr_svg(data: str) -> str | None:
    """Erzeugt einen lokalen Inline-SVG-QR-Code via qrencode (oder python3-qrcode Fallback)."""
    if shutil.which("qrencode"):
        try:
            res = subprocess.run(
                ["qrencode", "-t", "SVG", "-m", "2", "--svg-path", "-o", "-", data],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            if res.returncode == 0 and "<svg" in res.stdout:
                return res.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass

    try:
        import io
        import qrcode  # type: ignore
        import qrcode.image.svg  # type: ignore

        buf = io.BytesIO()
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


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
            env = min(1.0, t / 0.02, max(0.0, (0.38 - t) / 0.03))
            left = math.sin(2.0 * math.pi * 440.0 * t) * env
        elif 0.42 <= t < 0.80:
            dt = t - 0.42
            env = min(1.0, dt / 0.02, max(0.0, (0.80 - t) / 0.03))
            right = math.sin(2.0 * math.pi * 554.37 * t) * env
        elif 0.84 <= t < 1.18:
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
    hardware = collect_hardware(config)
    return render_template("index.html", config=config, hardware=hardware)


@app.route("/splash", methods=["GET"])
def splash():
    config = load_config()
    info = build_device_info(config)
    return render_template("splash.html", info=info, config=config)


@app.route("/api/info", methods=["GET"])
def api_info():
    config = load_config()
    info = build_device_info(config)
    return jsonify({"ok": True, **info})


@app.route("/api/hardware", methods=["GET"])
def api_hardware():
    config = load_config()
    return jsonify({"ok": True, "hardware": collect_hardware(config), "config": config})


@app.route("/wallpaper.jpg", methods=["GET"])
def wallpaper_file():
    if WALLPAPER_PATH.is_file() and WALLPAPER_PATH.stat().st_size > 0:
        resp = send_file(str(WALLPAPER_PATH), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return Response(status=404)


@app.route("/qr.svg", methods=["GET"])
def qr_svg():
    info = build_device_info()
    target = (request.args.get("target") or "auto").strip().lower()
    if target == "tailscale" and info["tailscale_url"]:
        url = info["tailscale_url"]
    elif target == "mdns":
        url = info["mdns_url"]
    else:
        url = info["config_url"]

    svg = generate_qr_svg(url)
    if not svg:
        return Response(status=404)
    return Response(
        svg,
        mimetype="image/svg+xml",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.route("/save", methods=["POST"])
def save_settings():
    existing_cfg = load_config()
    payload = request.get_json(silent=True)
    if payload is None:
        form = request.form
        payload = {
            "device_id": form.get("device_id", existing_cfg.get("device_id", "")),
            "url": form.get("url", ""),
            "splash_seconds": form.get("splash_seconds", 15),
            "wallpaper_url": form.get("wallpaper_url", ""),
            "display": {
                "primary_output": form.get("primary_output", ""),
                "resolution": form.get("resolution", "auto"),
                "rotation": form.get("rotation", "normal"),
                "secondary_output": form.get("secondary_output", ""),
                "secondary_mode": form.get("secondary_mode", "off"),
            },
            "touch": {
                "device": form.get("touch_device", ""),
                "rotation": form.get("touch_rotation", "auto"),
            },
            "audio": {
                "card": form.get("audio_card", ""),
                "profile": form.get("audio_profile", ""),
                "volume": form.get("audio_volume", 75),
            },
        }

    cfg, err_msg = validate_and_normalize_payload(payload, existing_config=existing_cfg)
    if err_msg or cfg is None:
        return jsonify({"ok": False, "message": err_msg}), 400

    warnings: list[str] = []

    # Optionales Hintergrundbild herunterladen, falls URL geaendert wurde oder Datei fehlt
    new_wp = cfg.get("wallpaper_url", "")
    old_wp = existing_cfg.get("wallpaper_url", "")
    if new_wp and (new_wp != old_wp or not WALLPAPER_PATH.is_file()):
        wp_ok, wp_err = download_wallpaper_atomic(new_wp)
        if not wp_ok and wp_err:
            warnings.append(f"[kiosX WARN] {wp_err}")

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

    # mDNS-Alias sofort aktualisieren, falls sich die Kiosk-ID geaendert hat
    mdns_manager.sync_now(cfg.get("device_id"))

    apply_output = ""
    apply_ok = True

    if APPLY_SCRIPT.is_file():
        try:
            res = run_cmd(["/usr/bin/env", "bash", str(APPLY_SCRIPT)], timeout=15)
            combined = "\n".join(part for part in (res.stdout.strip(), res.stderr.strip()) if part)
            apply_output = combined
            warnings.extend(
                line for line in combined.splitlines() if "[kiosX WARN]" in line or "[kiosX ERROR]" in line
            )
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
            "info": build_device_info(cfg),
        }
    )


@app.route("/test-audio", methods=["POST"])
def test_audio():
    tmp_wav: str | None = None
    try:
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
    # Kiosk-ID beim Start sicherstellen und mDNS-Alias (kiosx-<id>.local) aktivieren
    load_config(ensure_id=True)
    mdns_manager.start()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
