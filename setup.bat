@echo off
REM Ein-Klick-Einrichtung unter Windows (ruft setup.ps1 auf).
REM
REM Prueft und installiert bei Bedarf Python, Node.js und einen Browser
REM (Google Chrome; ist schon Firefox da, wird kein Chrome installiert) und
REM richtet danach die Anwendung ein. Mehrfach ausfuehrbar.
REM
REM Verwendung: setup.bat [-Check] [-NoChrome] [-NoStart] [-Rebuild]
REM   -Check     nur pruefen und berichten, nichts aendern
REM   -NoChrome  keinen Browser installieren, nur melden
REM   -NoStart   nicht starten, nur einrichten
REM   -Rebuild   Oberflaeche auch dann neu bauen, wenn sie schon da ist

setlocal
cd /d "%~dp0"

where powershell >nul 2>&1
if errorlevel 1 (
  echo FEHLER: PowerShell wurde nicht gefunden.
  echo         Windows 10/11 liefert PowerShell mit - bitte die Installation pruefen.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo Die Einrichtung wurde mit Fehlercode %RC% beendet.
  echo Die Meldung oberhalb nennt den Grund.
  echo.
  pause
)

endlocal & exit /b %RC%
