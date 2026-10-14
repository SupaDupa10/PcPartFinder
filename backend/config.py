"""Konfiguration des Backends.

Zwei Ebenen werden bewusst getrennt:

1. ``Settings`` (pydantic-settings): Infrastruktur und Geheimnisse aus der
   Umgebung / ``.env`` (Datenbank-URL, eBay-Zugangsdaten ...).
   Diese Werte werden NICHT über die UI verändert.

2. ``RuntimeSettings``: vom Benutzer in der Oberfläche aenderbare Werte
   (Score-Gewichte, Limits, Standard-Suchzeitraum ...). Sie werden in SQLite
   gespeichert und überschreiben die Defaults. Geheimnisse gehoeren NICHT
   hierher.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Umgebungsbasierte Bootstrap-Konfiguration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- eBay --------------------------------------------------------------
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_DE"
    ebay_api_base: str = "https://api.ebay.com"
    ebay_max_results_per_query: int = 400

    # --- eBay über die öffentliche Website (ohne Zugangsdaten) -------------
    # Steuert den Browser-Connector, der die normale eBay-Suchseite im lokal
    # installierten Chrome öffnet. Braucht keine API-Keys, ist aber von eBays
    # Nutzungsbedingungen nicht ausdrücklich erlaubt - daher abschaltbar.
    ebay_web_enabled: bool = True
    ebay_web_max_results_per_query: int = 480
    # Zeitlimit je Seitenaufruf in Sekunden (Browser-Start + Laden).
    ebay_web_timeout_seconds: float = 45.0
    # Treffer je Seite (60, 120 oder 240). Größere Seiten liefern dieselbe Liste
    # in deutlich weniger Anfragen. Liefert eBay kleiner als angefragt, richtet
    # sich die Anwendung selbst danach aus (keine Lücke in der Liste).
    ebay_web_page_size: int = 240
    # Gleichzeitige Abrufe. Der Mindestabstand (ebay_web_min_delay_seconds)
    # wird trotzdem eingehalten; die Wartezeit überlappt sich nur mit dem Laden
    # der Seite, statt es zu blockieren. Höhere Werte bringen kaum mehr.
    ebay_web_max_concurrency: int = 2
    # Persistentes Chrome-Profil für die Website-Suche. Echte Cookies aus
    # diesem Ordner senken den Bot-Verdacht erheblich.
    ebay_web_profile_dir: str = "data/ebay_profile"
    # Mindestabstand zwischen zwei Seitenaufrufen (Sekunden). Zu schnelle
    # Folgeabrufe werden von eBay gedrosselt.
    ebay_web_min_delay_seconds: float = 1.5
    # Browser nach so vielen Sekunden ohne Abruf schließen (0 = nie).
    ebay_web_idle_seconds: float = 180.0
    # Zusätzliche Versuche nach einer Bot-Blockade (Pause und frische Cookies).
    ebay_web_block_retries: int = 2
    # Kein sichtbares Fenster: Chrome läuft mit einem echten, aber außerhalb
    # des Bildschirms liegenden und minimierten Fenster. Auf false stellen,
    # um die Suche im Browser mitzusehen (Fehlersuche).
    ebay_web_hidden: bool = True
    # Reines Headless (gar kein Fenster). eBay blockt das häufig mit HTTP 403 -
    # dann bleibt der Suchlauf leer, obwohl er "erfolgreich" war.
    ebay_web_headless: bool = False
    # Trefferliste im bereits geladenen Tab per fetch holen, statt für jede
    # Anfrage eine Seite neu aufzubauen (deutlich schneller, gleiche Treffer).
    # Auf false stellen, um wieder klassisch zu navigieren (Vergleich).
    ebay_web_inline_fetch: bool = True

    # --- Kleinanzeigen -----------------------------------------------------
    kleinanzeigen_enabled: bool = False

    # --- Kleinanzeigen über die öffentliche Website (ohne Zugangsdaten) ---
    # Gleiches Vorgehen wie bei eBay (ebay_web): Der Connector öffnet die
    # normale Kleinanzeigen-Suche im lokalen Chrome und liest die
    # Ergebnisliste. Braucht keine API, ist aber von Kleinanzeigen nicht
    # ausdrücklich erlaubt - daher abschaltbar.
    kleinanzeigen_web_enabled: bool = True
    # Obergrenze je Suchanfrage. Kleinanzeigen liefert fest 25 Treffer je Seite,
    # die Zahl bestimmt also die Seitenzahl: 500 / 25 = 20 Seiten. Sie ist damit
    # der wirksame Deckel dieser Quelle (die größte des Projekts).
    kleinanzeigen_web_max_results_per_query: int = 500
    # Zeitlimit je Seitenaufruf in Sekunden (Browser-Start + Laden).
    kleinanzeigen_web_timeout_seconds: float = 45.0
    # Gleichzeitige Abrufe (siehe ebay_web_max_concurrency). Kleinanzeigen
    # liefert 25 Treffer je Seite; die Seiten werden parallel geholt, der
    # Mindestabstand bleibt dabei erhalten.
    kleinanzeigen_web_max_concurrency: int = 2
    # Kleinanzeigen liefert die Ergebnisliste serverseitig gerendert, ein
    # sichtbares Fenster ist dafür nicht nötig. Fallse die Seite strenger
    # antwortet, hier auf false stellen (dann öffnet sich ein Fenster).
    kleinanzeigen_web_headless: bool = True
    # Sicherheitsnetz, falls Headless auf true/false gedreht wird: Auch mit
    # echtem Fenster bleibt es unsichtbar (außerhalb des Bildschirms).
    kleinanzeigen_web_hidden: bool = True
    # Persistentes Chrome-Profil (eigenes Profil: ein Chrome-Profil kann nur
    # einmal gleichzeitig geöffnet sein).
    kleinanzeigen_web_profile_dir: str = "data/kleinanzeigen_profile"
    # Mindestabstand zwischen zwei Seitenaufrufen (Sekunden).
    kleinanzeigen_web_min_delay_seconds: float = 1.5
    # Browser nach so vielen Sekunden ohne Abruf schließen (0 = nie).
    kleinanzeigen_web_idle_seconds: float = 180.0
    # Zusätzliche Versuche nach einer Bot-Blockade.
    kleinanzeigen_web_block_retries: int = 1
    # Trefferliste im bereits geladenen Tab per fetch holen (siehe ebay_web).
    kleinanzeigen_web_inline_fetch: bool = True

    # --- Lokaler Import ---------------------------------------------------
    # Pfad zu einer JSON-Datei mit selbst beschafften Angebotsdaten.
    # Leer = Connector nicht konfiguriert.
    local_import_path: str = ""

    # --- Server / DB -------------------------------------------------------
    database_url: str = "sqlite:///./data/app.db"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "INFO"

    # --- Preisliste (Kauf-Limits) -----------------------------------------
    # Handgepflegte Liste der Komponenten mit ihrer Schmerzgrenze
    # ("Name = Preis €"). Sie bestimmt, wonach gesucht wird und bis zu
    # welchem Preis gekauft wird. Leer = keine Kauf-Limits.
    price_list_path: str = "Höchstpreise.txt"

    # --- Wissensbasis ------------------------------------------------------
    knowledge_path: str = "knowledge"
    # Optionale, zusätzliche Wissensdatei des Benutzers (Nischenwissen).
    knowledge_user_file: str = ""

    # --- Suchlauf-Limits ---------------------------------------------------
    search_max_variants_per_category: int = 12
    search_max_parallel_requests: int = 4
    # Seiten je Suchanfrage. Die Obergrenze der Plattform
    # (max_results_per_query je Seitengröße) wird zusätzlich eingehalten.
    #
    # Achtung, zwei Einheiten: Diese Zahl zählt *Seiten*, nicht Treffer. Quellen
    # mit kleinen Seiten (Kleinanzeigen: 25) brauchen einen höheren Wert als
    # solche mit großen (eBay-Website: 240). 20 deckt Kleinanzeigen vollständig
    # ab (20 x 25 = 500); für eBay bleibt es wirkungslos, dort greift der
    # Plattformdeckel (480 Treffer = 2 Seiten).
    search_max_pages_per_query: int = 20
    # Seiten, die gleichzeitig geholt werden dürfen. Der Mindestabstand der
    # Connectoren bleibt davon unberührt.
    search_parallel_pages_per_query: int = 3
    search_max_queries_per_run: int = 600
    search_max_rounds: int = 2
    # Zusätzliche Detailabrufe für neue Angebote mit duenner Beschreibung.
    search_max_detail_fetches: int = 8

    # --- Aufbewahrung ------------------------------------------------------
    retention_days: int = 400

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ebay_configured(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Vom Benutzer editierbare Laufzeit-Einstellungen
# ---------------------------------------------------------------------------


class ScoreWeights(BaseModel):
    """Gewichte des deterministischen Deal-Scores.

    Entspricht 1:1 den Bewertungskriterien A-J aus der Anforderung. Die
    Summe wird zur Laufzeit normalisiert, das Verhältnis zueinander zählt.

    Alle Gewichte muessen >= 0 sein: Ein negatives Gewicht würde die
    Normalisierung umkehren und den Score unbrauchbar machen.
    """

    price_advantage: float = Field(default=30.0, ge=0, le=100)  # A. Preisvorteil
    price_performance: float = Field(default=14.0, ge=0, le=100)  # B. Preis-Leistung
    data_quality: float = Field(default=6.0, ge=0, le=100)  # C. Datenqualität
    condition_risk: float = Field(default=18.0, ge=0, le=100)  # D. Zustand und Risiko
    total_cost: float = Field(default=6.0, ge=0, le=100)  # E. Gesamtkosten (Versand)
    freshness: float = Field(default=6.0, ge=0, le=100)  # F. Aktualität der Anzeige
    knowledge_match: float = Field(default=6.0, ge=0, le=100)  # G. Modellbesonderheiten
    location: float = Field(default=5.0, ge=0, le=100)  # H. Standort und Abholung
    market_comparison: float = Field(default=5.0, ge=0, le=100)  # I. Marktvergleich
    information_quality: float = Field(default=4.0, ge=0, le=100)  # J. Informationsqualität


class RuntimeSettings(BaseModel):
    """In der UI aenderbare Einstellungen (in SQLite persistiert)."""

    # Preisformate
    exclude_auctions: bool = True
    exclude_unknown_format: bool = True
    # Auktionen mit separat ausgewiesenem Sofort-Kauf-Preis zulassen?
    allow_mixed_auction_bin: bool = True
    # Limits
    search_max_variants_per_category: int = 12
    search_max_parallel_requests: int = 4
    search_max_pages_per_query: int = 20
    search_parallel_pages_per_query: int = 3
    search_max_queries_per_run: int = 600
    search_max_rounds: int = 2
    search_max_detail_fetches: int = 8
    # Standard-Suchparameter
    default_max_price_per_category: dict[str, float] = Field(default_factory=dict)
    default_days_back: int = 150
    default_platforms: list[str] = Field(default_factory=lambda: ["ebay"])
    default_min_deal_score: float = 0.0
    default_min_condition_rank: int = 0
    # Score
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    # Marktpreis
    market_min_observations: int = 5
    market_reference_window_days: int = 150
    # Kauf-Limits vor jedem Suchlauf aus den beobachteten Preisen nachführen.
    # Standardmäßig aus: Es verändert die handgepflegte Liste, deshalb ist der
    # Weg über "Vorschläge ansehen und übernehmen" der Standard.
    auto_update_price_limits: bool = False
    # Aufbewahrung
    retention_days: int = 400
    # Logging
    log_level: str = "INFO"


def settings_to_runtime(settings: Settings) -> RuntimeSettings:
    """Leitet sinnvolle Laufzeit-Defaults aus der Umgebungskonfiguration ab."""
    return RuntimeSettings(
        search_max_variants_per_category=settings.search_max_variants_per_category,
        search_max_parallel_requests=settings.search_max_parallel_requests,
        search_max_pages_per_query=settings.search_max_pages_per_query,
        search_parallel_pages_per_query=settings.search_parallel_pages_per_query,
        search_max_queries_per_run=settings.search_max_queries_per_run,
        search_max_rounds=settings.search_max_rounds,
        search_max_detail_fetches=settings.search_max_detail_fetches,
        retention_days=settings.retention_days,
        log_level=settings.log_level,
    )


SECRET_FIELDS: tuple[str, ...] = ("ebay_client_id", "ebay_client_secret")


def redact(settings: Settings) -> dict[str, Any]:
    """Gibt die Bootstrap-Konfiguration ohne Geheimnisse zurueck."""
    data = settings.model_dump()
    for field in SECRET_FIELDS:
        value = data.get(field)
        if value:
            data[field] = "***gesetzt***"
        else:
            data[field] = ""
    return data
