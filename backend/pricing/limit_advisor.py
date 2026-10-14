"""Kauf-Limits aus beobachteten Marktpreisen nachführen.

Warum es dieses Modul gibt
--------------------------
Die Liste ``Höchstpreise.txt`` ist handgepflegt - und genau darin lag das
Problem vom 13.10.2026: Die Werte waren veraltet, ein ganzer Suchlauf über 198
Anfragen speicherte **0** Angebote, weil alles über dem zu niedrigen Limit lag.
Danach wurden alle Werte pauschal um Faktor 1,5 angehoben, wieder von Hand.

Dieses Modul schließt die Lücke: Es liest die bereits **beobachteten Angebote**
aus der eigenen Datenbank und leitet je Komponente ein Limit aus dem
**günstigsten Zehntel** dieser Angebote ab. Es wird nichts geschätzt und nichts
erfunden: Jede Empfehlung steht auf einer nachvollziehbaren Datenbasis (Anzahl
Beobachtungen, Quartile), und bei zu dünner Datenlage bleibt das Limit
unverändert.

Ehrliche Grenzen
----------------
* Beobachtete Angebotspreise sind **keine erzielten Verkaufspreise**. Ein daraus
  abgeleitetes Limit ist eine Orientierung, keine Wahrheit.
* Ein bereits zu enges Limit heilt dieses Verfahren nicht von selbst: Was die
  Suche nie geladen hat, steht nicht in der Datenbank. Deshalb werden neue
  Kategorien bewusst mit einem **großzügigen Startwert** aufgenommen, den der
  Berater anschließend nach unten zieht.
* Beide Richtungen werden begrenzt (``max_change_ratio``): Ein einzelner Lauf
  verschiebt ein Limit höchstens um diesen Anteil, damit dünne Stichproben keine
  Sprünge erzeugen. Mehrere Läufe konvergieren.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.price_limits import PriceList
from ..models import FORMAT_FIXED_PRICE, STATUS_ACTIVE, Listing

logger = logging.getLogger(__name__)

# Zielniveau: das günstigste Zehntel - dieselbe Faustregel, die schon im Kopf der
# Liste steht ("die Werte liegen etwa im günstigsten Zehntel der Angebote").
TARGET_QUANTILE = 0.10
# Obergrenze der Veränderung je Durchlauf (0.5 = ±50 %). Verhindert, dass eine
# dünne Stichprobe das Limit springen lässt.
MAX_CHANGE_RATIO = 0.5
# Preise werden auf diesen Betrag gerundet - die Liste selbst ist so gepflegt.
ROUND_TO = 5.0
# Unter so vielen vergleichbaren Beobachtungen gibt es keine Empfehlung.
MIN_REQUIRED_OBSERVATIONS = 3

DIRECTION_KEEP = "halten"
DIRECTION_RAISE = "anheben"
DIRECTION_LOWER = "senken"
DIRECTION_NO_DATA = "keine_daten"

# Ab dieser Abweichung gilt eine Empfehlung als Änderung (5 %).
KEEP_TOLERANCE = 0.05

DISCLAIMER = (
    "Beobachtete Angebotspreise sind keine erzielten Verkaufspreise. Die "
    "Empfehlung ist eine Orientierung aus dem günstigsten Zehntel der "
    "gespeicherten Angebote - keine Wahrheit."
)


def _quantile(values: list[float], q: float) -> float:
    """Lineare Quantilsinterpolation (0 <= q <= 1)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("leere Werteliste")
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _round_to_step(value: float, step: float = ROUND_TO) -> float:
    """Rundet auf ein Vielfaches von ``step`` (mindestens ``step``)."""
    if step <= 0:
        return float(value)
    return max(step, round(float(value) / step) * step)


def _as_int_if_whole(value: float) -> float:
    rounded = round(value)
    return float(rounded) if abs(value - rounded) < 1e-9 else round(value, 2)


