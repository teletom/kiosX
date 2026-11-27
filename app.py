#!/usr/bin/env python3
"""
kiosX - Flask Backend & Web-UI zur Kiosk-Konfiguration fuer Ubuntu Server Mini-PCs.
"""

from __future__ import annotations

import atexit
import copy
from datetime import datetime, timedelta
import hmac
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
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

app = Flask(__name__)

REPO_DIR = Path(__file__).resolve().parent
VERSION_FILE = REPO_DIR / "VERSION"
CONFIG_PATH = Path(os.environ.get("KIOSX_CONFIG", "/etc/kiosk/config.json"))
WALLPAPER_PATH = Path(os.environ.get("KIOSX_WALLPAPER", "/etc/kiosk/wallpaper.jpg"))
APPLY_SCRIPT = Path(os.environ.get("KIOSX_APPLY_SCRIPT", "/opt/kiosX/kiosk-apply.sh"))
OVERLAY_SCRIPT = REPO_DIR / "kiosk-overlay.py"
SECRET_KEY_PATH = CONFIG_PATH.parent / ".flask_secret"
LISTEN_HOST = os.environ.get("KIOSX_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("KIOSX_PORT", "8080"))
SERVICE_NAME = "kiosk-ui.service"

DEFAULT_LAN_PIN = "050477"
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_UPDATE_LOCK = threading.Lock()
_IDENTIFY_LOCK = threading.Lock()
_IDENTIFY_TIMER: threading.Timer | None = None
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_LOCKOUT: dict[str, float] = {}


def _load_or_create_secret_key() -> str:
    for candidate in (SECRET_KEY_PATH, Path(tempfile.gettempdir()) / f"kiosx-secret-{os.getuid()}"):
        try:
            if candidate.is_file():
                val = candidate.read_text(encoding="utf-8").strip()
                if len(val) >= 32:
                    return val
            candidate.parent.mkdir(parents=True, exist_ok=True)
            val = secrets.token_hex(32)
            fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(val + "\n")
            return val
        except OSError:
            continue
    return secrets.token_hex(32)


app.secret_key = _load_or_create_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

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
VALID_SCHEDULE_SCOPES = ("both", "separate")
VALID_SCHEDULE_PRESETS = ("inhouse", "messe", "custom")
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS_DE = {
    "mon": "Mo",
    "tue": "Di",
    "wed": "Mi",
    "thu": "Do",
    "fri": "Fr",
    "sat": "Sa",
    "sun": "So",
}
DEFAULT_TIMEZONE = "Europe/Vienna"

SCHEDULE_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "inhouse": {
        "mon": {"enabled": True, "start": "07:15", "end": "16:45"},
        "tue": {"enabled": True, "start": "07:15", "end": "16:45"},
        "wed": {"enabled": True, "start": "07:15", "end": "16:45"},
        "thu": {"enabled": True, "start": "07:15", "end": "16:45"},
        "fri": {"enabled": True, "start": "07:15", "end": "12:15"},
        "sat": {"enabled": False, "start": "08:00", "end": "18:00"},
        "sun": {"enabled": False, "start": "08:00", "end": "18:00"},
    },
    "messe": {
        "mon": {"enabled": True, "start": "08:00", "end": "18:00"},
        "tue": {"enabled": True, "start": "08:00", "end": "18:00"},
        "wed": {"enabled": True, "start": "08:00", "end": "18:00"},
        "thu": {"enabled": True, "start": "08:00", "end": "18:00"},
        "fri": {"enabled": True, "start": "08:00", "end": "18:00"},
        "sat": {"enabled": True, "start": "08:00", "end": "18:00"},
        "sun": {"enabled": True, "start": "08:00", "end": "18:00"},
    },
}


def make_default_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "timezone": DEFAULT_TIMEZONE,
        "scope": "both",
        "primary": {
            "preset": "inhouse",
            "days": copy.deepcopy(SCHEDULE_PRESETS["inhouse"]),
        },
        "secondary": {
            "preset": "inhouse",
            "days": copy.deepcopy(SCHEDULE_PRESETS["inhouse"]),
        },
    }


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
    "gesture_enabled": True,
    "auth": {
        "lan_pin": DEFAULT_LAN_PIN,
    },
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
    "schedule": make_default_schedule(),
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
# Zeitplan-Steuerung fuer Monitor-Standby (DPMS / xrandr / Touch / Audio)
# ---------------------------------------------------------------------------
def _parse_hhmm(hhmm: str) -> int:
    h, m = hhmm.split(":", 1)
    return int(h) * 60 + int(m)


def _get_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def is_schedule_active_at(plan: dict[str, Any], dt: datetime) -> bool:
    """Prueft, ob das Display laut Zeitplan zum Zeitpunkt dt (timezone-aware) aktiv sein soll."""
    days = plan.get("days") or {}
    weekday = dt.weekday()  # 0=mon..6=sun
    now_min = dt.hour * 60 + dt.minute

    today_cfg = days.get(DAY_KEYS[weekday]) or {}
    if today_cfg.get("enabled"):
        s_min = _parse_hhmm(today_cfg.get("start", "07:15"))
        e_min = _parse_hhmm(today_cfg.get("end", "16:45"))
        if s_min == e_min:
            return True
        if s_min < e_min:
            if s_min <= now_min < e_min:
                return True
        else:
            # Ueber Mitternacht (z. B. 22:00 bis 06:00)
            if now_min >= s_min:
                return True

    # Ueberhang von gestern (falls gestern ein Ueber-Mitternacht-Fenster aktiv war)
    prev_cfg = days.get(DAY_KEYS[(weekday - 1) % 7]) or {}
    if prev_cfg.get("enabled"):
        ps_min = _parse_hhmm(prev_cfg.get("start", "07:15"))
        pe_min = _parse_hhmm(prev_cfg.get("end", "16:45"))
        if ps_min > pe_min and now_min < pe_min:
            return True

    return False


