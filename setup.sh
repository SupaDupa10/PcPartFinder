#!/usr/bin/env bash
# Ein-Klick-Einrichtung unter Linux und macOS.
#
# Prueft und installiert bei Bedarf Python (>= 3.11) und Node.js (>= 20), legt
# die virtuelle Umgebung an, installiert die Abhaengigkeiten, erzeugt ".env"
# aus der Vorlage und baut die Oberflaeche. Am Ende wird die Anwendung
# gestartet. Mehrfach ausfuehrbar: Was schon da ist, wird nur gemeldet.
#
# Verwendung: ./setup.sh [--check] [--no-install] [--no-start] [--rebuild]
#   --check       nur pruefen und berichten, nichts aendern
#   --no-install  fehlende Programme nicht zu installieren versuchen
#   --no-start    nicht starten, nur einrichten
#   --rebuild     Oberflaeche auch dann neu bauen, wenn sie schon da ist
#
# Schreibweise bewusst ASCII (ue/oe/ae/ss): Einige Terminals zeigen Umlaute
# sonst falsch an - dieselbe Regel wie in start.sh.
set -euo pipefail

cd "$(dirname "$0")"

CHECK=0
NO_INSTALL=0
NO_START=0
REBUILD=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --no-install) NO_INSTALL=1 ;;
    --no-start) NO_START=1 ;;
    --rebuild) REBUILD=1 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unbekannte Option: $arg (siehe --help)" >&2
      exit 2
      ;;
  esac
done

