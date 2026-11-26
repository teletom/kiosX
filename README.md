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
├── VERSION                    # Semantische Versionsnummer (z. B. 1.1.0)
├── install.sh                 # Idempotentes Installations-Skript fuer Ubuntu Server
├── kiosx                      # CLI-Tool (verlinkt als /usr/local/bin/kiosx, SSH-faehig)
├── kiosk-watchdog.sh          # Chromium-Watchdog-Schleife (unterstuetzt kiosx stop/start)
├── kiosk-apply.sh             # Live-Applier fuer xrandr, feh, xinput & pactl
├── app.py                     # Flask-Backend, Hardware-Erkennung, /splash, /qr.svg & Updates
├── wifi.conf                  # Standard-WLAN-Netzwerke (SSID=PASSWORT) fuer Netplan
├── templates/
│   ├── index.html             # Web-UI (Pico CSS v2) inkl. Versions- & Update-Bereich
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

## API fuer zentrale Web-App / Discovery & Updates

- `GET /api/info` liefert `{ version, commit, device_id, hostname, lan_ip, tailscale_ip, mdns_alias, config_url, tailscale_url, target_url, splash_seconds }` als JSON.
- `GET /qr.svg` erzeugt lokal einen SVG-QR-Code zur aktuellen Config-UI.
- `GET /api/update/check` & `POST /api/update/apply`: Pruefen und installieren Updates direkt ueber die Weboberflaeche (mit minimaler sudoers-Regel `/etc/sudoers.d/kiosx-updater` fuer `systemctl restart kiosk-ui.service` bzw. `install.sh --skip-netplan`).

> **Automatische Web-UI-Updates bei privatem GitHub-Repo (ohne manuelle Git-Befehle):**
> `install.sh` richtet `credential.helper` und `~/.git-credentials` (`0600`) automatisch ein – ueber eine der drei Moeglichkeiten:
> 1. **Direkt beim Klonen (Zero-Touch):**
>    ```bash
>    git clone https://teletom:<READONLY_PAT>@github.com/teletom/kiosX.git /opt/kiosX
>    cd /opt/kiosX && sudo bash install.sh
>    ```
>    *(Das Token wird automatisch aus der Remote-URL nach `~/.git-credentials` mit `0600` verschoben und aus `.git/config` entfernt.)*
> 2. **Per Parameter:** `sudo bash install.sh --git-token <READONLY_PAT>` (oder `KIOSX_GIT_TOKEN=...`).
> 3. **Interaktiv beim `install.sh`-Lauf:** Falls noch keine Credentials hinterlegt sind, fragt `install.sh` einmalig unsichtbar nach dem Read-Only Token (Enter zum Ueberspringen).

## Steuerung per CLI (auch ueber SSH)

Nach der Installation steht global der Befehl `kiosx` (`/usr/local/bin/kiosx`) zur Verfuegung – funktioniert direkt ueber SSH ohne manuelles Setzen von `DISPLAY` oder `XAUTHORITY`:

```bash
kiosx status                   # Uebersicht: Kiosk-ID, IPs, Display, Browser, Audio & Dienste
kiosx start                    # Startet Chromium Kiosk-Browser (und Web-UI bei Bedarf)
kiosx stop                     # Stoppt den Browser sauber (Watchdog pausiert, Wallpaper sichtbar)
kiosx restart                  # Startet den Browser neu (inkl. Start-Anzeige)
kiosx apply                    # Wendet /etc/kiosk/config.json sofort live an
kiosx url                      # Aktuelle Kiosk-URL anzeigen
kiosx url https://example.com  # Neue Kiosk-URL setzen und Browser neu starten
kiosx id                       # 4-stellige Kiosk-ID & Verbindungs-URLs anzeigen
kiosx ui restart               # Web-UI Dienst (kiosk-ui.service) neu starten
kiosx logs ui                  # Live-Logs der Web-UI (alternativ: browser | apply)
kiosx update                   # git pull && sudo bash install.sh
```

## Wartung & Fehlersuche

```bash
# Status & Logs pruefen
kiosx status
kiosx logs ui

# Aktuelle Konfiguration & Netplan-Backups anzeigen
cat /etc/kiosk/config.json
ls -la /etc/kiosk/netplan-backup-*
```
