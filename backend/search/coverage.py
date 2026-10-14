"""Suchabdeckungs-Bericht (Abschnitt 6).

Grundregel: Der Begriff "vollständig durchsucht" wird nur verwendet, wenn
das für den jeweiligen Suchraum tatsächlich belegbar ist. In der Praxis ist
das bei keiner der angebundenen Quellen der Fall, deshalb wird immer eine
Abdeckung mit Einschränkungen und expliziten Unsicherheiten ausgewiesen.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..connectors.registry import PLATFORM_LABELS
from ..models import SearchQuery, SearchRun

STATUS_OK = "ok"


# Anzeigenamen der Suchansichten (Sortierungen), die ein Connector liefert.
SORT_LABELS: dict[str, str] = {
    "": "Standardsicht",
    "newest": "neueste zuerst",
    "price": "Preis aufsteigend",
}


def _label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform)


def view_label(sort: str | None) -> str:
    """Klartext zur Suchansicht einer Anfrage."""
    return SORT_LABELS.get(sort or "", sort or "Standardsicht")


def build_coverage(run: SearchRun, queries: list[SearchQuery]) -> dict:
    """Aggregiert die tatsächlich ausgeführten Anfragen zu einem Bericht."""
    by_platform: dict[str, dict] = {}

    for query in queries:
        bucket = by_platform.setdefault(
            query.platform,
            {
                "platform": query.platform,
                "label": _label(query.platform),
                "queries": 0,
                "successful": 0,
                "failed": 0,
                "skipped": 0,
                "results_returned": 0,
                "results_usable": 0,
                "new_listings": 0,
                "duplicates": 0,
                "excluded_auctions": 0,
                "excluded_unknown_format": 0,
                "excluded_price": 0,
                "excluded_title_filter": 0,
                "excluded_terms": 0,
                "offset_reached": 0,
                "reported_total_sum": 0,
                "queries_with_total": 0,
                "truncated_queries": 0,
                "limits_reached": [],
                "errors": [],
                "queries_without_reported_total": 0,
            },
        )
        bucket["queries"] += 1
        if query.status == STATUS_OK:
            bucket["successful"] += 1
        elif query.status == "skipped":
            bucket["skipped"] += 1
        else:
            bucket["failed"] += 1
            if query.error:
                bucket["errors"].append(f"{query.term}: {query.error}")
        bucket["results_returned"] += query.results_returned or 0
        bucket["results_usable"] += query.results_usable or 0
        bucket["new_listings"] += query.new_listings or 0
        bucket["duplicates"] += query.duplicates or 0
        bucket["excluded_auctions"] += query.excluded_auctions or 0
        bucket["excluded_unknown_format"] += query.excluded_unknown_format or 0
        bucket["excluded_price"] += query.excluded_price or 0
        bucket["excluded_title_filter"] += getattr(query, "excluded_title_filter", 0) or 0
        bucket["offset_reached"] += query.offset_reached or 0
        if query.reported_total is not None:
            bucket["reported_total_sum"] += query.reported_total
            bucket["queries_with_total"] += 1
        else:
            bucket["queries_without_reported_total"] += 1
        if query.truncated:
            bucket["truncated_queries"] += 1
            if len(bucket["limits_reached"]) < 8:
                bucket["limits_reached"].append(
                    f"'{query.term}' ({view_label(getattr(query, 'sort', None))}): "
                    f"Ergebnisliste wurde abgeschnitten "
                    f"(Limit erreicht, {query.offset_reached} von "
                    f"{query.reported_total if query.reported_total is not None else '?'} "
                    "erreichbaren Treffern abgerufen)"
                )

    platforms: list[dict] = []
    estimate_reliable = True
    for bucket in by_platform.values():
        # Abdeckung nur, wenn die Quelle für JEDE erfolgreiche Anfrage eine
        # Gesamttrefferzahl gemeldet hat. Sonst ist keine belastbare Aussage
        # möglich und es wird bewusst keine Zahl erfunden.
        if bucket["successful"] == 0:
            bucket["coverage_ratio"] = None
            bucket["coverage_label"] = "Keine erfolgreiche Suche - Abdeckung 0 %"
        elif bucket["queries_without_reported_total"] > 0:
            bucket["coverage_ratio"] = None
            bucket["coverage_label"] = (
                "Abdeckung nicht bestimmbar: die Quelle hat nicht für jede "
                "Anfrage eine Gesamttrefferzahl gemeldet."
            )
            estimate_reliable = False
        else:
            total = bucket["reported_total_sum"]
            processed = min(bucket["offset_reached"], total) if total else 0
            ratio = (processed / total) if total else 0.0
            bucket["coverage_ratio"] = round(ratio, 4)
            bucket["coverage_label"] = (
                f"{processed} von {total} von der Quelle gemeldeten Treffern "
                f"abgerufen ({ratio * 100:.0f} %) - grobe Näherung, da sich "
                "Suchanfragen überlappen können"
            )
        bucket["errors"] = bucket["errors"][:10]
        platforms.append(bucket)

    platforms.sort(key=lambda item: item["platform"])

    totals = {
        "queries": sum(p["queries"] for p in platforms),
        "successful": sum(p["successful"] for p in platforms),
        "failed": sum(p["failed"] for p in platforms),
        "skipped": sum(p["skipped"] for p in platforms),
        "results_returned": sum(p["results_returned"] for p in platforms),
        "results_usable": sum(p["results_usable"] for p in platforms),
        "new_listings": sum(p["new_listings"] for p in platforms),
        "duplicates": sum(p["duplicates"] for p in platforms),
        "excluded_auctions": sum(p["excluded_auctions"] for p in platforms),
        "excluded_unknown_format": sum(p["excluded_unknown_format"] for p in platforms),
        "excluded_price": sum(p["excluded_price"] for p in platforms),
        "excluded_title_filter": sum(p["excluded_title_filter"] for p in platforms),
    }

    not_retrievable: list[str] = []
    for platform in platforms:
        if platform["queries"] == 0:
            continue
        if platform["successful"] == 0:
            reason = platform["errors"][0] if platform["errors"] else "unbekannter Fehler"
            not_retrievable.append(f"{platform['label']}: keine Ergebnisse abrufbar ({reason})")
        elif platform["queries_without_reported_total"] > 0:
            not_retrievable.append(
                f"{platform['label']}: Gesamtzahl der Treffer nicht gemeldet - "
                "vollständige Abdeckung nicht nachweisbar"
            )
        for limit in platform["limits_reached"][:3]:
            not_retrievable.append(f"{platform['label']}: {limit}")

    # Plattformen, die angefragt wurden, aber nicht einmal eine Anfrage
    # ausgeführt haben (z. B. fehlende Zugangsdaten).
    executed = {p["platform"] for p in platforms}
    for limitation in (run.limitations or []):
        if isinstance(limitation, dict) and limitation.get("platform") not in executed:
            not_retrievable.append(str(limitation.get("message", "")))

    notes = [
        "Angebotspreise sind keine erzielten Verkaufspreise.",
        "Die Abdeckung bezieht sich ausschließlich auf die über zulässige "
        "Schnittstellen erreichbaren Angebote.",
    ]
    if not estimate_reliable:
        notes.append(
            "Für mindestens eine Plattform konnte keine belastbare Abdeckungsquote "
            "bestimmt werden. Es wird bewusst keine Zahl ausgewiesen."
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
        "totals": totals,
        "not_retrievable": not_retrievable[:12],
        "estimate_reliable": estimate_reliable,
        "fully_searched": False,
        "fully_searched_reason": (
            "Keine der angebundenen Quellen erlaubt eine nachweisbar vollständige "
            "Suche des gesamten Marktplatzes."
        ),
        "notes": notes,
    }


def coverage_statement(coverage: dict) -> str:
    """Kurze deutsche Zusammenfassung für die UI."""
    totals = coverage.get("totals", {})
    if not totals.get("queries"):
        return "Es wurden keine Suchanfragen ausgeführt."
    parts = [
        f"{totals['successful']} von {totals['queries']} Suchanfragen erfolgreich",
        f"{totals['results_returned']} Roh-Treffer",
        f"{totals['excluded_auctions']} Auktionen ausgeschlossen",
    ]
    if totals.get("excluded_title_filter"):
        parts.append(f"{totals['excluded_title_filter']} Titel passten nicht gesuchtem Produkt")
    if totals.get("failed"):
        parts.append(f"{totals['failed']} fehlgeschlagen")
    return "; ".join(parts) + "."
