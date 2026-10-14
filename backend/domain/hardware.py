"""Hardware-Erkennung und technische Einordnung (Abschnitt 11).

Ziel: Ein Angebot mit vollstaendigem Produktnamen, eines mit nur einer
Modellnummer und eines mit einem umgangssprachlichen Namen sollen moeglichst
demselben Hardwaremodell zugeordnet werden.

Zwei Erkenntnisquellen werden kombiniert:

1. Die lokale Wissensbasis (``knowledge/``) - explizite Modell-Identitaeten
   inkl. Aliase, Modellnummern und Varianten. Hohe Zuversicht.
2. Strukturelle Mustererkennung im Anzeigentext - extrahiert die Modell-
   bezeichnung so, wie sie in der Anzeige steht. Keine erfundenen Daten,
   aber geringere Zuversicht.

Ist die genaue Variante nicht bestimmbar, werden mehrere Moeglichkeiten mit
Unsicherheit zurueckgegeben (``candidates``) statt einer geratenen Zuordnung.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..knowledge.base import KnowledgeBase, KnowledgeEntry
from .normalize import (
    TOKEN_DELIM,
    compact_key,
    match_key_contains,
    normalize_text,
    tokenize,
)

SOURCE_KNOWLEDGE_BASE = "knowledge_base"
SOURCE_PATTERN = "pattern"
SOURCE_NONE = "none"

# Generische Herstellerliste (reine Identitaetsdaten, keine Spezifikationen).
KNOWN_BRANDS: tuple[str, ...] = (
    "asus",
    "msi",
    "gigabyte",
    "aorus",
    "zotac",
    "palit",
    "gainward",
    "inno3d",
    "evga",
    "sapphire",
    "powercolor",
    "xfx",
    "asrock",
    "biostar",
    "fractal design",
    "be quiet",
    "bequiet",
    "seasonic",
    "corsair",
    "cooler master",
    "noctua",
    "arctic",
    "thermalright",
    "alphacool",
    "ekwb",
    "g.skill",
    "gskill",
    "kingston",
    "crucial",
    "samsung",
    "western digital",
    "seagate",
    "sandisk",
    "adata",
    "teamgroup",
    "lexar",
    "intenso",
    "lenovo",
    "dell",
    "hp",
    "acer",
    "amd",
    "nvidia",
    "intel",
    "qualcomm",
)

# Strukturelle Muster. Diese Muster erfinden nichts - sie lesen nur ab, was
# im Text steht. Reihenfolge = Prioritaet.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gpu", re.compile(r"\b(rtx|gtx|gts)\s?-?\s?(\d{3,4})\s?-?\s?(ti|super)?\b", re.I)),
    ("gpu", re.compile(r"\b(rx)\s?-?\s?(\d{3,4})\s?-?\s?(xtx|xt|gre|m)?\b", re.I)),
    ("gpu", re.compile(r"\b(arc)\s?-?\s?([ab])\s?-?\s?(\d{3})\b", re.I)),
    ("cpu", re.compile(r"\b(i3|i5|i7|i9)\s?-?\s?(\d{4,5})\s?([a-z]{1,2})?\b", re.I)),
    ("cpu", re.compile(r"\bryzen\s?-?\s?([3579])\s?-?\s?(\d{3,4})\s?([a-z]{0,3})?\b", re.I)),
    ("cpu", re.compile(r"\bthreadripper\s?-?\s?(\d{4,5})\s?([a-z]{0,2})?\b", re.I)),
    ("mainboard", re.compile(r"\b([bxzha]\d{3})\s?-?\s?([a-z0-9]{2,8})\b", re.I)),
    ("ram", re.compile(r"\bddr([345])\b", re.I)),
    ("psu", re.compile(r"\b(\d{3,4})\s?w(att)?\b", re.I)),
)

# Kapazitaetsangaben: unterscheiden Varianten (z. B. 8 GB vs. 12 GB VRAM).
_CAPACITY_RE = re.compile(r"\b(\d{1,4})\s?(gb|tb)\b", re.I)

# Ausstattungsmerkmale, die Preisunterschiede innerhalb einer Modellreihe
# erklaeren. 'refurbished' fehlt hier bewusst - das ist ein Zustand und wird
# bereits ueber den Zustandsrang erfasst.
_VARIANT_QUALIFIERS: tuple[str, ...] = (
    "ti",
    "super",
    "xtx",
    "xt",
    "gre",
    "oc",
    "founders edition",
    "watercooled",
    "aio",
    "low profile",
    "mini",
    "dual",
    "triple",
)


@dataclass
class ModelCandidate:
    model: str
    category: str
    manufacturer: str | None = None
    product_family: str | None = None
    score: float = 0.0
    source: str = SOURCE_KNOWLEDGE_BASE
    entry: KnowledgeEntry | None = None
    # Spezifitaet des Treffers = Laenge der passenden Schreibweise. Damit
    # gewinnt 'RTX 3080 Ti' gegenueber 'RTX 3080', wenn beide passen.
    specificity: int = 0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "category": self.category,
            "manufacturer": self.manufacturer,
            "product_family": self.product_family,
            "confidence": round(min(1.0, max(0.0, self.score)), 3),
            "source": self.source,
        }


@dataclass
class HardwareMatch:
    category: str | None = None
    manufacturer: str | None = None
    product_family: str | None = None
    model: str | None = None
    variant: str | None = None
    confidence: float = 0.0
    source: str = SOURCE_NONE
    candidates: list[ModelCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    entry: KnowledgeEntry | None = None

    @property
    def ambiguous(self) -> bool:
        return models_ambiguous([c.model for c in self.candidates])

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "manufacturer": self.manufacturer,
            "product_family": self.product_family,
            "model": self.model,
            "variant": self.variant,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "candidates": [c.to_dict() for c in self.candidates],
            "notes": list(self.notes),
        }


def detect_manufacturer(text: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    tokens = tokenize(text)
    for brand in KNOWN_BRANDS:
        if brand in normalized or compact_in(brand, tokens):
            return brand.title()
    return None


def compact_in(brand: str, tokens: list[str]) -> bool:
    brand_tokens = [t for t in re.split(r"[^a-z0-9]+", brand) if t]
    if len(brand_tokens) == 1:
        return brand_tokens[0] in tokens
    joined = "".join(tokens)
    return "".join(brand_tokens) in joined


def detect_capacity_variant(text: str | None) -> str | None:
    match = _CAPACITY_RE.search(text or "")
    if not match:
        return None
    return f"{match.group(1)} {match.group(2).upper()}"


def contains_word_sequence(text: str | None, phrase: str | None) -> bool:
    """Exakte Wort(folgen)-Pruefung ohne Teilstring-Rueckfall.

    Notwendig fuer kurze Variantenkuerzel: 'ti' darf nicht in 'edition'
    gefunden werden, 'fe' nicht in 'profile'.
    """
    hay_tokens = tokenize(text)
    needle_tokens = tokenize(phrase)
    if not hay_tokens or not needle_tokens:
        return False
    hay = TOKEN_DELIM + TOKEN_DELIM.join(hay_tokens) + TOKEN_DELIM
    needle = TOKEN_DELIM + TOKEN_DELIM.join(needle_tokens) + TOKEN_DELIM
    return needle in hay


def _variant_from_entry(entry: KnowledgeEntry, text: str) -> str | None:
    for variant in entry.variants:
        if contains_word_sequence(text, variant):
            return variant
    return None


def _qualifier_from_entry(text: str) -> str | None:
    for qualifier in _VARIANT_QUALIFIERS:
        if contains_word_sequence(text, qualifier):
            return qualifier.upper() if len(qualifier) <= 3 else qualifier.title()
    return None


def models_ambiguous(models: list[str | None]) -> bool:
    """Konkurrieren inhaltlich unterschiedliche Modelle um denselben Text?

    "RTX 3080" und "RTX 3080 Ti" sind keine Konkurrenz, sondern die groebere
    und die genauere Bezeichnung derselben Reihe. Erst wenn kein Kandidat eine
    Verfeinerung der genauesten Bezeichnung ist, gilt die Zuordnung als
    mehrdeutig.
    """
    keys = [compact_key(model) for model in models if model]
    if len(keys) < 2:
        return False
    most_specific = max(keys, key=len)
    return any(key != most_specific and key not in most_specific for key in keys)


def _is_distinct_model(candidate: ModelCandidate, best: ModelCandidate) -> bool:
    """Ist ``candidate`` ein anderes Modell oder nur die ungenauere Schreibweise?

    "RTX 3080" ist gegenueber "RTX 3080 Ti" kein konkurrierendes Modell,
    sondern die weniger spezifische Bezeichnung derselben Modellreihe. Solche
    Treffer duerfen die Bewertungssicherheit nicht senken.
    """
    if candidate.category != best.category:
        return True
    candidate_key = compact_key(candidate.model)
    best_key = compact_key(best.model)
    if not candidate_key or not best_key:
        return True
    return candidate_key not in best_key


class HardwareResolver:
    """Ordnet Anzeigentexte Hardwaremodellen zu."""

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self.knowledge = knowledge

    # -- Oeffentliche API --------------------------------------------------

    def resolve(
        self,
        title: str | None,
        description: str | None = None,
        category_hint: str | None = None,
        candidate_categories: list[str] | None = None,
    ) -> HardwareMatch:
        title_text = title or ""
        description_text = description or ""
        combined = f"{title_text}\n{description_text}".strip()

        if category_hint:
            categories = [category_hint]
        elif candidate_categories:
            categories = list(candidate_categories)
        else:
            categories = list(self.knowledge.summary().per_category.keys())

        scored = self._score_entries(title_text, description_text, categories)
        # Erst nach Fundstelle (Titel vor Beschreibung), dann nach Spezifitaet:
        # 'RTX 3080 Ti' schlaegt 'RTX 3080', wenn beide im Text stehen.
        scored.sort(key=lambda c: (c.score, c.specificity), reverse=True)
        scored = scored[:3]

        if scored:
            best = scored[0]
            confidence = self._confidence(scored)
            # Reihenfolge: explizit gepflegte Varianten der Wissensbasis,
            # dann Kapazitaet (VRAM/RAM/SSD-Groesse) - sie erklaert die
            # groessten Preisunterschiede -, dann Ausstattungsmerkmale.
            variant = None
            if best.entry:
                variant = _variant_from_entry(best.entry, combined)
            if variant is None:
                variant = detect_capacity_variant(combined)
            if variant is None and best.entry:
                variant = _qualifier_from_entry(combined)
            # Eine "Variante", die schon im Modellnamen steht ("RTX 4070 Ti" +
            # "TI"), ist keine zusaetzliche Information und wird nicht doppelt
            # angezeigt.
            if variant and compact_key(variant) in compact_key(best.model):
                variant = None

            competitors = [c for c in scored[1:] if _is_distinct_model(c, best)]
            notes: list[str] = []
            if (
                competitors
                and competitors[0].score >= best.score * 0.8
                and competitors[0].specificity >= best.specificity * 0.8
            ):
                notes.append(
                    "Mehrere Modelle passen zum Text: "
                    + ", ".join(c.model for c in competitors)
                )
            if best.entry and best.entry.is_identity_only:
                notes.append("Modelldaten aus Wissensbasis, keine Preisrichtwerte hinterlegt")
            return HardwareMatch(
                category=best.category,
                manufacturer=best.manufacturer or detect_manufacturer(title_text),
                product_family=best.product_family,
                model=best.model,
                variant=variant,
                confidence=confidence,
                source=SOURCE_KNOWLEDGE_BASE,
                candidates=scored,
                notes=notes,
                entry=best.entry,
            )

        return self._pattern_fallback(title_text, description_text, category_hint)

    # -- Interne Schritte --------------------------------------------------

    def _score_entries(
        self, title: str, description: str, categories: list[str]
    ) -> list[ModelCandidate]:
        candidates: list[ModelCandidate] = []
        for category in categories:
            for entry in self.knowledge.entries_for_category(category):
                if entry.exclude:
                    continue
                best_score = 0.0
                best_specificity = 0
                for term in entry.match_terms():
                    term_length = len(term.replace(" ", ""))
                    if match_key_contains(title, term):
                        if 1.0 > best_score or term_length > best_specificity:
                            best_score = max(best_score, 1.0)
                            best_specificity = max(best_specificity, term_length)
                    elif description and match_key_contains(description, term):
                        if best_score < 0.6:
                            best_score = 0.6
                        best_specificity = max(best_specificity, term_length)
                if best_score > 0:
                    candidates.append(
                        ModelCandidate(
                            model=entry.model,
                            category=entry.category,
                            manufacturer=entry.manufacturer,
                            product_family=entry.product_family,
                            score=best_score,
                            source=SOURCE_KNOWLEDGE_BASE,
                            entry=entry,
                            specificity=best_specificity,
                        )
                    )
        return candidates

    def _confidence(self, scored: list[ModelCandidate]) -> float:
        best = scored[0]
        base = 0.85 if best.score >= 1.0 else 0.6
        runners_up = [c for c in scored[1:] if _is_distinct_model(c, best)]
        if runners_up:
            runner_up = runners_up[0].score
            margin = best.score - runner_up
            if margin <= 0:
                base *= 0.55
            elif margin < 0.2:
                base *= 0.8
        return round(min(0.97, base), 3)

    def _pattern_fallback(
        self, title: str, description: str, category_hint: str | None
    ) -> HardwareMatch:
        text = title or description
        if not text:
            return HardwareMatch(source=SOURCE_NONE, notes=["Kein erkennbarer Modelltext"])

        for category, pattern in _PATTERNS:
            if category_hint and category != category_hint and category_hint != "other":
                continue
            match = pattern.search(text)
            if not match:
                continue
            groups = [g for g in match.groups() if g]
            model = " ".join(g.upper() for g in groups).strip()
            if not model:
                continue
            candidate = ModelCandidate(
                model=model,
                category=category,
                manufacturer=detect_manufacturer(text),
                score=0.45,
                source=SOURCE_PATTERN,
            )
            variant = detect_capacity_variant(text)
            return HardwareMatch(
                category=category,
                manufacturer=candidate.manufacturer,
                model=model,
                variant=variant,
                confidence=0.45,
                source=SOURCE_PATTERN,
                candidates=[candidate],
                notes=[
                    "Modell aus Anzeigentext abgeleitet (kein Wissensbasis-Eintrag) - "
                    "Zuordnung prüfen"
                ],
            )

        return HardwareMatch(
            source=SOURCE_NONE,
            notes=["Kein Hardwaremodell sicher erkennbar"],
        )
