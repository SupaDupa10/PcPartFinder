"""Tests für die gemeinsame Basis der Website-Connectoren (ohne echten Browser).

Geprüft wird, wie sich der Browser-Lebenszyklus unter Störungen verhält:
Bot-Blockade, Absturz/geschlossener Browser, Zeitüberschreitung, Ruhe-Erkennung
und der Mindestabstand zwischen zwei Abrufen. Es wird nie ein Browser gestartet
und nie eine Netzwerkverbindung aufgebaut.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from backend.connectors.base import ConnectorError, ConnectorUnavailableError
from backend.connectors.browser_web import (
    FIREFOX,
    BrowserWebConnector,
    german_float,
    inline_fetch_script,
)


class _Settings:
    """Minimale Einstellungen für ein Präfix wie in echten Settings."""

    demo_enabled = True
    demo_timeout_seconds = 0.2
    demo_max_concurrency = 1
    demo_min_delay_seconds = 0.05
    # Profilordner außerhalb des Projekts, damit Tests keine Ordner anlegen.
    demo_profile_dir = tempfile.mkdtemp(prefix="browser-web-test-")
    demo_idle_seconds = 0.05


class _Connector(BrowserWebConnector):
    """Minimaler Connector: nur die Basis-Funktionen werden getestet."""

    platform = "demo"
    settings_prefix = "demo"
    platform_label = "Demo"
    warmup_url = "https://example.invalid/"
    ready_selector = "article[data-href]"
    extract_js = "() => ({items: [], heading: null})"
    default_profile_dir = "data/demo_profile"

    @property
    def capabilities(self):  # pragma: no cover - nicht Teil dieses Tests
        raise NotImplementedError

    async def search_page(self, query):  # pragma: no cover - nicht Teil dieses Tests
        raise NotImplementedError


class TestZahlenformat:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.234,56", 1234.56),
            ("1.234", 1234.0),
            ("149,99", 149.99),
            ("37", 37.0),
            ("", None),
            ("keine Zahl", None),
        ],
    )
    def test_deutsche_schreibweisen(self, raw, expected):
        assert german_float(raw) == expected


class TestEinstellungen:
    def test_praefix_wird_vorangestellt(self):
        connector = _Connector(_Settings())
        assert connector._setting("enabled", False) is True
        assert connector._setting("timeout_seconds", 99.0) == 0.2

    def test_fehlende_einstellung_nutzt_den_standard(self):
        connector = _Connector(_Settings())
        assert connector._setting("gibt_es_nicht", "standard") == "standard"


class TestVerfuegbarkeit:
    def test_deaktiviert_ist_nicht_verfuegbar(self):
        class Disabled(_Settings):
            demo_enabled = False

        caps_connector = _Connector(Disabled())
        assert caps_connector.enabled is False
        assert caps_connector._unavailable_reason("aus") == "aus"

    def test_ohne_playwright_wird_der_grund_genannt(self, monkeypatch):
        connector = _Connector(_Settings())
        monkeypatch.setattr(connector, "_playwright_available", lambda: False)
        reason = connector._unavailable_reason("aus")
        assert reason is not None and "Playwright" in reason


class _FakeBrowser:
    """Minimaler Ersatz fuer playwright.chromium bzw. playwright.firefox."""

    def __init__(self, fail_times: int = 0, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self.error = error or RuntimeError("Wird in einer aktuellen Browsersitzung geöffnet")

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise self.error
        return f"context:{kwargs['user_data_dir']}"


class _FakePlaywright:
    """Playwright-Attrappe. Firefox scheitert standardmäßig (wie ohne
    ``playwright install firefox``), damit die Tests den Ausweichweg nicht
    versehentlich benutzen."""

    def __init__(
        self,
        chromium: _FakeBrowser | None = None,
        firefox: _FakeBrowser | None = None,
    ) -> None:
        self.chromium = chromium if chromium is not None else _FakeBrowser()
        self.firefox = firefox if firefox is not None else _FakeBrowser(fail_times=99)


class _FakeUser32:
    """Minimale WinAPI-Attrappe fuer die Fenstererkennung (Windows-API)."""

    def __init__(
        self,
        windows: dict[int, int],
        title: str = "",
        window_class: str = "Chrome_WidgetWin_1",
    ) -> None:
        self.windows = dict(windows)
        self.title = title
        self.window_class = window_class
        self.visible = set(self.windows)
        self.hidden: list[int] = []

    def EnumWindows(self, callback, _lparam):
        for handle in list(self.windows):
            callback(handle, 0)
        return 1

    def EnumDisplayMonitors(self, *_args):
        return 1

    def GetClassNameW(self, _handle, buffer, _size):
        buffer.value = self.window_class
        return len(buffer.value)

    def IsWindowVisible(self, handle):
        return handle in self.visible

    def GetWindowThreadProcessId(self, handle, out_pid):
        out_pid._obj.value = self.windows[handle]
        return 1

    def GetWindowRect(self, _handle, out_rect):
        out_rect._obj.left = 0
        out_rect._obj.top = 0
        out_rect._obj.right = 800
        out_rect._obj.bottom = 600
        return 1

    def GetWindowTextLengthW(self, _handle):
        return len(self.title)

    def GetWindowTextW(self, _handle, buffer, _size):
        buffer.value = self.title
        return len(self.title)

    def ShowWindow(self, handle, _command):
        self.visible.discard(handle)
        self.hidden.append(handle)
        return 1


class TestBrowserstart:
    def test_profilordner_hat_einen_ausweichordner(self):
        candidates = _Connector(_Settings())._profile_candidates()
        assert len(candidates) == 2
        assert candidates[1] == f"{candidates[0]}-2"

    def test_ausweichordner_ist_abschaltbar(self):
        class Single(_Settings):
            demo_profile_fallback = False

        assert len(_Connector(Single())._profile_candidates()) == 1

    @pytest.mark.asyncio()
    async def test_belegtes_profil_faellt_auf_zweiten_ordner_zurueck(self):
        connector = _Connector(_Settings())
        # Chrome (Ordner 1 und 2) und Chromium (Ordner 1) scheitern, Chromium
        # im zweiten Ordner kommt durch - der Ausweichordner rettet den Start.
        chromium = _FakeBrowser(fail_times=3)
        context = await connector._launch(_FakePlaywright(chromium=chromium))
        assert isinstance(context, str) and context.endswith("-2")
        assert len(chromium.calls) == 4

    @pytest.mark.asyncio()
    async def test_alle_versuche_scheitern_mit_klarer_meldung(self):
        connector = _Connector(_Settings())
        chromium = _FakeBrowser(fail_times=99)
        with pytest.raises(ConnectorUnavailableError) as excinfo:
            await connector._launch(_FakePlaywright(chromium=chromium))
        message = str(excinfo.value)
        assert "Google Chrome" in message and "Chromium" in message
        assert "Firefox" in message
        assert "PROFILE_DIR" in message


class TestBrowserauswahl:
    """<PRÄFIX>_BROWSER: welcher Browser die Website-Suche ausfuehrt."""

    def test_standard_probiert_chrome_dann_chromium_dann_firefox(self):
        engines = _Connector(_Settings())._engine_candidates()
        assert [engine.key for engine in engines] == ["chrome", "chromium", "firefox"]

    def test_firefox_laesst_sich_allein_waehlen(self):
        class NurFirefox(_Settings):
            demo_browser = "firefox"

        connector = _Connector(NurFirefox())
        engines = connector._engine_candidates()
        assert [engine.key for engine in engines] == ["firefox"]
        # Eigenes Profil: ein Chrome-Profil darf nicht als Firefox dienen.
        assert connector._profile_candidates(engines[0])[0].endswith("-firefox")

    def test_chromium_faellt_nicht_auf_chrome_zurueck(self):
        class NurChromium(_Settings):
            demo_browser = "chromium"

        engines = _Connector(NurChromium())._engine_candidates()
        assert [engine.key for engine in engines] == ["chromium"]

    def test_grossschreibung_ist_egal(self):
        class Gross(_Settings):
            demo_browser = "Firefox"

        assert [e.key for e in _Connector(Gross())._engine_candidates()] == ["firefox"]

    def test_unbekannter_wert_wird_gemeldet_und_gilt_als_auto(self, caplog):
        class Tippfehler(_Settings):
            demo_browser = "foxfire"

        with caplog.at_level("WARNING"):
            engines = _Connector(Tippfehler())._engine_candidates()
        assert [engine.key for engine in engines] == ["chrome", "chromium", "firefox"]
        assert "foxfire" in caplog.text and "DEMO_BROWSER" in caplog.text

    def test_hinweis_nennt_die_ausweichbrowser(self):
        hint = _Connector(_Settings()).browser_setup_hint()
        assert "Google Chrome" in hint
        assert "playwright install firefox" in hint

    def test_firefox_braucht_keine_chromium_schalter(self):
        connector = _Connector(_Settings())
        assert connector._launch_args(engine=FIREFOX, headless=False, hidden=True) == []
        chrome_args = connector._launch_args(headless=False, hidden=True)
        assert "--start-minimized" in chrome_args


class TestFirefoxStart:
    @pytest.mark.asyncio()
    async def test_firefox_startet_ohne_chromium_schalter_und_mit_eigener_kennung(self):
        class NurFirefox(_Settings):
            demo_browser = "firefox"

        connector = _Connector(NurFirefox())
        firefox = _FakeBrowser()
        await connector._launch(_FakePlaywright(firefox=firefox))
        assert connector.engine is not None and connector.engine.key == "firefox"
        call = firefox.calls[0]
        assert call["user_data_dir"].endswith("-firefox")
        assert call["args"] == []
        assert "channel" not in call
        # Firefox meldet sich selbst als Firefox - keine Chrome-Kennung.
        assert "user_agent" not in call
        assert call["firefox_user_prefs"]["dom.webdriver.enabled"] is False
        assert call["headless"] is False

    @pytest.mark.asyncio()
    async def test_auto_faellt_auf_firefox_zurueck(self):
        """Ohne Chrome und Chromium sucht die Anwendung mit Firefox."""
        connector = _Connector(_Settings())
        firefox = _FakeBrowser()
        context = await connector._launch(
            _FakePlaywright(chromium=_FakeBrowser(fail_times=99), firefox=firefox)
        )
        assert connector.engine is not None and connector.engine.key == "firefox"
        assert str(context).endswith("-firefox")

    @pytest.mark.asyncio()
    async def test_fehlender_firefox_nennt_den_installationsbefehl(self):
        class NurFirefox(_Settings):
            demo_browser = "firefox"

        connector = _Connector(NurFirefox())
        firefox = _FakeBrowser(
            fail_times=99,
            error=RuntimeError(
                "Executable doesn't exist at C:\\ms-playwright\\firefox-1543\\"
                "firefox\\firefox.exe"
            ),
        )
        with pytest.raises(ConnectorUnavailableError) as excinfo:
            await connector._launch(_FakePlaywright(firefox=firefox))
        message = str(excinfo.value)
        assert "playwright install firefox" in message
        # Fehlt der Browser selbst, hilft kein zweiter Profilordner.
        assert len(firefox.calls) == 1

    @pytest.mark.asyncio()
    async def test_chrome_bekommt_weiterhin_die_chrome_kennung(self):
        connector = _Connector(_Settings())
        chromium = _FakeBrowser()
        await connector._launch(_FakePlaywright(chromium=chromium))
        assert chromium.calls[0]["channel"] == "chrome"
        assert "Chrome" in chromium.calls[0]["user_agent"]
        assert "firefox_user_prefs" not in chromium.calls[0]


class TestUnsichtbaresFenster:
    """Der Browser läuft echt, aber ohne sichtbares Fenster über der Arbeit."""

    def test_hidden_ist_standard(self):
        connector = _Connector(_Settings())
        assert connector._hide_window_needed() is True

    def test_fenster_ausserhalb_des_bildschirms_und_minimiert(self):
        connector = _Connector(_Settings())
        args = connector._launch_args(headless=False, hidden=True)
        assert any(arg.startswith("--window-position=-32") for arg in args)
        assert "--start-minimized" in args
        assert "--window-size=1366,900" in args

    def test_es_bleibt_ein_echter_browser(self):
        """Wichtig: kein Headless. Nur so greift der Bot-Schutz nicht."""
        connector = _Connector(_Settings())
        assert "--headless" not in " ".join(
            connector._launch_args(headless=False, hidden=True)
        )

    def test_abschaltbar_fuer_die_fehlersuche(self):
        class Visible(_Settings):
            demo_hidden = False

        connector = _Connector(Visible())
        assert connector._hide_window_needed() is False
        args = connector._launch_args(headless=False, hidden=False)
        assert "--start-minimized" not in args
        assert not any(arg.startswith("--window-position") for arg in args)

    def test_headless_braucht_keine_fensterparameter(self):
        connector = _Connector(_Settings())
        args = connector._launch_args(headless=True, hidden=True)
        assert not any(arg.startswith("--window-position") for arg in args)
        assert "--start-minimized" not in args

    def test_headless_hebelt_das_verstecken_aus(self):
        class Headless(_Settings):
            demo_headless = True

        assert _Connector(Headless())._hide_window_needed() is False

    @pytest.mark.asyncio()
    async def test_start_bleibt_headed_und_versteckt(self):
        connector = _Connector(_Settings())
        chromium = _FakeBrowser(fail_times=0)
        await connector._launch(_FakePlaywright(chromium=chromium))
        assert chromium.calls[0]["headless"] is False
        assert "--start-minimized" in chromium.calls[0]["args"]

    @pytest.mark.asyncio()
    async def test_minimieren_ohne_browser_bleibt_harmlos(self):
        connector = _Connector(_Settings())
        assert await connector._minimize_window() is None
        assert await connector.window_state() is None

    @pytest.mark.asyncio()
    async def test_ausblenden_wird_bevorzugt(self, monkeypatch):
        """Unter Windows wird ausgeblendet (kein Taskbar-Eintrag)."""
        from backend.connectors import browser_web

        connector = _Connector(_Settings())
        monkeypatch.setattr(
            browser_web, "_hide_native_window", lambda known, owned=None, *a, **k: True
        )
        assert await connector._make_window_invisible(set()) == "hidden"

    @pytest.mark.asyncio()
    async def test_ohne_ausblenden_wird_minimiert(self, monkeypatch):
        from backend.connectors import browser_web

        connector = _Connector(_Settings())
        monkeypatch.setattr(
            browser_web, "_hide_native_window", lambda known, owned=None, *a, **k: False
        )
        minimized: list[bool] = []

        async def fake_minimize():
            minimized.append(True)
            return "minimized"

        connector._minimize_window = fake_minimize  # type: ignore[assignment]
        assert await connector._make_window_invisible(set()) == "minimized"
        assert minimized == [True]

    def test_fenstererkennung_liefert_eine_menge(self):
        from backend.connectors.browser_web import _window_handles

        assert isinstance(_window_handles(), set)
        assert isinstance(_window_handles(FIREFOX.window_class_markers), set)

    def test_prozesserkennung_liefert_eine_menge(self):
        from backend.connectors.browser_web import _process_ids

        assert isinstance(_process_ids(), set)
        assert isinstance(_process_ids(FIREFOX.process_names), set)

    @pytest.mark.asyncio()
    async def test_eigene_prozessliste_wird_weitergereicht(self, monkeypatch):
        """Das Ausblenden bekommt die eigenen Chrome-Prozesse mit."""
        from backend.connectors import browser_web

        connector = _Connector(_Settings())
        connector._owned_pids = {12345}
        seen: dict = {}

        def fake_hide(known, owned=None, *_args, **_kwargs):
            seen["owned"] = owned
            return True

        monkeypatch.setattr(browser_web, "_hide_native_window", fake_hide)
        assert await connector._make_window_invisible(set()) == "hidden"
        assert seen["owned"] == {12345}

    def test_fenster_bleiben_beim_seitenaufbau_verborgen(self, monkeypatch):
        """Auch nach jedem Seitenaufbau werden nur eigene Fenster behandelt."""
        from backend.connectors import browser_web

        connector = _Connector(_Settings())
        connector._context = object()
        connector._owned_pids = {7}
        seen: dict = {}

        def fake_hide(known, owned=None, *_args, **_kwargs):
            seen["known"] = known
            seen["owned"] = owned
            return True

        monkeypatch.setattr(browser_web, "_hide_native_window", fake_hide)
        connector._hide_window_if_needed()
        assert seen == {"known": set(), "owned": {7}}

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_fremde_fenster_bleiben_unangetastet(self):
        """Ein unbekanntes Prozess-Id blendet garantiert nichts aus."""
        from backend.connectors.browser_web import _hide_native_window

        assert _hide_native_window(set(), {999_999_999}) is False

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_nur_eigene_prozessfenster_werden_ausgeblendet(self, monkeypatch):
        """Bei bekannter Prozessliste: eigene Fenster ausblenden, fremde nicht."""
        import ctypes

        from backend.connectors.browser_web import _hide_native_window

        fake = _FakeUser32(windows={101: 1111, 202: 2222})
        monkeypatch.setattr(ctypes.windll, "user32", fake)

        assert _hide_native_window(set(), {1111}) is True
        assert fake.hidden == [101]
        assert 202 in fake.visible

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_firefox_fenster_werden_erkannt(self, monkeypatch):
        """Firefox-Fenster heißen anders ("Mozilla...") - die Erkennung auch."""
        import ctypes

        from backend.connectors.browser_web import _hide_native_window

        fake = _FakeUser32(
            windows={101: 1111}, window_class="MozillaWindowClass"
        )
        monkeypatch.setattr(ctypes.windll, "user32", fake)

        assert _hide_native_window(set(), {1111}, FIREFOX.window_class_markers) is True
        assert fake.hidden == [101]

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_chrome_marker_fassen_kein_firefox_fenster_an(self, monkeypatch):
        import ctypes

        from backend.connectors.browser_web import _hide_native_window

        fake = _FakeUser32(windows={101: 1111}, window_class="MozillaWindowClass")
        monkeypatch.setattr(ctypes.windll, "user32", fake)

        assert _hide_native_window(set(), {1111}) is False
        assert fake.hidden == []

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_firefox_fenster_werden_ohne_cdp_minimiert(self, monkeypatch):
        import ctypes

        from backend.connectors.browser_web import _minimize_native_window

        fake = _FakeUser32(windows={101: 1111, 202: 2222}, window_class="MozillaWindowClass")
        monkeypatch.setattr(ctypes.windll, "user32", fake)

        assert _minimize_native_window({1111}, FIREFOX.window_class_markers) is True
        # Nur das eigene Fenster wird angefasst.
        assert fake.hidden == [101]
        assert 202 in fake.visible

    def test_minimieren_ohne_prozessliste_ist_harmlos(self):
        """Ohne bekannte eigene Prozesse wird kein Fenster angefasst."""
        from backend.connectors.browser_web import _minimize_native_window

        assert _minimize_native_window(None, FIREFOX.window_class_markers) is False
        assert _minimize_native_window(set(), FIREFOX.window_class_markers) is False

    @pytest.mark.skipif(os.name != "nt", reason="nutzt die Windows-API")
    def test_fenster_des_nutzers_bleiben_im_rueckfall_offen(self, monkeypatch):
        """Ohne Prozessliste wird ein benanntes Fenster im Bild nicht angefasst."""
        import ctypes

        from backend.connectors.browser_web import _hide_native_window

        fake = _FakeUser32(windows={303: 3333}, title="Gmail - Chrome")
        monkeypatch.setattr(ctypes.windll, "user32", fake)

        assert _hide_native_window(set(), None) is False
        assert fake.hidden == []
        assert 303 in fake.visible


class TestAbrufWiederholung:
    @pytest.mark.asyncio()
    async def test_blockade_wird_wiederholt_und_startseite_neu_geladen(self):
        connector = _Connector(_Settings())
        calls: list[str] = []
        warmed: list[bool] = []

        async def fake_fetch_once(url):
            calls.append(url)
            if len(calls) == 1:
                return {"blocked": True, "items": []}
            return {"items": [{"title": "ok"}], "heading": None}

        async def fake_warmup(*, force: bool = False):
            warmed.append(force)

        connector._fetch_once = fake_fetch_once  # type: ignore[assignment]
        connector._warm_up = fake_warmup  # type: ignore[assignment]
        connector._context = object()
        data = await connector._fetch("https://example.invalid/?q=1")
        assert data["items"] == [{"title": "ok"}]
        assert len(calls) == 2
        assert warmed == [True]

    @pytest.mark.asyncio()
    async def test_blockade_wird_nicht_endlos_wiederholt(self):
        class Once(_Settings):
            demo_block_retries = 1

        connector = _Connector(Once())
        calls: list[str] = []

        async def fake_fetch_once(url):
            calls.append(url)
            return {"blocked": True, "items": []}

        async def fake_warmup(*, force: bool = False):
            return None

        connector._fetch_once = fake_fetch_once  # type: ignore[assignment]
        connector._warm_up = fake_warmup  # type: ignore[assignment]
        connector._context = object()
        data = await connector._fetch("https://example.invalid/?q=1")
        assert data["blocked"] is True
        assert len(calls) == 2

    @pytest.mark.asyncio()
    async def test_abgestuerzter_browser_wird_neu_aufgebaut(self):
        connector = _Connector(_Settings())
        recovered: list[bool] = []
        calls: list[str] = []

        async def fake_fetch_once(url):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectorError("Browser closed", platform="demo", retryable=True)
            return {"items": [{"title": "ok"}], "heading": None}

        async def fake_recover():
            recovered.append(True)

        connector._fetch_once = fake_fetch_once  # type: ignore[assignment]
        connector._recover_browser = fake_recover  # type: ignore[assignment]
        connector._context = object()  # es gab bereits einen Browser

        data = await connector._fetch("https://example.invalid/?q=1")
        assert recovered == [True]
        assert data["items"] == [{"title": "ok"}]
        assert len(calls) == 2

    @pytest.mark.asyncio()
    async def test_startender_browser_wird_nicht_endlos_wiederholt(self):
        connector = _Connector(_Settings())
        calls: list[str] = []

        async def fake_fetch_once(url):
            calls.append(url)
            raise ConnectorUnavailableError("Chrome fehlt", platform="demo")

        connector._fetch_once = fake_fetch_once  # type: ignore[assignment]
        connector._context = None

        with pytest.raises(ConnectorUnavailableError):
            await connector._fetch("https://example.invalid/?q=1")
        assert len(calls) == 1

    @pytest.mark.asyncio()
    async def test_haengender_abruf_laeuft_in_ein_zeitlimit(self):
        connector = _Connector(_Settings())
        calls: list[str] = []

        async def fake_fetch_once(url):
            calls.append(url)
            if len(calls) == 1:
                await asyncio.sleep(5)
            return {"items": [], "heading": None}

        async def fake_recover():
            return None

        connector._fetch_once = fake_fetch_once  # type: ignore[assignment]
        connector._recover_browser = fake_recover  # type: ignore[assignment]
        connector._fetch_deadline = lambda: 0.05  # type: ignore[assignment]
        connector._context = object()
        data = await asyncio.wait_for(connector._fetch("https://example.invalid/?q=1"), timeout=6)
        assert data["items"] == []
        assert len(calls) == 2


class _FakePage:
    """Minimaler Ersatz fuer eine Playwright-Seite."""

    def __init__(self, url: str = "https://example.invalid/", result=None) -> None:
        self.url = url
        self.result = result
        self.closed = False
        self.evaluated: list[tuple[str, object]] = []

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, js, arg=None):
        self.evaluated.append((js, arg))
        return self.result


class _FakeContext:
    """Minimaler Ersatz fuer einen Browser-Kontext mit Seiten."""

    def __init__(self, pages: list[_FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self.opened = 0

    async def new_page(self) -> _FakePage:
        self.opened += 1
        page = _FakePage()
        self.pages.append(page)
        return page


def _direktabruf_connector(*, page_loads: int = 1, **settings) -> _Connector:
    """Connector ohne echten Browser.

    ``page_loads`` steht für bereits geladene Seiten: Der Direktabruf startet
    erst nach einem erfolgreichen Seitenaufbau.
    """
    klass = type("Direkt", (_Settings,), {"demo_min_delay_seconds": 0.0, **settings})
    connector = _Connector(klass())
    connector._hide_window_if_needed = lambda: None  # type: ignore[assignment]
    connector._page_loads = page_loads
    return connector


def _fetch_ergebnis(status: int = 200, **data) -> dict:
    """Rohantwort des Direktabrufs wie aus dem Browser-JavaScript."""
    return {
        "status": status,
        "size": 1234,
        "title": "Treffer",
        "data": data or None,
        "error": None,
    }


class TestDirektabrufSkript:
    def test_auslesecode_wird_eingebettet_und_fetch_genutzt(self):
        js = inline_fetch_script("() => ({items: document.title})")
        assert "await fetch(url" in js
        assert "DOMParser" in js
        # Der Auslese-Code darf nicht umgeschrieben werden.
        assert "() => ({items: document.title})" in js

    def test_auslesefunktion_wird_geklammert_aufgerufen(self):
        """Ohne Klammern beendet JavaScript den Aufruf direkt nach 'return'."""
        js = inline_fetch_script("\n() => ({items: []})")
        assert json.dumps("\n() => ({items: []})") in js
        assert "return (" in js
        # Ein blankes 'return ' vor dem (mit Zeilenumbruch beginnenden) Code
        # wuerde still 'undefined' liefern statt der Trefferliste.
        assert "return " + json.dumps("\n() => ({items: []})") not in js

    def test_funktionsergebnis_wird_benutzt(self):
        js = inline_fetch_script("() => ({items: []})")
        assert "typeof data === 'function'" in js

    def test_anfuehrungszeichen_im_auslesecode_bleiben_gueltig(self):
        js = inline_fetch_script('() => document.querySelector("a[href*=\'/itm/\']")')
        # Als JSON-Zeichenkette eingebettet: der Code selbst steht unveraendert drin.
        assert "document.querySelector(" in js


class TestDirektabruf:
    @pytest.mark.asyncio()
    async def test_trefferliste_kommt_ohne_navigation(self):
        connector = _direktabruf_connector()
        page = _FakePage(result=_fetch_ergebnis(items=[{"title": "ok"}], heading="1 Ergebnis"))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": []}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert data["items"] == [{"title": "ok"}]
        assert navigated == []
        # Die Anfrage geht an dieselbe URL - nur ohne Seitenaufbau.
        assert page.evaluated[0][1] == "https://example.invalid/sch?q=1"

    @pytest.mark.asyncio()
    async def test_erste_anfrage_einer_sitzung_laedt_die_seite(self):
        """Eine ganz frische Sitzung weist den Direktabruf oft ab - also laden."""
        connector = _direktabruf_connector(page_loads=0)
        page = _FakePage(result=_fetch_ergebnis(items=[{"title": "per fetch"}]))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": [{"title": "geladen"}]}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert navigated == ["https://example.invalid/sch?q=1"]
        assert page.evaluated == []
        assert data["items"][0]["title"] == "geladen"

    @pytest.mark.asyncio()
    async def test_leere_trefferliste_mit_ueberschrift_zaehlt_als_ergebnis(self):
        connector = _direktabruf_connector()
        page = _FakePage(result=_fetch_ergebnis(items=[], heading="0 Ergebnisse für xyz"))
        connector._context = _FakeContext([page])

        async def fake_navigation(page, url):  # pragma: no cover - darf nicht laufen
            raise AssertionError("keine Navigation erwartet")

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert data["items"] == [] and data["heading"].startswith("0 Ergebnisse")

    @pytest.mark.asyncio()
    async def test_abgewiesener_direktabruf_ist_keine_blockade(self):
        """403 auf einen fetch heißt nicht, dass der Ursprung gesperrt ist.

        Die Seite selbst kann weiterhin Treffer liefern - deshalb wird normal
        geladen und keine Blockade gemeldet.
        """
        connector = _direktabruf_connector()
        page = _FakePage(result=_fetch_ergebnis(status=403))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": [{"title": "geladen"}], "heading": "1 Ergebnis"}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert data.get("blocked") is not True
        assert data["items"][0]["title"] == "geladen"
        assert navigated == ["https://example.invalid/sch?q=1"]

    @pytest.mark.asyncio()
    async def test_ohne_trefferliste_wird_normal_geladen(self):
        connector = _direktabruf_connector()
        # 200, aber weder Treffer noch Ueberschrift: keine Ergebnisseite.
        page = _FakePage(result=_fetch_ergebnis(items=[], heading=None))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": [{"title": "aus der Navigation"}]}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert navigated == ["https://example.invalid/sch?q=1"]
        assert data["items"][0]["title"] == "aus der Navigation"

    @pytest.mark.asyncio()
    async def test_fehler_im_skript_fuehrt_zur_navigation(self):
        connector = _direktabruf_connector()

        class Kaputt(_FakePage):
            async def evaluate(self, js, arg=None):
                raise RuntimeError("Blocked a frame with origin")

        connector._context = _FakeContext([Kaputt()])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": [{"title": "ok"}]}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert navigated and data["items"][0]["title"] == "ok"

    @pytest.mark.asyncio()
    async def test_fremder_ursprung_ueberspringt_den_direktabruf(self):
        """Von about:blank aus waere der Abruf cross-origin - also gleich laden."""
        connector = _direktabruf_connector()
        page = _FakePage(url="about:blank")
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": []}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        await connector._fetch_once("https://example.invalid/sch?q=1")
        assert navigated == ["https://example.invalid/sch?q=1"]
        assert page.evaluated == []

    @pytest.mark.asyncio()
    async def test_nach_abgewiesenem_abruf_pausiert_der_direktabruf(self):
        """Ein einmal abgewiesener fetch wird nicht sofort wiederholt."""
        connector = _direktabruf_connector()
        page = _FakePage(result=_fetch_ergebnis(status=403))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": []}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        await connector._fetch_once("https://example.invalid/sch?q=1")
        assert len(page.evaluated) == 1
        await connector._fetch_once("https://example.invalid/sch?q=2")
        # Die zweite Anfrage geht direkt über die Navigation.
        assert len(page.evaluated) == 1
        assert navigated == [
            "https://example.invalid/sch?q=1",
            "https://example.invalid/sch?q=2",
        ]

    @pytest.mark.asyncio()
    async def test_pause_ist_abschaltbar(self):
        connector = _direktabruf_connector(demo_inline_pause_seconds=0.0)
        page = _FakePage(result=_fetch_ergebnis(status=403))
        connector._context = _FakeContext([page])

        async def fake_navigation(page, url):
            return {"items": []}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        await connector._fetch_once("https://example.invalid/sch?q=1")
        await connector._fetch_once("https://example.invalid/sch?q=2")
        assert len(page.evaluated) == 2

    @pytest.mark.asyncio()
    async def test_nach_der_pause_versucht_es_der_direktabruf_erneut(self):
        connector = _direktabruf_connector()
        page = _FakePage(result=_fetch_ergebnis(status=403))
        connector._context = _FakeContext([page])
        connector._fetch_by_navigation = (  # type: ignore[assignment]
            lambda page, url: asyncio.sleep(0, result={"items": []})
        )
        await connector._fetch_once("https://example.invalid/sch?q=1")
        connector._inline_pause_until = 0.0
        page.result = _fetch_ergebnis(items=[{"title": "wieder da"}])
        data = await connector._fetch_once("https://example.invalid/sch?q=2")
        assert data["items"][0]["title"] == "wieder da"
        assert len(page.evaluated) == 2

    @pytest.mark.asyncio()
    async def test_abgeschaltet_wird_immer_navigiert(self):
        connector = _direktabruf_connector(demo_inline_fetch=False)
        page = _FakePage(result=_fetch_ergebnis(items=[{"title": "per fetch"}]))
        connector._context = _FakeContext([page])
        navigated: list[str] = []

        async def fake_navigation(page, url):
            navigated.append(url)
            return {"items": [{"title": "per Navigation"}]}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        data = await connector._fetch_once("https://example.invalid/sch?q=1")
        assert connector.inline_fetch_enabled is False
        assert navigated and data["items"][0]["title"] == "per Navigation"


class TestDauerhafterTab:
    @pytest.mark.asyncio()
    async def test_eine_seite_fuer_alle_anfragen(self):
        connector = _direktabruf_connector()
        context = _FakeContext()
        connector._context = context
        page = _FakePage(result=_fetch_ergebnis(items=[{"title": "ok"}]))
        context.pages.append(page)

        for _ in range(3):
            await connector._fetch_once("https://example.invalid/sch?q=1")
        # Genau der vorhandene Tab wird wiederverwendet - kein neuer Tab.
        assert context.opened == 0
        assert connector._page is page
        assert len(page.evaluated) == 3
        # Der Direktabruf bleibt aktiv: keine wachsende Pause durch Fehlschläge.
        assert connector._inline_failures == 0

    @pytest.mark.asyncio()
    async def test_pause_waechst_bei_wiederholter_abweisung(self):
        connector = _direktabruf_connector(demo_inline_pause_seconds=10.0)
        page = _FakePage(result=_fetch_ergebnis(status=403))
        connector._context = _FakeContext([page])
        loop = asyncio.get_running_loop()

        async def fake_navigation(page, url):
            return {"items": []}

        connector._fetch_by_navigation = fake_navigation  # type: ignore[assignment]
        await connector._fetch_once("https://example.invalid/sch?q=1")
        erste_pause = connector._inline_pause_until - loop.time()
        connector._inline_pause_until = 0.0
        await connector._fetch_once("https://example.invalid/sch?q=2")
        zweite_pause = connector._inline_pause_until - loop.time()
        assert connector._inline_failures == 2
        assert 9.0 < erste_pause < 11.0
        assert 29.0 < zweite_pause < 31.0

    @pytest.mark.asyncio()
    async def test_geschlossener_tab_wird_ersetzt(self):
        """Wurde der Tab geschlossen, wird genau einmal ein neuer geöffnet."""
        connector = _direktabruf_connector()
        context = _FakeContext([_FakePage()])
        connector._context = context
        alt = await connector._open_page()
        alt.closed = True
        ersatz = await connector._open_page()
        assert ersatz is not alt
        assert connector._page is ersatz
        assert context.opened == 1
        assert await connector._open_page() is ersatz
        assert context.opened == 1

    @pytest.mark.asyncio()
    async def test_aufraeumen_vergisst_den_tab(self):
        connector = _direktabruf_connector()
        connector._context = _FakeContext([_FakePage()])
        await connector._open_page()
        await connector.aclose()
        assert connector._page is None


class TestMindestabstand:
    """Der Mindestabstand wird als Zeitfenster reserviert (nicht als "Zeit seit
    dem letzten Abruf"). Nur so bleibt er auch bei gleichzeitigen Abrufen
    korrekt: Die Abrufe werden gleichmäßig verteilt, statt in einem Schwall zu
    starten."""

    @pytest.mark.asyncio()
    async def test_zweiter_abruf_wartet_den_abstand_ab(self):
        connector = _Connector(_Settings())
        await connector._pace()  # reserviert das erste Zeitfenster
        started = asyncio.get_running_loop().time()
        await connector._pace()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.04

    @pytest.mark.asyncio()
    async def test_erster_abruf_wartet_nicht(self):
        connector = _Connector(_Settings())
        connector._last_fetch_at = 0.0
        started = asyncio.get_running_loop().time()
        await connector._pace()
        assert asyncio.get_running_loop().time() - started < 0.02

    @pytest.mark.asyncio()
    async def test_gleichzeitige_abrufe_werden_verteilt(self):
        """Drei Abrufe gleichzeitig: sie starten nacheinander im Abstand."""
        connector = _Connector(_Settings())
        started = asyncio.get_running_loop().time()
        awaits = await asyncio.gather(*(connector._pace() for _ in range(3)))
        elapsed = asyncio.get_running_loop().time() - started
        # Der dritte Abruf wartet zwei Abstände ab (2 x 0,05 s).
        assert max(awaits) >= 0.08
        assert elapsed >= 0.08

    @pytest.mark.asyncio()
    async def test_abstand_ist_abschaltbar(self):
        class NoDelay(_Settings):
            demo_min_delay_seconds = 0.0

        connector = _Connector(NoDelay())
        awaits = await asyncio.gather(*(connector._pace() for _ in range(3)))
        assert max(awaits) == 0.0


class TestRuheErkennung:
    @pytest.mark.asyncio()
    async def test_browser_wird_nach_ruhe_geschlossen(self):
        connector = _Connector(_Settings())
        closed: list[bool] = []

        async def fake_ac(close: bool = True):
            if close:
                closed.append(True)
            connector._context = None

        connector._context = object()
        connector._last_fetch_at = asyncio.get_running_loop().time() - 1.0
        connector.aclose = fake_ac  # type: ignore[assignment]

        task = asyncio.create_task(connector._idle_watchdog())
        await asyncio.sleep(0.2)
        assert closed == [True]
        await task

    @pytest.mark.asyncio()
    async def test_ohne_browser_laeuft_der_wachhund_sofort_aus(self):
        connector = _Connector(_Settings())
        connector._context = None
        await asyncio.wait_for(connector._idle_watchdog(), timeout=1.0)


class TestAufraeumen:
    @pytest.mark.asyncio()
    async def test_mehrfaches_schliessen_ist_harmlos(self):
        connector = _Connector(_Settings())
        await connector.aclose()
        await connector.aclose()
        assert connector._context is None

    @pytest.mark.asyncio()
    async def test_ruhe_abschaltung_ist_abschaltbar(self):
        class NoIdle(_Settings):
            demo_idle_seconds = 0

        connector = _Connector(NoIdle())
        connector._schedule_idle_close()
        assert connector._idle_task is None
