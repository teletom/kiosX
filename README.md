# kiosX – Technische Dokumentation

Schlankes, produktionsreifes Kiosk-Setup-, Verwaltungs- und Update-System für Mini-PCs auf Basis von **Ubuntu Server (minimized)** – ohne schwergewichtigen Desktop-Manager.

---

## 1. Architektur & Boot-Ablauf

`kiosX` kombiniert einen minimalen X11/Openbox-Unterbau mit einem leichtgewichtigen Flask-Konfigurationsdienst, PipeWire-Audio, Avahi-mDNS-Discovery und einer fehlertoleranten Chromium-Watchdog-Schleife.

```text
Systemstart (Ubuntu Server minimized)
  ├── systemd: avahi-daemon.service      → mDNS (<hostname>.local + _http._tcp)
  ├── systemd: kiosk-ui.service (:8080)  → Flask Web-UI, REST-API, /splash, /qr.svg,
  │                                         avahi-publish (kiosx-<id>.local)
  ├── systemd user: pipewire + wireplumber + pipewire-pulse (via loginctl enable-linger)
  └── systemd: getty@tty1 (Autologin)
        └── ~/.bash_profile (nur /dev/tty1) → exec startx
              └── ~/.xinitrc → exec openbox-session
                    └── ~/.config/openbox/autostart (POSIX /bin/sh)
                          ├── xhost +SI:localuser:<user> (X11-Zugriff fuer Web-UI)
                          ├── xset s off / noblank / -dpms + unclutter -idle 2
                          ├── feh --bg-fill /etc/kiosk/wallpaper.jpg
                          ├── /opt/kiosX/kiosk-apply.sh (xrandr, feh, xinput, pactl)
                          └── /opt/kiosX/kiosk-watchdog.sh (Endlosschleife)
                                ├── pausiert sauber bei kiosx-browser.paused ('kiosx stop')
                                └── startet Chromium (--kiosk) → http://127.0.0.1:8080/splash
                                      └── nach N Sekunden → Weiterleitung auf Ziel-URL
```

### Tech-Stack
| Schicht | Komponenten |
|---|---|
| **Betriebssystem** | Ubuntu Server (minimized), getestet auf Ubuntu 26.04 LTS (x86_64 Mini-PCs) |
| **Grafik & Input** | `xorg`, `xinit`, `xauth`, `xserver-xorg-input-libinput`, `openbox`, `x11-xserver-utils`, `xinput`, `unclutter`, `feh` |
| **Browser** | `chromium` (nativ via apt oder Ubuntu Snap inkl. `camera` & `audio-record` Plugs) |
| **Audio** | `pipewire`, `wireplumber`, `pipewire-pulse`, `pipewire-alsa`, `pulseaudio-utils`, `alsa-utils` |
| **Netzwerk & Discovery** | `systemd-networkd` (Netplan), `wpasupplicant`, `avahi-daemon`, `avahi-utils` |
| **Backend & Web-UI** | Python 3, `python3-flask`, Pico CSS v2, `jq`, `qrencode`, `curl` |

---

## 2. Repository-Struktur & Systempfade

### Repository (`/opt/kiosX`)
```text
/opt/kiosX/
├── VERSION                    # Semantische Versionsnummer (aktuell 1.3.0)
├── install.sh                 # Idempotentes Installations- & Update-Skript
├── kiosx                      # SSH-faehiges CLI-Tool (verlinkt als /usr/local/bin/kiosx)
├── kiosk-watchdog.sh          # Chromium-Watchdog-Schleife (POSIX sh, Pause-Flag-faehig)
├── kiosk-overlay.py           # Natives X11/Tkinter Info-Overlay (10s, QR-Code, < 100ms)
├── kiosk-apply.sh             # Live-Applier fuer xrandr, feh, xinput & pactl
├── app.py                     # Flask-Backend, Hardware-Parser, Zeitplan, Z-Geste, LAN-PIN & Updater
├── wifi.conf                  # Standard-WLAN-Netzwerke (SSID=PASSWORT) fuer Netplan
├── templates/
│   ├── index.html             # Web-UI (Pico CSS v2, einklappbare Bloecke)
│   ├── login.html             # PIN-Login-Seite fuer LAN/WLAN-Zugriffe
│   ├── splash.html            # Start-Anzeige mit Kiosk-ID, IP, mDNS, Tailscale & QR-Code
│   └── touch_test.html        # Vollbild-Touch-Ecken-Test mit Koordinatenanzeige
├── .gitignore
└── README.md
```

