"""Funktionstest aller API-Endpunkte gegen ein echtes Backend.

Startet eine isolierte Backend-Instanz (eigene Datenbank, eigener Port) und
ruft *alle* Endpunkte echt über HTTP auf - inklusive eines vollständigen
Suchlaufs über die Website-Connectoren. Während des Browser-Laufs wird
zusätzlich geprüft, dass kein Chrome-Fenster sichtbar ist.

Aufruf (aus dem Projektverzeichnis, mit der Projekt-Umgebung):

    .venv/Scripts/python.exe scripts/api_smoke_test.py

Rückgabewert 0 = alle Prüfungen bestanden, 1 = mindestens eine fehlgeschlagen.
Auf dem Testport darf parallel kein Backend laufen.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8137
BASE = f"http://127.0.0.1:{PORT}/api"
DB = ROOT / "data" / "api_smoke_test.db"
PYTHON = Path(sys.executable)

RESULTS: list[tuple[str, str, str]] = []


def check(group: str, name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((group, name, "OK" if ok else "FEHLER", note))
    mark = "OK   " if ok else "FEHLER"
    print(f"[{mark}] {group} :: {name} {note}".rstrip(), flush=True)


def call(method: str, path: str, body=None, timeout: float = 60.0):
    url = path if path.startswith("http") else BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        return exc.code, parsed
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def limit_for_title(title: str, entries: list[dict]) -> dict | None:
    """Kauf-Limit zu einem Anzeigentitel - wie im Backend: längster Treffer."""
    key = compact(title)
    hits = [e for e in entries if compact(e.get("term", "")) and compact(e["term"]) in key]
    if not hits:
        return None
    return max(hits, key=lambda entry: len(compact(entry["term"])))


def offers_over_limit(entries: list[dict], items: list[dict]) -> list[str]:
    """Angebote, die über dem Kauf-Limit ihrer Komponente liegen."""
    problems: list[str] = []
    for item in items:
        limit = limit_for_title(item.get("title", ""), entries)
        price = item.get("total_price")
        if limit is None or price is None:
            continue
        if price > limit["max_price"] + 0.01:
            problems.append(
                f"{str(item.get('title'))[:60]!r}: {price:.2f} EUR "
                f"> Limit {limit['max_price']:.0f} EUR ({limit['name']})"
            )
    return problems


def expect(group: str, name: str, path: str, status: int, method: str = "GET", body=None,
           check_fn=None, timeout: float = 60.0):
    code, payload = call(method, path, body, timeout=timeout)
    ok = code == status
    note = f"HTTP {code}"
    if ok and check_fn is not None:
        try:
            ok = bool(check_fn(payload))
            if not ok:
                note += f" (Inhalt unerwartet: {str(payload)[:120]})"
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"Pruefung warf {type(exc).__name__}: {exc}"
    elif not ok:
        note += f" erwartet {status} - {str(payload)[:160]}"
    check(group, name, ok, note)
    return payload


# --- Fenster-Pruefung (Windows) -------------------------------------------
user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
TH32CS_SNAPPROCESS = 0x00000002
kernel32 = ctypes.windll.kernel32


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


MONITORS = [(0, 0, 2048, 1104)]


def on_screen(rect) -> bool:
    left, top, right, bottom = rect
    return any(
        left < sr and right > sl and top < sb and bottom > st
        for (sl, st, sr, sb) in MONITORS
    )


def chrome_pids() -> dict[int, str]:
    result: dict[int, str] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1 or not snapshot:
        return result
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
    while ok:
        result[int(entry.th32ProcessID)] = entry.szExeFile.decode("utf-8", "replace")
        ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)
    return result


def chrome_windows() -> list[tuple[int, int, str, tuple, bool]]:
    """(hwnd, pid, title, rect, sichtbar) aller Top-Level-Chrome-Fenster."""
    found: list = []
    if not sys.platform.startswith("win"):
        return found

    def cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "Chrome_WidgetWin" not in cls.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append(
            (
                int(hwnd),
                int(pid.value),
                buf.value,
                (rect.left, rect.top, rect.right, rect.bottom),
                bool(user32.IsWindowVisible(hwnd)),
            )
        )
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def main() -> int:
    DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": f"sqlite:///./data/{DB.name}",
            "BACKEND_PORT": str(PORT),
            "BACKEND_HOST": "127.0.0.1",
            "LOCAL_IMPORT_PATH": "samples/beispiel_angebote.json",
            "EBAY_WEB_ENABLED": "true",
            "KLEINANZEIGEN_WEB_ENABLED": "true",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
        }
    )
    log = open(ROOT / "data" / "api_smoke_test_backend.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        ready = False
        for _ in range(60):
            code, payload = call("GET", "/health", timeout=3)
            if code == 200:
                ready = True
                break
            time.sleep(0.5)
        check("Start", "Backend erreichbar (/api/health)", ready)
        if not ready:
            return 1

        # --- 1. System -----------------------------------------------------
        expect("System", "GET /api", f"http://127.0.0.1:{PORT}/api", 200,
               check_fn=lambda p: "endpoints" in p)
        expect("System", "GET /api/health", "/health", 200, check_fn=lambda p: p.get("status") == "ok")
        expect("System", "GET /api/system/status", "/system/status", 200,
               check_fn=lambda p: {"database", "listings", "knowledge", "connectors"} <= set(p))
        expect("System", "GET /api/system/categories", "/system/categories", 200,
               check_fn=lambda p: isinstance(p, list) and len(p) > 5 and "key" in p[0])
        conns = expect("System", "GET /api/connectors", "/connectors", 200,
                       check_fn=lambda p: isinstance(p, list) and len(p) == 5)
        expect("System", "GET /api/stats/overview", "/stats/overview", 200,
               check_fn=lambda p: isinstance(p, dict) and p)

        # --- 1b. Kauf-Limit-Liste -----------------------------------------
        limits = expect("Preisliste", "GET /api/price-limits", "/price-limits", 200,
                        check_fn=lambda p: p.get("available") and p.get("groups") and p.get("entries"))
        limit_entries = (limits or {}).get("entries", [])
        if limits:
            print("        " + ", ".join(
                f"{group['label']}: {group['count']} Komponenten "
                f"({group['min_price']:.0f}-{group['max_price']:.0f} EUR)"
                for group in limits["groups"]
            ))
            check("Preisliste", "Jede Komponente hat Suchbegriff und Limit",
                  all(entry.get("term") and (entry.get("max_price") or 0) > 0
                      for entry in limit_entries),
                  f"{len(limit_entries)} Eintraege")
            check("Preisliste", "Suchbegriffe ohne Herstellerpraefix",
                  all(not entry["term"].lower().startswith(("nvidia ", "amd ", "intel "))
                      for entry in limit_entries))

        # --- 2. Connector-Tests -------------------------------------------
        for platform in ("local_import", "ebay", "ebay_web", "kleinanzeigen", "kleinanzeigen_web", "gibtsnicht"):
            code, payload = call("POST", f"/connectors/{platform}/test", timeout=180)
            expected = 404 if platform == "gibtsnicht" else 200
            check("Connector-Test", f"POST /connectors/{platform}/test",
                  code == expected, f"HTTP {code} {str(payload)[:100]}")

        # --- 3. Einstellungen ---------------------------------------------
        settings = expect("Einstellungen", "GET /api/settings", "/settings", 200,
                          check_fn=lambda p: "runtime" in p and "environment" in p)
        runtime = dict(settings["runtime"])
        nonce = int(time.time()) % 1000
        runtime["default_days_back"] = 100 + nonce
        put = expect("Einstellungen", "PUT /api/settings", "/settings", 200, method="PUT", body=runtime,
                     check_fn=lambda p: p["runtime"]["default_days_back"] == 100 + nonce)
        expect("Einstellungen", "PUT /api/settings wirkt (GET erneut)", "/settings", 200,
               check_fn=lambda p: p["runtime"]["default_days_back"] == 100 + nonce)
        weights = dict(settings["runtime"]["weights"])
        weights["price_advantage"] = 35.0
        expect("Einstellungen", "PUT /api/settings/weights", "/settings/weights", 200,
               method="PUT", body=weights, check_fn=lambda p: p["price_advantage"] == 35.0)
        expect("Einstellungen", "PUT /api/settings (ungueltig -> 422)", "/settings", 422,
               method="PUT", body={"search_max_variants_per_category": "keine-zahl"})
        expect("Einstellungen", "PUT /api/settings/weights (negativ -> 422)", "/settings/weights",
               422, method="PUT", body={**weights, "price_advantage": -5})
        expect("Einstellungen", "POST /api/settings/reset", "/settings/reset", 200, method="POST",
               check_fn=lambda p: p["runtime"]["default_days_back"] == 150)

        # --- 4. Wissensbasis ----------------------------------------------
        expect("Wissen", "GET /api/knowledge", "/knowledge", 200,
               check_fn=lambda p: p.get("entries", 0) > 0)
        entries = expect("Wissen", "GET /api/knowledge/entries", "/knowledge/entries", 200,
                         check_fn=lambda p: isinstance(p, list) and len(p) > 0)
        check("Wissen", "Wissenseintrag hat Pflichtfelder",
              all({"category", "model", "aliases"} <= set(e) for e in entries[:20]))
        expect("Wissen", "POST /api/knowledge/reload", "/knowledge/reload", 200, method="POST",
               check_fn=lambda p: p.get("entries", 0) > 0)

        # --- 6. Benchmark & Wartung ---------------------------------------
        bench = expect("Wartung", "POST /api/benchmark/deals", "/benchmark/deals", 200, method="POST",
                       check_fn=lambda p: p.get("passed", 0) > 0 and p.get("failed") == 0)
        if bench:
            print(f"        Benchmark: {bench['passed']} bestanden, {bench['failed']} fehlgeschlagen")
        expect("Wartung", "POST /api/maintenance/cleanup", "/maintenance/cleanup", 200, method="POST")

        # --- 7. Fehlerpfade -----------------------------------------------
        expect("Fehlerpfade", "GET unbekannter Lauf -> 404", "/search/runs/999999", 404)
        expect("Fehlerpfade", "GET unbekanntes Angebot -> 404", "/listings/999999", 404)
        expect("Fehlerpfade", "GET ungueltiger Sort -> 422", "/listings?sort=quatsch", 422)
        expect("Fehlerpfade", "GET Limit 0 -> 422", "/listings?limit=0", 422)
        expect("Fehlerpfade", "POST Lauf ohne Plattform -> 422", "/search/runs", 422, method="POST", body={"product": "x"})
        expect("Fehlerpfade", "POST Lauf undefinierter Platform -> 422", "/search/runs", 422,
               method="POST", body={"product": "RTX 3080", "platforms": ["gibtsnicht"]})
        expect("Fehlerpfade", "POST Lauf ohne Produkt/Kategorie -> 422", "/search/runs", 422,
               method="POST", body={"platforms": ["local_import"]})
        expect("Fehlerpfade", "POST Pause fuer beendeten Lauf -> 409", "/search/runs/999999/pause",
               409, method="POST")
        expect("Fehlerpfade", "POST Cancel fuer beendeten Lauf -> 409", "/search/runs/999999/cancel",
               409, method="POST")
        expect("Fehlerpfade", "POST Connector-Test unbekannt -> 404", "/connectors/xyz/test",
               404, method="POST")
        expect("Fehlerpfade", "POST Abholung ohne PLZ -> 422", "/search/runs", 422, method="POST",
               body={"product": "RTX 3080", "platforms": ["local_import"], "delivery": "pickup"})
        expect("Fehlerpfade", "POST unbekannte Kategorie -> 422", "/search/runs", 422, method="POST",
               body={"categories": ["gibtsnicht"], "platforms": ["local_import"]})
        expect("Fehlerpfade", "POST unerlaubtes Feld -> 422", "/search/runs", 422, method="POST",
               body={"product": "RTX 3080", "platforms": ["local_import"], "boese": 1})
        expect("Fehlerpfade", "POST max_rounds=9 -> 422", "/search/runs", 422, method="POST",
               body={"product": "RTX 3080", "platforms": ["local_import"], "max_rounds": 9})
        expect("Fehlerpfade", "POST Plattform leer -> 422", "/search/runs", 422, method="POST",
               body={"product": "RTX 3080", "platforms": []})

        # --- 8. Suchlauf (lokaler Import, deterministisch) -----------------
        code, created = call("POST", "/search/runs",
                             {"product": "RTX 3080", "platforms": ["local_import"], "days_back": 3650})
        check("Suchlauf", "POST /search/runs (local_import)", code == 201 and created.get("run_id"),
              f"HTTP {code} {created}")
        run_id = created.get("run_id") if isinstance(created, dict) else None

        # Doppelstart -> 409
        if run_id:
            code2, payload2 = call("POST", "/search/runs",
                                   {"product": "RTX 3060", "platforms": ["local_import"]})
            check("Suchlauf", "Zweiter Lauf parallel -> 409 oder 201",
                  code2 in (409, 201), f"HTTP {code2} {str(payload2)[:80]}")

        if run_id:
            status = None
            for _ in range(240):
                code, run = call("GET", f"/search/runs/{run_id}")
                status = run.get("status")
                if status in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.5)
            check("Suchlauf", "Lauf wird 'completed'", status == "completed", f"Status {status}")
            print(f"        Lauf {run_id}: {run.get('result_count')} Ergebnisse, "
                  f"{run.get('new_count')} neu, {run.get('queries_done')}/{run.get('queries_total')} Anfragen")
            check("Suchlauf", "Lauf liefert Ergebnisse", (run.get("result_count") or 0) > 0)
            check("Suchlauf", "Plan vorhanden", bool(run.get("plan")))
            check("Suchlauf", "Abdeckungsbericht vorhanden", bool(run.get("coverage")))

            expect("Suchlauf", "GET /api/search/runs", "/search/runs", 200,
                   check_fn=lambda p: isinstance(p, list) and len(p) >= 1)
            expect("Suchlauf", "GET /api/search/runs?limit=1", "/search/runs?limit=1", 200,
                   check_fn=lambda p: len(p) == 1)
            qs = expect("Suchlauf", f"GET /search/runs/{run_id}/queries", f"/search/runs/{run_id}/queries", 200,
                        check_fn=lambda p: isinstance(p, list) and len(p) > 0)
            print(f"        {len(qs)} Anfragen im Plan")
            check("Suchlauf", "Anfragen mit Plattform+Status",
                  all(q.get("platform") and q.get("status") for q in qs[:20]))
            expect("Suchlauf", "GET /queries?platform=local_import",
                   f"/search/runs/{run_id}/queries?platform=local_import", 200,
                   check_fn=lambda p: all(q["platform"] == "local_import" for q in p))
            expect("Suchlauf", "GET /queries?status=ok", f"/search/runs/{run_id}/queries?status=ok", 200)
            cov = expect("Suchlauf", "GET /coverage", f"/search/runs/{run_id}/coverage", 200,
                         check_fn=lambda p: isinstance(p, dict) and p)
            expect("Suchlauf", "GET /summary", f"/search/runs/{run_id}/summary", 200,
                   check_fn=lambda p: "queries" in p and "families" in p)
            if cov:
                print(f"        Abdeckung: {str(cov)[:200]}")

        # --- 8b. Suchlauf nach der Preisliste (deterministisch) -----------
        code, created = call("POST", "/search/runs",
                             {"price_groups": ["gpu"], "platforms": ["local_import"],
                              "days_back": 3650})
        price_run_id = created.get("run_id") if isinstance(created, dict) else None
        check("Preisliste", "POST /search/runs (Preislisten-Modus)",
              code == 201 and price_run_id, f"HTTP {code} {str(created)[:120]}")
        if price_run_id:
            status = None
            for _ in range(240):
                _code, price_run = call("GET", f"/search/runs/{price_run_id}")
                status = price_run.get("status")
                if status in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.5)
            plan_queries = [
                query
                for category in (price_run.get("plan") or {}).get("categories", [])
                for query in category.get("queries", [])
            ]
            print(f"        Preislisten-Lauf {price_run_id}: Status {status}, "
                  f"{price_run.get('result_count')} Ergebnisse, "
                  f"{len(plan_queries)} Anfragen im Plan")
            check("Preisliste", "Preislisten-Lauf wird 'completed'", status == "completed",
                  f"Status {status}")
            check("Preisliste", "Jede geplante Anfrage traegt ihr eigenes Limit",
                  bool(plan_queries) and all(query.get("max_price") for query in plan_queries))
            check("Preisliste", "Jede Anfrage traegt ihren Mindestpreis (Viertel des Limits)",
                  bool(plan_queries) and all(
                      query.get("min_price") == round((query.get("max_price") or 0) * 0.25, 2)
                      for query in plan_queries
                  ),
                  ", ".join(
                      f"{query['term']}: {query.get('min_price')}" for query in plan_queries[:3]
                  ))
            check("Preisliste", "Preislisten-Lauf liefert Ergebnisse",
                  (price_run.get("result_count") or 0) > 0)
            _code, stored = call("GET", "/listings?limit=200")
            items = (stored or {}).get("items", [])
            problems = offers_over_limit(limit_entries, items)
            check("Preisliste", "Kein gespeichertes Angebot ueber seinem Kauf-Limit",
                  not problems, f"auffaellig: {problems[:3]}")

        # --- 9. Angebote ---------------------------------------------------
        listing = expect("Angebote", "GET /listings", "/listings", 200,
                         check_fn=lambda p: p.get("total", 0) > 0 and p["items"])
        items = listing["items"] if listing else []
        check("Angebote", "Jedes Angebot hat Kernfelder",
              all({"id", "platform", "title", "url", "deal_score"} <= set(i) for i in items[:20]))
        for sort in ("best_deal", "price_advantage", "lowest_price", "price_performance", "newest", "confidence"):
            expect("Angebote", f"GET /listings?sort={sort}", f"/listings?sort={sort}&limit=5", 200)
        for query in ("?platform=local_import", "?min_score=0", "?fixed_price_only=false",
                      "?favorites=false", "?limit=3&offset=0", "?search=rtx",
                      "?min_price=1&max_price=100000", "?min_condition_rank=0"):
            expect("Angebote", f"GET /listings{query}", f"/listings{query}", 200)
        expect("Angebote", "GET /listings/filters", "/listings/filters", 200,
               check_fn=lambda p: isinstance(p, dict) and p)
        expect("Angebote", "GET /listings/{id} (ungueltige id) -> 422", "/listings/abc", 422)

        if items:
            lid = items[0]["id"]
            expect("Angebote", "GET /listings/{id}", f"/listings/{lid}", 200,
                   check_fn=lambda p: p.get("id") == lid and "score_breakdown" in p)
            fav = expect("Angebote", "POST /listings/{id}/favorite", f"/listings/{lid}/favorite",
                         200, method="POST", check_fn=lambda p: p.get("id") == lid)
            code, _ = call("POST", f"/listings/{lid}/favorite")
            check("Angebote", "Favorit umschaltbar", code == 200)
            expect("Angebote", "GET /listings?favorites=true", "/listings?favorites=true", 200)
            expect("Angebote", "POST /listings/{id}/recheck", f"/listings/{lid}/recheck", 200, method="POST")
            expect("Angebote", "POST /listings/{id}/explain", f"/listings/{lid}/explain", 200,
                   method="POST", timeout=120,
                   check_fn=lambda p: "available" in p and "source" in p)
            expect("Angebote", "GET /listings/{id}/market", f"/listings/{lid}/market", 200)
            expect("Angebote", "POST /listings/999999/favorite -> 404",
                   "/listings/999999/favorite", 404, method="POST")
            expect("Angebote", "POST /listings/999999/explain -> 404",
                   "/listings/999999/explain", 404, method="POST")
            expect("Angebote", "GET /listings/999999/market -> 404",
                   "/listings/999999/market", 404)

        # --- 10. Frontend wird ausgeliefert --------------------------------
        def raw_get(path: str) -> tuple[int, str]:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}{path}", timeout=20
                ) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, ""
            except Exception as exc:  # noqa: BLE001
                return -1, str(exc)

        code, index_html = raw_get("/")
        check("Frontend", "GET / liefert die Oberflaeche",
              code == 200 and "<div id=\"root\">" in index_html,
              f"HTTP {code}, {len(index_html)} Zeichen")
        code, body = raw_get("/irgend/eine/route")
        check("Frontend", "Tiefe Route liefert index.html",
              code == 200 and "<div id=\"root\">" in body, f"HTTP {code}")
        # Der Dateiname des Bundles traegt einen Inhalts-Hash und aendert sich
        # mit jedem Build - er wird deshalb aus dem gelieferten index.html gelesen.
        asset = re.search(r'(?:src|href)="(/assets/[^"]+)"', index_html or "")
        if asset:
            code, bundle = raw_get(asset.group(1))
            check("Frontend", f"Asset wird ausgeliefert ({asset.group(1).rsplit('/', 1)[-1]})",
                  code == 200 and len(bundle) > 1000, f"HTTP {code}, {len(bundle)} Zeichen")
        else:
            check("Frontend", "Asset wird ausgeliefert", False,
                  "kein Bundle im index.html gefunden")
        code, body = raw_get("/api/system/status")
        check("Frontend", "/api bleibt der API vorbehalten",
              code == 200 and "\"listings\"" in body, f"HTTP {code}")

        # --- 11. Echter Browser-Suchlauf + Sichtbarkeitspruefung -----------
        print("\n--- Echter Browser-Suchlauf (eBay-Website + Kleinanzeigen) ---", flush=True)
        before_pids = {pid for pid, name in chrome_pids().items() if name.lower() == "chrome.exe"}
        before_windows = {hwnd for hwnd, *_ in chrome_windows()}
        owned_pids: set[int] = set()
        observed: dict[int, dict] = {}

        def watcher():
            while not stop.is_set():
                owned_pids.update(
                    {
                        pid
                        for pid, name in chrome_pids().items()
                        if name.lower() == "chrome.exe"
                    }
                    - before_pids
                )
                for hwnd, pid, title, rect, visible in chrome_windows():
                    if hwnd in before_windows:
                        continue
                    entry = observed.setdefault(
                        hwnd, {"pid": pid, "title": title, "on_screen_visible": 0}
                    )
                    if visible and on_screen(rect):
                        entry["on_screen_visible"] += 1
                stop.wait(0.25)

        stop = threading.Event()
        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        code, created = call("POST", "/search/runs",
                             {"product": "RTX 3080", "platforms": ["ebay_web", "kleinanzeigen_web"],
                              "days_back": 30})
        check("Live-Suche", "POST /search/runs (Browser)", code == 201, f"HTTP {code} {str(created)[:120]}")
        live_id = created.get("run_id") if isinstance(created, dict) else None

        if live_id:
            # Pause/Fortsetzen/Abbruch live testen
            time.sleep(6)
            code_p, payload_p = call("POST", f"/search/runs/{live_id}/pause")
            check("Live-Suche", "POST /pause", code_p in (200, 409), f"HTTP {code_p} {str(payload_p)[:80]}")
            if code_p == 200:
                time.sleep(3)
                code_r, payload_r = call("POST", f"/search/runs/{live_id}/resume")
                check("Live-Suche", "POST /resume", code_r == 200, f"HTTP {code_r} {str(payload_r)[:80]}")
            status = None
            for _ in range(900):
                code, live = call("GET", f"/search/runs/{live_id}")
                status = live.get("status")
                if status in ("completed", "failed", "cancelled"):
                    break
                time.sleep(2)
            stop.set()
            thread.join(timeout=5)
            offenders = [
                f"{info['title']!r} (pid {info['pid']})"
                for info in observed.values()
                if info["pid"] in owned_pids and info["on_screen_visible"]
            ]
            print(f"        Connector-Chrome-Prozesse: {len(owned_pids)}, Fenster beobachtet: {len(observed)}")
            for hwnd, info in observed.items():
                if info["pid"] not in owned_pids:
                    continue
                print(f"        eigenes Fenster {hwnd}: {info['title']!r} "
                      f"sichtbar-auf-bildschirm={info['on_screen_visible']}")
            check("Live-Suche", "Kein eigenes Chrome-Fenster sichtbar auf dem Bildschirm",
                  not offenders, f"auffaellig: {offenders}")
            check("Live-Suche", "Lauf endet sauber", status in ("completed", "cancelled"), f"Status {status}")
            print(f"        Live-Lauf {live_id}: Status {status}, {live.get('result_count')} Treffer, "
                  f"{live.get('queries_done')}/{live.get('queries_total')} Anfragen")
            if status == "completed":
                # Die Trefferzahl haengt am echten Markt (gesucht wird nur unter dem
                # Kauf-Limit). Belastbar pruefbar ist dagegen: alle Anfragen liefen,
                # und in der Liste steht nichts ueber dem Limit.
                check("Live-Suche", "Live-Lauf: jede Anfrage wurde ausgefuehrt",
                      (live.get("queries_done") or 0) == (live.get("queries_total") or 0)
                      and (live.get("queries_total") or 0) > 0,
                      f"{live.get('queries_done')}/{live.get('queries_total')}")
                print(f"        Live-Lauf lieferte {live.get('result_count')} Angebote unter den Kauf-Limits")
                live_queries = [
                    query
                    for category in (live.get("plan") or {}).get("categories", [])
                    for query in category.get("queries", [])
                ]
                check("Live-Suche", "Ohne Hoechstpreis wird kein Mindestpreis erfunden",
                      bool(live_queries) and all(
                          query.get("min_price") is None for query in live_queries
                      ))
                expect("Live-Suche", "GET /listings (Live-Daten)", "/listings?limit=5", 200,
                       check_fn=lambda p: p.get("total", 0) > 0)
                _code, stored = call("GET", "/listings?limit=200")
                problems = offers_over_limit(limit_entries, (stored or {}).get("items", []))
                check("Live-Suche", "Kein Angebot ueber dem Kauf-Limit in der Liste",
                      not problems, f"auffaellig: {problems[:3]}")
        else:
            stop.set()
            thread.join(timeout=5)

        # Cancel-Pfad zum Durchtesten der Abbruchlogik (Lauf mit vielen Anfragen)
        code, created = call("POST", "/search/runs",
                             {"product": "RTX 4090", "platforms": ["ebay_web"], "days_back": 3650})
        if code == 201:
            cancel_id = created["run_id"]
            time.sleep(3)
            code_c, payload_c = call("POST", f"/search/runs/{cancel_id}/cancel")
            check("Live-Suche", "POST /cancel", code_c in (200, 409), f"HTTP {code_c} {str(payload_c)[:80]}")
            final = None
            for _ in range(120):
                _c, data = call("GET", f"/search/runs/{cancel_id}")
                final = data.get("status")
                if final in ("completed", "failed", "cancelled"):
                    break
                time.sleep(1)
            check("Live-Suche", "Abgebrochener Lauf endet", final in ("cancelled", "completed"), f"Status {final}")
        else:
            check("Live-Suche", "POST /search/runs fuer Abbruchtest", False, f"HTTP {code}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    print("\n================= ZUSAMMENFASSUNG =================")
    failed = [r for r in RESULTS if r[2] != "OK"]
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for group, name, status, note in RESULTS:
        groups.setdefault(group, []).append((name, status, note))
    for group, rows in groups.items():
        ok = sum(1 for _n, s, _note in rows if s == "OK")
        print(f"{group:16s} {ok:3d}/{len(rows):<3d} bestanden")
    print(f"\nGesamt: {len(RESULTS) - len(failed)}/{len(RESULTS)} Pruefungen bestanden")
    if failed:
        print("\nFEHLGESCHLAGEN:")
        for group, name, _s, note in failed:
            print(f"  - {group} :: {name} -> {note}")
    return 1 if failed else 0


raise SystemExit(main())