step() { printf '\n==> %s\n' "$1"; }
ok() { printf '    OK: %s\n' "$1"; }
note() { printf '    Hinweis: %s\n' "$1"; }
missing() { printf '    FEHLT: %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
python_ok() {
  local exe="$1"
  [ -x "$(command -v "$exe" 2>/dev/null)" ] || return 1
  "$exe" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null
}

PY=""
for candidate in python3 python; do
  if python_ok "$candidate"; then PY="$candidate"; break; fi
done

step "Python pruefen"
if [ -n "$PY" ]; then
  ok "$("$PY" --version 2>&1) ($(command -v "$PY"))"
else
  missing "Python 3.11+"
  if [ "$CHECK" = "1" ]; then
    note "Installieren mit: sudo apt-get install -y python3 python3-venv  (bzw. dnf/zypper/brew)"
  elif [ "$NO_INSTALL" = "1" ]; then
    note "Nicht installiert (--no-install)."
  elif command -v apt-get >/dev/null 2>&1; then
    note "installiere python3 und python3-venv ueber apt-get (fragt ggf. nach dem Passwort)"
    sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper --non-interactive install python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm python
  elif command -v brew >/dev/null 2>&1; then
    brew install python@3.12
  else
    note "Kein bekannter Paketmanager gefunden - bitte Python 3.11+ von https://www.python.org/downloads/ installieren."
  fi

  for candidate in python3 python; do
    if python_ok "$candidate"; then PY="$candidate"; break; fi
  done
  if [ -z "$PY" ]; then
    echo
    echo "Ohne Python 3.11+ kann die Einrichtung nicht weiterlaufen." >&2
    exit 1
  fi
  ok "$("$PY" --version 2>&1) installiert"
fi

# ---------------------------------------------------------------------------
# 2. Node.js (nur fuer die Oberflaeche)
# ---------------------------------------------------------------------------
node_major() {
  command -v node >/dev/null 2>&1 || return 1
  node -v | sed 's/^v//' | cut -d. -f1
}

step "Node.js pruefen (nur fuer die Oberflaeche noetig)"
NODE_MAJOR="$(node_major || true)"
if [ -n "${NODE_MAJOR:-}" ] && [ "$NODE_MAJOR" -ge 20 ] 2>/dev/null; then
  ok "Node.js $NODE_MAJOR ($(command -v node))"
else
  if [ -n "${NODE_MAJOR:-}" ]; then missing "Node.js $NODE_MAJOR ist zu alt (noetig: 20+)"; else missing "Node.js 20+"; fi

  if [ "$CHECK" = "1" ] || [ "$NO_INSTALL" = "1" ]; then
    note "Node.js 20+ von https://nodejs.org/ installieren (oder: brew install node)."
  elif command -v brew >/dev/null 2>&1; then
    note "installiere Node.js ueber Homebrew"
    brew install node
  elif command -v apt-get >/dev/null 2>&1; then
    note "installiere nodejs und npm ueber apt-get - viele Distributionen liefern eine zu alte Version;"
    note "falls danach weiterhin < 20: NodeSource oder nvm verwenden (https://nodejs.org/)."
    sudo apt-get install -y -qq nodejs npm || true
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y nodejs npm || true
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm nodejs npm || true
  else
    note "Kein bekannter Paketmanager gefunden - Node.js 20+ von https://nodejs.org/ installieren."
  fi

  NODE_MAJOR="$(node_major || true)"
  if [ -z "${NODE_MAJOR:-}" ] || ! [ "$NODE_MAJOR" -ge 20 ] 2>/dev/null; then
    note "Ohne Node.js wird die Oberflaeche nicht gebaut - die API laeuft trotzdem: http://127.0.0.1:8000/docs"
    NODE_MAJOR=""
  else
    ok "Node.js $NODE_MAJOR installiert"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Browser fuer die Website-Suche
# ---------------------------------------------------------------------------
step "Browser pruefen (fuer die Suche ueber eBay- und Kleinanzeigen-Website)"
CHROME=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then CHROME="$candidate"; break; fi
done
FIREFOX=""
for candidate in firefox firefox-esr; do
  if command -v "$candidate" >/dev/null 2>&1; then FIREFOX="$candidate"; break; fi
done
if [ -n "$CHROME" ]; then
  ok "Chrome gefunden ($CHROME)"
elif [ -d "/Applications/Google Chrome.app" ]; then
  ok "Chrome gefunden (/Applications/Google Chrome.app)"
elif [ -n "$FIREFOX" ]; then
  # Kein Chrome, aber Firefox: die Website-Suche laeuft auch damit
  # (EBAY_WEB_BROWSER=firefox) - er braucht nur seinen eigenen Build.
  ok "Kein Chrome, aber Firefox gefunden ($FIREFOX)"
  note "Fuer die Suche in Firefox nach der Einrichtung einmalig: .venv/bin/python -m playwright install firefox"
else
  missing "Google Chrome (oder Firefox)"
  note "Installieren: https://www.google.com/chrome/ (bzw. apt/dnf/brew)."
  note "Ohne Browser gibt es keine Suche ueber eBay- oder Kleinanzeigen-Website."
fi

if [ "$CHECK" = "1" ]; then
  printf '\nNur geprueft - es wurde nichts geaendert. Einrichtung starten: ./setup.sh\n'
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Virtuelle Umgebung und Abhaengigkeiten
# ---------------------------------------------------------------------------
step "Python-Umgebung einrichten"
if [ -x ".venv/bin/python" ]; then
  ok ".venv ist schon vorhanden"
else
  printf '    lege .venv an ...\n'
  "$PY" -m venv .venv
  ok ".venv angelegt"
fi

printf '    installiere Abhaengigkeiten (requirements.txt) ...\n'
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
ok "Abhaengigkeiten installiert"

# ---------------------------------------------------------------------------
# 5. Konfiguration
# ---------------------------------------------------------------------------
step "Konfiguration"
if [ -f ".env" ]; then
  ok ".env ist vorhanden (wird nicht ueberschrieben)"
else
  cp .env.example .env
  ok ".env aus .env.example erzeugt - eigene Werte bei Bedarf dort eintragen"
fi

# ---------------------------------------------------------------------------
# 6. Oberflaeche bauen
# ---------------------------------------------------------------------------
step "Oberflaeche bauen"
if [ -z "${NODE_MAJOR:-}" ]; then
  note "Ohne Node.js uebersprungen - die API laeuft trotzdem: http://127.0.0.1:8000/docs"
elif [ -f "frontend/dist/index.html" ] && [ "$REBUILD" = "0" ]; then
  ok "frontend/dist ist schon vorhanden (neu bauen: ./setup.sh --rebuild)"
else
  (
    cd frontend
    if [ -f package-lock.json ]; then npm ci; else npm install; fi
    npm run build
  )
  ok "Oberflaeche gebaut (frontend/dist)"
fi

# ---------------------------------------------------------------------------
# 7. Starten
# ---------------------------------------------------------------------------
step "Fertig"
printf '    Einstellungen und eigene Daten: siehe README, Abschnitt 2 und 4.\n'
if [ "$NO_START" = "1" ]; then
  printf '    Starten mit: ./start.sh\n'
  exit 0
fi
printf '    Starte die Anwendung (Beenden mit Strg+C) ...\n'
exec ./start.sh