### Vom Installer angelegte Systempfade
| Pfad | Besitzer / Rechte | Zweck |
|---|---|---|
| `/etc/kiosk/config.json` | `<user>:<user>` `0664` | Persistente Kiosk-Konfiguration (atomar geschrieben) |
| `/etc/kiosk/wallpaper.jpg` | `<user>:<user>` `0664` | Lokales Hintergrundbild (offline-faehig fuer X11 & `/splash`) |
| `/etc/kiosk/netplan-backup-*` | `root:root` `0700` | Automatische Backups vor Netplan-Aenderungen |
| `/etc/netplan/50-kiosx-network.yaml` | `root:root` `0600` | Hardware-unabhaengige Ethernet- & WLAN-Konfiguration |
| `/etc/systemd/system/kiosk-ui.service` | `root:root` `0644` | systemd-Unit fuer `app.py` (Port `8080`) |
| `/etc/systemd/system/getty@tty1.service.d/autologin.conf` | `root:root` `0644` | Autologin des Kiosk-Benutzers auf `tty1` |
| `/etc/sudoers.d/kiosx-updater` | `root:root` `0440` | Minimalrechte fuer Web-UI-Updates (`visudo`-geprueft) |
| `/etc/avahi/services/kiosx.service` | `root:root` `0644` | mDNS-Service-Ankuendigung (`_http._tcp`, TXT `id=<ID>`) |
| `/usr/local/bin/kiosx` | Symlink | Globales CLI → `/opt/kiosX/kiosx` |
| `~/.bash_profile` & `~/.xinitrc` | `<user>:<user>` | Automatischer `startx`-Start ausschliesslich auf `/dev/tty1` |
| `~/.config/openbox/autostart` | `<user>:<user>` `0755` | POSIX-`sh`-Autostart fuer Openbox |
| `~/.git-credentials` | `<user>:<user>` `0600` | Gespeichertes Read-Only GitHub PAT fuer Web-UI-Updates |
| `/run/user/<UID>/kiosx-browser.paused` | `<user>:<user>` | Laufzeit-Pause-Flag fuer `kiosx stop` (nach Reboot automatisch weg) |
| `/run/user/<UID>/kiosx-watchdog.pid` | `<user>:<user>` | PID-Lockfile gegen doppelte Watchdog-Instanzen |
| `/run/user/<UID>/kiosx-touch-test.pid` | `<user>:<user>` | PID des aktiven Touch-Test-Vollbildfensters |
| `/tmp/kiosx-apply.log`, `/tmp/kiosx-chromium.log`, `/tmp/kiosx-update.log` | `<user>` / `root` | Laufzeit- und Update-Logs |

---

## 3. Konfigurationsschema (`/etc/kiosk/config.json`)

Die Datei wird von `app.py` ueber `NamedTemporaryFile` + `fsync` + `os.replace` atomar geschrieben und von `kiosk-apply.sh` sowie `kiosk-watchdog.sh` per `jq` gelesen.