def compute_next_change(plan: dict[str, Any], dt: datetime) -> dict[str, str] | None:
    """Berechnet den naechsten Zustandswechsel (EIN<->AUS) in den kommenden 7 Tagen."""
    current_active = is_schedule_active_at(plan, dt)
    days = plan.get("days") or {}
    tz = dt.tzinfo
    base_date = dt.date()

    candidates: list[datetime] = []
    for offset in range(0, 8):
        d_date = base_date + timedelta(days=offset)
        day_cfg = days.get(DAY_KEYS[d_date.weekday()]) or {}
        if not day_cfg.get("enabled"):
            continue
        s_min = _parse_hhmm(day_cfg.get("start", "07:15"))
        e_min = _parse_hhmm(day_cfg.get("end", "16:45"))
        if s_min == e_min:
            continue
        start_dt = datetime(d_date.year, d_date.month, d_date.day, s_min // 60, s_min % 60, tzinfo=tz)
        if s_min < e_min:
            end_dt = datetime(d_date.year, d_date.month, d_date.day, e_min // 60, e_min % 60, tzinfo=tz)
        else:
            next_date = d_date + timedelta(days=1)
            end_dt = datetime(next_date.year, next_date.month, next_date.day, e_min // 60, e_min % 60, tzinfo=tz)
        candidates.extend([start_dt, end_dt])

    # Eventuelles Ende eines Ueber-Mitternacht-Fensters von gestern
    y_date = base_date - timedelta(days=1)
    y_cfg = days.get(DAY_KEYS[y_date.weekday()]) or {}
    if y_cfg.get("enabled"):
        ys_min = _parse_hhmm(y_cfg.get("start", "07:15"))
        ye_min = _parse_hhmm(y_cfg.get("end", "16:45"))
        if ys_min > ye_min:
            candidates.append(datetime(base_date.year, base_date.month, base_date.day, ye_min // 60, ye_min % 60, tzinfo=tz))

    for cand in sorted(set(candidates)):
        if cand <= dt:
            continue
        next_active = is_schedule_active_at(plan, cand)
        if next_active != current_active:
            delta_days = (cand.date() - base_date).days
            if delta_days == 0:
                day_str = "Heute"
            elif delta_days == 1:
                day_str = "Morgen"
            else:
                day_str = DAY_LABELS_DE[DAY_KEYS[cand.weekday()]]
            return {
                "action": "on" if next_active else "off",
                "label": f"{'Aktiv' if next_active else 'Standby'} ab {day_str} {cand.strftime('%H:%M')}",
                "iso": cand.isoformat(),
            }
    return None


class DisplayScheduleManager:
    """Steuert Display-Standby (DPMS/xrandr), Touch-Sperre und Audio-Mute nach Zeitplan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._override: dict[str, str | None] = {"primary": None, "secondary": None}
        self._applied_state: tuple[bool, bool, bool] | None = None  # (primary_on, secondary_on, all_off)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="kiosx-schedule", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop_event.set()
        self._wakeup.set()

    def _has_secondary(self, cfg: dict[str, Any]) -> bool:
        disp = cfg.get("display") or {}
        return bool(disp.get("secondary_output")) and disp.get("secondary_mode", "off") != "off"

    def evaluate(self, cfg: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        sched = cfg.get("schedule") or make_default_schedule()
        tz = _get_tz(sched.get("timezone", DEFAULT_TIMEZONE))
        now_dt = now.astimezone(tz) if now is not None else datetime.now(tz)
        enabled = bool(sched.get("enabled", False))
        scope = sched.get("scope", "both")
        has_sec = self._has_secondary(cfg)

        p_plan = sched.get("primary") or {"preset": "inhouse", "days": SCHEDULE_PRESETS["inhouse"]}
        s_plan = sched.get("secondary") or p_plan

        if enabled:
            p_sched = is_schedule_active_at(p_plan, now_dt)
            s_sched = p_sched if scope == "both" else is_schedule_active_at(s_plan, now_dt)
            p_next = compute_next_change(p_plan, now_dt)
            s_next = p_next if scope == "both" else compute_next_change(s_plan, now_dt)
        else:
            p_sched = True
            s_sched = True
            p_next = None
            s_next = None

        with self._lock:
            ov_p = self._override["primary"]
            ov_s = self._override["secondary"]

        p_on = True if ov_p == "on" else (False if ov_p == "off" else p_sched)
        s_on = True if ov_s == "on" else (False if ov_s == "off" else s_sched)
        if not has_sec:
            s_on = False

        all_off = (not p_on) and (not has_sec or not s_on)

        return {
            "enabled": enabled,
            "timezone": sched.get("timezone", DEFAULT_TIMEZONE),
            "local_time": now_dt.strftime("%H:%M"),
            "local_day": DAY_LABELS_DE[DAY_KEYS[now_dt.weekday()]],
            "scope": scope,
            "has_secondary": has_sec,
            "primary_preset": p_plan.get("preset", "inhouse"),
            "secondary_preset": s_plan.get("preset", "inhouse"),
            "primary_state": "on" if p_on else "standby",
            "secondary_state": ("on" if s_on else "standby") if has_sec else "disabled",
            "primary_on": p_on,
            "secondary_on": s_on,
            "all_off": all_off,
            "override": {"primary": ov_p, "secondary": ov_s},
            "override_active": (ov_p is not None) or (ov_s is not None),
            "next_change_primary": p_next["label"] if p_next else "",
            "next_change_secondary": (s_next["label"] if s_next else "") if has_sec else "",
        }

    def set_override(self, action: str, target: str = "both") -> dict[str, Any]:
        val: str | None = None if action == "auto" else action
        with self._lock:
            if target in ("both", "primary"):
                self._override["primary"] = val
            if target in ("both", "secondary"):
                self._override["secondary"] = val
        return self.sync_now(force=True)

    def _set_touch_enabled(self, cfg: dict[str, Any], enabled: bool) -> None:
        touch_dev = (cfg.get("touch") or {}).get("device", "").strip()
        if not touch_dev or not shutil.which("xinput"):
            return
        ids: list[str] = []
        if touch_dev.isdigit():
            ids = [touch_dev]
        else:
            res = run_cmd(["xinput", "list"], timeout=4)
            if res.returncode == 0:
                line_re = re.compile(r"^(.*?)\s+id=(\d+)\s+\[slave\s+pointer\b")
                for line in res.stdout.splitlines():
                    m = line_re.search(line)
                    if not m:
                        continue
                    cleaned = re.sub(r"^[\s\u239c\u23a1\u23a2\u23a3\u21b3│├└─]+", "", m.group(1)).strip()
                    if cleaned == touch_dev:
                        ids.append(m.group(2))
        cmd_op = "enable" if enabled else "disable"
        for dev_id in ids:
            run_cmd(["xinput", cmd_op, dev_id], timeout=4)

    def _set_audio_muted(self, muted: bool) -> None:
        if not shutil.which("pactl"):
            return
        flag = "1" if muted else "0"
        run_cmd(["pactl", "set-sink-mute", "@DEFAULT_SINK@", flag], timeout=4)
        if muted:
            # Alle aktiven Sinks stummschalten, damit kein Ton waehrend Standby laeuft
            res = run_cmd(["pactl", "list", "short", "sinks"], timeout=4)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        run_cmd(["pactl", "set-sink-mute", parts[1], "1"], timeout=3)

    def _run_apply_script(self, force_off_primary: bool = False, force_off_secondary: bool = False, force_mute: bool = False) -> tuple[bool, str, list[str]]:
        if not APPLY_SCRIPT.is_file():
            return True, "", [f"Apply-Skript nicht gefunden: {APPLY_SCRIPT}"]
        env = build_runtime_env()
        env["KIOSX_FORCE_OFF_PRIMARY"] = "1" if force_off_primary else "0"
        env["KIOSX_FORCE_OFF_SECONDARY"] = "1" if force_off_secondary else "0"
        env["KIOSX_FORCE_MUTE"] = "1" if force_mute else "0"
        try:
            res = subprocess.run(
                ["/usr/bin/env", "bash", str(APPLY_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
                check=False,
            )
            combined = "\n".join(part for part in (res.stdout.strip(), res.stderr.strip()) if part)
            warnings = [
                line for line in combined.splitlines() if "[kiosX WARN]" in line or "[kiosX ERROR]" in line
            ]
            return res.returncode == 0, combined, warnings
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", [f"Fehler bei kiosk-apply.sh: {exc}"]

    def sync_now(self, cfg: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
        cfg = cfg or load_config(ensure_id=False)
        status = self.evaluate(cfg)
        target_tuple = (status["primary_on"], status["secondary_on"], status["all_off"])

        with self._lock:
            prev_tuple = self._applied_state
            state_changed = (prev_tuple != target_tuple)

        apply_ok = True
        apply_output = ""
        warnings: list[str] = []

        # Nichts an X11 tun, wenn Zeitplan aus ist, kein Override aktiv und kein force
        if not status["enabled"] and not status["override_active"] and not force:
            if prev_tuple is not None and not prev_tuple[2]:
                return {"status": status, "apply_ok": True, "output": "", "warnings": []}

        # 1) Komplett-Standby (alle aktiven Displays AUS)
        if status["all_off"]:
            if state_changed or force:
                self._set_touch_enabled(cfg, False)
                self._set_audio_muted(True)
            # DPMS-Standby setzen / im Standby periodisch absichern
            if shutil.which("xset"):
                run_cmd(["xset", "+dpms"], timeout=4)
                run_cmd(["xset", "dpms", "0", "0", "0"], timeout=4)
                run_cmd(["xset", "dpms", "force", "off"], timeout=4)
            with self._lock:
                self._applied_state = target_tuple
            return {"status": status, "apply_ok": True, "output": "Standby (DPMS off, Touch aus, Audio stumm)", "warnings": []}

        # 2) Mindestens ein Display EIN
        if state_changed or force:
            waking_from_dpms = prev_tuple is not None and prev_tuple[2]
            if shutil.which("xset"):
                run_cmd(["xset", "dpms", "force", "on"], timeout=4)
                run_cmd(["xset", "-dpms"], timeout=4)
                run_cmd(["xset", "s", "off"], timeout=4)
                run_cmd(["xset", "s", "noblank"], timeout=4)
                run_cmd(["xset", "s", "reset"], timeout=4)
            if waking_from_dpms:
                # Kurz warten, bis HDMI/DP-Monitor EDID nach Standby wieder meldet
                time.sleep(1.0)

            # Beim Einschalten IMMER kiosk-apply.sh ausfuehren -> stellt Aufloesung,
            # Drehung, Wallpaper, Touch (enable + map + Matrix) und Audio (unmute + Vol) wieder her!
            apply_ok, apply_output, warnings = self._run_apply_script(
                force_off_primary=not status["primary_on"],
                force_off_secondary=status["has_secondary"] and not status["secondary_on"],
                force_mute=False,
            )
            with self._lock:
                self._applied_state = target_tuple

        return {"status": status, "apply_ok": apply_ok, "output": apply_output, "warnings": warnings}

    def trigger_wakeup(self) -> None:
        self._wakeup.set()

    def _loop(self) -> None:
        # Beim Dienststart kurz warten, bis X11 ggf. bereit ist
        self._wakeup.wait(3.0)
        while not self._stop_event.is_set():
            self._wakeup.clear()
            try:
                cfg = load_config(ensure_id=False)
                sched = cfg.get("schedule") or {}
                with self._lock:
                    has_override = any(v is not None for v in self._override.values())
                if sched.get("enabled") or has_override:
                    self.sync_now(cfg=cfg, force=False)
            except Exception:
                pass
            self._wakeup.wait(20.0)


schedule_manager = DisplayScheduleManager()


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

    if "gesture_enabled" in raw and raw["gesture_enabled"] is not None:
        cfg["gesture_enabled"] = bool(raw["gesture_enabled"])

    raw_auth = raw.get("auth")
    if isinstance(raw_auth, dict):
        pin_val = str(raw_auth.get("lan_pin") or "").strip()
        if pin_val:
            cfg["auth"]["lan_pin"] = pin_val

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

    # Zeitplan-Sektion sanft zusammenfuehren
    raw_sched = raw.get("schedule")
    if isinstance(raw_sched, dict):
        sched = cfg["schedule"]
        if "enabled" in raw_sched:
            sched["enabled"] = bool(raw_sched["enabled"])
        tz_val = str(raw_sched.get("timezone") or "").strip()
        if tz_val:
            sched["timezone"] = tz_val
        scope_val = str(raw_sched.get("scope") or "").strip().lower()
        if scope_val in VALID_SCHEDULE_SCOPES:
            sched["scope"] = scope_val

        for target in ("primary", "secondary"):
            raw_plan = raw_sched.get(target)
            if not isinstance(raw_plan, dict):
                continue
            preset = str(raw_plan.get("preset") or "inhouse").strip().lower()
            if preset not in VALID_SCHEDULE_PRESETS:
                preset = "custom"
            sched[target]["preset"] = preset
            if preset in SCHEDULE_PRESETS and not isinstance(raw_plan.get("days"), dict):
                sched[target]["days"] = copy.deepcopy(SCHEDULE_PRESETS[preset])
            elif isinstance(raw_plan.get("days"), dict):
                base_days = copy.deepcopy(
                    SCHEDULE_PRESETS.get(preset, SCHEDULE_PRESETS["inhouse"])
                )
                for day_key in DAY_KEYS:
                    d_raw = raw_plan["days"].get(day_key)
                    if isinstance(d_raw, dict):
                        if "enabled" in d_raw:
                            base_days[day_key]["enabled"] = bool(d_raw["enabled"])
                        if isinstance(d_raw.get("start"), str) and re.match(r"^\d{2}:\d{2}$", d_raw["start"].strip()):
                            base_days[day_key]["start"] = d_raw["start"].strip()
                        if isinstance(d_raw.get("end"), str) and re.match(r"^\d{2}:\d{2}$", d_raw["end"].strip()):
                            base_days[day_key]["end"] = d_raw["end"].strip()
                sched[target]["days"] = base_days

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

    # Zeitplan validieren (wenn im Payload vorhanden; sonst bestehenden beibehalten)
    if "schedule" not in payload and existing_config and isinstance(existing_config.get("schedule"), dict):
        cfg["schedule"] = merge_defaults({"schedule": existing_config["schedule"]})["schedule"]
    else:
        sched = cfg["schedule"]
        tz_name = str(sched.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return None, f"Unbekannte Zeitzone: {tz_name}"
        sched["timezone"] = tz_name

        if sched.get("scope") not in VALID_SCHEDULE_SCOPES:
            return None, f"Ungueltiger Zeitplan-Bereich: {sched.get('scope')}"

        for target in ("primary", "secondary"):
            plan = sched[target]
            preset = str(plan.get("preset") or "inhouse").strip().lower()
            if preset not in VALID_SCHEDULE_PRESETS:
                return None, f"Ungueltige Zeitplan-Vorlage: {preset}"
            plan["preset"] = preset
            days = plan.get("days")
            if not isinstance(days, dict):
                return None, f"Fehlende Wochentage im Zeitplan ({target})."
            for day_key in DAY_KEYS:
                d = days.get(day_key)
                if not isinstance(d, dict):
                    return None, f"Fehlender Tag '{day_key}' im Zeitplan."
                d["enabled"] = bool(d.get("enabled", False))
                for field in ("start", "end"):
                    val = str(d.get(field, "")).strip()
                    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", val):
                        return None, f"Ungueltige Uhrzeit ({DAY_LABELS_DE[day_key]} {field}): '{val}'"
                    d[field] = val

    # Touch-Geste (Z-Muster) & LAN-PIN validieren
    if "gesture_enabled" in payload:
        cfg["gesture_enabled"] = bool(payload.get("gesture_enabled"))
    elif existing_config and "gesture_enabled" in existing_config:
        cfg["gesture_enabled"] = bool(existing_config["gesture_enabled"])

    raw_auth = payload.get("auth")
    if isinstance(raw_auth, dict) and "lan_pin" in raw_auth:
        pin_val = str(raw_auth.get("lan_pin") or "").strip()
        if pin_val:
            if len(pin_val) < 4 or len(pin_val) > 32:
                return None, "Zugangscode muss zwischen 4 und 32 Zeichen lang sein."
            cfg["auth"]["lan_pin"] = pin_val
        elif existing_config and isinstance(existing_config.get("auth"), dict):
            cfg["auth"]["lan_pin"] = str(existing_config["auth"].get("lan_pin") or DEFAULT_LAN_PIN)
    elif existing_config and isinstance(existing_config.get("auth"), dict):
        cfg["auth"]["lan_pin"] = str(existing_config["auth"].get("lan_pin") or DEFAULT_LAN_PIN)

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


def run_git(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Fuehrt einen Git-Befehl im Repository nicht-interaktiv aus."""
    env = build_runtime_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def get_version_info() -> dict[str, str]:
    """Liest Versionsnummer aus VERSION sowie lokalen Git-Commit und Branch."""
    version = "0.0.0"
    try:
        if VERSION_FILE.is_file():
            raw = VERSION_FILE.read_text(encoding="utf-8").strip()
            if raw:
                version = raw
    except OSError:
        pass

    commit = ""
    branch = ""
    commit_date = ""
    if shutil.which("git") and (REPO_DIR / ".git").exists():
        try:
            r1 = run_git(["rev-parse", "--short", "HEAD"], timeout=4)
            if r1.returncode == 0:
                commit = r1.stdout.strip()
            r2 = run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=4)
            if r2.returncode == 0:
                branch = r2.stdout.strip()
            r3 = run_git(["log", "-1", "--format=%cs"], timeout=4)
            if r3.returncode == 0:
                commit_date = r3.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass

    display = f"v{version}" + (f" ({commit})" if commit else "")
    return {
        "version": version,
        "commit": commit,
        "branch": branch or "main",
        "commit_date": commit_date,
        "display": display,
    }


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
    ver = get_version_info()

    return {
        "version": ver["version"],
        "commit": ver["commit"],
        "branch": ver["branch"],
        "commit_date": ver["commit_date"],
        "version_display": ver["display"],
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
        "schedule_status": schedule_manager.evaluate(cfg),
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
# LAN/WLAN PIN-Authentifizierung (Localhost & Tailscale ohne Code)
# ---------------------------------------------------------------------------
def is_trusted_client(remote_addr: str | None) -> bool:
    """Erlaubt direkten Zugriff ohne Code nur von Localhost und Tailscale (echte TCP-IP)."""
    if not remote_addr:
        return False
    addr_clean = remote_addr.split("%", 1)[0].strip()
    try:
        ip_obj = ipaddress.ip_address(addr_clean)
    except ValueError:
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if ip_obj.is_loopback:
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in TAILSCALE_V4:
        return True
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in TAILSCALE_V6:
        return True
    return False


def _is_rate_limited(client_ip: str) -> int:
    """Gibt verbleibende Sperrsekunden zurueck (0 = nicht gesperrt)."""
    now = time.monotonic()
    with _LOGIN_LOCK:
        until = _LOGIN_LOCKOUT.get(client_ip, 0.0)
        if until > now:
            return int(until - now + 0.99)
        if until:
            _LOGIN_LOCKOUT.pop(client_ip, None)
    return 0


def _record_login_failure(client_ip: str) -> int:
    """Speichert Fehlversuch; ab 5 Fehlversuchen in 5 Min -> 60 s Sperre."""
    now = time.monotonic()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(client_ip, []) if now - t < 300.0]
        fails.append(now)
        _LOGIN_FAILS[client_ip] = fails
        if len(fails) >= 5:
            _LOGIN_LOCKOUT[client_ip] = now + 60.0
            _LOGIN_FAILS[client_ip] = []
            return 60
    return 0


def _clear_login_failures(client_ip: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(client_ip, None)
        _LOGIN_LOCKOUT.pop(client_ip, None)


@app.before_request
def enforce_lan_pin_auth():
    if request.endpoint in ("login_view", "logout_view", "static"):
        return None
    if is_trusted_client(request.remote_addr):
        return None
    if session.get("lan_authed") is True:
        return None
    if request.path.startswith("/api/") or request.method != "GET":
        return jsonify({"ok": False, "auth_required": True, "message": "Anmeldung erforderlich (LAN-Zugangscode)."}), 401
    return redirect(url_for("login_view", next=request.path))


# ---------------------------------------------------------------------------
# Live-Info-Overlay ("ID am Display anzeigen", Option B + Option A Fallback)
# ---------------------------------------------------------------------------
def get_identify_pid_file() -> Path:
    return get_runtime_dir() / "kiosx-identify.pid"


def _has_tkinter() -> bool:
    try:
        res = subprocess.run(
            [sys.executable, "-c", "import tkinter"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _get_overlay_profile_dir(chrome_bin: str) -> Path:
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
        profile = Path.home() / "snap" / "chromium" / "common" / "kiosx-overlay"
    else:
        profile = get_runtime_dir() / "kiosx-overlay-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def stop_identify_overlay() -> bool:
    """Beendet ein aktuell angezeigtes Info-Overlay sauber."""
    global _IDENTIFY_TIMER
    with _IDENTIFY_LOCK:
        if _IDENTIFY_TIMER is not None:
            _IDENTIFY_TIMER.cancel()
            _IDENTIFY_TIMER = None

        pid_file = get_identify_pid_file()
        stopped = False
        if pid_file.is_file():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                if is_pid_alive(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                    except OSError:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except OSError:
                            pass
                    for _ in range(10):
                        if not is_pid_alive(pid):
                            break
                        time.sleep(0.08)
                    if is_pid_alive(pid):
                        try:
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                        except OSError:
                            pass
                    stopped = True
            except (OSError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)

        # Eventuelle Restprozesse der Fallback-Chromium-Instanz bereinigen
        try:
            run_cmd(["pkill", "-f", "kiosx-overlay"], timeout=3)
        except OSError:
            pass
        return stopped


def start_identify_overlay(seconds: int = 10) -> tuple[bool, str, str]:
    """Blendet Kiosk-ID & Infos fuer N Sekunden ueber dem laufenden Browser ein (ohne Neustart)."""
    global _IDENTIFY_TIMER
    seconds = max(3, min(60, int(seconds)))

    if shutil.which("xset"):
        check = run_cmd(["xset", "q"], timeout=4)
        if check.returncode != 0:
            return False, "Keine aktive X11-Session unter DISPLAY=:0 erreichbar.", "none"

    stop_identify_overlay()
    info = build_device_info()
    env = build_runtime_env()

    # Option B (bevorzugt): blitzschnelles natives Tkinter-Popup (< 100 ms)
    if OVERLAY_SCRIPT.is_file() and _has_tkinter():
        cmd = [
            sys.executable,
            str(OVERLAY_SCRIPT),
            "--seconds",
            str(seconds),
            "--info-json",
            json.dumps(info, ensure_ascii=False),
        ]
        method = "tkinter"
    else:
        # Option A (Fallback): isolierte Chromium-Overlay-Instanz auf /splash?mode=overlay
        chrome_bin = resolve_chromium_binary()
        if not chrome_bin:
            return False, "Weder python3-tk noch Chromium verfuegbar.", "none"
        profile_dir = _get_overlay_profile_dir(chrome_bin)
        overlay_url = f"http://127.0.0.1:{LISTEN_PORT}/splash?mode=overlay&seconds={seconds}"
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
            overlay_url,
        ]
        method = "chromium-fallback"

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"Overlay konnte nicht gestartet werden: {exc}", method

    with _IDENTIFY_LOCK:
        get_identify_pid_file().write_text(str(proc.pid), encoding="utf-8")
        timer = threading.Timer(seconds + 3.0, stop_identify_overlay)
        timer.daemon = True
        timer.start()
        _IDENTIFY_TIMER = timer

    return True, f"Kiosk-Info ({info['device_id']}) wird {seconds} s am Display eingeblendet.", method


# ---------------------------------------------------------------------------
# Globale Touch-Geste ("Z"-Muster: OL -> OR -> UL -> UR) via xinput test-xi2
# ---------------------------------------------------------------------------
class ZGestureDetector:
    """Erkennt Z-Sequenz (TL -> TR -> BL -> BR) sowohl als 4 Ecken-Tipps als auch als Strich."""

    SEQUENCE = ("TL", "TR", "BL", "BR")

    def __init__(
        self,
        corner_frac: float = 0.20,
        max_step_sec: float = 2.5,
        max_total_sec: float = 6.0,
        cooldown_sec: float = 5.0,
    ) -> None:
        self.corner_frac = corner_frac
        self.max_step_sec = max_step_sec
        self.max_total_sec = max_total_sec
        self.cooldown_sec = cooldown_sec
        self._step: int = 0
        self._start_ts: float = 0.0
        self._last_ts: float = 0.0
        self._cooldown_until: float = 0.0

    def classify_corner(self, x: float, y: float, width: float, height: float) -> str | None:
        if width <= 0 or height <= 0:
            return None
        nx = x / width
        ny = y / height
        f = self.corner_frac
        left = nx <= f
        right = nx >= (1.0 - f)
        top = ny <= f
        bottom = ny >= (1.0 - f)
        if top and left:
            return "TL"
        if top and right:
            return "TR"
        if bottom and left:
            return "BL"
        if bottom and right:
            return "BR"
        return None

    def feed(self, x: float, y: float, width: float, height: float, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        if ts < self._cooldown_until:
            return False

        corner = self.classify_corner(x, y, width, height)
        if corner is None:
            if self._step > 0 and (ts - self._last_ts > self.max_step_sec or ts - self._start_ts > self.max_total_sec):
                self._step = 0
            return False

        # Timeout pruefen
        if self._step > 0 and (ts - self._last_ts > self.max_step_sec or ts - self._start_ts > self.max_total_sec):
            self._step = 0

        # Bereits in der aktuellen Ecke (z. B. waehrend Ziehen/Finger bleibt kurz liegen) -> ignorieren
        if self._step > 0 and corner == self.SEQUENCE[self._step - 1]:
            self._last_ts = ts
            return False

        expected = self.SEQUENCE[self._step]
        if corner == expected:
            if self._step == 0:
                self._start_ts = ts
            self._last_ts = ts
            self._step += 1
            if self._step == len(self.SEQUENCE):
                self._step = 0
                self._cooldown_until = ts + self.cooldown_sec
                return True
            return False

        # Neustart falls wieder bei TL begonnen wird
        if corner == "TL":
            self._step = 1
            self._start_ts = ts
            self._last_ts = ts
        else:
            self._step = 0
        return False


class TouchGestureMonitor:
    """Hoert passiv per 'xinput test-xi2 --root' auf die globale Z-Geste am Bildschirm."""

    EVENT_RE = re.compile(r"^EVENT type \d+ \((ButtonPress|Motion|TouchBegin|TouchUpdate)\)")
    ROOT_RE = re.compile(r"^\s+root:\s*([0-9.]+)/([0-9.]+)")
    SCREEN_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._detector = ZGestureDetector()
        self._screen_size: tuple[int, int] = (1920, 1080)
        self._last_size_check: float = 0.0

    def start(self) -> None:
        if not shutil.which("xinput"):
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="kiosx-gesture", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def _refresh_screen_size(self) -> tuple[int, int]:
        now = time.monotonic()
        if now - self._last_size_check < 10.0:
            return self._screen_size
        self._last_size_check = now
        if shutil.which("xrandr"):
            try:
                res = run_cmd(["xrandr", "--query"], timeout=3)
                if res.returncode == 0:
                    m = self.SCREEN_RE.search(res.stdout)
                    if m:
                        self._screen_size = (int(m.group(1)), int(m.group(2)))
            except Exception:
                pass
        return self._screen_size

    def _loop(self) -> None:
        self._stop_event.wait(2.0)
        while not self._stop_event.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["xinput", "test-xi2", "--root"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=build_runtime_env(),
                )
                in_relevant_event = False
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._stop_event.is_set():
                        break
                    if line.startswith("EVENT "):
                        in_relevant_event = bool(self.EVENT_RE.match(line))
                        continue
                    if not in_relevant_event:
                        continue
                    m_root = self.ROOT_RE.match(line)
                    if m_root:
                        in_relevant_event = False
                        x = float(m_root.group(1))
                        y = float(m_root.group(2))
                        w, h = self._refresh_screen_size()
                        if self._detector.feed(x, y, w, h):
                            cfg = load_config(ensure_id=False)
                            if cfg.get("gesture_enabled", True):
                                start_identify_overlay(seconds=10)
            except Exception:
                pass
            finally:
                if self._proc is not None:
                    try:
                        self._proc.terminate()
                        self._proc.wait(timeout=2)
                    except Exception:
                        pass
                    self._proc = None
            self._stop_event.wait(5.0)


gesture_monitor = TouchGestureMonitor()


# ---------------------------------------------------------------------------
# Flask-Routen
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_view():
    if is_trusted_client(request.remote_addr) or session.get("lan_authed") is True:
        return redirect(url_for("index"))

    info = build_device_info()
    next_url = request.values.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"

    error: str | None = None
    client_ip = request.remote_addr or "unknown"

    if request.method == "POST":
        wait_sec = _is_rate_limited(client_ip)
        if wait_sec > 0:
            error = f"Zu viele Fehlversuche. Bitte {wait_sec} Sekunden warten."
            return render_template("login.html", info=info, error=error, next_url=next_url), 429

        entered_pin = (request.form.get("pin") or "").strip()
        cfg = load_config(ensure_id=False)
        expected_pin = str((cfg.get("auth") or {}).get("lan_pin") or DEFAULT_LAN_PIN)

        if entered_pin and hmac.compare_digest(entered_pin, expected_pin):
            _clear_login_failures(client_ip)
            session.permanent = True
            session["lan_authed"] = True
            return redirect(next_url)

        lock_sec = _record_login_failure(client_ip)
        if lock_sec > 0:
            error = f"Zu viele Fehlversuche. Zugriff fuer {lock_sec} Sekunden gesperrt."
            return render_template("login.html", info=info, error=error, next_url=next_url), 429
        error = "Falscher Zugangscode."
        return render_template("login.html", info=info, error=error, next_url=next_url), 401

    return render_template("login.html", info=info, error=error, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout_view():
    session.clear()
    return redirect(url_for("login_view"))


@app.route("/", methods=["GET"])
def index():
    config = load_config()
    hardware = collect_hardware(config)
    return render_template(
        "index.html",
        config=config,
        hardware=hardware,
        schedule_presets=SCHEDULE_PRESETS,
        lan_session_active=(not is_trusted_client(request.remote_addr) and bool(session.get("lan_authed"))),
    )


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

    # Konfiguration inkl. Zeitplan-Zustand sofort anwenden
    sync_res = schedule_manager.sync_now(cfg=cfg, force=True)
    schedule_manager.trigger_wakeup()
    apply_ok = sync_res["apply_ok"]
    apply_output = sync_res["output"]
    warnings.extend(sync_res["warnings"])

    if not apply_ok:
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "Gespeichert, aber beim Anwenden traten Fehler auf.",
                    "warnings": warnings,
                    "output": apply_output,
                    "config": cfg,
                    "info": build_device_info(cfg),
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
    # Eventuelle Pause durch 'kiosx stop' wieder aufheben
    (get_runtime_dir() / "kiosx-browser.paused").unlink(missing_ok=True)

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


def _resolve_git_upstream(branch: str) -> str:
    res = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=4)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return f"origin/{branch or 'main'}"


@app.route("/api/update/check", methods=["GET"])
def api_update_check():
    """Prueft ueber git fetch, ob neue Commits/Versionen auf dem Remote verfuegbar sind."""
    ver = get_version_info()
    if not shutil.which("git") or not (REPO_DIR / ".git").exists():
        return jsonify({"ok": False, "message": "Kein Git-Repository unter /opt/kiosX gefunden.", **ver}), 400

    try:
        fetch_res = run_git(["fetch", "--quiet", "origin"], timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "message": "Zeitueberschreitung bei git fetch (Netzwerk pruefen).", **ver}), 504
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Git-Fehler: {exc}", **ver}), 500

    if fetch_res.returncode != 0:
        err_out = (fetch_res.stderr or fetch_res.stdout).strip()
        auth_err = any(
            marker in err_out.lower()
            for marker in ("could not read username", "authentication failed", "permission denied", "terminal prompts disabled")
        )
        msg = (
            "Git-Authentifizierung fehlt fuer automatische Updates (SSH-Deploy-Key oder 'git config credential.helper store' einrichten)."
            if auth_err
            else f"Update-Pruefung fehlgeschlagen: {err_out}"
        )
        return jsonify({"ok": False, "auth_required": auth_err, "message": msg, **ver}), 502

    upstream = _resolve_git_upstream(ver["branch"])
    behind_res = run_git(["rev-list", "--count", f"HEAD..{upstream}"], timeout=5)
    ahead_res = run_git(["rev-list", "--count", f"{upstream}..HEAD"], timeout=5)
    remote_sha_res = run_git(["rev-parse", "--short", upstream], timeout=5)
    remote_ver_res = run_git(["show", f"{upstream}:VERSION"], timeout=5)
    log_res = run_git(["log", "--oneline", "-n", "10", f"HEAD..{upstream}"], timeout=5)
    dirty_res = run_git(["status", "--porcelain", "--untracked-files=no"], timeout=5)

    commits_behind = int(behind_res.stdout.strip()) if behind_res.returncode == 0 and behind_res.stdout.strip().isdigit() else 0
    commits_ahead = int(ahead_res.stdout.strip()) if ahead_res.returncode == 0 and ahead_res.stdout.strip().isdigit() else 0
    remote_commit = remote_sha_res.stdout.strip() if remote_sha_res.returncode == 0 else ""
    remote_version = remote_ver_res.stdout.strip() if remote_ver_res.returncode == 0 else ver["version"]
    changelog = [line.strip() for line in log_res.stdout.splitlines() if line.strip()] if log_res.returncode == 0 else []
    dirty = bool(dirty_res.stdout.strip()) if dirty_res.returncode == 0 else False

    update_available = commits_behind > 0
    msg = (
        f"Update verfuegbar: v{remote_version} ({remote_commit}) – {commits_behind} neuer Commit(s)."
        if update_available
        else f"kiosX ist aktuell ({ver['display']})."
    )
    return jsonify(
        {
            "ok": True,
            "current_version": ver["version"],
            "current_commit": ver["commit"],
            "branch": ver["branch"],
            "remote_version": remote_version,
            "remote_commit": remote_commit,
            "update_available": update_available,
            "commits_behind": commits_behind,
            "commits_ahead": commits_ahead,
            "changelog": changelog,
            "dirty": dirty,
            "message": msg,
        }
    )


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """Fuehrt git pull --ff-only aus und startet Web-UI (oder install.sh --skip-netplan) asynchron neu."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "message": "Ein Update laeuft bereits."}), 409

    try:
        payload = request.get_json(silent=True) or {}
        force_install = bool(payload.get("full_install", False))

        if not shutil.which("git") or not (REPO_DIR / ".git").exists():
            return jsonify({"ok": False, "message": "Kein Git-Repository unter /opt/kiosX gefunden."}), 400

        dirty_res = run_git(["status", "--porcelain", "--untracked-files=no"], timeout=5)
        if dirty_res.returncode == 0 and dirty_res.stdout.strip():
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "Lokale uncommittete Aenderungen im Repo – Update abgebrochen (bitte per SSH pruefen).",
                    }
                ),
                409,
            )

        old_ver = get_version_info()
        old_head_res = run_git(["rev-parse", "HEAD"], timeout=5)
        old_head = old_head_res.stdout.strip() if old_head_res.returncode == 0 else ""

        try:
            pull_res = run_git(["pull", "--ff-only"], timeout=30)
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "message": "Zeitueberschreitung bei git pull."}), 504

        if pull_res.returncode != 0:
            err_out = (pull_res.stderr or pull_res.stdout).strip()
            return jsonify({"ok": False, "message": f"git pull fehlgeschlagen: {err_out}"}), 500

        new_ver = get_version_info()
        new_head_res = run_git(["rev-parse", "HEAD"], timeout=5)
        new_head = new_head_res.stdout.strip() if new_head_res.returncode == 0 else ""

        changed_files: list[str] = []
        if old_head and new_head and old_head != new_head:
            diff_res = run_git(["diff", "--name-only", f"{old_head}..{new_head}"], timeout=5)
            if diff_res.returncode == 0:
                changed_files = [f.strip() for f in diff_res.stdout.splitlines() if f.strip()]

        updated = bool(old_head != new_head)
        needs_install = force_install or ("install.sh" in changed_files)

        if not updated and not force_install:
            return jsonify(
                {
                    "ok": True,
                    "updated": False,
                    "restarting": False,
                    "version": new_ver["version"],
                    "commit": new_ver["commit"],
                    "message": f"Bereits auf dem neuesten Stand ({new_ver['display']}).",
                }
            )

        # Verzoegerten Neustart / install.sh im Hintergrund ausfuehren, damit HTTP-Antwort sauber ankommt
        if needs_install:
            bg_cmd = f"sleep 1; sudo -n /usr/bin/bash {REPO_DIR}/install.sh --skip-netplan >/tmp/kiosx-update.log 2>&1 || sudo -n /usr/bin/systemctl restart {SERVICE_NAME} >>/tmp/kiosx-update.log 2>&1"
        else:
            bg_cmd = f"sleep 1; sudo -n /usr/bin/systemctl restart {SERVICE_NAME} >/tmp/kiosx-update.log 2>&1"

        restarting = True
        try:
            subprocess.Popen(
                ["/bin/sh", "-c", bg_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            restarting = False

        mode_txt = "Vollstaendiges Update (install.sh --skip-netplan)" if needs_install else "Web-UI Neustart"
        return jsonify(
            {
                "ok": True,
                "updated": updated,
                "restarting": restarting,
                "full_install": needs_install,
                "old_version": old_ver["version"],
                "new_version": new_ver["version"],
                "new_commit": new_ver["commit"],
                "changed_files": changed_files,
                "message": f"Update auf {new_ver['display']} geladen – {mode_txt} laeuft...",
            }
        )
    finally:
        _UPDATE_LOCK.release()


@app.route("/api/display-power", methods=["POST"])
def api_display_power():
    """Manuelles Ein-/Ausschalten (Standby) oder Zuruecksetzen auf Zeitplan-Automatik."""
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    target = str(payload.get("target", "both")).strip().lower()

    if action not in ("on", "off", "auto"):
        return jsonify({"ok": False, "message": "Ungueltige Aktion (erlaubt: on, off, auto)."}), 400
    if target not in ("both", "primary", "secondary"):
        return jsonify({"ok": False, "message": "Ungueltiges Ziel (erlaubt: both, primary, secondary)."}), 400

    res = schedule_manager.set_override(action=action, target=target)
    status = res["status"]
    labels = {
        "on": "Display(s) eingeschaltet (manuelle Uebersteuerung aktiv – Aufloesung, Touch & Audio wiederhergestellt).",
        "off": "Display(s) in Standby versetzt (Touch gesperrt, Audio stummgeschaltet).",
        "auto": "Zeitplan-Automatik wieder aktiv.",
    }
    return jsonify(
        {
            "ok": res["apply_ok"],
            "message": labels[action],
            "schedule_status": status,
            "warnings": res["warnings"],
        }
    )


@app.route("/api/identify", methods=["POST"])
def api_identify():
    """Blendet Kiosk-ID & Infos fuer N Sekunden am Display ein (ohne Browser-Neustart)."""
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "start")).strip().lower()

    if action == "stop":
        stop_identify_overlay()
        return jsonify({"ok": True, "active": False, "message": "Info-Overlay wurde geschlossen."})

    if action not in ("start", "toggle"):
        return jsonify({"ok": False, "message": f"Unbekannte Aktion: {action}"}), 400

    try:
        seconds = int(payload.get("seconds", 10))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Ungueltige Anzeigedauer."}), 400

    seconds = max(3, min(60, seconds))
    ok, msg, method = start_identify_overlay(seconds=seconds)
    return jsonify({"ok": ok, "active": ok, "seconds": seconds, "method": method, "message": msg}), (200 if ok else 500)


if __name__ == "__main__":
    # Kiosk-ID beim Start sicherstellen, mDNS-Alias, Zeitplan & Z-Touch-Geste starten
    load_config(ensure_id=True)
    mdns_manager.start()
    schedule_manager.start()
    gesture_monitor.start()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
