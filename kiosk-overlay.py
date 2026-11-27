#!/usr/bin/env python3
"""
kiosX - Natives, rahmenloses X11/Tkinter Info-Overlay ("ID am Display anzeigen").
Schwebt per overrideredirect(True) sofort (< 100 ms) ueber dem laufenden Chromium-Kiosk,
ohne den Browser neu zu starten.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk


def build_qr_png(url: str) -> str | None:
    """Erzeugt eine temporaere PNG-QR-Code-Datei per qrencode (nativ von Tk 8.6+ ladbar)."""
    if not url or not shutil.which("qrencode"):
        return None
    fd, tmp_path = tempfile.mkstemp(prefix="kiosx-qr-", suffix=".png")
    os.close(fd)
    try:
        res = subprocess.run(
            ["qrencode", "-t", "PNG", "-s", "5", "-m", "2", "-o", tmp_path, url],
            capture_output=True,
            timeout=4,
            check=False,
        )
        if res.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
            return tmp_path
    except Exception:
        pass
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="kiosX Info-Overlay")
    parser.add_argument("--seconds", type=int, default=10, help="Anzeigedauer in Sekunden")
    parser.add_argument("--info-json", type=str, default="{}", help="JSON-String mit Geraeteinfos")
    args = parser.parse_args()

    duration = max(3, min(60, args.seconds))
    try:
        info = json.loads(args.info_json)
    except json.JSONDecodeError:
        info = {}

    device_id = str(info.get("device_id") or "----")
    hostname = str(info.get("hostname") or "kiosX")
    lan_url = str(info.get("lan_url") or "")
    mdns_url = str(info.get("mdns_url") or "")
    tailscale_url = str(info.get("tailscale_url") or "")
    config_url = str(info.get("config_url") or lan_url or mdns_url or "")
    version_display = str(info.get("version_display") or "")

    qr_path = build_qr_png(config_url)

    root = tk.Tk()
    root.title("kiosX Info")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0f172a")

    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    is_portrait = screen_h > screen_w

    # Farben
    bg_outer = "#38bdf8"   # duenner Akzent-Rahmen
    bg_card = "#0f172a"
    bg_row = "#1e293b"
    fg_main = "#f8fafc"
    fg_muted = "#94a3b8"
    accent = "#2563eb"

    outer = tk.Frame(root, bg=bg_outer, padx=3, pady=3)
    outer.pack(fill="both", expand=True)

    card = tk.Frame(outer, bg=bg_card, padx=28, pady=24)
    card.pack(fill="both", expand=True)

    # Kopfzeile: Hostname + riesige Kiosk-ID
    header = tk.Frame(card, bg=bg_card)
    header.pack(fill="x", pady=(0, 16))

    left_hdr = tk.Frame(header, bg=bg_card)
    left_hdr.pack(side="left", fill="x", expand=True)

    tk.Label(
        left_hdr,
        text=f"KIOSX  ·  {version_display}" if version_display else "KIOSX  ·  GERAETE-INFO",
        font=("DejaVu Sans", 11, "bold"),
        fg="#93c5fd",
        bg=bg_card,
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        left_hdr,
        text=hostname,
        font=("DejaVu Sans", 24, "bold"),
        fg=fg_main,
        bg=bg_card,
        anchor="w",
    ).pack(fill="x", pady=(2, 0))

    id_box = tk.Frame(header, bg=accent, padx=18, pady=8)
    id_box.pack(side="right", padx=(16, 0))
    tk.Label(
        id_box,
        text="KIOSK-ID",
        font=("DejaVu Sans", 9, "bold"),
        fg="#dbeafe",
        bg=accent,
    ).pack()
    tk.Label(
        id_box,
        text=device_id,
        font=("DejaVu Sans Mono", 30, "bold"),
        fg="#ffffff",
        bg=accent,
    ).pack()

    # Trennlinie
    tk.Frame(card, bg="#334155", height=1).pack(fill="x", pady=(0, 14))

    # Inhaltsbereich (Links Infos, Rechts QR-Code; im Hochformat gestapelt falls schmal)
    body = tk.Frame(card, bg=bg_card)
    body.pack(fill="both", expand=True)

    info_col = tk.Frame(body, bg=bg_card)
    if is_portrait and screen_w < 900:
        info_col.pack(fill="x", expand=True)
    else:
        info_col.pack(side="left", fill="both", expand=True)

    def add_info_row(label_txt: str, val_txt: str) -> None:
        if not val_txt:
            return
        row = tk.Frame(info_col, bg=bg_row, padx=12, pady=8)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(
            row,
            text=label_txt,
            font=("DejaVu Sans", 9, "bold"),
            fg=fg_muted,
            bg=bg_row,
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            row,
            text=val_txt,
            font=("DejaVu Sans Mono", 13, "bold"),
            fg=fg_main,
            bg=bg_row,
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

    add_info_row("LAN-KONFIGURATION", lan_url or "Kein LAN verbunden")
    add_info_row("MDNS (LAN OHNE IP)", mdns_url)
    if tailscale_url:
        add_info_row("TAILSCALE", tailscale_url)

    qr_photo = None
    if qr_path:
        try:
            qr_photo = tk.PhotoImage(file=qr_path)
            qr_box = tk.Frame(body, bg="#ffffff", padx=10, pady=10)
            if is_portrait and screen_w < 900:
                qr_box.pack(pady=(8, 0))
            else:
                qr_box.pack(side="right", padx=(18, 0), anchor="n")
            tk.Label(qr_box, image=qr_photo, bg="#ffffff").pack()
            tk.Label(
                qr_box,
                text="Config-UI scannen",
                font=("DejaVu Sans", 9, "bold"),
                fg="#334155",
                bg="#ffffff",
            ).pack(pady=(4, 0))
        except Exception:
            qr_photo = None

    # Fusszeile: Countdown-Balken + Hinweis
    footer = tk.Frame(card, bg=bg_card)
    footer.pack(fill="x", pady=(14, 0))

    bar_bg = tk.Canvas(footer, height=8, bg="#1e293b", highlightthickness=0)
    bar_bg.pack(fill="x", pady=(0, 8))

    status_lbl = tk.Label(
        footer,
        text=f"Schliesst automatisch in {duration} s  ·  Tippen zum Schliessen",
        font=("DejaVu Sans", 10),
        fg=fg_muted,
        bg=bg_card,
        anchor="w",
    )
    status_lbl.pack(fill="x")

    # Fenster auf optimale Groesse berechnen und mittig auf dem Bildschirm platzieren
    root.update_idletasks()
    win_w = min(max(root.winfo_reqwidth(), 640 if not is_portrait else 560), int(screen_w * 0.9))
    win_h = min(root.winfo_reqheight(), int(screen_h * 0.85))
    pos_x = max(0, (screen_w - win_w) // 2)
    pos_y = max(0, (screen_h - win_h) // 2)
    root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
    root.lift()
    root.focus_force()

    def close_overlay(_event=None) -> None:
        try:
            root.destroy()
        except Exception:
            pass

    # Klick / Touch irgendwo auf der Karte oder Escape schliesst das Overlay sofort
    root.bind("<Button-1>", close_overlay)
    root.bind("<Escape>", close_overlay)

    start_ts = time.monotonic()

    def tick() -> None:
        elapsed = time.monotonic() - start_ts
        remaining = max(0.0, duration - elapsed)
        bar_w = bar_bg.winfo_width()
        if bar_w > 1:
            fill_w = int(bar_w * (remaining / duration))
            bar_bg.delete("all")
            bar_bg.create_rectangle(0, 0, fill_w, 8, fill="#38bdf8", outline="")
        sec_ceil = int(remaining + 0.99)
        status_lbl.config(text=f"Schliesst automatisch in {sec_ceil} s  ·  Tippen zum Schliessen")
        if remaining <= 0:
            close_overlay()
            return
        root.after(100, tick)

    root.after(50, tick)

    try:
        root.mainloop()
    finally:
        if qr_path and os.path.exists(qr_path):
            try:
                os.unlink(qr_path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