```json
{
  "device_id": "525Z",
  "url": "https://example.com",
  "splash_seconds": 15,
  "wallpaper_url": "https://images.unsplash.com/photo-1493606278519-11aa9f86e40a?auto=format&fit=crop&w=1920&q=85",
  "display": {
    "primary_output": "HDMI-2",
    "resolution": "1920x1080",
    "rotation": "normal",
    "secondary_output": "",
    "secondary_mode": "off"
  },
  "touch": {
    "device": "eGalax Inc. eGalaxTouch P86H128 2735 v00_T2 k4.10.143",
    "rotation": "auto"
  },
  "audio": {
    "card": "alsa_card.pci-0000_00_1f.3",
    "profile": "output:hdmi-stereo",
    "volume": 75
  }
}
```

| Feld | Wertebereich / Beschreibung |
|---|---|
| `device_id` | 4-stelliger verwechslungssicherer Code (`23456789ABCDEFGHJKMNPQRSTVWXYZ`, ohne `0/O/1/I/L/U`), manuell 2–12 alphanumerische Zeichen |
| `url` | Ziel-URL (`http://`, `https://` oder `file://`) |
| `splash_seconds` | `0`–`120` Sekunden (`0` deaktiviert `/splash`) |
| `wallpaper_url` | Optionale `http(s)://`-Bild-URL (JPEG/PNG/WebP, max. 15 MB); wird lokal unter `/etc/kiosk/wallpaper.jpg` gespeichert |
| `display.primary_output` | `xrandr`-Ausgang (z. B. `HDMI-1`, `DP-1`) oder `""` (Auto-Fallback auf ersten verbundenen Monitor) |
| `display.resolution` | `"auto"` oder `<Breite>x<Hoehe>` (z. B. `"1920x1080"`) |
| `display.rotation` | `"normal"`, `"right"`, `"inverted"`, `"left"` |
| `display.secondary_output` | Optionaler zweiter `xrandr`-Ausgang oder `""` |
| `display.secondary_mode` | `"off"`, `"right"`, `"left"`, `"mirror"` |
| `touch.device` | Exakter Pointer-Geraetename aus `xinput list` (oder numerische ID) |
| `touch.rotation` | `"auto"` (Standard, uebernimmt `xrandr`-Drehung via `map-to-output`), oder manuelle Korrektur `"90"`, `"180"`, `"270"` (Legacy `"0"` wird automatisch auf `"auto"` gemappt) |
| `audio.card` | PipeWire/PulseAudio-Kartenname aus `pactl list cards` |
| `audio.profile` | Karten-Profil (z. B. `output:hdmi-stereo`, `output:analog-stereo`) |
| `audio.volume` | Ganzzahl `0`–`100` (in Prozent) |
| `schedule.enabled` | `true`/`false` – aktiviert zeitgesteuerten Monitor-Standby (inkl. Touch-Sperre & Audio-Mute) |
| `schedule.timezone` | IANA-Zeitzone (Standard: `"Europe/Vienna"`, inkl. Sommer-/Winterzeit) |
| `schedule.scope` | `"both"` (beide Displays gemeinsam) oder `"separate"` (Primär & Sekundär getrennt) |
| `schedule.primary` / `secondary` | `{ preset: "inhouse"\|"messe"\|"custom", days: { mon..sun: { enabled, start, end } } }` |

### Fertige Zeitplan-Vorlagen
- **Inhouse:** Mo–Do `07:15–16:45`, Fr `07:15–12:15`, Sa–So Standby
- **Messe:** Mo–So `08:00–18:00`
- **Eigener Zeitplan:** Frei pro Wochentag (auch Über-Mitternacht-Fenster wie `22:00–06:00` werden unterstützt)

> Beim Übergang in den Standby wird Touch (`xinput disable`) gesperrt und Audio stummgeschaltet; beim Wiedereinschalten wird nach `xset dpms force on` automatisch `kiosk-apply.sh` ausgeführt, sodass Auflösung, Drehung, Wallpaper, Touch-Kalibrierung und Audio-Lautstärke vollständig wiederhergestellt werden.

---

## 4. Installation & Zero-Touch-Rollout

### Erstinstallation auf neuem Mini-PC (Ubuntu Server minimized, per SSH)

