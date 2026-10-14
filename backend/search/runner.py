"""Suchlauf-Orchestrierung (Abschnitt 14).

Reihenfolge: Eingaben validieren -> Modelle/Suchbegriffe -> Suchplan ->
Plan validieren -> Plattformen abfragen -> paginieren -> Festpreis filtern ->
normalisieren -> deduplizieren -> fehlende Details nachladen -> Modelle
zuordnen -> mit Wissensbasis und Marktbeobachtungen vergleichen -> neu
bewerten -> sortieren -> Datenbank aktualisieren -> Abdeckung protokollieren.

Der Fortschritt stammt ausschließlich aus echten Backend-Daten. Es gibt
keine geschätzten oder simulierten Fortschrittsbalken.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..categories import get_category
from ..config import RuntimeSettings, Settings
from ..connectors.base import (
    ConnectorError,
    ConnectorRateLimitError,
    LocationFilter,
    SearchPage,
    SearchQuerySpec,
)
from ..connectors.registry import ConnectorRegistry
from ..domain.hardware import HardwareResolver, models_ambiguous, models_ambiguous
from ..domain.normalize import CONDITION_LABELS, normalize_text, searchable_condition_ranks
from ..domain.price_limits import (
    PriceLimit,
    PriceList,
    load_price_list,
    minimum_price,
    rewrite_price_limits,
)
from ..domain.product import interpret_product
from ..knowledge.base import KnowledgeBase
from ..models import (
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    STATUS_ACTIVE,
    STATUS_GONE,
    Listing,
    SearchQuery,
    SearchRun,
    utcnow,
)
from ..pricing.limit_advisor import advise_limits
from ..pricing.market import MarketPriceEstimator
from ..schemas import SearchRequest
from ..scoring.deal import ComparableOffer, DealScorer, ScoringInput
from ..scoring.risks import analyze_text_risk
from ..services.repository import update_assessment
from .coverage import STATUS_OK, build_coverage, coverage_statement
from .ingest import (
    RESULT_AUCTION,
    RESULT_DEFECT,
    RESULT_ERROR,
    RESULT_EXCLUSION_TERM,
    RESULT_LOCATION,
    RESULT_NEW_ITEM,
    RESULT_PRICE,
    RESULT_RELEVANCE,
    RESULT_TITLE_FILTER,
    RESULT_UNKNOWN_FORMAT,
    RESULT_USABLE,
    IngestOptions,
    OfferIngestor,
)
from .planner import FinalSearchPlan, PlannedQuery, SearchPlanner, describe_plan_source
from .revalidate import revalidate_stored_listings
from .titles import BROAD_FAMILIES, TitleFilter, TitleTarget

logger = logging.getLogger(__name__)

PHASE_QUEUED = "queued"
PHASE_VALIDATING = "validating"
PHASE_PLANNING = "planning"
PHASE_SEARCHING = "searching"
PHASE_ENRICHING = "enriching"
PHASE_SCORING = "scoring"
PHASE_FINALIZING = "finalizing"
PHASE_DONE = "done"
PHASE_CANCELLED = "cancelled"

PHASE_LABELS = {
    PHASE_QUEUED: "In der Warteschlange",
    PHASE_VALIDATING: "Eingaben werden geprüft",
    PHASE_PLANNING: "Suchplan wird erstellt",
    PHASE_SEARCHING: "Plattformen werden durchsucht",
    PHASE_ENRICHING: "Fehlende Details werden nachgeladen",
    PHASE_SCORING: "Angebote werden bewertet",
    PHASE_FINALIZING: "Abdeckung wird berechnet",
    PHASE_DONE: "Abgeschlossen",
    PHASE_CANCELLED: "Abgebrochen",
}

_RESULT_COUNTER_MAP = {
    RESULT_AUCTION: "auction",
    RESULT_UNKNOWN_FORMAT: "unknown_format",
    RESULT_PRICE: "price",
    RESULT_EXCLUSION_TERM: "exclusion_term",
    RESULT_LOCATION: "location",
    RESULT_RELEVANCE: "relevance",
    RESULT_DEFECT: "defect",
    RESULT_NEW_ITEM: "new_item",
    RESULT_TITLE_FILTER: "title_filter",
    RESULT_ERROR: "error",
}

MAX_ERRORS_KEPT = 40


def _price_reason_key(reason: str | None) -> str:
    """Kurzer Grund für einen Preis-Ausschluss (für die Auswahl-Zusammenfassung).

    Der Ausschluss ``price`` umfasst sehr Verschiedenes: über dem Kauf-Limit,
    unter dem Mindestpreis, außerhalb des Zeitraums. Ohne Aufschlüsselung sieht
    der Lauf aus, als hätte er "einfach nichts gefunden".
    """
    if not reason:
        return "Preis nicht angegeben"
    if "unter dem Mindestpreis" in reason:
        return "unter dem Mindestpreis"
    if "Kauf-Limit" in reason:
        return "über dem Kauf-Limit der Komponente"
    if "über dem Limit" in reason:
        return "über dem Höchstpreis"
    if "Zeitraum" in reason:
        return "außerhalb des gewählten Zeitraums"
    return "sonstiger Preisgrund"


@dataclass
class RunControl:
    """Steuerung eines laufenden Suchauftrags."""

    run_id: int
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    progress: dict = field(default_factory=dict)
    # Die Titelprüfung des Laufs. Wird beim Start gesetzt; fehlt sie, werden
    # die Titel gar nicht geprüft (z. B. beim lokalen Import).
    title_filter: TitleFilter | None = None

    def __post_init__(self) -> None:
        # Ein frischer Lauf ist nicht pausiert. Ohne das würde ein direkt
        # gestarteter Lauf am ersten ``await control.resume.wait()`` hängen -
        # pausiert wird ausschließlich über :meth:`pause`.
        self.resume.set()
        if not self.progress:
            self.progress = {
                "phase": PHASE_QUEUED,
                "phase_label": PHASE_LABELS[PHASE_QUEUED],
                "message": "Suchlauf wurde angelegt.",
                "queries_total": 0,
                "queries_done": 0,
                "platforms": [],
                "results_found": 0,
                "results_usable": 0,
                "results_matching_filters": 0,
                "new_listings": 0,
                "updated_listings": 0,
                "raw_hits": 0,
                "raw_hits_usable": 0,
                "raw_hits_unchanged": 0,
                "duplicates": 0,
                "excluded": {key: 0 for key in _RESULT_COUNTER_MAP.values()},
                "excluded_reasons": {},
                "scored": 0,
                "details_fetched": 0,
                "title_check": {
                    "aktiv": False,
                    "geprueft": 0,
                    "passt": 0,
                    "passt_nicht": 0,
                    "zubehoer": 0,
                },
                "rounds": 1,
                "errors": [],
                "limitations": [],
                "paused": False,
                "cancelled": False,
                "started_at": None,
                "updated_at": utcnow().isoformat(),
                # Interne Zähler und Indizes: werden NICHT in die DB geschrieben.
                "_counters": {},
                "_platform_index": {},
            }

    @property
    def paused(self) -> bool:
        return not self.resume.is_set()

    def error(self, message: str) -> None:
        errors = self.progress.setdefault("errors", [])
        if message not in errors:
            errors.append(message)
            del errors[:-MAX_ERRORS_KEPT]

    def limitation(self, message: str) -> None:
        if not message:
            return
        items = self.progress.setdefault("limitations", [])
        if message not in items:
            items.append(message)
            del items[:-MAX_ERRORS_KEPT]


class SearchRunner:
    """Führt einen Suchlauf vollständig aus."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: ConnectorRegistry,
        knowledge: KnowledgeBase,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.knowledge = knowledge
        # Eindeutige Zaehlung über den gesamten Suchlauf. Die Roh-Zähler
        # zählen pro Suchanfrage und enthalten daher Mehrfachtreffer, weil
        # sich Suchanfragen bewusst überlappen. Beide Zahlen werden getrennt
        # ausgewiesen, damit die Abdeckung nicht beschoenigt wird.
        self._seen_listing_ids: set[int] = set()
        self._new_listing_ids: set[int] = set()
        self._duplicate_hits: int = 0

    # -- Einstieg ----------------------------------------------------------

    async def execute(
        self,
        run_id: int,
        request: SearchRequest,
        control: RunControl,
        runtime: RuntimeSettings,
        session: Session,
    ) -> None:
        run = session.get(SearchRun, run_id)
        if run is None:
            raise RuntimeError(f"Suchlauf {run_id} existiert nicht")

        run.started_at = utcnow()
        run.status = RUN_RUNNING
        control.resume.set()
        control.progress["started_at"] = run.started_at.isoformat()
        self._set_phase(control, PHASE_VALIDATING, "Eingaben werden geprüft.")
        self._flush(session, run, control)

        try:
            await self._execute_inner(session, run, request, control, runtime)
        except asyncio.CancelledError:
            run.status = RUN_CANCELLED
            control.progress["cancelled"] = True
            control.progress["message"] = "Suchlauf wurde abgebrochen."
            raise
        except Exception as exc:  # Unerwarteter Gesamtfehler
            logger.exception("Suchlauf %s fehlgeschlagen", run_id)
            run.status = RUN_FAILED
            control.error(f"Unerwarteter Fehler: {type(exc).__name__}: {exc}")
            control.progress["message"] = "Suchlauf mit Fehler beendet."
        finally:
            run.finished_at = utcnow()
            cancelled = control.cancel.is_set()
            if run.status == RUN_RUNNING:
                run.status = RUN_CANCELLED if cancelled else RUN_COMPLETED
            run.limitations = self._structured_limitations(run, control)
            self._set_phase(
                control,
                PHASE_CANCELLED if cancelled else PHASE_DONE,
                "Suchlauf abgebrochen." if cancelled else "Suchlauf beendet.",
            )
            self._flush(session, run, control)

    def _structured_limitations(self, run: SearchRun, control: RunControl) -> list[dict]:
        base = [item for item in (run.limitations or []) if isinstance(item, dict)]
        known = {item.get("message") for item in base}
        for message in control.progress.get("limitations", []):
            if message not in known:
                base.append({"platform": "system", "code": "limitation", "message": message})
                known.add(message)
        return base[:60]

    # -- Ablauf ------------------------------------------------------------

    async def _execute_inner(
        self,
        session: Session,
        run: SearchRun,
        request: SearchRequest,
        control: RunControl,
        runtime: RuntimeSettings,
    ) -> None:
        usable, notices = self.registry.usable_platforms(request.platforms)
        for notice in notices:
            control.limitation(str(notice.get("message", "")))
        run.limitations = notices
        for message in _location_limitations(request, usable, self.registry):
            control.limitation(message)
        if not usable:
            control.error(
                "Keine der ausgewählten Plattformen ist nutzbar. Es kann nichts "
                "durchsucht werden."
            )
            return

        # --- Suchplan -----------------------------------------------------
        self._set_phase(control, PHASE_PLANNING, "Suchplan wird erstellt.")
        self._flush(session, run, control)

        # --- Suchauftrag: Kauf-Limit-Liste oder Produktname ----------------
        # Ist eine Gruppe der Preisliste gewaehlt, ist die Liste selbst der
        # Suchauftrag: Jede Komponente wird gesucht, und zwar nur bis zu ihrem
        # eigenen Kauf-Limit.
        price_list = load_price_list(self.settings.price_list_path)
        # Optional: die handgepflegten Limits vor dem Lauf aus den beobachteten
        # Preisen nachführen (standardmäßig aus, siehe RuntimeSettings).
        price_list = self._maybe_auto_update_limits(session, price_list, runtime, control)
        price_targets: list[PriceLimit] = []
        if request.price_groups:
            price_targets = self._price_targets(request, price_list, control)
            if not price_targets:
                return

        # Bei einem konkreten Produktnamen leitet das Backend die Kategorien
        # selbst ab - der Benutzer muss keine Kategorie mehr auswaehlen.
        product_interpretation = interpret_product(request.product, self.knowledge)
        categories = (
            list(product_interpretation.categories)
            if product_interpretation is not None
            else list(request.categories)
        )
        if product_interpretation is not None:
            control.progress["product"] = product_interpretation.to_dict()
            if product_interpretation.match_quality == "none":
                control.limitation(
                    f"Zum Produkt '{product_interpretation.product}' war keine "
                    "bekannte Hardware zuzuordnen. Es wird breit gesucht; die "
                    "Ergebnisse werden auf den Produktnamen gefiltert."
                )

        planner = SearchPlanner(
            self.knowledge,
            max_variants_per_category=runtime.search_max_variants_per_category,
        )
        min_condition_label = (
            CONDITION_LABELS.get(request.min_condition_rank)
            if request.min_condition_rank is not None
            else None
        )
        plan = await planner.build(
            categories=categories,
            max_price=request.max_price,
            price_per_category=request.price_per_category,
            min_condition_label=min_condition_label,
            product=request.product,
            product_models=(
                product_interpretation.matched_models
                if product_interpretation is not None
                else None
            ),
            price_targets=price_targets,
        )
        for limitation in plan.limitations:
            control.limitation(limitation)

        run.plan = plan.to_dict()
        run.plan_source = plan.source
        control.progress["plan_source"] = plan.source
        control.progress["plan_source_label"] = describe_plan_source(plan.source)
        control.progress["plan_summary"] = {
            "total_queries": plan.total_queries,
            "expected_coverage": plan.expected_coverage,
            "uncertainties": plan.uncertainties[:6],
            "categories": [
                {
                    "category": c.category,
                    "label": c.label,
                    "queries": len(c.queries),
                    "detected_models": len(c.detected_models),
                    "families": sorted({q.family for q in c.queries}),
                }
                for c in plan.categories
            ],
        }
        self._flush(session, run, control)

        if plan.total_queries == 0:
            control.error("Der Suchplan enthielt keine ausfuehrbaren Suchanfragen.")
            return

        # --- Anfragen anlegen --------------------------------------------
        pending = self._materialize_queries(plan, usable, request)
        max_queries = max(1, runtime.search_max_queries_per_run)
        if len(pending) > max_queries:
            control.limitation(
                f"{len(pending)} Suchanfragen geplant, Limit {max_queries} pro "
                "Suchlauf - der Rest wird nicht ausgeführt."
            )
            pending = pending[:max_queries]

        rows: list[SearchQuery] = []
        for spec, variant in pending:
            row = SearchQuery(
                run_id=run.id,
                platform=spec.platform,
                category=spec.category,
                term=spec.term,
                family=variant.family,
                sort=spec.sort,
                round_index=spec.round_index,
                status="pending",
            )
            session.add(row)
            rows.append(row)
        session.commit()

        run.queries_total = len(rows)
        control.progress["queries_total"] = len(rows)
        self._init_platform_progress(control, usable, pending)
        self._flush(session, run, control)

        ingestor = OfferIngestor(
            session,
            knowledge=self.knowledge,
            resolver=HardwareResolver(self.knowledge),
            options=build_ingest_options(runtime, request, price_list=price_list),
        )
        counters = control.progress["_counters"]

        # --- Bestandsprüfung ---------------------------------------------
        # Was frühere Läufe gespeichert haben, wird mit den heutigen Regeln
        # erneut beurteilt. Ohne diesen Schritt bliebe alles, was eine frühere,
        # schwächere Fassung der Titelregeln durchgelassen hat, für immer in der
        # Ergebnisliste stehen - und genau diese Altlast verdeckt dann die
        # echten Treffer.
        report = revalidate_stored_listings(session, price_list=price_list)
        control.progress["revalidation"] = report.to_dict()
        control.limitation(report.summary())

        # --- Titelprüfung ------------------------------------------------
        # Rein über Schlagwörter: Jede Anzeige bekommt ein Urteil, es gibt
        # keine Wartezeit und keinen Ausfall. Auch bereits gespeicherte Titel
        # werden erneut geprüft - es gibt keinen Bestandsschutz.
        control.title_filter = self._build_title_filter(price_list, control)
        control.progress["title_check"] = (
            control.title_filter.stats.to_dict()
            if control.title_filter is not None
            else control.progress["title_check"]
        )

        # --- Runde 1 ------------------------------------------------------
        self._set_phase(control, PHASE_SEARCHING, "Plattformen werden durchsucht.")
        await self._run_round(
            session, run, control, runtime, pending, rows, ingestor, request
        )

        # --- Weitere Runden (begrenzt und begruendet) ---------------------
        max_rounds = request.max_rounds or runtime.search_max_rounds
        executed_keys = {spec.key() for spec, _ in pending}
        round_index = 1
        while (
            round_index < max_rounds
            and not control.cancel.is_set()
            and len(pending) < max_queries
        ):
            follow_up = self._follow_up_queries(
                session,
                run.id,
                plan,
                request,
                executed_keys,
                round_index + 1,
                runtime,
                price_list=price_list,
            )
            if not follow_up:
                break
            round_index += 1
            control.progress["rounds"] = round_index
            control.limitation(
                f"Nachsuche in Runde {round_index} mit {len(follow_up)} zusätzlichen "
                "Anfragen auf Basis der bisher gefundenen Modelle."
            )
            new_rows: list[SearchQuery] = []
            for spec, variant in follow_up:
                row = SearchQuery(
                    run_id=run.id,
                    platform=spec.platform,
                    category=spec.category,
                    term=spec.term,
                    family=variant.family,
                    sort=spec.sort,
                    round_index=spec.round_index,
                    status="pending",
                )
                session.add(row)
                new_rows.append(row)
                executed_keys.add(spec.key())
            session.commit()
            rows.extend(new_rows)
            pending.extend(follow_up)
            run.queries_total = len(rows)
            control.progress["queries_total"] = len(rows)
            self._bump_platform_totals(control, follow_up)
            await self._run_round(
                session, run, control, runtime, follow_up, new_rows, ingestor, request
            )

        # --- Details nachladen -------------------------------------------
        await self._enrich_details(session, run, control, runtime)

        # --- Bewerten -----------------------------------------------------
        await self._score_listings(session, run, control, runtime, request)

        # --- Titelprüfung: Ergebnis festhalten ----------------------------
        if control.title_filter is not None:
            control.progress["title_check"] = control.title_filter.stats.to_dict()
            control.limitation(control.title_filter.summary())

        # --- Auswahl: Was ohne KI aussortiert wurde ------------------------
        # Diese Zahlen gehoeren sichtbar in den Lauf: Sie erklaeren, warum die
        # Ergebnisliste kuerzer ist als die Roh-Trefferzahl.
        control.limitation(self._selection_summary(control))

        # --- Abdeckung ----------------------------------------------------
        self._set_phase(control, PHASE_FINALIZING, "Abdeckung wird berechnet.")
        self._flush(session, run, control)
        coverage = build_coverage(run, rows)
        run.coverage = coverage
        run.result_count = int(counters.get("matching_filters", 0))
        run.new_count = int(counters.get("new", 0))
        run.updated_count = int(counters.get("updated", 0))
        control.progress["coverage"] = coverage
        control.progress["coverage_statement"] = coverage_statement(coverage)
        self._flush(session, run, control)

    def _selection_summary(self, control: RunControl) -> str:
        """Satz zur Auswahl: defekte Ware, Neuware, unpassende Titel, Altbestand."""
        excluded = control.progress.get("excluded") or {}
        teile: list[str] = []
        if excluded.get("defect"):
            teile.append(f"{excluded['defect']} als defekt oder Ersatzteil")
        if excluded.get("new_item"):
            teile.append(f"{excluded['new_item']} als Neuware")
        if excluded.get("title_filter"):
            teile.append(f"{excluded['title_filter']} mit unpassendem Titel")
        if excluded.get("price"):
            reasons = control.progress.get("excluded_reasons") or {}
            top = max(reasons.items(), key=lambda item: item[1])[0] if reasons else None
            grund = f", meist {top}" if top else ""
            teile.append(f"{excluded['price']} wegen des Preises{grund}")
        bestand = int((control.progress.get("revalidation") or {}).get("aussortiert") or 0)
        if bestand:
            teile.append(f"{bestand} aus dem Bestand entfernt")
        if not teile:
            return "Auswahl: keine Angebote aussortiert."
        return "Auswahl per Titelregel aussortiert: " + ", ".join(teile) + "."

    # -- Titelprüfung ------------------------------------------------------

    def _maybe_auto_update_limits(
        self,
        session: Session,
        price_list: PriceList,
        runtime: RuntimeSettings,
        control: RunControl,
    ) -> PriceList:
        """Führt die Kauf-Limits aus beobachteten Preisen nach (falls aktiviert).

        Gibt die (neu geladene) Liste zurück, damit der Lauf mit den Werten
        arbeitet, die er gerade geschrieben hat. Ist die Einstellung aus oder
        gibt es keine Empfehlung, bleibt alles beim Alten.
        """
        if not runtime.auto_update_price_limits or not price_list.available:
            return price_list
        try:
            report = advise_limits(
                session,
                price_list,
                min_observations=runtime.market_min_observations,
                window_days=runtime.market_reference_window_days,
            )
            updates = report.actionable()
            if not updates:
                control.limitation("Kauf-Limits: " + report.summary)
                return price_list
            applied, backup = rewrite_price_limits(price_list.path, updates)
            control.limitation(
                f"Kauf-Limits automatisch nachgeführt: {applied} Komponente(n) in "
                f"'{price_list.path}' (Sicherung: {backup})."
            )
        except Exception as exc:  # Eine Dateiänderung darf den Lauf nie kippen
            logger.warning("Kauf-Limits konnten nicht nachgeführt werden: %s", exc)
            control.limitation(f"Kauf-Limits konnten nicht nachgeführt werden: {exc}")
            return price_list
        # Neu einlesen, damit der Lauf mit den geschriebenen Werten arbeitet.
        return load_price_list(self.settings.price_list_path)

    def _build_title_filter(
        self, price_list: PriceList, control: RunControl
    ) -> TitleFilter:
        """Baut die Titelprüfung des Laufs (rein über Schlagwörter).

        Der lokale Import bleibt außen vor: Dort hat der Benutzer die Angebote
        selbst ausgewählt (siehe ``titles.SKIP_PLATFORMS``).
        """
        return TitleFilter(
            price_list=price_list if price_list.available else None,
            on_limitation=control.limitation,
        )

    def _category_label(self, key: str | None) -> str | None:
        if not key:
            return None
        category = get_category(key)
        return category.label if category is not None else None

    def _views_for(self, platform: str, variant: PlannedQuery) -> tuple[str | None, ...]:
        """Suchansichten (Sortierungen) einer Anfrage.

        Die Standardsicht ist immer dabei. Zusätzliche Ansichten der Plattform
        (z.B. eBay "neueste zuerst") werden nur bei **breiten** Suchbegriffen
        ergänzt: Dort zeigt jede Sortierung einen anderen Ausschnitt der Treffer,
        während eine enge Suche nach einem konkreten Modell in der Standardsicht
        bereits vollständig ist. So wächst die Abdeckung, ohne für jeden Begriff
        doppelt abzufragen.
        """
        connector = self.registry.get(platform)
        extra = tuple(getattr(connector, "sort_variants", ()) or ())
        if not extra:
            return (None,)
        broad = variant.family in BROAD_FAMILIES or variant.expected_yield == "high"
        return (None, *extra) if broad else (None,)

    def _price_targets(
        self, request: SearchRequest, price_list: PriceList, control: RunControl
    ) -> list[PriceLimit]:
        """Komponenten der gewaehlten Preislisten-Gruppen.

        Eine leere Rueckgabe bedeutet: Es gibt nichts zu suchen. Der Grund
        steht dann als Fehler im Fortschritt - nicht als stiller Leerlauf.
        """
        if not price_list.available:
            control.error(
                f"Die Kauf-Limit-Liste '{price_list.path or self.settings.price_list_path}' "
                "enthaelt keine Komponenten. Ohne Liste laesst sich so nicht suchen."
            )
            return []
        known = price_list.group_keys()
        unknown = [group for group in request.price_groups if group not in known]
        if unknown:
            control.limitation(
                "Unbekannte Gruppe(n) der Preisliste wurden uebergangen: "
                + ", ".join(unknown)
                + f" (bekannt: {', '.join(sorted(known))})."
            )
        wanted = [group for group in request.price_groups if group in known]
        targets = price_list.entries_for_groups(wanted)
        if not targets:
            control.error("Keine der gewaehlten Preislisten-Gruppen enthaelt Komponenten.")
            return []
        labels = ", ".join(
            f"{group.label} ({group.count})"
            for group in price_list.groups()
            if group.key in set(wanted)
        )
        control.progress["price_list"] = {
            "groups": wanted,
            "labels": labels,
            "targets": len(targets),
            "max_price": max(target.max_price for target in targets),
        }
        control.limitation(
            f"Suchlauf nach der Kauf-Limit-Liste: {len(targets)} Komponenten ({labels}) - "
            "je Komponente gilt ihr eigenes Limit."
        )
        return targets

    # -- Anfragen erzeugen -------------------------------------------------

    def _materialize_queries(
        self, plan: FinalSearchPlan, usable: list[str], request: SearchRequest
    ) -> list[tuple[SearchQuerySpec, PlannedQuery]]:
        result: list[tuple[SearchQuerySpec, PlannedQuery]] = []
        # Zustandsfilter der Quelle: Die Suche ist auf gebrauchte, funktionierende
        # Hardware ausgelegt. Neuware und defekte Ware schon auf der Plattform
        # auszuschließen spart Anfragen - und genau diese Treffer würden hier
        # sonst als "Neuware" oder "defekt" wieder herausfallen.
        condition_texts = [
            str(rank) for rank in searchable_condition_ranks(request.min_condition_rank)
        ]
        for category_plan in plan.categories:
            for variant in category_plan.queries:
                # Im Preislisten-Modus hat jede Anfrage ihr eigenes Kauf-Limit;
                # sonst gilt das Budget der Kategorie. Der Mindestpreis ist
                # immer ein Viertel des wirksamen Höchstpreises.
                max_price = variant.max_price or category_plan.max_price
                for platform in usable:
                    for view in self._views_for(platform, variant):
                        spec = SearchQuerySpec(
                            term=variant.term,
                            platform=platform,
                            category=category_plan.category,
                            family=variant.family,
                            sort=view,
                            round_index=1,
                            max_price=max_price,
                            min_price=minimum_price(max_price),
                            days_back=request.days_back,
                            location=_location_filter(request),
                            condition_texts=list(condition_texts),
                            shipping_only=request.delivery == "shipping",
                            exclusion_terms=list(plan.global_exclusions)
                            + list(variant.exclusion_terms),
                        )
                        result.append((spec, variant))
        return result

    def _follow_up_queries(
        self,
        session: Session,
        run_id: int,
        plan: FinalSearchPlan,
        request: SearchRequest,
        executed_keys: set[str],
        round_index: int,
        runtime: RuntimeSettings,
        price_list: PriceList | None = None,
    ) -> list[tuple[SearchQuerySpec, PlannedQuery]]:
        """Nachsuche auf Basis der bisherigen Ergebnisse.

        Zwei Quellen, in dieser Reihenfolge:

        1. Modelle, die im Lauf bereits einen guten Deal-Score erreicht haben -
           davon gibt es erfahrungsgemäß mehr Angebote am Markt.
        2. Modelle, die überhaupt in den Ergebnissen aufgetaucht sind. Damit
           werden auch Modelle gezielt nachgesucht, die der deterministische
           Suchplan nicht kannte.
        """
        good_rows = session.scalars(
            select(Listing)
            .where(
                Listing.model.is_not(None),
                Listing.deal_score.is_not(None),
                Listing.deal_score >= 60,
                Listing.status == STATUS_ACTIVE,
                Listing.listing_format == "fixed_price",
            )
            .order_by(Listing.deal_score.desc())
            .limit(20)
        ).all()
        discovered_rows = session.scalars(
            select(Listing)
            .where(
                Listing.model.is_not(None),
                Listing.last_seen_run_id == run_id,
                Listing.status == STATUS_ACTIVE,
            )
            .limit(60)
        ).all()

        models: list[tuple[str, str | None]] = []
        for row in good_rows:
            key = (row.model or "", row.category)
            if key not in models:
                models.append(key)
        for row in discovered_rows:
            key = (row.model or "", row.category)
            if key not in models:
                models.append(key)
        if not models:
            return []

        usable, _notices = self.registry.usable_platforms(request.platforms)
        if not usable:
            return []

        # Zustandsfilter der Quelle: Die Suche ist auf gebrauchte, funktionierende
        # Hardware ausgelegt. Neuware und defekte Ware schon auf der Plattform
        # auszuschließen spart Anfragen - und genau diese Treffer würden hier
        # sonst als "Neuware" oder "defekt" wieder herausfallen.
        condition_texts = [
            str(rank) for rank in searchable_condition_ranks(request.min_condition_rank)
        ]
        results: list[tuple[SearchQuerySpec, PlannedQuery]] = []
        for model, category in models[:8]:
            category_plan = next((c for c in plan.categories if c.category == category), None)
            existing_terms = (
                {normalize_text(q.term) for q in category_plan.queries} if category_plan else set()
            )
            if normalize_text(model) in existing_terms:
                continue
            # Kennt die Kauf-Limit-Liste das Modell, gilt auch bei der
            # Nachsuche dessen Limit statt des breiten Kategorie-Budgets.
            limit = price_list.limit_for(title=model) if price_list is not None else None
            variant = PlannedQuery(
                term=model,
                family="exact_model",
                source="results",
                category=category or "other",
                reason=(
                    "Nachsuche: dieses Modell ist in den bisherigen Ergebnissen "
                    "aufgetaucht und wird gezielt weiter durchsucht"
                ),
                exclusion_terms=list(plan.global_exclusions),
                max_price=limit.max_price if limit is not None else None,
                min_price=minimum_price(limit.max_price) if limit is not None else None,
            )
            for platform in usable:
                for view in self._views_for(platform, variant):
                    budget = variant.max_price or (
                        category_plan.max_price if category_plan else request.max_price
                    )
                    spec = SearchQuerySpec(
                        term=model,
                        platform=platform,
                        category=category,
                        family=variant.family,
                        sort=view,
                        round_index=round_index,
                        max_price=budget,
                        min_price=minimum_price(budget),
                        days_back=request.days_back,
                        location=_location_filter(request),
                        condition_texts=list(condition_texts),
                        shipping_only=request.delivery == "shipping",
                        exclusion_terms=list(plan.global_exclusions),
                    )
                    if spec.key() in executed_keys:
                        continue
                    results.append((spec, variant))
        return results[: max(1, runtime.search_max_queries_per_run // 8)]

    # -- Eine Runde ausfuehren --------------------------------------------

    async def _run_round(
        self,
        session: Session,
        run: SearchRun,
        control: RunControl,
        runtime: RuntimeSettings,
        pending: list[tuple[SearchQuerySpec, PlannedQuery]],
        rows: list[SearchQuery],
        ingestor: OfferIngestor,
        request: SearchRequest,
    ) -> None:
        semaphore = asyncio.Semaphore(max(1, runtime.search_max_parallel_requests))
        row_by_key = {
            _row_key(row.term, row.platform, row.round_index, row.sort): row for row in rows
        }
        counters = control.progress["_counters"]
        lock = asyncio.Lock()

        async def worker(spec: SearchQuerySpec, variant: PlannedQuery) -> None:
            await control.resume.wait()
            if control.cancel.is_set():
                return
            row = row_by_key.get(_row_key(spec.term, spec.platform, spec.round_index, spec.sort))
            async with semaphore:
                if control.cancel.is_set():
                    return
                if row is not None:
                    row.status = "running"
                    row.started_at = utcnow()
                try:
                    outcome = await self._execute_query(
                        session, run, control, runtime, spec, ingestor, request
                    )
                except ConnectorRateLimitError as exc:
                    control.error(str(exc))
                    outcome = _failed_outcome(str(exc))
                except ConnectorError as exc:
                    message = f"{spec.platform}: {exc}"
                    control.error(message)
                    outcome = _failed_outcome(str(exc))
                except Exception as exc:  # Einzelne Anfrage darf nichts abbrechen
                    message = f"{spec.platform}/{spec.term}: {type(exc).__name__}: {exc}"
                    logger.warning("Suchanfrage fehlgeschlagen: %s", message)
                    control.error(message)
                    outcome = _failed_outcome(message)
            async with lock:
                control.progress["queries_done"] = control.progress.get("queries_done", 0) + 1
                self._merge_counters(control, outcome)
                self._update_platform_progress(control, spec.platform, outcome)
                self._update_row(row, outcome)
                try:
                    session.commit()
                except Exception as exc:  # pragma: no cover
                    logger.warning("Zwischenstand konnte nicht gespeichert werden: %s", exc)
                    session.rollback()
                if control.progress["queries_done"] % 4 == 0:
                    self._flush(session, run, control)

        await asyncio.gather(*(worker(spec, variant) for spec, variant in pending))
        self._flush(session, run, control)
        _ = counters

    async def _execute_query(
        self,
        session: Session,
        run: SearchRun,
        control: RunControl,
        runtime: RuntimeSettings,
        spec: SearchQuerySpec,
        ingestor: OfferIngestor,
        request: SearchRequest,
    ) -> dict:
        connector = self.registry.get(spec.platform)
        if connector is None:
            return {
                "status": "skipped",
                "results_returned": 0,
                "results_usable": 0,
                "offset_reached": 0,
                "error": f"Plattform '{spec.platform}' ist nicht angebunden",
            }
        if not connector.capabilities.available:
            return {
                "status": "skipped",
                "results_returned": 0,
                "results_usable": 0,
                "offset_reached": 0,
                "error": connector.capabilities.reason_unavailable,
            }

        caps = connector.capabilities
        # Größere Seiten liefern dieselbe Liste in weniger Anfragen. Die
        # Obergrenze der Plattform gilt; eine kleinere Anfrage wird angehoben.
        page_size = max(spec.page_size, caps.page_size_max or spec.page_size)
        spec = replace(spec, page_size=page_size)
        max_pages = self._page_budget(connector, page_size, runtime)
        title_filter = control.title_filter
        use_filter = title_filter is not None and title_filter.active_for(spec.platform)
        target = TitleTarget.from_query(
            term=spec.term,
            platform=spec.platform,
            category=spec.category,
            category_label=self._category_label(spec.category),
            product=request.product,
            max_price=spec.max_price,
        )

        pages = 0
        results_returned = 0
        results_usable = 0
        new = updated = unchanged = 0
        reported_total: int | None = None
        truncated = False
        excluded = {key: 0 for key in _RESULT_COUNTER_MAP}
        price_reasons: dict[str, int] = {}
        seen_keys: set[str] = set()
        last_page: SearchPage | None = None

        while pages < max_pages:
            await control.resume.wait()
            if control.cancel.is_set():
                break
            wave = await self._fetch_wave(
                connector, spec, control, runtime, last_page, max_pages - pages
            )
            if not wave:
                break
            pages += len(wave)

            for page in wave:
                if reported_total is None:
                    reported_total = page.total_reported
                results_returned += page.count
                if page.truncated:
                    truncated = True
                for limitation in page.limitations:
                    control.limitation(f"{spec.platform}: {limitation}")

            # 1. Vorprüfung: Ausschlussbegriffe, Auktionsformat, Preis,
            #    Standort, Zeitraum sowie die eindeutigen Verpackungs- und
            #    Zubehörfälle. Was hier ausfällt, muss die Titelprüfung nicht
            #    mehr beurteilen.
            candidates = []
            for page in wave:
                for raw in page.listings:
                    key = raw.platform_listing_id or raw.url
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    vorab = ingestor.precheck(
                        raw, exclusion_terms=spec.exclusion_terms, min_price=spec.min_price
                    )
                    if vorab is not None:
                        excluded[vorab.result] = excluded.get(vorab.result, 0) + 1
                        if vorab.result == RESULT_PRICE:
                            key = _price_reason_key(vorab.reason)
                            price_reasons[key] = price_reasons.get(key, 0) + 1
                        continue
                    candidates.append(raw)

            # 2. Titelprüfung (je Welle): erkennt fremde Produkte, Zubehör und
            #    Verpackung über Schlagwörter im Titel. Nicht passende Treffer
            #    werden gar nicht erst gespeichert.
            if use_filter and candidates:
                verdicts = title_filter.review(target, candidates)
            else:
                verdicts = [None] * len(candidates)

            # 3. Speichern und zählen
            stored_here = 0
            for raw, verdict in zip(candidates, verdicts):
                if verdict is not None and verdict.passt is False:
                    excluded[RESULT_TITLE_FILTER] = excluded.get(RESULT_TITLE_FILTER, 0) + 1
                    continue
                outcome = ingestor.ingest(
                    raw,
                    run_id=run.id,
                    exclusion_terms=spec.exclusion_terms,
                    min_price=spec.min_price,
                )
                if outcome.result == RESULT_USABLE:
                    results_usable += 1
                    stored_here += 1
                    if outcome.outcome == "new":
                        new += 1
                    elif outcome.outcome == "updated":
                        updated += 1
                    else:
                        unchanged += 1
                    # Eindeutige Zaehlung (ohne await dazwischen, daher sicher).
                    if outcome.listing_id is not None:
                        if outcome.listing_id in self._seen_listing_ids:
                            self._duplicate_hits += 1
                        else:
                            self._seen_listing_ids.add(outcome.listing_id)
                            if outcome.outcome == "new":
                                self._new_listing_ids.add(outcome.listing_id)
                else:
                    excluded[outcome.result] = excluded.get(outcome.result, 0) + 1
                    if outcome.result == RESULT_PRICE:
                        key = _price_reason_key(outcome.reason)
                        price_reasons[key] = price_reasons.get(key, 0) + 1
            if stored_here:
                try:
                    session.commit()
                except Exception as exc:  # pragma: no cover
                    logger.warning("Angebote konnten nicht gespeichert werden: %s", exc)
                    session.rollback()
            if control.title_filter is not None:
                control.progress["title_check"] = control.title_filter.stats.to_dict()

            last_page = wave[-1]
            if not last_page.has_next or last_page.count == 0:
                break

        # Wurde abgebrochen, weil das Seitenlimit erreicht ist, obwohl es
        # laut Quelle weiterginge? Das gehört als Einschränkung in die Abdeckung.
        if last_page is not None and last_page.has_next and pages >= max_pages:
            truncated = True
        offset_reached = 0
        if last_page is not None:
            offset_reached = (
                last_page.next_offset
                if last_page.has_next and last_page.next_offset is not None
                else last_page.offset + last_page.count
            )

        return {
            "status": STATUS_OK,
            "pages": pages,
            "results_returned": results_returned,
            "results_usable": results_usable,
            "offset_reached": offset_reached,
            "reported_total": reported_total,
            "truncated": truncated,
            "excluded": excluded,
            "excluded_reasons": price_reasons,
            "new": new,
            "updated": updated,
            "unchanged": unchanged,
            "sort": spec.sort,
            "duplicates": excluded.get(RESULT_EXCLUSION_TERM, 0),
        }

    def _page_budget(self, connector, page_size: int, runtime: RuntimeSettings) -> int:
        """Seiten je Anfrage: Limit des Laufs, begrenzt durch die Plattform.

        ``max_results_per_query`` der Plattform gilt je Suchansicht; mit
        größeren Seiten wird daraus eine kleinere Zahl von Anfragen - die
        Abdeckung wächst also, ohne mehr Last zu erzeugen.
        """
        limit = max(1, int(runtime.search_max_pages_per_query))
        caps = connector.capabilities
        if caps.max_results_per_query:
            per_page = max(1, page_size)
            by_results = max(1, -(-int(caps.max_results_per_query) // per_page))
            limit = min(limit, by_results)
        return limit

    async def _fetch_wave(
        self,
        connector,
        spec: SearchQuerySpec,
        control: RunControl,
        runtime: RuntimeSettings,
        last: SearchPage | None,
        remaining: int,
    ) -> list[SearchPage]:
        """Die nächsten Seiten einer Anfrage holen - wenn möglich gleichzeitig.

        Die erste Seite wird immer einzeln geholt: Erst sie verrät, ob und in
        welcher Schrittweite es weitergeht (die Plattform kann eine andere
        Seitengröße liefern als angefragt). Danach dient die gemeldete
        Gesamttrefferzahl als Vorhersage, wie viele weitere Seiten es gibt.
        Nur dann werden mehrere Seiten gleichzeitig angefragt - sonst würde eine
        Anfrage verbrannt, die es nie gab.

        Fehler einzelner Folgeseiten brechen die Suchanfrage nicht ab: Die
        bereits geholten Seiten bleiben nutzbar, der Fehler wird als
        Einschränkung gemeldet. Scheitert die **erste** Seite, gilt die Anfrage
        wie bisher als fehlgeschlagen.
        """
        if last is None:
            return [await connector.search_page(spec.with_offset(spec.offset))]
        if not last.has_next or remaining <= 0:
            return []
        step = (last.next_offset if last.next_offset is not None else last.offset + last.count) - (
            last.offset
        )
        step = max(1, step)
        parallel = max(1, int(runtime.search_parallel_pages_per_query))
        wave = 1
        total = last.total_reported
        if total is not None:
            # Wie viele Seiten gibt es NACH der nächsten? Die nächste Seite ist
            # sicher (die vorige meldete "es geht weiter"), alles danach wird nur
            # vorhergesagt - und zwar aus der Gesamttrefferzahl.
            naechster_offset = last.offset + step
            danach = max(0, -(-(int(total) - naechster_offset) // step) - 1)
            wave = max(1, min(parallel, remaining, 1 + danach))
        offsets = [last.offset + step * (index + 1) for index in range(wave)]
        results = await asyncio.gather(
            *(connector.search_page(spec.with_offset(item)) for item in offsets),
            return_exceptions=True,
        )
        pages: list[SearchPage] = []
        errors: list[str] = []
        for item, result in zip(offsets, results):
            if isinstance(result, BaseException):
                if not pages and item == offsets[0]:
                    raise result
                errors.append(f"Seite ab Treffer {item}: {result}")
                continue
            pages.append(result)
        if errors:
            control.limitation(
                f"{spec.platform}/{spec.term}: " + "; ".join(errors[:2])
            )
        return pages

    # -- Details nachladen -------------------------------------------------

    async def _enrich_details(
        self,
        session: Session,
        run: SearchRun,
        control: RunControl,
        runtime: RuntimeSettings,
    ) -> None:
        budget = max(0, runtime.search_max_detail_fetches)
        if budget == 0:
            return
        self._set_phase(control, PHASE_ENRICHING, "Fehlende Beschreibungen werden nachgeladen.")
        self._flush(session, run, control)

        candidates = session.scalars(
            select(Listing)
            .where(
                Listing.last_seen_run_id == run.id,
                Listing.deal_score.is_not(None),
                Listing.deal_score >= 55,
                Listing.status == STATUS_ACTIVE,
            )
            .order_by(Listing.deal_score.desc())
            .limit(budget * 3)
        ).all()

        fetched = 0
        checked = 0
        for listing in candidates:
            if fetched >= budget or control.cancel.is_set():
                break
            if (listing.description or "").strip():
                continue
            connector = self.registry.get(listing.platform)
            if (
                connector is None
                or not connector.capabilities.supports_details
                or not listing.platform_listing_id
            ):
                continue
            await control.resume.wait()
            try:
                detail = await connector.fetch_details(listing.platform_listing_id)
            except ConnectorError as exc:
                control.limitation(
                    f"Detailabruf für ein {listing.platform}-Angebot fehlgeschlagen: {exc}"
                )
                continue
            checked += 1
            if detail is None:
                # Die Quelle meldet das Angebot nicht mehr -> zuverlässig feststellbar.
                listing.status = STATUS_GONE
                listing.warnings = list(listing.warnings or []) + [
                    "Angebot ist über die Schnittstelle nicht mehr abrufbar"
                ]
                session.commit()
                continue
            if detail.description:
                listing.description = detail.description
                listing.scored_at = None
                fetched += 1
                session.commit()
        if fetched:
            control.limitation(
                f"{fetched} Beschreibung(en) über Detailabruf ergänzt und neu bewertet."
            )
        if checked and checked < budget:
            control.limitation(
                f"{checked} Detailabruf(e) durchgeführt (Budget {budget})."
            )
        control.progress["details_fetched"] = fetched

    # -- Bewerten ----------------------------------------------------------

    async def _score_listings(
        self,
        session: Session,
        run: SearchRun,
        control: RunControl,
        runtime: RuntimeSettings,
        request: SearchRequest,
    ) -> None:
        self._set_phase(control, PHASE_SCORING, "Angebote werden bewertet.")
        self._flush(session, run, control)

        estimator = MarketPriceEstimator(
            self.knowledge,
            min_observations=runtime.market_min_observations,
            window_days=runtime.market_reference_window_days,
        )
        scorer = DealScorer(runtime.weights)

        rows = session.scalars(
            select(Listing).where(
                Listing.last_seen_run_id == run.id,
                Listing.listing_format == "fixed_price",
            )
        ).all()

        scored = 0
        matching = 0
        for listing in rows:
            if control.cancel.is_set():
                break
            if listing.scored_at is not None and listing.deal_score is not None:
                if self._passes_filters(listing, request, run.id):
                    matching += 1
                continue

            model_confidence = _model_confidence(listing)
            text_risk = analyze_text_risk(listing.title, listing.description)

            market = estimator.estimate(
                session,
                category=listing.category,
                model=listing.model,
                condition_rank=listing.condition_rank,
                exclude_listing_id=listing.id,
            )
            comparables = self._comparables(session, listing)
            entry = (
                self.knowledge.find_entry(listing.category, listing.model)
                if listing.category and listing.model
                else None
            )
            reference = (
                self.knowledge.find_price_reference(listing.category, listing.model)
                if listing.category and listing.model
                else None
            )

            assessment = scorer.score(
                ScoringInput(
                    title=listing.title,
                    description=listing.description,
                    category=listing.category,
                    model=listing.model,
                    variant=listing.variant,
                    model_confidence=model_confidence if model_confidence is not None else 0.5,
                    model_ambiguous=models_ambiguous(
                        [
                            item.get("model")
                            for item in (listing.model_candidates or [])
                            if isinstance(item, dict)
                        ]
                    ),
                    price=listing.price,
                    shipping_cost=listing.shipping_cost,
                    shipping_known=listing.shipping_cost_known,
                    total_price=listing.total_price,
                    pickup_available=listing.pickup_available,
                    buyer_postal_code=request.postal_code,
                    seller_postal_code=listing.postal_code,
                    condition_rank=listing.condition_rank,
                    condition_text=listing.condition_text,
                    published_at=listing.published_at,
                    status=listing.status,
                    seller_feedback_percent=listing.seller_feedback_percent,
                    has_image=bool(listing.image_url),
                    market=market,
                    knowledge_entry=entry,
                    knowledge_reference=reference,
                    comparables=comparables,
                    text_risk=text_risk,
                    now=datetime.now(timezone.utc),
                )
            )
            breakdown = assessment.to_breakdown()
            breakdown["market_basis"] = market.to_basis()
            breakdown["assessment_source"] = "deterministic"
            update_assessment(session, listing, breakdown)
            listing.market_basis = market.to_basis()
            scored += 1
            if self._passes_filters(listing, request, run.id):
                matching += 1
            if scored % 20 == 0:
                control.progress["scored"] = scored
                self._flush(session, run, control)

        session.commit()
        control.progress["scored"] = scored
        control.progress["results_matching_filters"] = matching
        control.progress["_counters"]["matching_filters"] = matching
        self._flush(session, run, control)

    def _comparables(self, session: Session, listing: Listing) -> list[ComparableOffer]:
        if not listing.category or not listing.model:
            return []
        rows = session.scalars(
            select(Listing)
            .where(
                Listing.category == listing.category,
                Listing.model == listing.model,
                Listing.id != listing.id,
                Listing.total_price.is_not(None),
            )
            .limit(40)
        ).all()
        result: list[ComparableOffer] = []
        for row in rows:
            entry = self.knowledge.find_entry(row.category or "", row.model or "")
            result.append(
                ComparableOffer(
                    listing_id=row.id,
                    model=row.model,
                    total_price=row.total_price,
                    condition_rank=row.condition_rank,
                    performance_index=entry.performance_index if entry else None,
                    title=row.title,
                )
            )
        return result

    def _passes_filters(self, listing: Listing, request: SearchRequest, run_id: int) -> bool:
        breakdown = listing.score_breakdown or {}
        if request.min_deal_score and (listing.deal_score or 0) < request.min_deal_score:
            return False
        if request.min_price_advantage_pct is not None:
            advantage = breakdown.get("price_advantage_pct")
            if advantage is None or advantage < request.min_price_advantage_pct:
                return False
        if request.min_condition_rank is not None:
            if listing.condition_rank is None or listing.condition_rank < request.min_condition_rank:
                return False
        if request.only_working:
            if listing.condition_rank == 0:
                return False
            if any(
                token in (warning or "").lower()
                for warning in (listing.warnings or [])
                for token in ("defekt", "ersatzteil", "funktioniert nicht")
            ):
                return False
        if request.only_new_since_last_run and listing.first_seen_run_id != run_id:
            return False
        if request.exclude_problematic and listing.category and listing.model:
            entry = self.knowledge.find_entry(listing.category, listing.model)
            if entry and (entry.problematic or entry.exclude):
                return False
        return True

    # -- Fortschritt -------------------------------------------------------

    def _set_phase(self, control: RunControl, phase: str, message: str) -> None:
        control.progress["phase"] = phase
        control.progress["phase_label"] = PHASE_LABELS.get(phase, phase)
        control.progress["message"] = message
        control.progress["updated_at"] = utcnow().isoformat()

    def _init_platform_progress(
        self,
        control: RunControl,
        platforms: list[str],
        pending: list[tuple[SearchQuerySpec, PlannedQuery]],
    ) -> None:
        totals: dict[str, int] = {platform: 0 for platform in platforms}
        for spec, _variant in pending:
            totals[spec.platform] = totals.get(spec.platform, 0) + 1
        items = [
            {
                "platform": platform,
                "label": self._platform_label(platform),
                "queries_total": totals.get(platform, 0),
                "queries_done": 0,
                "results_returned": 0,
                "results_usable": 0,
                "errors": 0,
                "status": "wartend",
            }
            for platform in platforms
        ]
        control.progress["platforms"] = items
        control.progress["_platform_index"] = {item["platform"]: item for item in items}

    def _bump_platform_totals(
        self, control: RunControl, pending: list[tuple[SearchQuerySpec, PlannedQuery]]
    ) -> None:
        index = control.progress.get("_platform_index") or {}
        for spec, _variant in pending:
            item = index.get(spec.platform)
            if item is not None:
                item["queries_total"] += 1

    def _platform_label(self, platform: str) -> str:
        connector = self.registry.get(platform)
        return connector.capabilities.label if connector else platform

    def _update_platform_progress(self, control: RunControl, platform: str, outcome: dict) -> None:
        index = control.progress.setdefault("_platform_index", {})
        item = index.get(platform)
        if item is None:
            item = {
                "platform": platform,
                "label": self._platform_label(platform),
                "queries_total": 0,
                "queries_done": 0,
                "results_returned": 0,
                "results_usable": 0,
                "errors": 0,
                "status": "laufend",
            }
            control.progress.setdefault("platforms", []).append(item)
            index[platform] = item
        item["queries_done"] += 1
        item["results_returned"] += outcome.get("results_returned", 0)
        item["results_usable"] += outcome.get("results_usable", 0)
        if outcome.get("error"):
            item["errors"] += 1
        item["status"] = "fehlerhaft" if outcome.get("error") else "laufend"

    def _merge_counters(self, control: RunControl, outcome: dict) -> None:
        progress = control.progress
        # Roh-Zähler: zählen pro Suchanfrage (Mehrfachtreffer möglich).
        progress["raw_hits"] = progress.get("raw_hits", 0) + outcome.get("results_returned", 0)
        progress["raw_hits_usable"] = progress.get("raw_hits_usable", 0) + outcome.get(
            "results_usable", 0
        )
        progress["raw_hits_unchanged"] = progress.get("raw_hits_unchanged", 0) + outcome.get(
            "unchanged", 0
        )
        # Eindeutige Zähler: über den gesamten Suchlauf ohne Mehrfachtreffer.
        progress["results_found"] = progress["raw_hits"]
        progress["results_usable"] = len(self._seen_listing_ids)
        progress["new_listings"] = len(self._new_listing_ids)
        progress["updated_listings"] = len(self._seen_listing_ids) - len(self._new_listing_ids)
        progress["duplicates"] = self._duplicate_hits
        excluded = progress.setdefault("excluded", {})
        for key, value in (outcome.get("excluded") or {}).items():
            mapped = _RESULT_COUNTER_MAP.get(key, key)
            excluded[mapped] = excluded.get(mapped, 0) + value
        reasons = progress.setdefault("excluded_reasons", {})
        for key, value in (outcome.get("excluded_reasons") or {}).items():
            reasons[key] = reasons.get(key, 0) + value
        counters = progress.setdefault("_counters", {})
        counters["new"] = len(self._new_listing_ids)
        counters["updated"] = len(self._seen_listing_ids) - len(self._new_listing_ids)
        progress["updated_at"] = utcnow().isoformat()

    def _update_row(self, row: SearchQuery | None, outcome: dict) -> None:
        if row is None:
            return
        excluded = outcome.get("excluded") or {}
        row.status = outcome.get("status", STATUS_OK)
        row.pages_fetched = outcome.get("pages", 0)
        row.offset_reached = outcome.get("offset_reached", 0)
        row.reported_total = outcome.get("reported_total")
        row.results_returned = outcome.get("results_returned", 0)
        row.results_usable = outcome.get("results_usable", 0)
        row.excluded_auctions = excluded.get(RESULT_AUCTION, 0)
        row.excluded_unknown_format = excluded.get(RESULT_UNKNOWN_FORMAT, 0)
        row.excluded_price = excluded.get(RESULT_PRICE, 0)
        row.excluded_title_filter = excluded.get(RESULT_TITLE_FILTER, 0)
        if outcome.get("sort") is not None:
            row.sort = outcome.get("sort")
        row.new_listings = outcome.get("new", 0)
        row.duplicates = excluded.get(RESULT_EXCLUSION_TERM, 0)
        row.truncated = bool(outcome.get("truncated"))
        row.error = outcome.get("error")
        row.finished_at = utcnow()

    def _flush(self, session: Session, run: SearchRun, control: RunControl) -> None:
        """Schreibt den Fortschritt in die Datenbank (Quelle für das Polling der UI)."""
        progress = {k: v for k, v in control.progress.items() if not k.startswith("_")}
        progress["updated_at"] = utcnow().isoformat()
        progress["paused"] = control.paused
        progress["cancelled"] = control.cancel.is_set()
        run.progress = progress
        run.queries_done = int(progress.get("queries_done", 0))
        if run.queries_total < int(progress.get("queries_total", 0)):
            run.queries_total = int(progress.get("queries_total", 0))
        try:
            session.commit()
        except Exception as exc:  # pragma: no cover - DB-Fehler beim Fortschritt
            logger.warning("Fortschritt konnte nicht gespeichert werden: %s", exc)
            session.rollback()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _row_key(term: str, platform: str, round_index: int, sort: str | None) -> str:
    """Schlüssel einer Suchanfrage-Zeile (je Suchansicht eine eigene Zeile)."""
    return f"{term}|{platform}|r{round_index}|{sort or ''}"


def _failed_outcome(message: str) -> dict:
    return {
        "status": "failed",
        "results_returned": 0,
        "results_usable": 0,
        "offset_reached": 0,
        "excluded": {key: 0 for key in _RESULT_COUNTER_MAP},
        "error": message,
    }


def _model_confidence(listing: Listing) -> float | None:
    candidates = listing.model_candidates
    if not candidates:
        return None
    values: list[float] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            try:
                values.append(float(candidate.get("confidence") or 0.0))
            except (TypeError, ValueError):
                continue
    return max(values) if values else None


def _location_limitations(
    request: SearchRequest, usable: list[str], registry: ConnectorRegistry
) -> list[str]:
    """Ehrliche Hinweise, wenn der Standortfilter auf einer Quelle nicht wirkt.

    Ein Wunschstandort kann nur berücksichtigt werden, wenn die Quelle den
    Verkäuferstandort liefert (lokale Auswertung) oder serverseitig filtern
    kann. Alles andere wird ausgewiesen, statt still zu passieren.
    """
    if not request.postal_code or request.delivery == "shipping":
        return []
    messages: list[str] = []
    for platform in usable:
        connector = registry.get(platform)
        caps = connector.capabilities if connector is not None else None
        if caps is None or caps.supports_location_filter or caps.supports_pickup_info:
            continue
        messages.append(
            f"{caps.label} liefert keinen Verkäuferstandort und kann den Umkreis "
            "auch nicht serverseitig einschränken. Angebote dieser Quelle "
            "erscheinen deshalb unabhängig von der Entfernung."
        )
    return messages


def _location_filter(request: SearchRequest) -> LocationFilter | None:
    # Bei reiner Versandsuche darf der Standort die Quellensuche nicht
    # einschränken - sonst wären günstige Angebote weiter weg unsichtbar.
    if request.delivery == "shipping":
        return None
    if not request.postal_code:
        return None
    return LocationFilter(postal_code=request.postal_code, radius_km=request.radius_km or 50.0)


def build_ingest_options(
    runtime: RuntimeSettings,
    request: SearchRequest,
    *,
    price_list: PriceList | None = None,
) -> IngestOptions:
    """Hilfsfunktion für Tests und alternative Aufrufer."""
    interpretation = interpret_product(request.product)
    required_tokens = interpretation.required_tokens if interpretation else []
    required_min_matches = interpretation.required_min_matches if interpretation else 0
    return IngestOptions(
        max_price=request.max_price,
        min_price=minimum_price(request.max_price),
        price_list=price_list,
        delivery=request.delivery,
        postal_code=request.postal_code,
        radius_km=request.radius_km,
        days_back=request.days_back,
        exclude_auctions=runtime.exclude_auctions,
        exclude_unknown_format=runtime.exclude_unknown_format,
        allow_mixed_auction_bin=runtime.allow_mixed_auction_bin,
        exclude_problematic=request.exclude_problematic,
        required_tokens=required_tokens,
        required_min_matches=required_min_matches,
    )


__all__ = [
    "PHASE_LABELS",
    "RunControl",
    "SearchRunner",
    "build_ingest_options",
]
