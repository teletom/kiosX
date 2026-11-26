# kiosX

Schlankes, produktionsreifes Kiosk-Setup- und Konfigurations-Tool fuer Mini-PCs auf Basis von **Ubuntu Server (minimized)** – ohne schwergewichtigen Desktop-Manager.

## Architektur

- **Grafik-Unterbau:** Minimalistisches X11 + Openbox (`xorg`, `openbox`, `x11-xserver-utils`, `xinput`, `unclutter`, `feh`)
- **Browser:** Chromium im Kiosk-Vollbildmodus mit Watchdog-Neustart, deaktivierten Infobars/Overscroll/DPMS sowie **Start-Info-Overlay (`/splash`)** mit 4-stelliger Kiosk-ID, Hostname, IP/mDNS und lokalem QR-Code
- **Hintergrundbild:** Lokal unter `/etc/kiosk/wallpaper.jpg` (standardmaessig ueber `install.sh` geladen, optional im Web-UI konfigurierbar)
- **Netzwerk & Discovery:** Hardware-unabhaengige Netplan-Konfiguration (`en*`/`eth*` + automatisch erkannte WLAN-Adapter aus `wifi.conf`, mit Backup & `netplan generate`-Pruefung) sowie `avahi-daemon` (mDNS `<hostname>.local` & `kiosx-<id>.local`)
- **Audio:** PipeWire (`pipewire`, `wireplumber`, `pipewire-pulse`, `pipewire-alsa`, `pulseaudio-utils`, `alsa-utils`)
- **Konfigurations-UI:** Python 3 + Flask (`python3-flask`) & Pico CSS v2, erreichbar per LAN/mDNS/Tailscale auf Port `8080`
- **Persistente Konfiguration:** `/etc/kiosk/config.json` (atomar geschrieben, Live-Anwendung ueber `kiosk-apply.sh` ohne Neustart)

## Repository-Struktur

```text
/opt/kiosX/
├── install.sh                 # Idempotentes Installations-Skript fuer Ubuntu Server
├── kiosk-apply.sh             # Live-Applier fuer xrandr, feh, xinput & pactl
├── app.py                     # Flask-Backend, Hardware-Erkennung, /splash, /qr.svg & /api/info
├── wifi.conf                  # Standard-WLAN-Netzwerke (SSID=PASSWORT) fuer Netplan
├── templates/
│   ├── index.html             # Web-UI (Pico CSS v2)
│   ├── splash.html            # 15s Start-Anzeige mit Kiosk-ID, IP, mDNS & QR-Code
│   └── touch_test.html        # Vollbild-Touch-Ecken-Test mit Koordinatenanzeige
├── .gitignore
└── README.md
```

## Installation & Rollout auf neuen Kiosk-Rechnern

1. **Ubuntu Server (minimized)** installieren (inkl. `git` und OpenSSH/Tailscale).
2. Repository nach `/opt/kiosX` klonen:

   ```bash
   sudo apt-get update && sudo apt-get install -y git
   sudo mkdir -p /opt/kiosX
   sudo chown "$USER:$USER" /opt/kiosX
   git clone <REPO-URL> /opt/kiosX
   ```

3. Installations-Skript ausfuehren (idempotent – kann jederzeit erneut gestartet werden):

   ```bash
   cd /opt/kiosX
   sudo bash install.sh
   ```

   Optionale Parameter:

   ```bash
   sudo bash install.sh --user kiosk --port 8080
   sudo bash install.sh --skip-netplan   # bestehendes Netplan nicht aendern
   ```

4. System neu starten, damit Autologin auf `tty1`, X11/Openbox, Start-Anzeige und Chromium starten:

   ```bash
   sudo reboot
   ```

## Bedienung der Weboberflaeche

Nach der Installation ist die Weboberflaeche ueber LAN, mDNS oder Tailscale erreichbar:

```text
http://<kiosk-ip>:8080
http://<hostname>.local:8080
http://kiosx-<4-zeichen-id>.local:8080
```

Ueber das Web-UI koennen live konfiguriert werden:

1. **Kiosk-URL, Kiosk-ID & Start-Anzeige:** Ziel-URL, 4-stellige Kiosk-ID, Countdown-Dauer der Start-Anzeige (`0–120 s`), optionales Hintergrundbild und sauberer Browser-Neustart.
2. **Display-Konfiguration:** Primaeres Display, Aufloesung (`auto` oder nativer Modus), Drehung (`0°`, `90°`, `180°`, `270°`) sowie optionales Zweitdisplay (`off`, `right`, `left`, `mirror`).
3. **Touch-Kalibrierung:** Bindung an den Primaermonitor (`xinput map-to-output`, folgt im Modus **Automatisch** direkt der Display-Drehung), optionale Korrektur (`+90°/+180°/+270°`) fuer physisch verdrehte Panels sowie Vollbild-**Touch-Ecken-Test**.
4. **Audio-Ausgabe:** Soundkarte und Ausgabe-Profil (HDMI vs. analoger Line-Out/Klinke) umschalten, Lautstaerke regeln und Stereo-Testton abspielen.

## API fuer zentrale Web-App / Discovery

- `GET /api/info` liefert `{ device_id, hostname, lan_ip, tailscale_ip, mdns_alias, config_url, tailscale_url, target_url, splash_seconds }` als JSON.
- `GET /qr.svg` erzeugt lokal einen SVG-QR-Code zur aktuellen Config-UI.

## Wartung & Fehlersuche

```bash
# Status & Logs der Web-UI pruefen
systemctl status kiosk-ui.service
journalctl -u kiosk-ui.service -f

# Gespeicherte Konfiguration manuell anwenden (als Kiosk-Benutzer)
DISPLAY=:0 /opt/kiosX/kiosk-apply.sh

# Aktuelle Konfiguration & Netplan-Backups anzeigen
cat /etc/kiosk/config.json
ls -la /etc/kiosk/netplan-backup-*

# Updates ausrollen
cd /opt/kiosX && git pull && sudo bash install.sh
```
