# kiosX

Schlankes, produktionsreifes Kiosk-Setup- und Konfigurations-Tool fuer Mini-PCs auf Basis von **Ubuntu Server (minimized)** – ohne schwergewichtigen Desktop-Manager.

## Architektur

- **Grafik-Unterbau:** Minimalistisches X11 + Openbox (`xorg`, `openbox`, `x11-xserver-utils`, `xinput`, `unclutter`)
- **Browser:** Chromium im Kiosk-Vollbildmodus mit Watchdog-Neustart, deaktivierten Infobars/Overscroll/DPMS
- **Audio:** PipeWire (`pipewire`, `wireplumber`, `pipewire-pulse`, `pulseaudio-utils`, `alsa-utils`)
- **Konfigurations-UI:** Python 3 + Flask (`python3-flask`) & Pico CSS v2, erreichbar per LAN/Tailscale auf Port `8080`
- **Persistente Konfiguration:** `/etc/kiosk/config.json` (atomar geschrieben, Live-Anwendung ueber `kiosk-apply.sh` ohne Neustart)

## Repository-Struktur

```text
/opt/kiosX/
├── install.sh                 # Idempotentes Installations-Skript fuer Ubuntu Server
├── kiosk-apply.sh             # Live-Applier fuer xrandr, xinput & pactl
├── app.py                     # Flask-Backend & Hardware-Erkennung
├── templates/
│   ├── index.html             # Web-UI (Pico CSS v2)
│   └── touch_test.html        # Vollbild-Touch-Ecken-Test mit Koordinatenanzeige
├── .gitignore
└── README.md
```

## Installation & Rollout auf neuen Kiosk-Rechnern

1. **Ubuntu Server (minimized)** installieren (inkl. `git` und OpenSSH/Tailscale).
2. Repository nach `/opt/kiosX` klonen:

   ```bash
   sudo mkdir -p /opt/kiosX
   sudo chown "$USER:$USER" /opt/kiosX
   git clone <REPO-URL> /opt/kiosX
   ```

3. Installations-Skript ausfuehren (idempotent – kann jederzeit erneut gestartet werden):

   ```bash
   cd /opt/kiosX
   sudo bash install.sh
   ```

   Optional mit eigenem Kiosk-Benutzer oder anderem Port:

   ```bash
   sudo bash install.sh --user kiosk --port 8080
   ```

4. System neu starten, damit Autologin auf `tty1`, X11/Openbox und Chromium starten:

   ```bash
   sudo reboot
   ```

## Bedienung der Weboberflaeche

Nach der Installation ist die Weboberflaeche ueber LAN oder Tailscale erreichbar:

```text
http://<kiosk-ip-oder-hostname>:8080
```

Ueber das Web-UI koennen live konfiguriert werden:

1. **Kiosk-URL & Browser:** Ziel-URL aendern und Chromium sauber ueber den Watchdog neu starten.
2. **Display-Konfiguration:** Primaeres Display, Aufloesung (`auto` oder nativer Modus), Drehung (`0°`, `90°`, `180°`, `270°`) sowie optionales Zweitdisplay (`off`, `right`, `left`, `mirror`).
3. **Touch-Kalibrierung:** Touch-Geraet an Primaermonitor binden (`xinput map-to-output`), Rotationsmatrix anwenden und ueber den **Touch-Ecken-Test** direkt am Bildschirm verifizieren.
4. **Audio-Ausgabe:** Soundkarte und Ausgabe-Profil (z. B. HDMI vs. analoger Line-Out/Klinke) umschalten, Lautstaerke regeln und Stereo-Testton abspielen.

## Wartung & Fehlersuche

```bash
# Status & Logs der Web-UI pruefen
systemctl status kiosk-ui.service
journalctl -u kiosk-ui.service -f

# Gespeicherte Konfiguration manuell anwenden (als Kiosk-Benutzer)
DISPLAY=:0 /opt/kiosX/kiosk-apply.sh

# Aktuelle Konfiguration anzeigen
cat /etc/kiosk/config.json

# Updates ausrollen
cd /opt/kiosX && git pull && sudo bash install.sh
```
