"""Mehrstufige Suchplanung (Abschnitt 5).

Der Suchplan wird vollständig deterministisch erzeugt - ohne KI. Quellen:

1. Kategorie-Basisbegriffe (``categories.py``).
2. Modell-Identitäten aus der lokalen Wissensbasis, gefiltert auf das Budget.
3. Strukturelle Schreibvarianten (mit/ohne Leerzeichen).
4. Bei einer Suche über die Kauf-Limit-Liste: eine Anfrage je Komponente.

Familien (``SEARCH_FAMILIES``) folgen der Anforderung: vollständige
Produktnamen, Hersteller/Modell, Abkürzungen, alternative Schreibweisen,
Tippfehler, Community-Namen, Modellnummern, Varianten, Titelbegriffe,
Beschreibungs-Hinweise, unvollständige Titel und plattformabhängige Varianten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..categories import Category, get_category
from ..domain.price_limits import PriceLimit, minimum_price
from ..knowledge.base import KnowledgeBase
from ..domain.normalize import normalize_text

logger = logging.getLogger(__name__)

# Suchfamilien (Abschnitt 5). Jede Anfrage wird einer Familie zugeordnet,
# damit die Abdeckung ausgewertet werden kann.
SEARCH_FAMILIES: tuple[str, ...] = (
    "exact_model",  # vollständige Produktbezeichnung
    "manufacturer_model",  # Hersteller + Modellbezeichnung
    "abbreviation",  # gaengige Abkuerzung
    "alternate_spelling",  # alternative Schreibweise
    "typo",  # häufiger Tippfehler
    "community_name",  # Name aus der Hardware-Community
    "model_number",  # Modellnummer
    "variant",  # Modellvariante
    "title_keyword",  # Begriff aus Titeln
    "description_hint",  # Begriff, der Modelle in Beschreibungen findet
    "incomplete_title",  # schlecht betitelte Angebote
    "platform_specific",  # plattformabhängige Variante
    "generic",  # kategorieweiter Sammelbegriff
)

FAMILY_LABELS: dict[str, str] = {
    "exact_model": "Vollständige Produktbezeichnung",
    "manufacturer_model": "Hersteller und Modell",
    "abbreviation": "Abkuerzung",
    "alternate_spelling": "Alternative Schreibweise",
    "typo": "Häufiger Tippfehler",
    "community_name": "Community-Bezeichnung",
    "model_number": "Modellnummer",
    "variant": "Modellvariante",
    "title_keyword": "Titel-Schluesselwort",
    "description_hint": "Beschreibungs-Hinweis",
    "incomplete_title": "Unvollständiger Titel",
    "platform_specific": "Plattformspezifisch",
    "generic": "Kategorie-Sammelbegriff",
}

SOURCE_DETERMINISTIC = "deterministic"
SOURCE_KNOWLEDGE = "knowledge_base"
SOURCE_PRICE_LIST = "price_list"

# Obergrenze für die Zahl der Komponenten aus der Preisliste pro Kategorie.
# Sie greift nur als Sicherung; die harte Grenze ist das Anfragelimit des Laufs.
MAX_PRICE_TARGETS = 400

PLAN_SOURCE_DETERMINISTIC = "deterministic"


@dataclass
class PlannedQuery:
    term: str
    family: str
    source: str
    category: str
    reason: str = ""
    exclusion_terms: list[str] = field(default_factory=list)
    expected_yield: str = "medium"
    # Eigenes Kauf-Limit dieser Anfrage. Ist es gesetzt, gilt es statt des
    # Kategorie-Budgets (Preislisten-Modus: je Komponente ein eigenes Limit).
    max_price: float | None = None
    # Mindestpreis dieser Anfrage: ein Viertel des wirksamen Höchstpreises.
    # Darunter wird nichts gespeichert (siehe ``minimum_price``).
    min_price: float | None = None

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "family": self.family,
            "family_label": FAMILY_LABELS.get(self.family, self.family),
            "source": self.source,
            "category": self.category,
            "reason": self.reason,
            "exclusion_terms": list(self.exclusion_terms),
            "expected_yield": self.expected_yield,
            "max_price": self.max_price,
            "min_price": self.min_price,
        }


@dataclass
class CategoryPlan:
    category: str
    label: str
    max_price: float | None
    detected_models: list[str] = field(default_factory=list)
    queries: list[PlannedQuery] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        families: dict[str, int] = {}
        for query in self.queries:
            families[query.family] = families.get(query.family, 0) + 1
        return {
            "category": self.category,
            "label": self.label,
            "max_price": self.max_price,
            "min_price": minimum_price(self.max_price),
            "detected_models": list(self.detected_models),
            "query_count": len(self.queries),
            "families": families,
            "queries": [q.to_dict() for q in self.queries],
            "notes": list(self.notes),
        }


@dataclass
class FinalSearchPlan:
    categories: list[CategoryPlan]
    source: str
    global_exclusions: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    expected_coverage: str = ""
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def total_queries(self) -> int:
        return sum(len(c.queries) for c in self.categories)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "total_queries": self.total_queries,
            "global_exclusions": list(self.global_exclusions),
            "uncertainties": list(self.uncertainties),
            "expected_coverage": self.expected_coverage,
            "notes": list(self.notes),
            "limitations": list(self.limitations),
            "categories": [c.to_dict() for c in self.categories],
        }


def spacing_variants(term: str) -> list[str]:
    """Strukturelle Schreibvarianten: 'rtx 4070' <-> 'rtx4070'."""
    parts = term.split()
    if len(parts) < 2:
        return []
    return ["".join(parts)]


class SearchPlanner:
    """Erzeugt den finalen, validierten Suchplan."""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        *,
        max_variants_per_category: int = 12,
    ) -> None:
        self.knowledge = knowledge
        self.max_variants_per_category = max(3, max_variants_per_category)

    async def build(
        self,
        *,
        categories: list[str],
        max_price: float | None,
        price_per_category: dict[str, float] | None = None,
        min_condition_label: str | None = None,
        product: str | None = None,
        product_models: list[str] | None = None,
        price_targets: list[PriceLimit] | None = None,
    ) -> FinalSearchPlan:
        price_map = dict(price_per_category or {})
        product_name = (product or "").strip() or None
        product_models = [m for m in (product_models or []) if m.strip()]
        price_targets = list(price_targets or [])
        if price_targets:
            # Die Komponenten der Preisliste bestimmen, was gesucht wird -
            # damit auch die beteiligten Kategorien.
            categories = list(dict.fromkeys(t.category for t in price_targets))

        core_limit = max(3, self.max_variants_per_category)

        category_plans: list[CategoryPlan] = []
        for key in categories:
            category = get_category(key)
            if category is None:
                continue
            targets = [t for t in price_targets if t.category == key]
            if targets:
                # Preislisten-Modus: eine Suchanfrage je Komponente, jede mit
                # ihrem eigenen Kauf-Limit. Das Variantenbudget gilt hier
                # nicht - die Liste selbst ist der Suchauftrag.
                budget = max(t.max_price for t in targets)
                queries = self._price_limit_queries(category, targets)
                detected = [t.term for t in targets][:60]
            else:
                budget = price_map.get(key, max_price)
                if product_name:
                    queries = self._product_queries(
                        category, product_name, product_models, budget=budget, limit=core_limit
                    )
                    detected = self._product_models_first(category, budget, product_models)
                else:
                    queries = self._deterministic_queries(category, budget, limit=core_limit)
                    detected = self._knowledge_models(category, budget)
            plan = CategoryPlan(
                category=key,
                label=category.label,
                max_price=budget,
                queries=queries,
            )
            plan.detected_models = detected
            category_plans.append(plan)

        plan = FinalSearchPlan(
            categories=category_plans,
            source=PLAN_SOURCE_DETERMINISTIC,
            global_exclusions=self._global_exclusions(categories),
            notes=[
                (
                    f"Suchplan auf den Produktnamen '{product_name}' zugeschnitten: "
                    "zuerst der Name selbst, dann passende Modelle der Wissensbasis."
                    if product_name
                    else (
                        f"Suchplan aus der Preisliste: {len(price_targets)} Komponenten, "
                        "jede mit ihrem eigenen Kauf-Limit."
                        if price_targets
                        else "Deterministischer Suchkern: breite Kategoriebegriffe und Modelle "
                        "der Wissensbasis werden immer durchsucht."
                    )
                )
            ],
        )
        if price_targets:
            for category_plan in plan.categories:
                if len(category_plan.queries) >= MAX_PRICE_TARGETS:
                    plan.limitations.append(
                        f"In der Kategorie {category_plan.label} wurden nur die ersten "
                        f"{MAX_PRICE_TARGETS} Komponenten der Preisliste übernommen."
                    )
        if not self.has_price_knowledge(categories):
            plan.limitations.append(
                "Die Wissensbasis enthält keine Preisrichtwerte. Die Auswahl der "
                "Modell-Suchbegriffe kann deshalb nicht nach Budget priorisiert "
                "werden; die Abdeckung stützt sich vor allem auf die breiten "
                "Kategoriebegriffe und die Nachsuche auf gefundene Modelle."
            )

        self._apply_exclusions(plan)
        return plan

    # -- Deterministischer Kern -------------------------------------------

    def _price_limit_queries(
        self, category: Category, targets: list[PriceLimit]
    ) -> list[PlannedQuery]:
        """Eine Suchanfrage je Komponente der Preisliste.

        Jede Anfrage trägt ihr eigenes Kauf-Limit, damit die Plattformen schon
        serverseitig nur Angebote unter diesem Limit liefern.
        """
        queries: list[PlannedQuery] = []
        seen: set[str] = set()
        for target in targets:
            key = normalize_text(target.term)
            if not key or key in seen or len(queries) >= MAX_PRICE_TARGETS:
                continue
            seen.add(key)
            queries.append(
                PlannedQuery(
                    term=target.term,
                    family="exact_model",
                    source=SOURCE_PRICE_LIST,
                    category=category.key,
                    reason=(
                        f"Komponente aus der Preisliste, Kauf-Limit "
                        f"{target.max_price:.0f} €, Mindestpreis "
                        f"{minimum_price(target.max_price):.0f} €"
                    ),
                    expected_yield="high",
                    max_price=target.max_price,
                    min_price=minimum_price(target.max_price),
                )
            )
        return queries

    def _knowledge_models(self, category: Category, budget: float | None) -> list[str]:
        """Modelle der Wissensbasis in sinnvoller Reihenfolge.

        Priorisierung: vom Benutzer bevorzugte Modelle (``weight`` > 1), dann
        Modelle mit bekanntem und ins Budget passendem Preisrichtwert (günstige
        zuerst), dann Modelle ohne Preisangabe. Letztere können nicht nach
        Budget priorisiert werden - das wird als Einschränkung ausgewiesen.
        """
        entries = self.knowledge.entries_for_category(category.key)
        priced: list[tuple[float, str]] = []
        unpriced: list[tuple[float, str]] = []
        preferred_models: list[str] = []
        for entry in entries:
            if entry.exclude:
                continue
            if entry.weight and entry.weight > 1.0:
                preferred_models.append(entry.model)
            target = None
            for value in (entry.good_price_max, entry.fair_price_max, entry.overpriced_above):
                if value is not None:
                    target = value
                    break
            if target is not None:
                if budget is not None and target > budget * 1.35:
                    # Bewusst mit Toleranz: Marktpreise schwanken, und ein zu
                    # strenger Filter würde günstige Angebote übersehen.
                    continue
                priced.append((target, entry.model))
            else:
                unpriced.append((0.0, entry.model))

        priced.sort(key=lambda item: (item[0], item[1]))
        unpriced.sort(key=lambda item: item[1])
        ordered = [*preferred_models, *(m for _p, m in priced), *(m for _p, m in unpriced)]
        result: list[str] = []
        for model in ordered:
            if model not in result:
                result.append(model)
            if len(result) >= 60:
                break
        return result

    def has_price_knowledge(self, categories: list[str]) -> bool:
        """Enthält die Wissensbasis Preisrichtwerte für eine der Kategorien?"""
        for key in categories:
            for entry in self.knowledge.entries_for_category(key):
                if not entry.is_identity_only:
                    return True
        return False

    def _product_models_first(
        self, category: Category, budget: float | None, product_models: list[str]
    ) -> list[str]:
        """Produktnahe Modelle zuerst, danach die gewohnte Reihenfolge."""
        ordered: list[str] = []
        for model in [*product_models, *self._knowledge_models(category, budget)]:
            if model not in ordered:
                ordered.append(model)
            if len(ordered) >= 60:
                break
        return ordered

    def _product_queries(
        self,
        category: Category,
        product: str,
        product_models: list[str],
        *,
        budget: float | None = None,
        limit: int | None = None,
    ) -> list[PlannedQuery]:
        """Suchplan für einen konkreten Produktnamen.

        Reihenfolge nach Aussagekraft: der eingegebene Name selbst, die
        Schreibvariante ohne Leerzeichen, passende Modelle aus der
        Wissensbasis - und erst danach ein kleiner Rest breiter
        Kategoriebegriffe, um schlecht betitelte Angebote nicht zu verlieren.
        """
        queries: list[PlannedQuery] = []
        limit = max(3, limit or self.max_variants_per_category)
        seen: set[str] = set()

        def add(term: str, family: str, source: str, reason: str, expected: str = "medium") -> None:
            key = normalize_text(term)
            if not key or key in seen or len(queries) >= limit:
                return
            seen.add(key)
            queries.append(
                PlannedQuery(
                    term=term,
                    family=family,
                    source=source,
                    category=category.key,
                    reason=reason,
                    expected_yield=expected,
                    min_price=minimum_price(budget),
                )
            )

        # 1. Der Name, den der Benutzer eingegeben hat.
        add(
            product,
            "exact_model" if product in product_models else "full_name",
            SOURCE_DETERMINISTIC,
            "Vom Benutzer eingegebener Produktname",
            expected="high",
        )

        # 2. Schreibvarianten ohne Leerzeichen ("rtx3080").
        for variant in spacing_variants(product):
            add(
                variant,
                "alternate_spelling",
                SOURCE_DETERMINISTIC,
                "Gleicher Name ohne Leerzeichen",
                expected="medium",
            )

        # 3. Konkrete Modelle der Wissensbasis, die zum Namen passen.
        model_slots = max(1, int(round(limit * 0.4)))
        for model in product_models[:model_slots]:
            add(
                model,
                "exact_model",
                SOURCE_KNOWLEDGE,
                "Passendes Modell aus der lokalen Wissensbasis",
                expected="high",
            )

        # 4. Wenige breite Begriffe als Sicherheitsnetz für schlecht
        #    betitelte Anzeigen. Der Relevanzfilter der Ingestion sorgt dafür,
        #    dass nur Angebote zum Produkt gespeichert werden.
        generic_slots = max(1, min(3, limit - len(queries)))
        for index, term in enumerate(category.base_terms):
            if index >= generic_slots:
                break
            add(
                term,
                "generic",
                SOURCE_DETERMINISTIC,
                "Breiter Kategoriebegriff (Sicherheitsnetz)",
                expected="low",
            )

        return queries

    def _deterministic_queries(
        self,
        category: Category,
        budget: float | None,
        *,
        limit: int | None = None,
    ) -> list[PlannedQuery]:
        """Deterministischer Kern des Suchplans.

        Der breite Kategorieabgleich bekommt bewusst einen erheblichen Anteil
        am Variantenbudget: Er funktioniert immer, unabhängig davon, ob die
        Wissensbasis Preisrichtwerte enthält. Konkrete Modelle aus der
        Wissensbasis ergänzen ihn, können ihn aber nicht ersetzen.
        """
        queries: list[PlannedQuery] = []
        limit = max(3, limit or self.max_variants_per_category)
        seen: set[str] = set()

        def add(term: str, family: str, source: str, reason: str, expected: str = "medium") -> None:
            key = normalize_text(term)
            if not key or key in seen or len(queries) >= limit:
                return
            seen.add(key)
            queries.append(
                PlannedQuery(
                    term=term,
                    family=family,
                    source=source,
                    category=category.key,
                    reason=reason,
                    expected_yield=expected,
                    min_price=minimum_price(budget),
                )
            )

        # 1. Breiter Kategorieabgleich (immer verfügbar).
        generic_budget = max(3, int(round(limit * 0.42)))
        for index, term in enumerate(category.base_terms):
            if len(queries) >= generic_budget:
                break
            add(
                term,
                "generic" if index < 3 else "title_keyword",
                SOURCE_DETERMINISTIC,
                "Breiter Kategoriebegriff",
                expected="high" if index < 2 else "medium",
            )

        # 2. Konkrete Modelle aus der Wissensbasis.
        models = self._knowledge_models(category, budget)
        model_slots = max(1, limit - len(queries) - max(1, limit // 6))
        for model in models[:model_slots]:
            add(model, "exact_model", SOURCE_KNOWLEDGE, "Modell aus der lokalen Wissensbasis")

        # 3. Schreibvarianten der wichtigsten Modelle (ohne Leerzeichen).
        for model in models[:3]:
            for variant in spacing_variants(model):
                add(
                    variant,
                    "alternate_spelling",
                    SOURCE_KNOWLEDGE,
                    "Alternative Schreibweise ohne Leerzeichen",
                    expected="low",
                )

        return queries

    def _global_exclusions(self, categories: list[str]) -> list[str]:
        exclusions: list[str] = []
        for key in categories:
            category = get_category(key)
            if category is None:
                continue
            for term in category.noise_terms:
                if term not in exclusions:
                    exclusions.append(term)
            for term in self.knowledge.excluded_terms(key):
                if term not in exclusions:
                    exclusions.append(term)
        return exclusions

    def _apply_exclusions(self, plan: FinalSearchPlan) -> None:
        """Ausschlussbegriffe an die Varianten anhängen (lokal angewendet)."""
        for category_plan in plan.categories:
            category = get_category(category_plan.category)
            category_noise = list(category.noise_terms) if category else []
            for query in category_plan.queries:
                merged = list(query.exclusion_terms)
                for term in category_noise:
                    if term not in merged:
                        merged.append(term)
                query.exclusion_terms = merged


def describe_plan_source(source: str) -> str:
    return {
        PLAN_SOURCE_DETERMINISTIC: "Deterministisch (ohne KI)",
    }.get(source, source)
