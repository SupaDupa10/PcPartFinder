"""Loader und Abfragelogik der lokalen Hardware-Wissensbasis (Abschnitt 9).

Unterstützte Formate: YAML (``.yaml``/``.yml``) und JSON (``.json``).
Markdown (``.md``) wird für allgemeine Hinweise geladen, aber nicht als
Preisquelle verwendet.

Grundsaetze:
* Es werden niemals fehlende Preise erfunden. Fehlt ein Wert, bleibt er ``None``.
* Manuell gepflegtes Preiswissen wird getrennt von beobachteten Marktpreisen
  gehalten (siehe ``pricing/market.py``).
* Die Wissensbasis ist ohne Codeaenderung erweiterbar und wird von Suchplaner
  und Deal-Bewertung genutzt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from ..domain.normalize import compact_key, match_key_contains, normalize_text

logger = logging.getLogger(__name__)

KNOWLEDGE_SOURCE_MANUAL = "manual_knowledge_base"
KNOWLEDGE_SOURCE_SHIPPED = "shipped_catalog"


class KnowledgeEntry(BaseModel):
    """Ein Eintrag der Wissensbasis.

    Identitätsfelder (``model``, ``aliases``, ``model_numbers``, ``variants``)
    werden zur Hardware-Erkennung genutzt, Preisfelder zur Bewertung.
    Unbekannte Zusatzfelder sind erlaubt und werden unter ``extra`` gesichert.
    """

    model_config = {"extra": "allow"}

    category: str
    model: str
    manufacturer: str | None = None
    product_family: str | None = None
    aliases: list[str] = Field(default_factory=list)
    model_numbers: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)

    currency: str = "EUR"

    # Manuell gepflegte Richtwerte (nur setzen, wenn tatsächlich bekannt!)
    good_price_max: float | None = None
    fair_price_max: float | None = None
    overpriced_above: float | None = None
    # Optionaler, vom Benutzer gepflegter Leistungsindex (KEINE KI-Schätzung)
    performance_index: float | None = None
    performance_index_note: str | None = None

    known_issues: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # True = vom Benutzer als problematisch markiert (Standardfilter kann greifen)
    problematic: bool = False
    # True = generell vom Scoring ausschließen
    exclude: bool = False

    # Herkunft und Gewichtung (Abschnitt 9: Versionierung/Gewichtung)
    source: str = KNOWLEDGE_SOURCE_MANUAL
    version: str | None = None
    weight: float = 1.0

    @field_validator("aliases", "model_numbers", "variants", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    @field_validator("known_issues", "risk_flags", "notes", mode="before")
    @classmethod
    def _coerce_text_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    @property
    def is_identity_only(self) -> bool:
        return (
            self.good_price_max is None
            and self.fair_price_max is None
            and self.overpriced_above is None
        )

    def match_terms(self) -> list[str]:
        """Alle Schreibweisen, unter denen der Eintrag erkannt werden soll."""
        terms: list[str] = [self.model, *self.aliases, *self.model_numbers]
        if self.manufacturer:
            terms.append(f"{self.manufacturer} {self.model}")
        return [t for t in dict.fromkeys(terms) if t and t.strip()]


@dataclass
class KnowledgePriceReference:
    """Preisrichtwert aus der Wissensbasis."""

    model: str
    category: str
    currency: str
    product_family: str | None
    manufacturer: str | None
    good_price_max: float | None
    fair_price_max: float | None
    overpriced_above: float | None
    source: str
    version: str | None
    weight: float
    notes: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    problematic: bool = False
    exclude: bool = False

    def target_value(self) -> float | None:
        """Zentraler Zielwert für den Preisvergleich.

        Priorität: 'fair' (üblicher Marktpreis) > 'good' > 'overpriced'.
        """
        if self.fair_price_max is not None:
            return self.fair_price_max
        if self.good_price_max is not None:
            return self.good_price_max
        if self.overpriced_above is not None:
            return self.overpriced_above
        return None


@dataclass
class KnowledgeSummary:
    entries: int
    identity_only_entries: int
    priced_entries: int
    sources: list[str]
    per_category: dict[str, int]
    loaded_at: datetime
    paths: list[str]
    errors: list[str]
    markdown_notes_available: bool


def _load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text)
    if path.suffix.lower() == ".json":
        return json.loads(text)
    raise ValueError(f"Nicht unterstütztes Format: {path.suffix}")


def _extract_entries(document: Any) -> list[dict[str, Any]]:
    """Akzeptiert sowohl eine Liste als auch {'entries': [...]}."""
    if document is None:
        return []
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if isinstance(document, dict):
        for key in ("entries", "models", "items", "hardware"):
            value = document.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # Einzelner Eintrag
        if "model" in document and "category" in document:
            return [document]
    return []


class KnowledgeBase:
    """In-Memory-Wissensbasis mit Reload-Unterstützung."""

    def __init__(self) -> None:
        self._entries: list[KnowledgeEntry] = []
        self._by_category: dict[str, list[KnowledgeEntry]] = {}
        self._markdown: list[tuple[str, str]] = []
        self._errors: list[str] = []
        self._paths: list[str] = []
        self._loaded_at: datetime = datetime.now(timezone.utc)

    # -- Laden --------------------------------------------------------------

    def load(self, paths: list[str | Path]) -> KnowledgeSummary:
        self._entries = []
        self._by_category = {}
        self._markdown = []
        self._errors = []
        self._paths = []

        files: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if not raw:
                continue
            if path.is_dir():
                for pattern in ("*.yaml", "*.yml", "*.json", "*.md"):
                    files.extend(sorted(path.glob(pattern)))
            elif path.is_file():
                files.append(path)
            else:
                self._errors.append(f"Wissensquelle nicht gefunden: {path}")

        for file_path in files:
            self._paths.append(str(file_path))
            try:
                if file_path.suffix.lower() == ".md":
                    self._markdown.append(
                        (file_path.name, file_path.read_text(encoding="utf-8"))
                    )
                    continue
                document = _load_document(file_path)
                raw_entries = _extract_entries(document)
                default_version = None
                if isinstance(document, dict):
                    default_version = document.get("version")
                for index, raw_entry in enumerate(raw_entries):
                    # Jeder Eintrag wird einzeln geprüft: Ein Tippfehler in einer
                    # Zeile darf nicht die gesamte Wissensdatei unbrauchbar machen.
                    try:
                        entry = KnowledgeEntry.model_validate(raw_entry)
                    except Exception as exc:
                        label = raw_entry.get("model") or f"Eintrag {index + 1}"
                        message = (
                            f"{file_path.name}: {label}: {type(exc).__name__}: {exc}"
                        )
                        logger.warning("Wissenseintrag fehlerhaft - %s", message)
                        self._errors.append(message)
                        continue
                    if entry.version is None and default_version:
                        entry.version = str(default_version)
                    self._entries.append(entry)
            except Exception as exc:  # Einzelne defekte Datei bricht nichts ab
                message = f"{file_path.name}: {type(exc).__name__}: {exc}"
                logger.warning("Wissensdatei fehlerhaft - %s", message)
                self._errors.append(message)

        for entry in self._entries:
            self._by_category.setdefault(entry.category, []).append(entry)

        self._loaded_at = datetime.now(timezone.utc)
        return self.summary()

    def ensure_loaded(self, paths: list[str | Path]) -> KnowledgeSummary:
        if not self._paths and not self._errors:
            return self.load(paths)
        return self.summary()

    # -- Abfragen -----------------------------------------------------------

    @property
    def entries(self) -> list[KnowledgeEntry]:
        return list(self._entries)

    def entries_for_category(self, category: str) -> list[KnowledgeEntry]:
        return list(self._by_category.get(category, []))

    def markdown_notes(self) -> list[tuple[str, str]]:
        return list(self._markdown)

    def summary(self) -> KnowledgeSummary:
        priced = sum(1 for e in self._entries if not e.is_identity_only)
        sources: list[str] = []
        for entry in self._entries:
            if entry.source not in sources:
                sources.append(entry.source)
        return KnowledgeSummary(
            entries=len(self._entries),
            identity_only_entries=len(self._entries) - priced,
            priced_entries=priced,
            sources=sources,
            per_category={k: len(v) for k, v in sorted(self._by_category.items())},
            loaded_at=self._loaded_at,
            paths=list(self._paths),
            errors=list(self._errors),
            markdown_notes_available=bool(self._markdown),
        )

    def find_price_reference(self, category: str, model: str) -> KnowledgePriceReference | None:
        """Sucht einen Preisrichtwert anhand Kategorie + Modellbezeichnung."""
        candidates = self.entries_for_category(category) or self._entries
        target = compact_key(model)
        if not target:
            return None
        best: tuple[int, KnowledgeEntry] | None = None
        for entry in candidates:
            for term in entry.match_terms():
                key = compact_key(term)
                if key and key == target:
                    score = len(key) + 100  # exakte Uebereinstimmung bevorzugt
                elif match_key_contains(model, term, min_len=3):
                    score = len(key)
                else:
                    continue
                if best is None or score > best[0]:
                    best = (score, entry)
        if best is None:
            return None
        entry = best[1]
        return KnowledgePriceReference(
            model=entry.model,
            category=entry.category,
            currency=entry.currency,
            product_family=entry.product_family,
            manufacturer=entry.manufacturer,
            good_price_max=entry.good_price_max,
            fair_price_max=entry.fair_price_max,
            overpriced_above=entry.overpriced_above,
            source=entry.source,
            version=entry.version,
            weight=entry.weight,
            notes=list(entry.notes),
            known_issues=list(entry.known_issues),
            problematic=entry.problematic,
            exclude=entry.exclude,
        )

    def find_entry(self, category: str, model: str) -> KnowledgeEntry | None:
        target = compact_key(model)
        if not target:
            return None
        for entry in self.entries_for_category(category):
            for term in entry.match_terms():
                if compact_key(term) == target:
                    return entry
        return None

    def search(self, query: str | None = None, category: str | None = None) -> list[KnowledgeEntry]:
        entries = self._by_category.get(category, []) if category else self._entries
        if not query:
            return list(entries)
        needle = normalize_text(query)
        result = []
        for entry in entries:
            haystack = " ".join(
                [entry.model, entry.manufacturer or "", *entry.aliases, *entry.model_numbers]
            )
            if needle in normalize_text(haystack):
                result.append(entry)
        return result

    def preferred_terms(self, category: str) -> list[str]:
        """Vom Benutzer gepflegte Bevorzugungen (``weight`` > 1) für den Suchplaner."""
        result = []
        for entry in self.entries_for_category(category):
            if entry.weight and entry.weight > 1.0:
                result.extend(entry.match_terms())
        return list(dict.fromkeys(result))

    def excluded_terms(self, category: str) -> list[str]:
        """Als problematisch markierte Modelle (Ausschlussbegriffe)."""
        result = []
        for entry in self.entries_for_category(category):
            if entry.problematic or entry.exclude:
                result.extend(entry.match_terms())
        return list(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# Globale Instanz
# ---------------------------------------------------------------------------

_default_base: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    global _default_base
    if _default_base is None:
        _default_base = KnowledgeBase()
    return _default_base


def reset_knowledge_base() -> None:
    """Nur für Tests."""
    global _default_base
    _default_base = None