```bash
# 1. git installieren
sudo apt-get update && sudo apt-get install -y git

# 2. Repo nach /opt/kiosX klonen (mit Read-Only PAT fuer automatische Web-UI-Updates)
sudo mkdir -p /opt/kiosX && sudo chown "$USER:$USER" /opt/kiosX
git clone https://teletom:<READONLY_PAT>@github.com/teletom/kiosX.git /opt/kiosX

# 3. Vollstaendige Installation ausfuehren (idempotent)
cd /opt/kiosX
sudo bash install.sh

# 4. Neustart fuer tty1-Autologin, X11/Openbox, /splash und Chromium
sudo reboot
```

### Parameter von `install.sh`
| Option | Standard | Beschreibung |
|---|---|---|
| `--user <name>` | `$SUDO_USER` oder `kiosk` | Linux-Benutzer fuer X11-Session, PipeWire und Web-UI |
| `--port <port>` | `8080` | HTTP-Port der Flask-Weboberflaeche |
| `--git-token <pat>` | `$KIOSX_GIT_TOKEN` | Read-Only GitHub PAT fuer Web-UI-Updates (alternativ in Clone-URL oder interaktiver Prompt) |
| `--git-user <name>` | `teletom` | GitHub-Benutzername fuer `--git-token` |
| `--wifi-conf <pfad>` | `/opt/kiosX/wifi.conf` | Datei mit Standard-WLANs (`SSID=PASSWORT`) |
| `--skip-netplan` | aus | Bestehende Netplan-Konfiguration unveraendert lassen |

### Besonderheiten im Installer
- **Ubuntu `universe` & Mirror-Resilienz:** Aktiviert `universe` bei Bedarf automatisch und wiederholt `apt-get install` bis zu 3× (`Acquire::Retries=3` + `apt-get update`), um kurzzeitige Mirror-Sync-404s abzufangen.
- **Snap-Chromium-Bootstrap:** Prueft auf natives apt-`chromium`; auf Ubuntu Server wird bei Bedarf `snapd` installiert, auf `snap wait system seed.loaded` gewartet, `chromium-browser`/Snap installiert und `chromium:camera` sowie `chromium:audio-record` explizit verbunden.
- **Hardware-unabhaengiges Netplan:** Ethernet nutzt Wildcards (`en*`, `eth*`, `route-metric: 100`, `optional: true`). WLAN-Adapter werden zur Laufzeit ueber `/sys/class/net/*/wireless` erkannt (umgeht den bekannten Netplan-Wildcard-Bug bei `wifis:` mit `systemd-networkd`) und mit `route-metric: 200` + den Netzen aus `wifi.conf` eingetragen. Vor `netplan apply` erfolgt immer ein Backup nach `/etc/kiosk/netplan-backup-*` sowie eine Validierung per `netplan generate` mit automatischem Rollback im Fehlerfall.
- **GitHub-Update-Auth:** Erkennt ein Token in der Clone-URL, ueber `--git-token` oder per einmaligem unsichtbarem Prompt, speichert es mit `0600` in `~/.git-credentials`, setzt `credential.helper "store --file=~/.git-credentials"` im Repo und bereinigt eingebettete Tokens aus `.git/config`.

---

## 5. Hardware-Subsysteme im Detail

### Display & Hintergrundbild
- `kiosk-apply.sh` wendet primaeres und optionales sekundaeres Display in einem einzigen atomaren `xrandr`-Aufruf an (`--primary`, `--auto`/`--mode`, `--rotate`, `--off`/`--right-of`/`--left-of`/`--same-as`).
- Nach jeder `xrandr`-Aenderung wird `/etc/kiosk/wallpaper.jpg` via `feh --no-fehbg --bg-fill` neu skaliert.

