#!/usr/bin/env bash
# Startet Backend (und die gebaute Oberflaeche) lokal.
# Verwendung: ./start.sh [--rebuild-frontend] [--restart]
#   --restart beendet ein laufendes Backend dieses Projekts und startet es mit
#   dem aktuellen Code neu (sonst bedient ein alter Prozess weiter).
set -euo pipefail

cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  # Windows-Layout (Git Bash) als Rueckfall
  if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
  fi
fi

if [ ! -x "$PY" ]; then
  echo "FEHLER: Keine virtuelle Umgebung gefunden." >&2
  echo "Bitte zuerst die Einrichtung ausfuehren:  bash setup.sh" >&2
  echo "Von Hand waere es:" >&2
  echo "  python -m venv .venv" >&2
  echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "HINWEIS: Es gibt keine .env - es gelten die Standardwerte." >&2
  echo "         Vorlage: .env.example" >&2
fi

if [ ! -d "frontend/dist" ] || [ "${1:-}" = "--rebuild-frontend" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Baue Oberflaeche ..."
    (cd frontend && npm install --silent && npm run build)
  else
    echo "WARNUNG: npm nicht gefunden - die Oberflaeche wird nicht gebaut." >&2
    echo "         Die API ist trotzdem unter /docs erreichbar." >&2
  fi
fi

HOST="${BACKEND_HOST:-127.0.0.1}"
PORT="${BACKEND_PORT:-8000}"

# --restart: ein laufendes Backend dieses Projekts wird beendet, damit der
# aktuelle Code geladen wird.
PORT_ARGS=("$HOST" "$PORT")
for arg in "$@"; do
  if [ "$arg" = "--restart" ]; then
    PORT_ARGS+=("--takeover")
  fi
done

# Datenquelle: Beispieldaten nur, wenn WIRKLICH keine echte Quelle vorhanden ist
# - weder eigene Importdatei noch eBay-Zugangsdaten (sonst wuerden sich Demo-
# Angebote unter echte Ergebnisse mischen).
keine_quelle() {
  if [ -n "${LOCAL_IMPORT_PATH:-}" ] || [ -n "${EBAY_CLIENT_ID:-}" ]; then
    return 1
  fi
  if grep -qE '^[[:space:]]*LOCAL_IMPORT_PATH=.+' .env 2>/dev/null; then
    return 1
  fi
  if grep -qE '^[[:space:]]*EBAY_CLIENT_ID=.+' .env 2>/dev/null; then
    return 1
  fi
  return 0
}

if keine_quelle; then
  if [ -f "samples/beispiel_angebote.json" ]; then
    LOCAL_IMPORT_PATH="samples/beispiel_angebote.json"
    export LOCAL_IMPORT_PATH
    echo "HINWEIS: Keine eigene Importdatei gesetzt - es werden Beispieldaten aus" >&2
    echo "         samples/beispiel_angebote.json geladen. Das sind erfundene" >&2
    echo "         Demo-Angebote: keine Live-Suche, keine echten Marktpreise." >&2
    echo "         Eigene Daten: LOCAL_IMPORT_PATH in .env eintragen." >&2
  fi
fi

# Port vorab pruefen: Ein belegter Port wuerde sonst als Bind-Fehler enden,
# ohne den Grund zu nennen.
port_status=0
"$PY" scripts/port_check.py "${PORT_ARGS[@]}" || port_status=$?
case "$port_status" in
  0)
    ;;
  3)
    echo "          Es wird nichts doppelt gestartet." >&2
    exit 0
    ;;
  5)
    echo "          Das laufende Backend arbeitet noch mit aelterem Code." >&2
    echo "          Neu starten:  ./start.sh --restart" >&2
    exit 1
    ;;
  *)
    echo "          Starten Sie start.sh erneut, sobald der Port frei ist." >&2
    exit 1
    ;;
esac

echo "Starte Backend auf http://${HOST}:${PORT}"
exec "$PY" -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
