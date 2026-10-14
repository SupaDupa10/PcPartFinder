"""Die Portpruefung der Startskripte.

Die Startskripte (``start.sh``/``start.bat``) entscheiden anhand des Exit-Codes
von ``scripts/port_check.py``, ob gestartet, auf ein laufendes Backend
verwiesen oder mit einer klaren Meldung abgebrochen wird. Genau dieser
Exit-Code-Vertrag wird hier geprueft.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "port_check.py"

EXIT_FREE = 0
EXIT_USAGE = 2
EXIT_OURS = 3
EXIT_FOREIGN = 4
EXIT_STALE = 5


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_check(port: int, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "127.0.0.1", str(port), *extra],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(REPO_ROOT),
    )


class _Handler(BaseHTTPRequestHandler):
    """Antwortet wie das Projekt-Backend oder wie ein fremder Dienst."""

    health_payload: dict | None = None

    def log_message(self, *_args) -> None:  # noqa: D102 - Logs unterdruecken
        return

    def do_GET(self) -> None:  # noqa: N802 - HTTP-Verb
        if self.health_payload is not None and self.path.rstrip("/") == "/api/health":
            body = json.dumps(self.health_payload).encode("utf-8")
            self.send_response(200)
        else:
            body = b"<html>fremder Dienst</html>"
            self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(health_payload: dict | None) -> tuple[ThreadingHTTPServer, int]:
    handler = type("Handler", (_Handler,), {"health_payload": health_payload})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


FRESH_HEALTH = {"status": "ok", "version": "0.1.0"}
STALE_HEALTH = {
    "status": "ok",
    "version": "0.1.0",
    "runtime": {"started_at": "2026-10-12T16:12:02Z", "stale": True, "newer_sources": ["search/titles.py"]},
}

# Ein echtes Kindprozess-Backend: Nur so lässt sich prüfen, dass --takeover
# wirklich den Prozess beendet, der den Port hält (und nicht nur redet).
CHILD_SERVER = """
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD = json.dumps({
    'status': 'ok',
    'version': '0.1.0',
    'runtime': {'started_at': '2026-10-12T16:12:02Z', 'stale': True, 'newer_sources': ['search/titles.py']},
}).encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        if self.path.rstrip('/') == '/api/health':
            body = PAYLOAD
            self.send_response(200)
        else:
            body = b'<html>kind</html>'
            self.send_response(404)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def port_still_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class TestPortCheck:
    def test_freier_port_meldet_frei(self):
        result = run_check(free_port())
        assert result.returncode == EXIT_FREE, result.stdout + result.stderr

    def test_eigenes_backend_wird_erkannt(self):
        server, port = start_server(FRESH_HEALTH)
        try:
            result = run_check(port)
        finally:
            server.shutdown()
            server.server_close()
        assert result.returncode == EXIT_OURS, result.stdout + result.stderr
        assert "laeuft bereits" in result.stdout
        assert "PID" in result.stdout
        assert "AELTEREM" not in result.stdout

    def test_alter_code_wird_als_neustart_gemeldet(self):
        """Ein laufendes Backend mit älterem Code darf nicht als "ok" gelten."""
        server, port = start_server(STALE_HEALTH)
        try:
            result = run_check(port)
        finally:
            server.shutdown()
            server.server_close()
        assert result.returncode == EXIT_STALE, result.stdout + result.stderr
        assert "AELTEREM Code" in result.stdout
        assert "search/titles.py" in result.stdout
        assert "start.bat --restart" in result.stdout

    def test_health_ohne_runtime_feld_gilt_weiter_als_laufend(self):
        """Ältere Backends melden nichts - dann bleibt es beim alten Verhalten.

        Ohne ``--takeover``: Mit dem Schalter wuerde der Testprozess selbst
        beendet (der Testserver laeuft im selben Prozess).
        """
        server, port = start_server({"status": "ok", "version": "0.1.0"})
        try:
            assert run_check(port).returncode == EXIT_OURS
        finally:
            server.shutdown()
            server.server_close()

    @pytest.mark.skipif(sys.platform != "win32" and not shutil.which("kill"), reason="kein kill")
    def test_takeover_beendet_das_eigene_backend(self):
        port = free_port()
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_SERVER, str(port)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert wait_for_port(port), "Kindprozess hat den Port nicht geoeffnet"
            result = run_check(port, "--takeover")
            assert result.returncode == EXIT_FREE, result.stdout + result.stderr
            assert "wird beendet" in result.stdout
            assert not port_still_open(port), "Port ist nach --takeover noch belegt"
            assert child.wait(timeout=15) is not None
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_takeover_beendet_keinen_fremden_dienst(self):
        server, port = start_server(None)
        try:
            result = run_check(port, "--takeover")
            assert result.returncode == EXIT_FOREIGN, result.stdout + result.stderr
            assert port_still_open(port), "fremder Dienst wurde beendet"
        finally:
            server.shutdown()
            server.server_close()

    def test_fremder_dienst_wird_als_belegung_gemeldet(self):
        server, port = start_server(None)
        try:
            result = run_check(port)
        finally:
            server.shutdown()
            server.server_close()
        assert result.returncode == EXIT_FOREIGN, result.stdout + result.stderr
        assert "bereits belegt" in result.stdout
        assert f"BACKEND_PORT={port + 10}" in result.stdout

    def test_ungueltiger_port_ist_ein_aufruffehler(self):
        result = run_check(0)
        assert result.returncode == EXIT_USAGE
        result = run_check(70000)
        assert result.returncode == EXIT_USAGE

    def test_startskripte_verwenden_die_pruefung(self):
        """Beide Startskripte muessen die Pruefung vor uvicorn aufrufen."""
        shell = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
        batch = (REPO_ROOT / "start.bat").read_text(encoding="utf-8")
        assert "scripts/port_check.py" in shell
        assert "scripts\\port_check.py" in batch
        # Die Batch-Variante muss die Faelle in absteigender Reihenfolge pruefen,
        # sonst faengt "errorlevel 1" auch 3, 4 und 5 ab.
        assert batch.index("if errorlevel 5") < batch.index("if errorlevel 4")
        assert batch.index("if errorlevel 4") < batch.index("if errorlevel 3")

    def test_startskripte_kennen_den_neustart(self):
        """--restart muss den veralteten Prozess ersetzen koennen."""
        shell = (REPO_ROOT / "start.sh").read_text(encoding="utf-8")
        batch = (REPO_ROOT / "start.bat").read_text(encoding="utf-8")
        assert "--takeover" in shell
        assert "--takeover" in batch
        assert "--restart" in shell
        assert "--restart" in batch
        # Ein veralteter Lauf darf nicht als "laeuft schon" durchgehen.
        assert "exit /b 0" not in batch.split("if errorlevel 5")[1].split("if errorlevel 4")[0]
