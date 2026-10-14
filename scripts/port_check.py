"""Prueft vor dem Start, ob der gewuenschte Backend-Port belegt ist.

Aufruf::

    python scripts/port_check.py [host] [port] [--takeover]

Exit-Codes (bewusst unterscheidbar, damit die Startskripte reagieren koennen)::

    0  Port ist frei - das Backend kann starten.
    2  Aufruf fehlgeschlagen (ungueltige Argumente).
    3  Port ist belegt, und es antwortet bereits das Backend dieses Projekts.
    4  Port ist belegt von einem anderen Programm.
    5  Port ist belegt vom Backend dieses Projekts, das aber noch mit
       aelterem Code laeuft (gestartet vor der letzten Codeaenderung).

``--takeover`` beendet in den Faellen 3 und 5 das laufende Projekt-Backend und
meldet den Port danach als frei. Ein fremder Dienst wird damit nie beendet.

Die Ausgabe ist bewusst ASCII: Die Startskripte zeigen sie in einer
Windows-Konsole an, die UTF-8 nicht zuverlaessig darstellt.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HEALTH_PATH = "/api/health"
TIMEOUT_SECONDS = 2.0
TAKEOVER_WAIT_SECONDS = 10.0

EXIT_FREE = 0
EXIT_USAGE = 2
EXIT_OURS = 3
EXIT_FOREIGN = 4
EXIT_STALE = 5
TAKEOVER_FLAG = "--takeover"


def port_is_open(host: str, port: int) -> bool:
    """Laeuscht auf dem Port bereits ein Prozess?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        return sock.connect_ex((host, port)) == 0


def is_our_backend(host: str, port: int) -> dict | None:
    """Antwortet auf dem Port das Backend dieses Projekts?

    Rueckgabe ist die Antwort von ``/api/health`` oder ``None``, wenn dort
    etwas anderes laeuft.
    """
    url = f"http://{host}:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if isinstance(payload, dict) and payload.get("status") == "ok" and "version" in payload:
        return payload
    return None


def runs_stale_code(health: dict) -> bool:
    """Meldet der laufende Prozess, dass auf der Platte neuerer Code liegt?

    Aeltere Backends kennen das Feld nicht. Dann gilt der bisherige Fall
    (Exit-Code 3): Ein Fehlalarm waere schlimmer als eine fehlende Warnung.
    """
    runtime = health.get("runtime")
    return bool(isinstance(runtime, dict) and runtime.get("stale"))


def owning_pid(port: int) -> int | None:
    """Bestmoegliche Ermittlung des Prozesses, der den Port haelt.

    Nutzt ``netstat -ano`` (Windows) bzw. ``lsof``/``ss`` (POSIX). Schlaegt die
    Ermittlung fehl, wird ``None`` zurueckgegeben - der Aufrufer kommt dann ohne
    PID-Hinweis aus.
    """
    if sys.platform == "win32":
        command = ["netstat", "-ano"]
    else:
        command = ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout or ""
    if sys.platform != "win32":
        for line in output.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    for line in output.splitlines():
        parts = line.split()
        # Spalten: Proto, lokale Adresse, Gegenstelle, Zustand, PID
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        if parts[-1].isdigit():
            return int(parts[-1])
    return None


def stop_process(pid: int) -> bool:
    """Beendet einen Prozess. Rueckgabe: hat der Aufruf funktioniert?"""
    if sys.platform == "win32":
        command = ["taskkill", "/PID", str(pid), "/F"]
    else:
        command = ["kill", str(pid)]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def wait_for_free_port(host: str, port: int) -> bool:
    """Wartet, bis der Port wirklich frei ist (Prozesse brauchen kurz)."""
    deadline = time.monotonic() + TAKEOVER_WAIT_SECONDS
    while True:
        if not port_is_open(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def takeover(host: str, port: int) -> int:
    """Beendet das laufende Projekt-Backend, damit der neue Code starten kann.

    Wird nur aufgerufen, wenn auf dem Port nachweislich das Backend dieses
    Projekts antwortet - ein fremder Dienst wird nie beendet.
    """
    pid = owning_pid(port)
    if pid is None:
        print(f"FEHLER: Der Prozess auf Port {port} liess sich nicht ermitteln.")
        return EXIT_STALE
    print(f"HINWEIS: Das laufende Backend (PID {pid}) wird beendet ...")
    if not stop_process(pid) or not wait_for_free_port(host, port):
        print(f"FEHLER: Das Backend auf Port {port} laeuft weiter - bitte von Hand beenden.")
        return EXIT_STALE
    print(f"OK: Port {port} ist frei - starte mit dem aktuellen Code.")
    return EXIT_FREE


def main(argv: list[str]) -> int:
    takeover_requested = TAKEOVER_FLAG in argv
    args = [argument for argument in argv if argument != TAKEOVER_FLAG]
    unknown = [argument for argument in args if argument.startswith("-")]
    if unknown or len(args) > 2:
        print(f"Aufruf: python scripts/port_check.py [host] [port] [{TAKEOVER_FLAG}]", file=sys.stderr)
        return EXIT_USAGE
    host = args[0] if args else "127.0.0.1"
    try:
        port = int(args[1]) if len(args) > 1 else 8000
    except ValueError:
        print(f"Ungueltiger Port: {args[1]}", file=sys.stderr)
        return EXIT_USAGE
    if not 1 <= port <= 65535:
        print(f"Ungueltiger Port: {port}", file=sys.stderr)
        return EXIT_USAGE

    if not port_is_open(host, port):
        return EXIT_FREE

    health = is_our_backend(host, port)
    if health is not None:
        pid = owning_pid(port)
        runtime = health.get("runtime") if isinstance(health.get("runtime"), dict) else {}
        if runs_stale_code(health):
            print(
                f"WARNUNG: Auf http://{host}:{port}/ laeuft noch das Backend "
                "dieses Projekts, aber mit AELTEREM Code"
                + (f" (PID {pid})." if pid is not None else ".")
            )
            changed = runtime.get("newer_sources") or []
            if changed:
                print("         Seit dem Start geaendert: " + ", ".join(str(name) for name in changed))
            print(
                "         Bis zum Neustart gilt weiter der alte Stand: neue Filter "
                "wirken nicht,"
            )
            print("         entfernte Funktionen laufen trotzdem weiter.")
            if pid is not None and sys.platform == "win32":
                print(f"         Prozess beenden:  taskkill /PID {pid} /F")
            elif pid is not None:
                print(f"         Prozess beenden:  kill {pid}")
            print("         Oder gleich neu starten:  start.bat --restart  (bzw. ./start.sh --restart)")
            if takeover_requested:
                return takeover(host, port)
            return EXIT_STALE
        print(
            f"HINWEIS: Auf http://{host}:{port}/ laeuft bereits das Backend "
            f"dieses Projekts" + (f" (PID {pid})." if pid is not None else ".")
        )
        if takeover_requested:
            return takeover(host, port)
        return EXIT_OURS

    pid = owning_pid(port)
    print(
        f"FEHLER: Port {port} ist bereits belegt"
        + (f" von PID {pid}." if pid is not None else " von einem anderen Programm.")
    )
    if pid is not None and sys.platform == "win32":
        print(f"         Prozess beenden:  taskkill /PID {pid} /F")
    elif pid is not None:
        print(f"         Prozess beenden:  kill {pid}")
    else:
        print("         Belegung pruefen:  netstat -ano | findstr :%d" % port)
    print(f"         Anderer Port:      BACKEND_PORT={port + 10} setzen und erneut starten")
    return EXIT_FOREIGN


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