@dataclass(frozen=True)
class LimitSuggestion:
    """Vorschlag für das Kauf-Limit einer Komponente."""

    index: int
    group: str
    group_label: str
    category: str
    name: str
    term: str
    current_max_price: float
    suggested_max_price: float | None
    observations: int
    min_total: float | None
    q10: float | None
    median: float | None
    q75: float | None
    direction: str
    reason: str

    @property
    def change_pct(self) -> float | None:
        if self.suggested_max_price is None or self.current_max_price <= 0:
            return None
        return round(
            (self.suggested_max_price - self.current_max_price) / self.current_max_price * 100.0, 1
        )

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "group": self.group,
            "group_label": self.group_label,
            "category": self.category,
            "name": self.name,
            "term": self.term,
            "current_max_price": self.current_max_price,
            "suggested_max_price": self.suggested_max_price,
            "observations": self.observations,
            "min_total": self.min_total,
            "q10": self.q10,
            "median": self.median,
            "q75": self.q75,
            "direction": self.direction,
            "change_pct": self.change_pct,
            "reason": self.reason,
        }


@dataclass
class LimitAdviceReport:
    """Ergebnis eines Beratungsdurchgangs."""

    generated_at: datetime
    path: str
    min_observations: int
    window_days: int
    target_quantile: float
    max_change_ratio: float
    components: int
    with_data: int
    changes: int
    suggestions: list[LimitSuggestion] = field(default_factory=list)
    summary: str = ""
    disclaimer: str = DISCLAIMER
    applied: int = 0
    backup_path: str | None = None

    def actionable(self) -> dict[int, float]:
        """Index -> neuer Preis, nur für echte Änderungen (nicht ``halten``)."""
        return {
            item.index: float(item.suggested_max_price)
            for item in self.suggestions
            if item.direction in (DIRECTION_RAISE, DIRECTION_LOWER)
            and item.suggested_max_price is not None
        }

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "path": self.path,
            "min_observations": self.min_observations,
            "window_days": self.window_days,
            "target_quantile": self.target_quantile,
            "max_change_ratio": self.max_change_ratio,
            "components": self.components,
            "with_data": self.with_data,
            "changes": self.changes,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "summary": self.summary,
            "disclaimer": self.disclaimer,
            "applied": self.applied,
            "backup_path": self.backup_path,
        }


def _observe(
    session: Session | None,
    price_list: PriceList,
    *,
    window_days: int,
    now: datetime,
) -> dict[int, list[float]]:
    """Gesamtpreise je Komponente, aus den gespeicherten, aktiven Angeboten.

    Der Schlüssel ist der Index des Eintrags in der Liste. Zuordnung über
    ``PriceList.limit_for`` - also über dieselbe Regel, mit der ein Angebot
    gespeichert und angezeigt wird. Was dort kein Limit hat, zählt hier nicht.
    """
    buckets: dict[int, list[float]] = {}
    if session is None or not price_list.available:
        return buckets

    position_of = {id(entry): position for position, entry in enumerate(price_list.entries)}
    cutoff = now - timedelta(days=max(1, int(window_days)))
    rows = session.scalars(
        select(Listing).where(
            Listing.status == STATUS_ACTIVE,
            Listing.listing_format == FORMAT_FIXED_PRICE,
            Listing.total_price.is_not(None),
        )
    ).all()

    for row in rows:
        seen = row.last_seen_at or row.fetched_at
        if seen is not None:
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen < cutoff:
                continue
        total = row.total_price
        if total is None or float(total) <= 0:
            continue
        # Defekte Ware (Rang 0) ist kein Marktpreis für funktionierende Hardware.
        if row.condition_rank == 0:
            continue
        limit = price_list.limit_for(
            title=row.title or "", model=row.model, variant=row.variant
        )
        if limit is None:
            continue
        position = position_of.get(id(limit))
        if position is None:  # pragma: no cover - defensiv
            continue
        buckets.setdefault(position, []).append(float(total))
    return buckets


