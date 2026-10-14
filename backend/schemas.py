"""Pydantic-Schemas der HTTP-API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .categories import CATEGORY_KEYS
from .config import RuntimeSettings

DeliveryMode = Literal["shipping", "pickup", "both"]


class SearchRequest(BaseModel):
    """Alle Eingaben der Suchkonfiguration (Abschnitt 4)."""

    model_config = ConfigDict(extra="forbid")

    # Entweder ein konkreter Produktname (bevorzugt, seit der Vereinfachung
    # der Oberfläche) oder - für Aufrufer, die Kategorien direkt steuern -
    # eine Kategorienliste. Mindestens eines von beidem muss gesetzt sein.
    product: Annotated[str | None, Field(default=None, max_length=120)] = None
    categories: list[str] = Field(default_factory=list)
    # Suche nach den Komponenten der Kauf-Limit-Liste ("Höchstpreise.txt"):
    # Die Werte sind Gruppen-Schlüssel dieser Liste (z. B. "gpu" oder "cpu").
    # Je Komponente gilt ihr eigenes Limit als Preisgrenze.
    price_groups: list[str] = Field(default_factory=list)
    platforms: Annotated[list[str], Field(default_factory=lambda: ["ebay"], min_length=1)]

    max_price: Annotated[float | None, Field(default=None, gt=0, le=100_000)] = None
    price_per_category: dict[str, float] = Field(default_factory=dict)

    delivery: DeliveryMode = "both"
    postal_code: str | None = None
    radius_km: Annotated[float | None, Field(default=50.0, gt=0, le=500)] = 50.0

    days_back: Annotated[int, Field(default=150, ge=1, le=3650)] = 150

    min_condition_rank: Annotated[int | None, Field(default=None, ge=0, le=4)] = None
    only_working: bool = False
    seller_location: str | None = None

    min_deal_score: Annotated[float, Field(default=0.0, ge=0, le=100)] = 0.0
    min_price_advantage_pct: Annotated[float | None, Field(default=None, ge=0, le=100)] = None
    only_new_since_last_run: bool = False
    exclude_problematic: bool = True
    preferred_models: list[str] = Field(default_factory=list)

    max_rounds: Annotated[int | None, Field(default=None, ge=1, le=5)] = None

    @field_validator("product")
    @classmethod
    def _clean_product(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, value: list[str]) -> list[str]:
        unknown = [key for key in value if key not in CATEGORY_KEYS]
        if unknown:
            raise ValueError(f"Unbekannte Kategorien: {', '.join(unknown)}")
        # Reihenfolge stabil halten und Duplikate entfernen.
        return [key for key in CATEGORY_KEYS if key in set(value)]

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, value: list[str]) -> list[str]:
        allowed = {
            "ebay",
            "ebay_web",
            "kleinanzeigen",
            "kleinanzeigen_web",
            "local_import",
        }
        unknown = [key for key in value if key not in allowed]
        if unknown:
            raise ValueError(f"Unbekannte Plattformen: {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("postal_code")
    @classmethod
    def _postal_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned[:5].isdigit() and not cleaned.isalpha():
            raise ValueError("Postleitzahl muss mit Ziffern beginnen (z. B. 10115).")
        return cleaned

    @field_validator("preferred_models")
    @classmethod
    def _clean_models(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()][:30]

    @field_validator("price_groups")
    @classmethod
    def _clean_price_groups(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        # Reihenfolge stabil halten, doppelte Gruppen entfernen.
        return list(dict.fromkeys(cleaned))[:20]

    @model_validator(mode="after")
    def _require_search_scope(self) -> "SearchRequest":
        # Ohne Produktnamen, Kategorie und Preislisten-Gruppe gäbe es keinen
        # Suchbegriff - das wäre ein stiller Lauf ohne Ergebnis.
        if not self.product and not self.categories and not self.price_groups:
            raise ValueError(
                "Bitte einen Produktnamen angeben (oder mindestens eine Kategorie "
                "oder eine Gruppe der Preisliste)."
            )
        return self

    @model_validator(mode="after")
    def _price_list_is_exclusive(self) -> "SearchRequest":
        # Ein Produktname und die Preisliste sind zwei verschiedene
        # Suchaufträge. Beides gleichzeitig wäre nicht nachvollziehbar.
        if self.price_groups and (self.product or self.categories):
            raise ValueError(
                "Entweder einen Produktnamen bzw. Kategorien angeben oder nach "
                "der Preisliste suchen (price_groups) - nicht beides."
            )
        return self

    @model_validator(mode="after")
    def _check_delivery(self) -> "SearchRequest":
        # Nur die reine Abholsuche braucht zwingend einen Standort: ohne ihn
        # lässt sich kein Angebot als "in der Nähe abholbar" einordnen.
        # Bei "Versand und Abholung" ist der Standort optional - fehlt er,
        # wird die Entfernung schlicht nicht berechnet (und nicht erfunden).
        if self.delivery == "pickup" and not self.postal_code:
            raise ValueError(
                "Für die reine Abholsuche wird eine Postleitzahl oder ein Ort benötigt."
            )
        return self


class ListingOut(BaseModel):
    """Angebot in der Ergebnisliste."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    url: str
    title: str
    price: float | None
    shipping_cost: float | None
    shipping_cost_known: bool
    total_price: float | None
    currency: str
    market_value: float | None
    price_advantage_abs: float | None = None
    price_advantage_pct: float | None = None
    deal_score: float | None
    confidence: float | None
    verdict: str | None
    category: str | None
    manufacturer: str | None
    model: str | None
    variant: str | None
    condition_text: str | None
    condition_rank: int | None
    published_at: datetime | None
    published_at_known: bool
    fetched_at: datetime
    last_seen_at: datetime
    location_text: str | None
    postal_code: str | None
    seller_name: str | None
    seller_feedback_percent: float | None
    listing_format: str
    pickup_available: bool
    status: str
    image_url: str | None
    is_favorite: bool
    reasons: list[str] | None = None
    warnings: list[str] | None = None
    market_uncertain: bool = False
    market_method: str | None = None


class PricePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    observed_at: datetime
    price: float | None
    shipping_cost: float | None
    total_price: float | None
    status: str | None


class ListingDetailOut(ListingOut):
    description: str | None = None
    format_evidence: str | None = None
    raw: dict | None = None
    market_basis: dict | None = None
    score_breakdown: dict | None = None
    model_candidates: list | None = None
    price_history: list[PricePoint] = Field(default_factory=list)


class ListingListOut(BaseModel):
    items: list[ListingOut]
    total: int
    limit: int
    offset: int
    sort: str


class QueryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    category: str | None
    term: str
    family: str | None
    round_index: int
    status: str
    pages_fetched: int
    offset_reached: int
    reported_total: int | None
    results_returned: int
    results_usable: int
    excluded_auctions: int
    excluded_unknown_format: int
    excluded_price: int
    new_listings: int
    duplicates: int
    truncated: bool
    error: str | None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: str
    request: dict
    plan: dict | None
    plan_source: str | None
    progress: dict | None
    coverage: dict | None
    errors: list | None
    limitations: list | None
    queries_total: int
    queries_done: int
    result_count: int
    new_count: int
    updated_count: int


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: str
    queries_total: int
    queries_done: int
    result_count: int
    new_count: int
    updated_count: int
    plan_source: str | None


class RunCreatedOut(BaseModel):
    run_id: int
    status: str
    message: str


class ConnectorNotice(BaseModel):
    platform: str
    code: str
    message: str
    hint: str | None = None


class SettingsOut(BaseModel):
    runtime: RuntimeSettings
    environment: dict
    connector_notices: list[ConnectorNotice] = Field(default_factory=list)


class KnowledgeEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    model: str
    manufacturer: str | None
    product_family: str | None
    aliases: list[str]
    model_numbers: list[str]
    variants: list[str]
    currency: str
    good_price_max: float | None
    fair_price_max: float | None
    overpriced_above: float | None
    performance_index: float | None
    known_issues: list[str]
    risk_flags: list[str]
    notes: list[str]
    problematic: bool
    exclude: bool
    source: str
    version: str | None
    weight: float


class KnowledgeSummaryOut(BaseModel):
    entries: int
    identity_only_entries: int
    priced_entries: int
    sources: list[str]
    per_category: dict[str, int]
    loaded_at: datetime
    paths: list[str]
    errors: list[str]
    markdown_notes: list[dict]


class CategoryOut(BaseModel):
    key: str
    label: str
    default_max_price: float
    base_terms: list[str]


class PriceLimitOut(BaseModel):
    """Eine Komponente der Kauf-Limit-Liste."""

    group: str
    group_label: str
    category: str
    name: str
    term: str
    max_price: float


class PriceLimitGroupOut(BaseModel):
    """Eine Gruppe der Kauf-Limit-Liste (z. B. alle Grafikkarten)."""

    key: str
    label: str
    category: str
    count: int
    min_price: float
    max_price: float


class PriceListOut(BaseModel):
    """Kauf-Limits aus "Höchstpreise.txt" samt Gruppen für die Auswahl."""

    available: bool
    path: str
    loaded_at: datetime | None = None
    groups: list[PriceLimitGroupOut] = Field(default_factory=list)
    entries: list[PriceLimitOut] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class PriceLimitSuggestionOut(BaseModel):
    """Empfehlung für das Kauf-Limit einer Komponente.

    Aus den beobachteten Angebotspreisen abgeleitet (günstigstes Zehntel),
    nicht geschätzt. ``suggested_max_price`` ist ``None``, wenn die Datenbasis
    zu dünn ist - dann bleibt das Limit unverändert.
    """

    index: int
    group: str
    group_label: str
    category: str
    name: str
    term: str
    current_max_price: float
    suggested_max_price: float | None = None
    observations: int
    min_total: float | None = None
    q10: float | None = None
    median: float | None = None
    q75: float | None = None
    direction: str
    change_pct: float | None = None
    reason: str


class PriceLimitAdviceOut(BaseModel):
    """Ergebnis des Limit-Beraters (Vorschläge bzw. ausgeführte Änderungen)."""

    generated_at: datetime
    path: str
    min_observations: int
    window_days: int
    target_quantile: float
    max_change_ratio: float
    components: int
    with_data: int
    changes: int
    suggestions: list[PriceLimitSuggestionOut] = Field(default_factory=list)
    summary: str
    disclaimer: str
    applied: int = 0
    backup_path: str | None = None


class ConnectorTestOut(BaseModel):
    """Ergebnis des "Testen"-Knopfes einer Plattform."""

    platform: str
    ok: bool
    message: str
    details: dict = Field(default_factory=dict)


class SystemStatusOut(BaseModel):
    database: dict
    listings: dict
    knowledge: KnowledgeSummaryOut
    connectors: list[dict]
    version: str


class BenchmarkCaseOut(BaseModel):
    name: str
    description: str
    score: float | None
    verdict: str | None
    verdict_label: str | None = None
    confidence: float | None = None
    price_advantage_pct: float | None = None
    expectation: str
    passed: bool
    failures: list[str] = Field(default_factory=list)


class BenchmarkOut(BaseModel):
    cases: list[BenchmarkCaseOut]
    passed: int
    failed: int
    notes: list[str]


class ExplainOut(BaseModel):
    available: bool
    source: str
    message: str | None = None
    summary: str | None = None
    main_arguments: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    suggested_comparisons: list[str] = Field(default_factory=list)
