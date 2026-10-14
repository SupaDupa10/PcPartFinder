"""Interpretation eines freien Produktnamens (z. B. "RTX 3080 Ti").

In der Oberfläche gibt der Benutzer keinen Kategorie-Schlüssel mehr an,
sondern nur noch einen konkreten Produktnamen und ein Budget. Diese Datei
leitet daraus deterministisch und ohne Netzwerkzugriff ab:

* welche Kategorien (``categories.py``) in Frage kommen,
* welche Modelle der lokalen Wissensbasis dazu passen (Suchvarianten),
* welche Bestandteile des Namens ein Angebot enthalten muss, damit es
  überhaupt als Treffer zu diesem Produkt zählt (Relevanzfilter).

Der Suchlauf ist damit vollständig deterministisch und ohne KI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..categories import CATEGORIES, get_category
from ..knowledge.base import KnowledgeBase
from .normalize import compact_key, tokenize

# Ohne erkennbaren Bezug wird in dieser Kategorie gesucht. Sie ist bewusst
# breit gehalten, damit auch exotische Produkte durchsuchbar bleiben.
FALLBACK_CATEGORY = "other"

# Höchstens so viele Kategorien werden durchsucht. Mehr Kategorien streuen
# die Ergebnisse nur, ohne die Abdeckung zu verbessern.
MAX_CATEGORIES = 2
MAX_MATCHED_MODELS = 8

# Wörter, die für den Relevanzfilter nichts aussagen: Sie kommen in fast
# jeder Anzeige vor und würden jedes Angebot durchlassen.
GENERIC_TOKENS = frozenset(
    {
        "gpu",
        "grafikkarte",
        "grafik",
        "cpu",
        "prozessor",
        "ram",
        "arbeitsspeicher",
        "speicher",
        "mainboard",
        "motherboard",
        "board",
        "ssd",
        "festplatte",
        "hdd",
        "nvme",
        "netzteil",
        "psu",
        "gehaeuse",
        "kuehler",
        "luefter",
        "komponente",
        "komponenten",
        "zubehoer",
        "pc",
        "computer",
        "rechner",
        "gaming",
        "gebraucht",
        "karte",
        "neu",
        "mit",
        "ohne",
        "original",
        "inkl",
        "und",
        "fuer",
        "der",
        "die",
        "das",
        "zum",
        "aus",
        "von",
        "bis",
        "pro",
    }
)


@dataclass(frozen=True)
class ProductInterpretation:
    """Ergebnis der Produktnamen-Deutung."""

    product: str
    categories: list[str]
    matched_models: list[str] = field(default_factory=list)
    required_tokens: list[str] = field(default_factory=list)
    # "model"  - der Name ist/war ein bekanntes Modell der Wissensbasis
    # "category" - nur die Kategorie war ableitbar
    # "none"   - kein Bezug erkennbar (breite Suche)
    match_quality: str = "none"

    @property
    def required_min_matches(self) -> int:
        """Wie viele der ``required_tokens`` ein Angebot mindestens zeigen muss.

        Bei zwei oder mehr Bestandteilen genügt die doppelte Mindestzahl, damit
        Schreibvarianten ("GTX 1080" vs. "GTX1080 Ti") nicht herausfallen. Bei
        genau einem Bestandteil ist dieser zwingend.
        """
        if not self.required_tokens:
            return 0
        return min(2, len(self.required_tokens))

    def to_dict(self) -> dict:
        return {
            "product": self.product,
            "categories": list(self.categories),
            "matched_models": list(self.matched_models),
            "required_tokens": list(self.required_tokens),
            "required_min_matches": self.required_min_matches,
            "match_quality": self.match_quality,
        }


def product_tokens(product: str | None) -> list[str]:
    """Aussagekräftige Bestandteile eines Produktnamens.

    "GPU Grafikkarte RTX 3080 Ti gebraucht" -> ["rtx", "3080", "ti"]
    """
    tokens: list[str] = []
    for token in tokenize(product, drop_noise=True):
        if token in GENERIC_TOKENS or len(token) < 2:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:6]


def _category_base_scores(text_compact: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for category in CATEGORIES:
        score = 0.0
        for term in category.base_terms:
            key = compact_key(term)
            # Erst ab drei Zeichen ist ein Teilstring-Treffer aussagekräftig
            # ("rx" steckt sonst in jedem "RTX..."-Titel).
            if len(key) < 3 or key not in text_compact:
                continue
            score += 2.0 if len(key) >= 4 else 0.5
        if score:
            scores[category.key] = score
    return scores


def interpret_product(
    product: str | None, knowledge: KnowledgeBase | None = None
) -> ProductInterpretation | None:
    """Deutet einen freien Produktnamen."""
    raw = (product or "").strip()
    if not raw:
        return None

    text_compact = compact_key(raw)
    tokens = product_tokens(raw)
    scores = _category_base_scores(text_compact)

    exact_models: list[str] = []
    related_models: list[str] = []
    orders: dict[str, int] = {}
    if knowledge is not None and text_compact:
        for order, entry in enumerate(knowledge.entries):
            best = 0.0
            for term in entry.match_terms():
                key = compact_key(term)
                if len(key) < 3:
                    continue
                if key == text_compact:
                    best = max(best, 6.0)
                elif key in text_compact:
                    # Der Produktname nennt das Modell selbst
                    # ("RTX 3080 Ti Gaming X" enthält "rtx 3080 ti").
                    best = max(best, 4.0 + min(len(key), 12) / 100)
                elif text_compact in key or (tokens and all(t in key for t in tokens)):
                    # Der Name ist ein Ausschnitt eines bekannten Modells
                    # ("RTX 3080" -> "RTX 3080 Ti", "RTX 3080 10GB").
                    best = max(best, 3.0 + min(len(key), 12) / 100)
            if best >= 4.0:
                if entry.model not in exact_models:
                    exact_models.append(entry.model)
                scores[entry.category] = scores.get(entry.category, 0.0) + 8.0
                orders.setdefault(entry.category, order)
            elif best >= 2.5:
                if entry.model not in related_models:
                    related_models.append(entry.model)
                scores[entry.category] = scores.get(entry.category, 0.0) + 3.0
                orders.setdefault(entry.category, order)

    if not exact_models and not related_models and not scores:
        # Kein Bezug zu bekannter Hardware: breit in "other" suchen, aber den
        # Produktnamen selbstverständlich unverändert verwenden.
        return ProductInterpretation(
            product=raw,
            categories=[FALLBACK_CATEGORY],
            matched_models=[],
            required_tokens=tokens,
            match_quality="none",
        )

    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], orders.get(item[0], 10_000), item[0]),
    )
    categories = [key for key, _score in ranked if get_category(key) is not None]
    categories = categories[:MAX_CATEGORIES] or [FALLBACK_CATEGORY]

    # Exakte Treffer zuerst ("RTX 3080" schlägt "RTX 3080 Ti 12GB").
    matched: list[str] = []
    for model in [*exact_models, *related_models]:
        if model not in matched:
            matched.append(model)
    matched = matched[:MAX_MATCHED_MODELS]

    return ProductInterpretation(
        product=raw,
        categories=categories,
        matched_models=matched,
        required_tokens=tokens,
        match_quality="model" if exact_models else "category",
    )


__all__ = [
    "FALLBACK_CATEGORY",
    "GENERIC_TOKENS",
    "ProductInterpretation",
    "interpret_product",
    "product_tokens",
]
