"""Gemeinsame Basis für Connectoren, die eine öffentliche Website auslesen.

Warum ein echter Chrome (und warum er trotzdem unsichtbar bleibt)
----------------------------------------------------------------
Marktplätze wie eBay und Kleinanzeigen blocken einfache HTTP-Abrufe und auch
Headless-Browser häufig mit ``HTTP 403`` (Bot-Schutz). Nur ein realer Browser
(kein Headless) erhält die Ergebnisseite. Damit die Anwendung dieselben Seiten
lesen kann, die ein Benutzer im Browser öffnet, steuern diese Connectoren den
lokal installierten Google Chrome über Playwright.

Ein echtes Browserfenster muss dafür nicht im Weg stehen: Im Standardmodus
("hidden") startet Chrome mit einem Fenster außerhalb des sichtbaren Bereichs
und wird direkt danach minimiert - es bleibt technisch ein normaler Browser,
aber es blitzt kein Fenster über der Arbeit auf. Wer den Browser beim Suchen
sehen will (Fehlersuche), setzt ``<PRÄFIX>_HIDDEN=false``. Reines Headless
(``<PRÄFIX>_HEADLESS=true``) bleibt möglich, wo die Seite es erlaubt.

Was hier gemeinsam ist
----------------------
* Lebenszyklus des Browsers (persistentes Profil, Aufwärmen, Aufräumen)
* EIN dauerhafter Tab für alle Anfragen (kein Fenster- und Tab-Wechsel mehr)
* Abruf einer Seite: Direktabruf per fetch, mit Navigation als Rückfallweg,
  inklusive eines Wiederholungsversuchs nach einer Blockade
* Mindestabstand zwischen zwei Abrufen (schont den Bot-Schutz)

Was die einzelnen Connectoren beitragen
---------------------------------------
URL-Bau, Warteselektor, Auslese-JavaScript und die Umwandlung in
:class:`~backend.connectors.base.RawListing`. Alles, was der Connector nicht
sicher lesen kann, wird als Einschränkung gemeldet - es werden keine Daten
geraten oder erfunden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from .base import (
    ConnectorError,
    ConnectorUnavailableError,
    ListingConnector,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Weit außerhalb jedes Bildschirms (auch bei mehreren Monitoren) und dazu
# minimiert: Das Fenster existiert für den Bot-Schutz als echtes Fenster,
# ist für den Benutzer aber nicht sichtbar.
HIDDEN_WINDOW_POSITION = "-32000,-32000"
WINDOW_SIZE = "1366,900"

# Windows: SW_HIDE blendet ein Fenster vollständig aus - auch aus der
# Taskleiste. Minimieren allein lässt dort einen Eintrag stehen.
SW_HIDE = 0
WINDOW_STATE_MINIMIZED = "minimized"
WINDOW_STATE_HIDDEN = "hidden"

# Direktabruf: Die Trefferliste wird per ``fetch`` im _schon geladenen_ Tab
# geholt und mit ``DOMParser`` ausgewertet, statt für jede Anfrage eine Seite
# zu navigieren. Kein Rendern, kein Tab- und Fensterwechsel. Live gemessen an
# derselben Suche (inkl. Mindestabstand von 1,5 s je Anfrage): eBay 3,7 s statt
# 7,6 s, Kleinanzeigen 1,8 s statt 2,6 s. Die abgerufene HTML-Antwort ist
# dieselbe wie beim Seitenaufbau; die Treffermenge war identisch (nur einzelne
# beworbene Anzeigen können anders sortiert sein). Der Auslese-Code der
# Unterklassen bleibt unverändert - ihm wird das geparste Dokument als
# ``document`` untergeschoben.
_INLINE_FETCH_JS = """
async (url) => {
  const response = await fetch(url, {
    credentials: 'include',
    headers: { 'Accept': 'text/html,application/xhtml+xml' },
  });
  const html = await response.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');
  let data = null;
  let error = null;
  try {
    // Der Auslese-Code der Unterklasse ist eine Funktion ohne Argumente
    // (Playwright ruft sie beim Seitenaufbau ebenfalls auf).
    // Klammern sind Pflicht: Der Auslese-Code beginnt mit einem Zeilenumbruch,
    // ein blosses "return" davor wuerde von JavaScript sofort beendet.
    const extract = new Function('document', 'return (' + EXTRACT_SOURCE + ')');
    data = extract(doc);
    if (typeof data === 'function') data = data();
  } catch (err) {
    error = String(err);
  }
  if (data && typeof data.then === 'function') data = await data;
  return {
    status: response.status,
    url: response.url,
    size: html.length,
    title: (doc.title || '').trim(),
    data: data,
    error: error,
  };
}
"""


def inline_fetch_script(extract_js: str) -> str:
    """Baut das JavaScript des Direktabrufs um den Auslese-Code herum."""
    return _INLINE_FETCH_JS.replace("EXTRACT_SOURCE", json.dumps(extract_js))


def _page_url(page: Any) -> str:
    """Aktuelle URL einer Playwright-Seite (leer, wenn nicht ermittelbar)."""
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:  # pragma: no cover - Seite bereits geschlossen
        return ""


def _screen_rects() -> list[tuple[int, int, int, int]]:
    """Bildschirmbereiche (nur Windows); leer, wenn nicht ermittelbar."""
    if os.name != "nt":  # pragma: no cover - nur Windows
        return []
    try:  # pragma: no cover - Plattformabhängig
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    user32 = ctypes.windll.user32
    screens: list[tuple[int, int, int, int]] = []
    try:
        procedure = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT),
            ctypes.c_double,
        )

        def callback(_handle, _hdc, rect, _data):
            area = rect.contents
            screens.append((area.left, area.top, area.right, area.bottom))
            return 1

        user32.EnumDisplayMonitors(0, 0, procedure(callback), 0)
    except Exception:
        return []
    return screens


def _is_on_screen(rect: tuple[int, int, int, int], screens: list[tuple[int, int, int, int]]) -> bool:
    """Liegt das Rechteck (auch nur teilweise) auf einem Bildschirm?"""
    if not screens:
        # Ohne Bildschirminformation gilt alles als sichtbar: dann wird
        # bewusst nichts ausgeblendet, statt zu raten.
        return True
    left, top, right, bottom = rect
    return any(
        left < screen_right and right > screen_left
        and top < screen_bottom and bottom > screen_top
        for (screen_left, screen_top, screen_right, screen_bottom) in screens
    )


def _foreign_chrome_window_handles() -> set[int]:
    """HWNDs aller Chrome-Fenster (nur Windows, sonst leer).

    Wird genutzt, um vor dem Start den Bestand aufzunehmen und danach nur das
    eigene, neue Fenster auszublenden - fremde Fenster bleiben unberührt.
    """
    if os.name != "nt":  # pragma: no cover - nur Windows
        return set()
    try:  # pragma: no cover - Plattformabhängig
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()
    user32 = ctypes.windll.user32
    handles: set[int] = set()

    def callback(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "Chrome_WidgetWin" in cls.value:
            handles.add(int(hwnd))
        return True

    procedure = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(procedure(callback), 0)
    return handles


def _chrome_process_ids() -> set[int]:
    """PIDs aller laufenden ``chrome.exe``-Prozesse (nur Windows, sonst leer)."""
    if os.name != "nt":  # pragma: no cover - nur Windows
        return set()
    try:  # pragma: no cover - Plattformabhängig
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()

    class _ProcessEntry32(ctypes.Structure):
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

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == -1:
        return set()
    pids: set[int] = set()
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.decode("utf-8", "replace").lower() == "chrome.exe":
                pids.add(int(entry.th32ProcessID))
            ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def _hide_native_window(known: set[int], owned_pids: set[int] | None = None) -> bool:
    """Blendet die Fenster des *eigenen* Chrome aus (Windows).

    Sicherheitsregel: Es wird nie ein fremdes Fenster angefasst.

    * Ist ``owned_pids`` bekannt (die Chrome-Prozesse, die wir selbst gestartet
      haben), gilt deren Zugehörigkeit - damit werden auch Hilfsfenster
      erwischt, die Chrome an beliebiger Stelle aufklappt (Übersetzen-Hinweis,
      Wiederherstellen-Hinweis). Ohne diese Zuordnung blieben solche Fenster
      sichtbar, sobald sie einmal auf dem Bildschirm liegen.
    * Sonst greift der vorsichtige Rückfall: Nur neue Fenster, die weit
      außerhalb des Bildschirms liegen oder noch leer sind, werden ausgeblendet.

    Rückgabe: ``True``, wenn wirklich nichts mehr sichtbar ist.
    """
    if os.name != "nt":  # pragma: no cover - nur Windows
        return False
    try:  # pragma: no cover - Plattformabhängig
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    screens = _screen_rects()
    candidates: list[int] = []

    def callback(hwnd, _lparam):
        handle = int(hwnd)
        if handle in known or not user32.IsWindowVisible(handle):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, cls, 256)
        if "Chrome_WidgetWin" not in cls.value:
            return True
        if owned_pids:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if int(pid.value) not in owned_pids:
                return True  # Fenster des Benutzers - nicht anfassen
            candidates.append(handle)
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(rect))
        visible_area = (rect.left, rect.top, rect.right, rect.bottom)
        off_screen = rect.left <= -10000 or not _is_on_screen(visible_area, screens)
        length = user32.GetWindowTextLengthW(handle)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, title, length + 1)
        text = title.value.strip()
        if not off_screen and text and "about:blank" not in text.lower():
            # Fenster des Benutzers auf dem Bildschirm - nicht anfassen.
            return True
        candidates.append(handle)
        return True

    procedure = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(procedure(callback), 0)
    hidden = False
    for handle in candidates:
        user32.ShowWindow(handle, SW_HIDE)
        if not user32.IsWindowVisible(handle):
            hidden = True
    return hidden


def german_float(raw: str) -> float | None:
    """Wandelt deutsche und englische Zahlenschreibweisen in einen Float.

    Gemeinsam genutzt von allen Website-Connectoren: '1.234,56' -> 1234.56,
    '1.234' (deutscher Tausenderpunkt) -> 1234.0, '149,99' -> 149.99.
    """
    text = raw.strip()
    if not text:
        return None
    if "," in text:
        # "1.234,56" -> 1234.56
        normalized = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        # "1.234" -> 1234 (deutscher Tausenderpunkt)
        normalized = text.replace(".", "")
    else:
        normalized = text
    try:
        return float(normalized)
    except ValueError:
        return None


class BrowserWebConnector(ListingConnector):
    """Basisklasse für Connectoren mit echtem, standardmäßig unsichtbarem Chrome.

    Unterklassen setzen:

    * ``platform``            - Kennung (z. B. ``ebay_web``)
    * ``settings_prefix``     - Präfix der Einstellungen (z. B. ``ebay_web``
      für ``EBAY_WEB_ENABLED``, ``EBAY_WEB_TIMEOUT_SECONDS`` ...)
    * ``warmup_url``          - Startseite, die beim ersten Abruf Cookies setzt
    * ``ready_selector``      - Selektor, auf den die Ergebnisseite wartet
    * ``extract_js``          - JavaScript, das die Trefferliste als Daten liefert
    * ``blocked_title_markers`` - Seitentitel, die auf Bot-Schutz hindeuten
    * ``platform_label``      - Anzeigename in der Oberfläche
    """

    platform = "browser_web"
    settings_prefix = ""
    platform_label = "Website"
    warmup_url = ""
    ready_selector = ""
    extract_js = ""
    probe_term = "grafikkarte"
    blocked_title_markers: tuple[str, ...] = ("Error Page",)
    default_profile_dir = "data/browser_profile"
    default_timeout_seconds = 45.0
    # Zwei gleichzeitige Abrufe: Der Mindestabstand wird trotzdem eingehalten
    # (siehe ``_pace``), aber die Wartezeit überlappt sich mit dem Laden der
    # Seite statt es zu blockieren. Höhere Werte bringen kaum mehr, weil der
    # Mindestabstand den Takt vorgibt.
    default_max_concurrency = 2
    default_min_delay_seconds = 1.5
    # Ein echter Browser (kein Headless) ist bei Marktplätzen mit Bot-Schutz
    # nötig (eBay). Liefert die Seite die Treffer serverseitig gerendert, reicht
    # Headless (Kleinanzeigen) - dann entsteht gar kein Fenster.
    default_headless = False
    # Kein sichtbares Fenster: Chrome läuft mit einem echten, aber außerhalb
    # des Bildschirms liegenden und minimierten Fenster. Der Benutzer sieht
    # nichts; für den Bot-Schutz bleibt es ein normaler Browser.
    # Abschalten mit <PRÄFIX>_HIDDEN=false (dann öffnet sich ein Fenster).
    default_hidden = True
    # Ein Browser, der nur noch nutzlos im Hintergrund läuft, wird geschlossen:
    # Nach so vielen Sekunden ohne Abruf gibt der Connector Speicher und Fenster
    # frei. Beim nächsten Aufruf startet er von selbst wieder.
    default_idle_seconds = 180.0
    # Bot-Schutz lässt sich oft mit einer Pause und einem erneuten Besuch der
    # Startseite (frische Cookies) überwinden. Mehr Versuche als nötig treiben
    # die Laufzeit hoch, zu wenige kosten Treffer.
    default_block_retries = 2
    # Wartezeit vor dem ersten neuen Versuch nach einer Blockade; danach
    # jeweils verdreifacht (3 s, 9 s, ...). Zu kurze Pausen verlängern die
    # Sperre eher, als sie zu lösen.
    default_block_pause_seconds = 3.0
    # Trefferliste per fetch im schon geladenen Tab holen statt für jede Anfrage
    # zu navigieren (siehe ``_INLINE_FETCH_JS``). Abschalten mit
    # ``<PRÄFIX>_INLINE_FETCH=false`` - dann baut jede Anfrage wieder eine
    # eigene Seite auf (Vergleichsmessung, Fehlersuche).
    default_inline_fetch = True
    # Wird der Direktabruf abgewiesen (HTTP 403/429), pausiert er so lange.
    # Danach jeweils verdreifacht (15 s, 45 s, 135 s ...) und bei 300 s gedeckelt:
    # Eine abgewiesene Anfrage soll nicht bei jeder weiteren Anfrage wiederholt
    # werden, ein späterer Versuch aber möglich bleiben. Das normale Laden der
    # Seite läuft währenddessen weiter.
    default_inline_pause_seconds = 15.0
    default_inline_pause_max_seconds = 300.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright = None
        self._context = None
        self._page = None
        self._inline_js: str | None = None
        self._inline_pause_until = 0.0
        self._inline_failures = 0
        self._inline_announced = False
        # Erfolgreiche Seitenaufbauten in dieser Sitzung. Der Direktabruf startet
        # erst danach: Die ersten Anfragen einer ganz frischen Sitzung weist die
        # Seite häufiger ab (live gemessen) - dann lieber gleich laden.
        self._page_loads = 0
        self._warm = False
        self._last_fetch_at = 0.0
        self._start_lock = asyncio.Lock()
        # Ratenbegrenzung: Der Mindestabstand wird als Zeitfenster reserviert
        # (siehe ``_pace``). Das bleibt auch bei gleichzeitigen Abrufen korrekt.
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0
        # Navigationen teilen sich denselben Tab und dürfen deshalb nie
        # gleichzeitig laufen. Der Direktabruf ändert den Tab nicht und darf
        # parallel laufen.
        self._nav_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(
            max(1, int(self._setting("max_concurrency", self.default_max_concurrency)))
        )
        self._launch_error: str | None = None
        # Chrome-Prozesse, die zu diesem Connector gehören. Nur deren Fenster
        # werden ausgeblendet - nie ein Fenster des Benutzers.
        self._owned_pids: set[int] = set()
        self._idle_task: asyncio.Task | None = None
        self._idle_seconds = float(
            self._setting("idle_seconds", self.default_idle_seconds) or 0.0
        )

    # -- Einstellungen -----------------------------------------------------

    def _setting(self, name: str, default: Any) -> Any:
        """Liest ``<präfix>_<name>`` aus der Konfiguration (mit Standardwert)."""
        key = f"{self.settings_prefix}_{name}" if self.settings_prefix else name
        return getattr(self.settings, key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._setting("enabled", True))

    @property
    def inline_fetch_enabled(self) -> bool:
        """Wird die Trefferliste per fetch statt per Navigation geholt?"""
        return bool(self._setting("inline_fetch", self.default_inline_fetch))

    def _profile_dir(self) -> str:
        """Persistentes Browser-Profil (echte Cookies senken den Bot-Verdacht)."""
        raw = str(self._setting("profile_dir", "") or self.default_profile_dir)
        path = Path(raw)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - Rechteproblem
            logger.warning("Browser-Profilordner %s nicht anlegbar", path)
        return str(path)

    @staticmethod
    def _playwright_available() -> bool:
        try:
            import playwright  # noqa: F401
        except Exception:
            return False
        return True

    def _unavailable_reason(self, disabled_text: str) -> str | None:
        """Gemeinsame Begründungen: deaktiviert oder Playwright fehlt."""
        if not self.enabled:
            return disabled_text
        if not self._playwright_available():
            return (
                "Playwright ist nicht installiert. Bitte 'pip install playwright' "
                "ausführen; Google Chrome muss installiert sein."
            )
        return None

    # -- Browser-Lebenszyklus ---------------------------------------------

    async def _ensure_browser(self) -> None:
        async with self._start_lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:  # pragma: no cover - Installationsfehler
                raise ConnectorUnavailableError(
                    "Playwright ist nicht installiert. Bitte 'pip install playwright' "
                    "ausführen; Google Chrome muss installiert sein.",
                    platform=self.platform,
                ) from exc

            self._playwright = await async_playwright().start()
            try:
                need_hide = self._hide_window_needed()
                known_windows = _foreign_chrome_window_handles() if need_hide else set()
                before_pids = _chrome_process_ids() if need_hide else set()
                self._context = await self._launch(self._playwright)
                if need_hide:
                    # Chrome erzeugt sein Fenster manchmal erst kurz nach dem
                    # Start - deshalb ein paar Versuche statt nur einem.
                    state = None
                    for _attempt in range(5):
                        # Alle Chrome-Prozesse, die es vor dem Start nicht gab,
                        # gehören zu diesem Browser.
                        self._owned_pids = _chrome_process_ids() - before_pids
                        state = await self._make_window_invisible(known_windows)
                        if state is not None:
                            break
                        await asyncio.sleep(0.2)
                    if state is None:
                        logger.warning(
                            "%s: Browserfenster konnte nicht unsichtbar gemacht "
                            "werden - es bleibt sichtbar. Abschalten mit %s_HIDDEN=false.",
                            self.platform,
                            self.settings_prefix.upper() or "BROWSER",
                        )
                    else:
                        logger.info("%s: Browserfenster ist unsichtbar (%s)", self.platform, state)
            except Exception as exc:
                await self._stop_playwright()
                self._launch_error = str(exc)
                if isinstance(exc, ConnectorUnavailableError):
                    raise
                raise ConnectorUnavailableError(
                    f"Browser konnte nicht gestartet werden: {exc}",
                    platform=self.platform,
                ) from exc
            await self._warm_up()
            # Das Aufwärmen öffnet eine Seite und holt das Fenster zurück.
            self._hide_window_if_needed()
            self._schedule_idle_close()
            logger.info("%s-Website-Connector: Chrome bereit", self.platform)

    def _profile_candidates(self) -> list[str]:
        """Profilordner der Reihe nach.

        Ist das feste Profil noch von einem alten (abgestürzten) Chrome belegt,
        scheitert der Start mit "Wird in einer aktuellen Browsersitzung
        geöffnet". Ein zweiter Ordner verhindert, dass die Anwendung deshalb
        dauerhaft blockiert.
        """
        primary = self._profile_dir()
        candidates = [primary]
        if self._setting("profile_fallback", True):
            candidates.append(f"{primary}-2")
        return candidates

    async def _launch(self, playwright):
        """Startet Chrome. Headless nur, wo es die Seite erlaubt.

        Reihenfolge je Profilordner: echtes Google Chrome, dann mitgeliefertes
        Chromium (nur wenn installiert). Schlägt alles fehl, nennt der Fehler
        die tatsächlichen Ursachen statt nur der letzten - vor allem den
        häufigen Fall "das Profil ist noch von einem Chrome-Fenster belegt".
        """
        headless = bool(self._setting("headless", self.default_headless))
        hidden = bool(self._setting("hidden", self.default_hidden)) and not headless
        args = self._launch_args(headless=headless, hidden=hidden)
        timeout = float(self._setting("timeout_seconds", self.default_timeout_seconds))
        errors: list[str] = []
        for profile in self._profile_candidates():
            common = dict(
                user_data_dir=profile,
                headless=headless,
                locale="de-DE",
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 900},
                args=args,
                timeout=timeout * 1000,
            )
            try:
                return await playwright.chromium.launch_persistent_context(
                    channel="chrome", **common
                )
            except Exception as exc:
                errors.append(f"Chrome ({profile}): {exc}")
                logger.warning("Chrome im Profil %s nicht gestartet: %s", profile, exc)
            try:
                # Fallback: mitgeliefertes Chromium (nur nach
                # 'playwright install chromium' vorhanden).
                return await playwright.chromium.launch_persistent_context(**common)
            except Exception as exc:
                errors.append(f"Chromium ({profile}): {exc}")
        hint = (
            " Ist noch ein Chrome-Fenster dieser Suche offen, bitte schließen "
            "oder einen anderen Profilordner setzen (…_PROFILE_DIR)."
        )
        raise ConnectorUnavailableError(
            "Browser konnte nicht gestartet werden: " + " | ".join(errors[-3:]) + hint,
            platform=self.platform,
        )

    # -- Unsichtbares Fenster ---------------------------------------------

    def _launch_args(self, *, headless: bool, hidden: bool) -> list[str]:
        """Chrome-Parameter. Im Hidden-Modus startet das Fenster außerhalb des
        Bildschirms (und wird gleich danach noch minimiert).

        Wichtig: Es bleibt ein echtes, sichtbares ("headed") Chrome - nur eben
        nicht im sichtbaren Bereich. Genau deshalb greift der Bot-Schutz nicht
        so wie bei Headless-Browsern.
        """
        args = ["--disable-blink-features=AutomationControlled"]
        if hidden and not headless:
            args.extend(
                [
                    f"--window-position={HIDDEN_WINDOW_POSITION}",
                    f"--window-size={WINDOW_SIZE}",
                    "--start-minimized",
                ]
            )
        return args

    def _hide_window_needed(self) -> bool:
        """Muss das Fenster nach dem Start versteckt werden?"""
        headless = bool(self._setting("headless", self.default_headless))
        return bool(self._setting("hidden", self.default_hidden)) and not headless

    def _hide_window_if_needed(self) -> None:
        """Fenster unsichtbar halten.

        Chrome zeigt sein Fenster bei jeder neuen Seite wieder an und öffnet
        dabei auch Hilfsfenster (Übersetzen-Hinweis). Deshalb wird nach jedem
        Seitenaufbau erneut ausgeblendet. Das ist billig (ein paar WinAPI-Aufrufe)
        und betrifft nur Fenster der eigenen Chrome-Prozesse.
        """
        if self._context is None or not self._hide_window_needed():
            return
        _hide_native_window(set(), self._owned_pids)

    async def _make_window_invisible(self, known_windows: set[int]) -> str | None:
        """Fenster unsichtbar machen: unter Windows ausblenden, sonst minimieren.

        Ausblenden ist die gründlichere Variante (kein Eintrag in der
        Taskleiste). Klappt das nicht, wird wenigstens minimiert - dann steht
        das Fenster außerhalb des Bildschirms und ist zugeklappt.
        """
        if _hide_native_window(known_windows, self._owned_pids):
            return WINDOW_STATE_HIDDEN
        return await self._minimize_window()

    async def _minimize_window(self) -> str | None:
        """Minimiert das Chrome-Fenster und liefert den erreichten Zustand.

        Rückgabe ``None`` bedeutet: Das Verstecken ist fehlgeschlagen (dann
        bleibt das Fenster sichtbar und das wird als Warnung protokolliert).
        """
        if self._context is None:
            return None
        try:
            from playwright.async_api import Error as PlaywrightError
        except Exception:  # pragma: no cover - Playwright fehlt
            return None
        session = None
        try:
            pages = list(self._context.pages)
            page = pages[0] if pages else await self._context.new_page()
            session = await self._context.new_cdp_session(page)
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is None:
                return None
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
            bounds = await session.send(
                "Browser.getWindowBounds", {"windowId": window_id}
            )
            return str((bounds.get("bounds") or {}).get("windowState") or "unknown")
        except PlaywrightError as exc:  # pragma: no cover - Umgebungssache
            logger.debug("Fenster konnte nicht minimiert werden: %s", exc)
            return None
        except Exception as exc:  # pragma: no cover - defensiv
            logger.debug("Fenster konnte nicht minimiert werden: %s", exc)
            return None
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception:  # pragma: no cover
                    pass

    async def window_state(self) -> str | None:
        """Aktueller Fensterzustand (``minimized``/``normal``) oder ``None``.

        Nur für Prüfungen: Damit lässt sich belegen, dass wirklich kein Fenster
        im Weg steht.
        """
        if self._context is None:
            return None
        session = None
        try:
            pages = list(self._context.pages)
            page = pages[0] if pages else await self._context.new_page()
            session = await self._context.new_cdp_session(page)
            info = await session.send("Browser.getWindowForTarget")
            bounds = await session.send(
                "Browser.getWindowBounds", {"windowId": info.get("windowId")}
            )
            return str((bounds.get("bounds") or {}).get("windowState") or "unknown")
        except Exception:
            return None
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass

    async def _warm_up(self, *, force: bool = False) -> None:
        """Besuch der Startseite: setzt Cookies, senkt Bot-Sperren.

        ``force=True`` lädt sie erneut, etwa nach einer Bot-Blockade. Fehler
        hier sind unkritisch - die Suche läuft danach trotzdem.
        """
        if self._context is None or not self.warmup_url:
            return
        if self._warm and not force:
            return
        self._warm = True
        try:
            # Denselben Tab benutzen, der später auch die Trefferlisten holt:
            # Die Startseite setzt dort die Cookies _und_ liefert den Ursprung,
            # von dem aus der Direktabruf erlaubt ist.
            page = await self._open_page()
            async with self._nav_lock:
                await page.goto(
                    self.warmup_url, wait_until="domcontentloaded", timeout=30000
                )
        except Exception as exc:  # pragma: no cover - Aufwärmen ist optional
            logger.debug("%s nicht geladen: %s", self.warmup_url, exc)

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pragma: no cover - Aufräumen darf nie werfen
                pass
        self._playwright = None

    async def aclose(self) -> None:
        """Browser schließen. Mehrfaches Aufrufen ist harmlos."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # pragma: no cover
                pass
        self._context = None
        self._page = None
        self._warm = False
        self._page_loads = 0
        # Frischer Browser: Der nächste Abruf startet ohne alte Wartemarke.
        self._next_slot = 0.0
        self._inline_failures = 0
        self._inline_pause_until = 0.0
        self._inline_announced = False
        self._owned_pids = set()
        await self._stop_playwright()

    async def _recover_browser(self) -> None:
        """Abgestürzten oder geschlossenen Browser sauber neu aufbauen.

        Ein abgestürzter Chrome darf einen Suchlauf nicht dauerhaft lahmlegen:
        einmal aufräumen, dann beim nächsten Abruf frisch starten.
        """
        logger.warning("%s: Browser wird neu aufgebaut", self.platform)
        await self.aclose()

    def _schedule_idle_close(self) -> None:
        """Wachhund starten, der den Browser nach langer Ruhe schließt."""
        if self._idle_seconds <= 0:
            return
        if self._idle_task is not None and not self._idle_task.done():
            return
        try:
            self._idle_task = asyncio.create_task(
                self._idle_watchdog(), name=f"browser-idle-{self.platform}"
            )
        except RuntimeError:  # pragma: no cover - kein laufender Loop
            self._idle_task = None

    async def _idle_watchdog(self) -> None:
        """Schließt den Browser, wenn er ``idle_seconds`` lang unbenutzt war."""
        interval = max(0.05, min(30.0, self._idle_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            if self._context is None:
                return
            elapsed = asyncio.get_running_loop().time() - self._last_fetch_at
            if self._last_fetch_at > 0 and elapsed >= self._idle_seconds:
                logger.info(
                    "%s: Browser nach %.0f s Ruhe geschlossen", self.platform, elapsed
                )
                await self.aclose()
                return

    # -- Abruf -------------------------------------------------------------

    async def _pace(self) -> float:
        """Reserviert den nächsten Abruf-Zeitpunkt (Mindestabstand, Bot-Schutz).

        Der Abstand wird als Zeitfenster verwaltet und nicht als "seit dem
        letzten Abruf vergangene Zeit". Nur so bleibt er auch dann richtig,
        wenn mehrere Seiten gleichzeitig geholt werden (parallele Paginierung):
        Die Abrufe werden gleichmäßig verteilt, statt in einem Schwall zu
        starten. Rückgabe: die Wartezeit in Sekunden.
        """
        gap = float(self._setting("min_delay_seconds", self.default_min_delay_seconds) or 0.0)
        if gap <= 0:
            return 0.0
        loop = asyncio.get_running_loop()
        async with self._rate_lock:
            now = loop.time()
            start = max(now, self._next_slot)
            self._next_slot = start + gap
        wait = start - now
        if wait > 0:
            await asyncio.sleep(wait)
        return max(0.0, wait)

    async def _fetch(self, url: str) -> dict[str, Any]:
        """Einen Abruf durchführen - mit Neustart- und Wiederholungslogik.

        Drei Fälle sind im Betrieb aufgetreten und werden hier abgefangen:
        1. Bot-Schutz (HTTP 403/429): einmal mit kurzer Pause wiederholen.
        2. Browser hängt oder antwortet nicht: Abruf nach Zeitlimit abbrechen.
        3. Browser abgestürzt/geschlossen: einmal neu aufbauen und erneut lesen.
        """
        try:
            data = await asyncio.wait_for(self._fetch_once(url), timeout=self._fetch_deadline())
        except (asyncio.TimeoutError, ConnectorError) as exc:
            if self._context is None:
                # Der Browser ließ sich gar nicht erst starten - ein zweiter
                # Versuch würde nur denselben Fehler wiederholen.
                raise
            logger.warning(
                "%s: Abruf fehlgeschlagen (%s) - Browser wird neu aufgebaut",
                self.platform,
                exc,
            )
            await self._recover_browser()
            deadline = self._fetch_deadline()
            try:
                data = await asyncio.wait_for(self._fetch_once(url), timeout=deadline)
            except asyncio.TimeoutError as exc:
                raise ConnectorError(
                    f"{self.platform_label} antwortet nicht (Zeitlimit {deadline:.0f} s).",
                    platform=self.platform,
                    retryable=True,
                ) from exc
        retries = max(0, int(self._setting("block_retries", self.default_block_retries)))
        attempt = 0
        while data.get("blocked") and attempt < retries:
            attempt += 1
            base_pause = float(
                self._setting("block_pause_seconds", self.default_block_pause_seconds)
            )
            pause = base_pause * (3 ** (attempt - 1))
            logger.info(
                "%s: Abruf wurde blockiert - %.0f s Pause, dann neuer Versuch (%s/%s)",
                self.platform,
                pause,
                attempt,
                retries,
            )
            await asyncio.sleep(pause)
            # Startseite erneut laden: frische Cookies erhöhen die Chance,
            # dass die Suche danach durchkommt.
            await self._warm_up(force=True)
            data = await self._fetch_once(url)
        return data

    def _fetch_deadline(self) -> float:
        """Hartes Zeitlimit für einen Abruf (Seitenladezeit plus Browserstart)."""
        timeout = float(self._setting("timeout_seconds", self.default_timeout_seconds))
        return timeout + 15.0

    async def _open_page(self):
        """Der dauerhafte Tab: EINE Seite für alle Anfragen.

        Vorher wurde für jede Anfrage eine Seite geöffnet und wieder geschlossen.
        Genau das ließ bei jeder Anfrage ein Chrome-Fenster aufblitzen und kostete
        Sekunden. Jetzt bleibt ein Tab offen - dort laufen Aufwärmen,
        Direktabruf und (im Rückfallweg) auch die Navigation.
        """
        assert self._context is not None
        async with self._page_lock:
            page = self._page
            if page is not None and not page.is_closed():
                return page
            pages = [p for p in self._context.pages if not p.is_closed()]
            if pages:
                page = pages[0]
            else:
                page = await self._context.new_page()
                # Ein neuer Tab holt das Fenster wieder nach vorn.
                self._hide_window_if_needed()
            self._page = page
            return page

    async def _fetch_once(self, url: str) -> dict[str, Any]:
        await self._ensure_browser()
        try:
            page = await self._open_page()
        except Exception:
            # Kontext verloren (z. B. Fenster geschlossen) - neu aufbauen.
            self._context = None
            self._page = None
            self._warm = False
            await self._ensure_browser()
            page = await self._open_page()
        if (
            self.inline_fetch_enabled
            and self._page_loads > 0
            and asyncio.get_running_loop().time() >= self._inline_pause_until
        ):
            data = await self._fetch_inline(page, url)
            if data is not None:
                return data
        return await self._fetch_by_navigation(page, url)

    async def _fetch_inline(self, page, url: str) -> dict[str, Any] | None:
        """Trefferliste per ``fetch`` im geladenen Tab holen (Direktabruf).

        Rückgabe ``None`` heißt: Dieser Weg ging nicht - der Aufrufer navigiert
        dann normal. Wichtig: Der Direktabruf meldet **keine** Blockade. Ein
        ``403`` auf einen fetch heißt bei eBay nicht, dass der Ursprung gesperrt
        ist - geladene Seiten liefern weiterhin Treffer (live gemessen). Die
        Blockade erkennt deshalb weiterhin allein das Laden der Seite.
        """
        current = urlparse(_page_url(page))
        target = urlparse(url)
        if (current.scheme, current.netloc) != (target.scheme, target.netloc):
            # Ohne passenden Ursprung würde der Abruf an der Same-Origin-Regel
            # scheitern - dann gleich regulär laden.
            return None
        if self._inline_js is None:
            self._inline_js = inline_fetch_script(self.extract_js)
        timeout = float(self._setting("timeout_seconds", self.default_timeout_seconds))
        try:
            result = await asyncio.wait_for(
                page.evaluate(self._inline_js, url), timeout=timeout
            )
        except Exception as exc:
            logger.info(
                "%s: Direktabruf nicht möglich (%s) - Seite wird normal geladen",
                self.platform,
                exc,
            )
            return None
        if not isinstance(result, dict):
            return None
        status = int(result.get("status") or 0)
        title = str(result.get("title") or "")
        if status in (403, 429) or any(
            marker in title for marker in self.blocked_title_markers
        ):
            # Weist die Seite den fetch ab, wird er kurz ausgesetzt: Ein
            # weiterer Versuch wuerde nur eine Anfrage verbrennen, waehrend das
            # normale Laden der Seite (der massgebliche Weg) weiterlaeuft.
            self._pause_inline("abgewiesen (HTTP %s)" % status)
            return None
        data = result.get("data")
        if status == 200 and isinstance(data, dict):
            # Auch eine leere Trefferliste ist gültig: Die Überschrift
            # ("0 Ergebnisse für ...") verrät, dass es die Ergebnisseite war.
            if data.get("items") or data.get("heading"):
                if not self._inline_announced:
                    self._inline_announced = True
                    logger.info(
                        "%s: Direktabruf aktiv - Trefferlisten kommen ohne "
                        "Seitenaufbau (schneller, kein Fensterwechsel)",
                        self.platform,
                    )
                self._inline_failures = 0
                return data
        logger.info(
            "%s: Direktabruf lieferte keine Trefferliste (HTTP %s, %s kB, %s)",
            self.platform,
            status,
            int(result.get("size") or 0) // 1024,
            result.get("error") or "kein Auslesefehler",
        )
        return None

    def _pause_inline(self, grund: str) -> None:
        """Direktabruf vorübergehend aussetzen (mit wachsender Pause)."""
        base = float(
            self._setting("inline_pause_seconds", self.default_inline_pause_seconds)
            or 0.0
        )
        if base <= 0:
            return
        maximum = float(
            self._setting(
                "inline_pause_max_seconds", self.default_inline_pause_max_seconds
            )
            or base
        )
        self._inline_failures += 1
        pause = min(maximum, base * (3 ** (self._inline_failures - 1)))
        self._inline_pause_until = asyncio.get_running_loop().time() + pause
        logger.info(
            "%s: Direktabruf pausiert (%.0f s) - %s; die Seite wird normal geladen",
            self.platform,
            pause,
            grund,
        )

    async def _fetch_by_navigation(self, page, url: str) -> dict[str, Any]:
        """Trefferliste durch vollständigen Seitenaufbau im dauerhaften Tab.

        Läuft immer allein (``_nav_lock``): Alle Abrufe teilen sich denselben
        Tab, und während der Direktabruf parallel laufen darf, würde eine
        gleichzeitige Navigation den Auslesecode der anderen Anfrage auf einer
        fremden Seite auswerten.
        """
        async with self._nav_lock:
            return await self._fetch_by_navigation_locked(page, url)

    async def _fetch_by_navigation_locked(self, page, url: str) -> dict[str, Any]:
        timeout = float(self._setting("timeout_seconds", self.default_timeout_seconds))
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            status = response.status if response is not None else None
            title = ""
            try:
                title = await page.title()
            except Exception:  # pragma: no cover
                title = ""
            if status in (403, 429) or any(
                marker in title for marker in self.blocked_title_markers
            ):
                return {"blocked": True, "items": []}
            # Die Seite wurde geladen - ab hier ist auch der Direktabruf erlaubt.
            self._page_loads += 1
            if self.ready_selector:
                try:
                    await page.wait_for_selector(
                        self.ready_selector, timeout=min(15000, timeout * 1000)
                    )
                except Exception:
                    # Kein Treffer ist kein Fehler: die Auswertung oben meldet es.
                    pass
            await page.wait_for_timeout(500)
            data = await page.evaluate(self.extract_js)
            # Das Laden der Seite holt Chrome sein Fenster zurück - erneut
            # ausblenden, bevor die nächste Anfrage kommt.
            self._hide_window_if_needed()
            if not isinstance(data, dict):
                return {"items": []}
            return data
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                f"{self.platform_label} konnte nicht geladen werden: {exc}",
                platform=self.platform,
                retryable=True,
            ) from exc