### Touch-Kalibrierung
- Modernes `xinput map-to-output <id> <output>` liest die aktuelle `xrandr`-CRTC-Drehung selbststaendig aus und schreibt sie direkt in die `Coordinate Transformation Matrix`.
- Im Standardmodus **`auto`** belaesst `kiosk-apply.sh` diese Matrix unveraendert – Touch folgt jeder Display-Drehung automatisch ohne Doppel-Rotation.
- Nur fuer physisch verdreht eingebaute Touch-Panels kann eine zusaetzliche Korrektur (`+90°`, `+180°`, `+270°`) gewaehlt werden; diese wird per 3×3-Matrizenmultiplikation ($M_{\text{out}} \cdot R$) auf die bestehende Output-Matrix aufmultipliziert, sodass auch Dual-Monitor-Setups exakt abgebildet bleiben.
- Ueber den **Touch-Ecken-Test** (`/touch-test` → `/touch-test-view`) wird eine isolierte Vollbild-Chromium-Instanz ueber dem Kiosk-Browser geoeffnet und beim Beenden sauber geschlossen.

### Audio (PipeWire)
- `kiosk-apply.sh` setzt per `pactl set-card-profile` das gewaehlte Profil (z. B. Umschaltung zwischen HDMI-Audio und analogem Line-Out auf derselben Soundkarte), ermittelt nach kurzer Wartezeit den neu erzeugten Sink, setzt ihn als `@DEFAULT_SINK@`, hebt Stummschaltung auf und setzt die Lautstaerke (`0–100 %`).
- `POST /test-audio` generiert on-the-fly eine 1,2 s Stereo-WAV-Datei (Links 440 Hz, Rechts 554 Hz, Stereo 659 Hz) fuer `paplay` direkt ueber den aktiven PipeWire-Sink (Fallback: `speaker-test -t wav -c 2 -l 1`).

### USB-Webcams & Mikrofone
- `kiosk-watchdog.sh` startet Chromium mit `--use-fake-ui-for-media-stream` und `--autoplay-policy=no-user-gesture-required` → Kamera-/Mikrofon-Zugriffe auf HTTPS-Kiosk-Seiten benoetigen keinen manuellen Klick auf „Zulassen“.
- `install.sh` fuegt den Kiosk-Benutzer den Gruppen `video` und `audio` hinzu und verbindet bei Snap-Chromium `chromium:camera` sowie `chromium:audio-record`.
- **Hinweis fuer eingebettete Web-Apps:** Wenn die Kamera innerhalb eines `<iframe>` verwendet wird, muss das `<iframe>`-Tag die Permissions-Policy explizit freigeben:
  ```html
  <iframe src="https://..." allow="autoplay *; camera *; microphone *; fullscreen *"></iframe>
  ```

---

## 6. HTTP-API & Routen (Port 8080)

Alle Hardware-Aufrufe laufen mit `LC_ALL=C`, `DISPLAY=:0`, `XAUTHORITY` und `XDG_RUNTIME_DIR=/run/user/<UID>`.