def advise_limits(
    session: Session | None,
    price_list: PriceList,
    *,
    min_observations: int = 5,
    window_days: int = 150,
    target_quantile: float = TARGET_QUANTILE,
    max_change_ratio: float = MAX_CHANGE_RATIO,
    now: datetime | None = None,
) -> LimitAdviceReport:
    """Leitet je Komponente ein Kauf-Limit aus den beobachteten Preisen ab."""
    generated = now or datetime.now(timezone.utc)
    minimum = max(MIN_REQUIRED_OBSERVATIONS, int(min_observations))
    report = LimitAdviceReport(
        generated_at=generated,
        path=price_list.path,
        min_observations=minimum,
        window_days=int(window_days),
        target_quantile=target_quantile,
        max_change_ratio=max_change_ratio,
        components=len(price_list.entries),
        with_data=0,
        changes=0,
    )
    if not price_list.available:
        report.summary = "Es ist keine Kauf-Limit-Liste geladen - es gibt nichts nachzuführen."
        return report

    buckets = _observe(session, price_list, window_days=window_days, now=generated)

    for position, entry in enumerate(price_list.entries):
        values = [value for value in buckets.get(position, []) if value > 0]
        count = len(values)
        if count >= minimum:
            report.with_data += 1
            q10 = _quantile(values, target_quantile)
            median = statistics.median(values)
            q75 = _quantile(values, 0.75)
            proposed = _round_to_step(q10)
            low = entry.max_price * (1 - max_change_ratio)
            high = entry.max_price * (1 + max_change_ratio)
            clamped = min(max(proposed, low), high)
            suggested = _as_int_if_whole(_round_to_step(clamped))
            suggested = max(1.0, suggested)

            if suggested >= entry.max_price * (1 + KEEP_TOLERANCE):
                direction = DIRECTION_RAISE
            elif suggested <= entry.max_price * (1 - KEEP_TOLERANCE):
                direction = DIRECTION_LOWER
            else:
                direction = DIRECTION_KEEP

            basis = (
                f"günstigstes Zehntel von {count} vergleichbaren Angeboten: "
                f"{_as_int_if_whole(round(q10, 2)):.0f} € "
                f"(Median {_as_int_if_whole(round(median, 2)):.0f} €, "
                f"75 % ab {_as_int_if_whole(round(q75, 2)):.0f} €)"
            )
            if direction == DIRECTION_KEEP:
                reason = (
                    f"{basis}. Die Empfehlung liegt innerhalb von 5 % des aktuellen "
                    f"Limits ({_as_int_if_whole(entry.max_price):.0f} €) - keine Änderung."
                )
            elif direction == DIRECTION_RAISE:
                reason = (
                    f"{basis}. Empfehlung {suggested:.0f} € liegt über dem aktuellen "
                    f"Limit ({_as_int_if_whole(entry.max_price):.0f} €) - der Markt liegt höher."
                )
            else:
                reason = (
                    f"{basis}. Empfehlung {suggested:.0f} € liegt unter dem aktuellen "
                    f"Limit ({_as_int_if_whole(entry.max_price):.0f} €) - der Markt liegt tiefer."
                )
            if abs(clamped - proposed) > 1e-9:
                reason += (
                    f" Auf ±{int(max_change_ratio * 100)} % je Durchlauf begrenzt "
                    "(mehrere Läufe konvergieren)."
                )
            if direction != DIRECTION_KEEP:
                report.changes += 1
            report.suggestions.append(
                LimitSuggestion(
                    index=position,
                    group=entry.group,
                    group_label=entry.group_label,
                    category=entry.category,
                    name=entry.name,
                    term=entry.term,
                    current_max_price=entry.max_price,
                    suggested_max_price=suggested,
                    observations=count,
                    min_total=round(min(values), 2),
                    q10=round(q10, 2),
                    median=round(median, 2),
                    q75=round(q75, 2),
                    direction=direction,
                    reason=reason,
                )
            )
        else:
            report.suggestions.append(
                LimitSuggestion(
                    index=position,
                    group=entry.group,
                    group_label=entry.group_label,
                    category=entry.category,
                    name=entry.name,
                    term=entry.term,
                    current_max_price=entry.max_price,
                    suggested_max_price=None,
                    observations=count,
                    min_total=round(min(values), 2) if values else None,
                    q10=None,
                    median=None,
                    q75=None,
                    direction=DIRECTION_NO_DATA,
                    reason=(
                        f"nur {count} vergleichbare Angebote - für eine Empfehlung sind "
                        f"mindestens {minimum} nötig. Das Limit bleibt unverändert."
                    ),
                )
            )

    ohne_daten = report.components - report.with_data
    if report.changes:
        report.summary = (
            f"{report.changes} von {report.components} Komponenten mit neuer Empfehlung "
            f"({report.with_data} mit ausreichender Datenbasis, {ohne_daten} ohne)."
        )
    else:
        report.summary = (
            f"Keine Änderung vorgeschlagen: {report.with_data} von {report.components} "
            f"Komponenten haben eine ausreichende Datenbasis, {ohne_daten} haben keine."
        )
    return report


__all__ = [
    "DIRECTION_KEEP",
    "DIRECTION_LOWER",
    "DIRECTION_NO_DATA",
    "DIRECTION_RAISE",
    "DISCLAIMER",
    "LimitAdviceReport",
    "LimitSuggestion",
    "advise_limits",
]
