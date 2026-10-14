@echo off
REM Startet Backend (und die gebaute Oberflaeche) lokal unter Windows.
REM Verwendung: start.bat [--rebuild-frontend] [--restart]
REM   --restart beendet ein laufendes Backend dieses Projekts und startet es
REM   mit dem aktuellen Code neu (sonst laeuft ein alter Prozess weiter).

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo FEHLER: Keine virtuelle Umgebung gefunden.
  echo Bitte zuerst die Einrichtung ausfuehren:
  echo   setup.bat
  echo Von Hand waere es:
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo HINWEIS: Es gibt keine .env - es gelten die Standardwerte.
  echo          Vorlage: .env.example
)

if not exist "frontend\dist" goto build
if /I "%~1"=="--rebuild-frontend" goto build
goto run

:build
where npm >nul 2>&1
if errorlevel 1 (
  echo WARNUNG: npm nicht gefunden - die Oberflaeche wird nicht gebaut.
  echo          Die API ist trotzdem unter /docs erreichbar.
  goto run
)
echo Baue Oberflaeche ...
pushd frontend
call npm install --silent
if errorlevel 1 (
  echo FEHLER: npm install fehlgeschlagen.
  popd
  echo.
  pause
  exit /b 1
)
call npm run build
if errorlevel 1 (
  echo FEHLER: Frontend-Build fehlgeschlagen.
  popd
  echo.
  pause
  exit /b 1
)
popd

:run
if "%BACKEND_HOST%"=="" set "BACKEND_HOST=127.0.0.1"
if "%BACKEND_PORT%"=="" set "BACKEND_PORT=8000"

REM Datenquelle: Beispieldaten nur, wenn WIRKLICH keine echte Quelle vorhanden
REM ist - weder eine eigene Importdatei noch eBay-Zugangsdaten. Kleinanzeigen
REM hat keine zulaessige API, daher braucht es mindestens eine der beiden.
if not "%LOCAL_IMPORT_PATH%"=="" goto port_check
if not "%EBAY_CLIENT_ID%"=="" goto port_check
if not exist ".env" goto demo_data
findstr /R /C:"^LOCAL_IMPORT_PATH=." ".env" >nul 2>&1
if not errorlevel 1 goto port_check
findstr /R /C:"^EBAY_CLIENT_ID=." ".env" >nul 2>&1
if not errorlevel 1 goto port_check

:demo_data
if not exist "samples\beispiel_angebote.json" goto port_check
set "LOCAL_IMPORT_PATH=samples\beispiel_angebote.json"
echo HINWEIS: Keine eigene Importdatei gesetzt - es werden Beispieldaten aus
echo          samples\beispiel_angebote.json geladen. Das sind erfundene
echo          Demo-Angebote: keine Live-Suche und keine echten Marktpreise.
echo          Eigene Daten: LOCAL_IMPORT_PATH in .env eintragen.

:port_check
REM Port vorab pruefen: Ein belegter Port wird sonst als Bind-Fehler gemeldet
REM und das Fenster schliesst sich, bevor man die Ursache lesen kann.
set "PORT_ARGS=%BACKEND_HOST% %BACKEND_PORT%"
if /I "%~1"=="--restart" set "PORT_ARGS=%PORT_ARGS% --takeover"
if /I "%~2"=="--restart" set "PORT_ARGS=%PORT_ARGS% --takeover"
"%PY%" "scripts\port_check.py" %PORT_ARGS%
REM Reihenfolge: von der groessten Zahl abwaerts, sonst faengt "errorlevel 1"
REM auch 3, 4 und 5 ab. Ein laufendes Backend mit aelterem Code darf nicht als
REM "laeuft schon" durchgehen - sonst wirkt keine Aenderung.
if errorlevel 5 (
  echo.
  echo Das laufende Backend arbeitet noch mit AELTEREM Code. Solange es laeuft,
  echo gilt weder ein neuer Filter noch eine entfernte Funktion.
  echo          Neu starten:  start.bat --restart
  echo.
  pause
  exit /b 1
)
if errorlevel 4 (
  echo.
  echo Starten Sie start.bat erneut, sobald der Port frei ist.
  echo.
  pause
  exit /b 1
)
if errorlevel 3 (
  echo          Es wird nichts doppelt gestartet - die Oberflaeche wird geoeffnet.
  start "" "http://%BACKEND_HOST%:%BACKEND_PORT%/"
  echo.
  pause
  exit /b 0
)
if errorlevel 2 (
  echo WARNUNG: Die Portpruefung war nicht moeglich - Start wird trotzdem versucht.
)

echo Starte Backend auf http://%BACKEND_HOST%:%BACKEND_PORT%
"%PY%" -m uvicorn backend.main:app --host %BACKEND_HOST% --port %BACKEND_PORT%
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo FEHLER: Das Backend wurde mit Fehlercode %RC% beendet.
  echo         Die Meldung oberhalb nennt den Grund.
  echo.
  pause
)
endlocal