| Methode & Pfad | Beschreibung |
|---|---|
| `GET /` | Rendert das Konfigurations-Web-UI (`templates/index.html`) |
| `GET /splash` | Vollbild-Startanzeige mit Kiosk-ID, Hostname, LAN-/mDNS-/Tailscale-URL, QR-Code & Countdown |
| `GET /touch-test-view` | Vollbild-Touch-Kalibrierungstest mit 4 Ecken-Zielen & Koordinaten-Canvas |
| `GET /wallpaper.jpg` | Liefert das lokale Hintergrundbild (`/etc/kiosk/wallpaper.jpg`, `no-cache`) |
| `GET /qr.svg` | Erzeugt lokal einen SVG-QR-Code zur Config-UI (`?target=auto\|tailscale\|mdns`) |
| `GET /api/info` | JSON mit `version`, `commit`, `branch`, `device_id`, `hostname`, `lan_ip`, `tailscale_ip`, `mdns_alias`, `config_url`, `tailscale_url`, `target_url`, `splash_seconds` |
| `GET /api/hardware` | Aktuelle Hardware-Listen (`displays`, `touch`, `audio`, `system`) + gespeicherte Config |
| `POST /save` | Validiert JSON/Form-Daten, laedt ggf. neues Wallpaper, speichert `config.json` atomar, aktualisiert mDNS-Alias und fuehrt `kiosk-apply.sh` aus |
| `POST /test-audio` | Spielt Stereo-Testton ueber den aktiven Ausgang ab |
| `POST /touch-test` | Oeffnet/schliesst das Touch-Test-Vollbildfenster (`{"action": "toggle"\|"start"\|"stop"}`) |
| `POST /restart-browser` | Hebt eventuelle `kiosx stop`-Pause auf und beendet Chromium fuer sauberen Watchdog-Neustart |
| `POST /api/display-power` | Manuelles Ein-/Ausschalten oder Zeitplan-Automatik (`{"action": "on"\|"off"\|"auto", "target": "both"\|"primary"\|"secondary"}`) |
| `POST /api/identify` | Blendet Kiosk-ID, IPs & QR-Code fuer N Sekunden (Standard 10 s) per Tkinter-Overlay (oder Chromium-Fallback) ein – ohne Browser-Neustart |
| `GET/POST /login`, `POST /logout` | PIN-Anmeldung (`050477`) fuer LAN/WLAN-Zugriffe (Localhost & Tailscale ohne Code; Brute-Force-Sperre nach 5 Fehlversuchen) |
| `GET /api/update/check` | Prueft per `git fetch origin` auf neue Versionen/Commits inkl. Changelog |
| `POST /api/update/apply` | Fuehrt `git pull --ff-only` aus und startet im Hintergrund `kiosk-ui.service` (oder `install.sh --skip-netplan`, falls `install.sh` geaendert wurde) neu |

---

## 7. CLI-Referenz (`kiosx`, SSH-kompatibel)

Der Befehl `kiosx` (`/usr/local/bin/kiosx`) ermittelt automatisch den Kiosk-Benutzer und die X11-/PipeWire-Umgebung – funktioniert direkt ueber SSH ohne manuelles Setzen von `DISPLAY` oder `XAUTHORITY`:

```bash
kiosx status                    # Version, Kiosk-ID, IPs, mDNS, Display, Browser, Audio & Dienste
kiosx start                     # Startet Chromium Kiosk-Browser (hebt Pause auf, startet Web-UI bei Bedarf)
kiosx stop                      # Stoppt Browser dauerhaft (Watchdog pausiert, Hintergrundbild sichtbar)
kiosx restart                   # Startet Chromium sauber neu (inkl. Start-Anzeige /splash)
kiosx apply                     # Wendet /etc/kiosk/config.json sofort live an (xrandr/feh/xinput/pactl)
kiosx display off               # Monitor sofort in Standby (Touch aus, Audio stumm)
kiosx display on                # Monitor sofort einschalten (stellt Aufloesung, Touch & Audio wieder her)
kiosx display auto              # Zurueck zur Zeitplan-Automatik
kiosx url                       # Aktuelle Kiosk-URL anzeigen
kiosx url https://example.com   # Neue Kiosk-URL atomar setzen und Browser neu starten
kiosx id                        # 4-stellige Kiosk-ID & Verbindungs-URLs anzeigen
kiosx identify [sekunden]       # Kiosk-ID & Infos fuer 10s am Display einblenden (ohne Neustart)
kiosx version                   # Installierte kiosX-Version, Git-Commit & Branch anzeigen
kiosx ui start|stop|restart|status
kiosx logs ui|browser|apply     # Live-Logs von Web-UI, Chromium oder kiosk-apply.sh
kiosx update [--skip-netplan]   # git pull --ff-only && sudo bash install.sh
```

---

## 8. Sicherheits- & Berechtigungskonzept

- **LAN/WLAN-PIN-Schutz:** Zugriffe aus dem lokalen LAN/WLAN erfordern den Zugangscode (Standard `050477`, konfigurierbar in `config.json` unter `auth.lan_pin`) inkl. Brute-Force-Sperre (5 Fehlversuche → 60 s Sperre) und signiertem `HttpOnly`-Session-Cookie (8 h). **Tailscale** (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) und **Localhost** werden direkt ueber die echte TCP-Gegenstelle (`remote_addr`) ohne Code zugelassen.
- **Globale Z-Touch-Geste:** Ein passiver `xinput test-xi2 --root`-Listener erkennt das Z-Muster (`Oben-Links → Oben-Rechts → Unten-Links → Unten-Rechts`, als 4 schnelle Ecken-Tipps oder gezogener Fingerstrich, automatisch kompatibel zu jeder Display-Drehung) und blendet das 10-Sekunden-Info-Overlay ein.
- **Kein Root im Web-UI:** `kiosk-ui.service` laeuft als unprivilegierter Kiosk-Benutzer.
- **Eingeschraenkte Update-Rechte:** `/etc/sudoers.d/kiosx-updater` (`0440`, `visudo`-geprueft) erlaubt passwortlos ausschliesslich `systemctl restart kiosk-ui.service` und `bash /opt/kiosX/install.sh --skip-netplan`.
- **Snap-Confinement:** Profilverzeichnisse fuer Snap-Chromium liegen unter `~/snap/chromium/common/kiosx-profile` bzw. `~/snap/chromium/common/kiosx-touch-test` (nicht in gesperrten Dot-Verzeichnissen).
- **Geschuetzte Credentials:** `/etc/netplan/50-kiosx-network.yaml` und `~/.git-credentials` erhalten strikt `chmod 0600`.
- **URL-Validierung:** `app.py` blockiert unzulaessige Schemes (`javascript:`, `data:`, `file://` bei Wallpapers) und begrenzt Wallpaper-Downloads auf gueltige Bild-Header bis max. 15 MB.

---

## 9. Wartung & Fehlersuche

```bash
# Gesamtstatus & Logs pruefen
kiosx status
kiosx logs ui
kiosx logs browser
kiosx logs apply

# Web-UI-Update-Log pruefen
cat /tmp/kiosx-update.log

# Aktuelle Konfiguration & Netplan-Backups
cat /etc/kiosk/config.json
ls -la /etc/kiosk/netplan-backup-*

# Hardware-Diagnose (Monitor, Touch, Webcam, Audio)
DISPLAY=:0 xrandr --query
DISPLAY=:0 xinput list
lsusb && ls -la /dev/video*
XDG_RUNTIME_DIR="/run/user/$(id -u)" pactl list short cards
```

### Typische Praxis-Hinweise
- **Touchscreen nicht in `xinput list` (z. B. iiyama TF-Serie mit Weida-Controller):** Zuerst per `lsusb` pruefen, ob der USB-Touch-Controller ueberhaupt am Bus sichtbar ist. Bei iiyama-PCAP-Monitoren haengt sich der interne USB-Hub/Touch-Controller gelegentlich auf – ein echter Power-Cycle des Monitors (10 s stromlos) behebt das sofort.
- **Touch-Eingabe nach Display-Drehung verschoben:** Im Web-UI sicherstellen, dass Touch-Korrektur auf **„Automatisch (folgt Display-Drehung)“** steht und einmal speichern.
- **Automatische Web-UI-Updates melden fehlende Git-Auth:** Einmalig `sudo bash /opt/kiosX/install.sh --skip-netplan --git-token "<READONLY_PAT>"` ausfuehren.
- **Versionierung beim Entwickeln:** Fuer ein neues Release einfach `/opt/kiosX/VERSION` erhoehen (z. B. `1.2.0`), committen und auf `origin/main` pushen – alle Kiosks erkennen das Update direkt im Web-UI.
