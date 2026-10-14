"""Kauf-Limits aus der handgepflegten Liste ``Höchstpreise.txt``.

Die Datei beantwortet zwei Fragen auf einmal:

* **Was** gesucht wird - jede Zeile ist eine Komponente (``Name = Preis``).
* **Bis wohin** gekauft wird - der Wert ist die Schmerzgrenze in Euro inkl.
  Versand. Angebote darüber sind kein Schnäppchen mehr und werden weder
  gespeichert noch angezeigt (siehe ``allows``).

Die Datei ist von Hand gepflegt und bleibt es: Abschnitte, Trennlinien und
Hinweistexte sind normal. Überschriften werden nur über eine kleine Tabelle
bekannter Begriffe zugeordnet; was nicht zugeordnet werden kann, wird
übersprungen statt geraten. Eine fehlende Datei ist kein Fehler - dann gelten
schlicht keine Kauf-Limits.

Aufbau einer Zeile::

    NVIDIA GeForce RTX 3080 10GB = 270 €

Der Suchbegriff wird daraus abgeleitet (hier ``RTX 3080 10GB``), weil
Herstellerpräfixe in Anzeigen praktisch nie stehen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

from .normalize import compact_key, normalize_text

logger = logging.getLogger(__name__)

DEFAULT_PRICE_LIST_FILE = "Höchstpreise.txt"

# Abschnittsüberschrift -> (Gruppenschlüssel, Beschriftung, Kategorie)
# Die Kategorie-Schlüssel stammen aus ``backend/categories.py``.
GROUP_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"\b(grafikkarte|grafikkarten|gpu)\b", "gpu", "Grafikkarten", "gpu"),
    (r"\b(cpus?|prozessoren?|ryzen|core ultra|intel core)\b", "cpu", "CPUs", "cpu"),
    (r"\b(ram|arbeitsspeicher|speicherriegel)\b", "ram", "RAM", "ram"),
    (r"\b(ssds?|nvme)\b", "ssd", "SSDs", "ssd"),
    (r"\b(hdds?|festplatten?)\b", "hdd", "Festplatten", "hdd"),
    (r"\b(mainboards?|motherboards?)\b", "mainboard", "Mainboards", "mainboard"),
    (r"\b(netzteile?|psu)\b", "psu", "Netzteile", "psu"),
    (r"\b(kühler|kuehler|lüfter|luefter|wasserkühlung|wasserkuehlung|aio)\b", "cooler", "Kühler", "cooler"),
    (r"\b(gehäuse|gehaeuse|tower)\b", "case", "Gehäuse", "case"),
)

# Herstellerpräfixe, die in Anzeigen praktisch nicht vorkommen (und die Suche
# nur einschränken würden). Reihenfolge: längste Variante zuerst. Der zweite
# Wert ist der Ersatz, denn "Intel Core Ultra" darf sein "Core Ultra" behalten.
_VENDOR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("nvidia geforce ", ""),
    ("nvidia ", ""),
    ("amd radeon ", ""),
    ("amd ", ""),  # "AMD Ryzen 5 3600" -> "Ryzen 5 3600"
    ("intel core ultra ", "Core Ultra "),  # -> "Core Ultra 5 245K"
    ("intel core ", ""),  # "Intel Core i5-12400F" -> "i5-12400F"
    ("intel arc ", "Arc "),  # -> "Arc B580"
    ("intel ", ""),
)

# Speichergrößen ("10GB", "8 GB") unterscheiden Varianten desselben Modells.
# Für den ungenauen Rückfall werden sie entfernt.
_SIZE_RE = re.compile(r"\b\d{1,3}\s?gb\b", re.IGNORECASE)

# Warenzeichen- und Rechtezeichen stehen in echten Anzeigen mitten im
# Modellnamen ("RTX™ 3080 Ti"). ``compact_key`` macht daraus "tm" - damit
# fänden "rtx" und "3080" nie zusammen und die Anzeige bekäme kein Kauf-Limit.
# Für den Abgleich werden diese Zeichen deshalb entfernt.
_TITLE_MARKS_RE = re.compile(r"[™®©]")

# Der Mindestpreis eines Produkts ist ein Viertel seines Höchstpreises. Sehr
# günstige Angebote sind im Gebrauchtmarkt praktisch immer Zubehör, Einzelteile
# oder defekte Ware - die Grenze hält sie aus den Ergebnissen heraus. Sie gilt
# für jedes Produkt und jede Quelle gleich und wird nirgends konfiguriert.
MIN_PRICE_RATIO = 0.25


def minimum_price(max_price: float | None) -> float | None:
    """Mindestpreis zu einem Höchstpreis: ein Viertel davon.

    ``None`` bleibt ``None``: Ohne Höchstpreis gibt es keine Grenze, die sich
    ableiten ließe - dann wird auch nicht gefiltert.
    """
    if max_price is None:
        return None
    return round(float(max_price) * MIN_PRICE_RATIO, 2)


_ENTRY_RE = re.compile(
    r"^(?P<name>[^=]{2,90}?)\s*=\s*(?P<price>\d[\d.,]*)\s*(?:€|eur)?\s*$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"^[-=_*~]{3,}$")


def _parse_price(raw: str) -> float | None:
    """Wandelt "270", "1.234" und "1.234,56" in einen Float."""
    text = raw.strip()
    if not text:
        return None
    if "," in text:
        normalized = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        normalized = text.replace(".", "")  # deutscher Tausenderpunkt
    else:
        normalized = text
    try:
        return float(normalized)
    except ValueError:
        return None


def search_term(name: str) -> str:
    """Leitet den Suchbegriff aus dem Namen der Liste ab.

    ``NVIDIA GeForce RTX 3080 10GB`` -> ``RTX 3080 10GB``
    ``Intel Core i5-12400F``        -> ``i5-12400F``
    """
    text = " ".join(name.split())
    lowered = text.lower()
    for prefix, replacement in _VENDOR_PREFIXES:
        if lowered.startswith(prefix):
            return f"{replacement}{text[len(prefix):]}".strip()
    return text


@dataclass(frozen=True)
class PriceLimit:
    """Eine Komponente der Preisliste mit ihrer Schmerzgrenze."""

    group: str
    group_label: str
    category: str
    name: str
    term: str
    max_price: float

    # Die Schlüssel werden bei jeder Anzeige gebraucht; sie werden deshalb nur
    # einmal berechnet (eine Anfrage prüft leicht tausende Angebote).
    @cached_property
    def full_key(self) -> str:
        return compact_key(self.term)

    @cached_property
    def base_key(self) -> str:
        """Schlüssel ohne Speichergröße - für ungenau betitelte Anzeigen."""
        return compact_key(_SIZE_RE.sub(" ", self.term))

    def keys(self) -> list[str]:
        return [key for key in dict.fromkeys([self.full_key, self.base_key]) if key]

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "group_label": self.group_label,
            "category": self.category,
            "name": self.name,
            "term": self.term,
            "max_price": self.max_price,
        }


@dataclass(frozen=True)
class PriceLimitGroup:
    """Eine Gruppe der Preisliste (z. B. alle Grafikkarten)."""

    key: str
    label: str
    category: str
    count: int
    min_price: float
    max_price: float

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "count": self.count,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }


@dataclass
class PriceList:
    """Alle Komponenten und Kauf-Limits aus der Liste."""

    entries: list[PriceLimit] = field(default_factory=list)
    path: str = ""
    loaded_at: datetime | None = None
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    @property
    def available(self) -> bool:
        return bool(self.entries)

    def groups(self) -> list[PriceLimitGroup]:
        """Gruppen in der Reihenfolge ihres Auftretens in der Datei."""
        buckets: dict[str, list[PriceLimit]] = {}
        for entry in self.entries:
            buckets.setdefault(entry.group, []).append(entry)
        result: list[PriceLimitGroup] = []
        for key, items in buckets.items():
            prices = [item.max_price for item in items]
            result.append(
                PriceLimitGroup(
                    key=key,
                    label=items[0].group_label,
                    category=items[0].category,
                    count=len(items),
                    min_price=min(prices),
                    max_price=max(prices),
                )
            )
        return result

    def group_keys(self) -> set[str]:
        return {entry.group for entry in self.entries}

    def entries_for_groups(self, groups: list[str] | None = None) -> list[PriceLimit]:
        wanted = set(groups or [])
        if not wanted:
            return list(self.entries)
        return [entry for entry in self.entries if entry.group in wanted]

    @cached_property
    def _key_index(self) -> list[tuple[str, PriceLimit]]:
        """(Schlüssel, Eintrag)-Paare - einmal je geladener Liste aufgebaut."""
        return [(key, entry) for entry in self.entries for key in entry.keys()]

    def limit_for(self, *, title: str, model: str | None = None, variant: str | None = None) -> PriceLimit | None:
        """Passendes Kauf-Limit zu einer Anzeige (oder ``None``).

        Der längste passende Schlüssel gewinnt ("RTX 3060 Ti" schlägt
        "RTX 3060"). Passen mehrere gleich lange Schlüssel - etwa "RTX 3080"
        ohne Angabe der Speichergröße -, gilt das **strengste** Limit: Ohne
        Gewissheit über die Variante wird nichts schöngerechnet.
        """
        if not self.entries:
            return None
        text = _TITLE_MARKS_RE.sub(" ", " ".join(part for part in (title, model, variant) if part))
        haystack = compact_key(text)
        if not haystack:
            return None
        matches: dict[str, list[PriceLimit]] = {}
        for key, entry in self._key_index:
            if key in haystack:
                matches.setdefault(key, []).append(entry)
        if not matches:
            return None
        longest = max(matches, key=len)
        return min(matches[longest], key=lambda entry: entry.max_price)

    def models_in_title(self, title: str) -> set[str]:
        """Welche Komponenten der Liste nennt der Titel?

        Gebraucht für die Erkennung von Sammelanzeigen: "Grafikkarten: RTX
        3080Ti 2060 GTX1080 1060 RX580 uvm" nennt mehrere Modelle auf einmal -
        damit laesst sich kein einzelnes Angebot vergleichen.
        """
        return {entry.term for entry in self._matching_entries(title)}

    def model_families_in_title(self, title: str) -> dict[str, str]:
        """Die *Modelle* (ohne Ausbaustufe) aus der Liste, die der Titel nennt.

        Rückgabe: ``Basisschlüssel -> lesbarer Name``. Für eine Sammelanzeige
        zählt das Modell, nicht die Ausbaustufe: "RTX 3080 Ti 12GB" nennt genau
        ein Modell, obwohl die Liste dafür drei Einträge führt ("RTX 3080
        10GB", "RTX 3080 12GB", "RTX 3080 Ti"). Erst mehrere *verschiedene*
        Modelle sind eine Sammelanzeige.
        """
        familien: dict[str, str] = {}
        for entry in self._matching_entries(title):
            schluessel = entry.base_key or entry.full_key
            if not schluessel:
                continue
            # Name ohne Speichergröße steht für die Familie: "RTX 3080", nicht
            # "RTX 3080 10GB".
            anzeige = " ".join(_SIZE_RE.sub(" ", entry.term).split()) or entry.term
            vorhanden = familien.get(schluessel)
            if vorhanden is None or len(anzeige) < len(vorhanden):
                familien[schluessel] = anzeige
        return familien

    def _matching_entries(self, title: str) -> list[PriceLimit]:
        """Einträge, deren Schlüssel im Titel steht - jeder höchstens einmal."""
        if not self.entries or not title:
            return []
        haystack = compact_key(_TITLE_MARKS_RE.sub(" ", title))
        if not haystack:
            return []
        treffer: dict[str, PriceLimit] = {}
        for key, entry in self._key_index:
            if key and key in haystack:
                treffer.setdefault(entry.term, entry)
        return list(treffer.values())

    def allows(
        self,
        *,
        title: str,
        price: float | None,
        model: str | None = None,
        variant: str | None = None,
    ) -> bool:
        """True, wenn der Preis unter dem Limit liegt (oder kein Limit bekannt ist)."""
        if price is None:
            return True
        limit = self.limit_for(title=title, model=model, variant=variant)
        if limit is None:
            return True
        return float(price) <= limit.max_price + 1e-9

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "path": self.path,
            "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
            "groups": [group.to_dict() for group in self.groups()],
            "entries": [entry.to_dict() for entry in self.entries],
            "errors": list(self.errors),
        }


def _match_group(line: str) -> tuple[str, str, str] | None:
    normalized = normalize_text(line)
    for pattern, key, label, category in GROUP_RULES:
        if re.search(pattern, normalized):
            return key, label, category
    return None


def _looks_like_header(line: str) -> bool:
    return len(line) <= 60 and len(line.split()) <= 8


def _next_line_is_separator(lines: list[str], index: int) -> bool:
    for following in lines[index + 1 :]:
        stripped = following.strip()
        if not stripped:
            continue
        return bool(_SEPARATOR_RE.match(stripped))
    return False


def _iter_price_entries(
    text: str,
) -> Iterator[tuple[int, str, float, tuple[str, str, str]]]:
    """Je Eintrag ``(Zeilennummer, Name, Preis, Abschnitt)`` - in Dateireihenfolge.

    Lesen und Schreiben teilen sich diesen Generator. Damit kann
    :func:`rewrite_price_limits` genau die Zahlen ersetzen, die
    :func:`parse_price_limits` gelesen hat - Abschnittslogik und Zeilenfilter
    können nicht auseinanderlaufen.
    """
    lines = text.splitlines()
    section: tuple[str, str, str] | None = None

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or _SEPARATOR_RE.match(line):
            continue

        match = _ENTRY_RE.match(line)
        if match is not None:
            if section is None:
                continue  # Eintrag ohne zuordenbaren Abschnitt - nicht raten
            name = " ".join(match.group("name").split())
            price = _parse_price(match.group("price"))
            if price is None or price <= 0:
                continue
            yield index, name, price, section
            continue

        rule = _match_group(line)
        if _next_line_is_separator(lines, index) or (rule is not None and _looks_like_header(line)):
            # Überschrift: bekannte Gruppe übernehmen, sonst Abschnitt leeren.
            section = rule


def parse_price_limits(text: str) -> list[PriceLimit]:
    """Liest die Listendatei. Unbekannte Abschnitte werden übersprungen."""
    entries: list[PriceLimit] = []
    for _index, name, price, (group, label, category) in _iter_price_entries(text):
        entries.append(
            PriceLimit(
                group=group,
                group_label=label,
                category=category,
                name=name,
                term=search_term(name),
                max_price=price,
            )
        )
    return entries


def _format_price(value: float) -> str:
    """Zahl so schreiben, wie die Liste sie liest ("270", "1.234,50")."""
    rounded = round(float(value))
    if abs(float(value) - rounded) < 0.005:
        return str(int(rounded))
    return f"{value:.2f}".replace(".", ",")


def _backup_path(path: Path) -> Path:
    """Sicherungskopie neben der Datei - wie die vorhandene "_alt_"-Ablage."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{path.stem}_{stamp}{path.suffix}")


def rewrite_price_limits(path: str | Path, updates: Mapping[int, float]) -> tuple[int, str]:
    """Schreibt neue Kauf-Limits in die Listendatei.

    ``updates`` ordnet den **Index** eines Eintrags (0-basiert, in der
    Reihenfolge von :func:`parse_price_limits`) seinem neuen Preis zu. Ersetzt
    werden ausschließlich die Zahlen - Überschriften, Trennlinien, Kommentare
    und Hinweistexte bleiben unverändert, ebenso die Zeilenenden. Vor dem
    Schreiben entsteht eine Sicherungskopie.

    Rückgabe: ``(Anzahl geschriebener Werte, Pfad der Sicherung)``. Ohne
    Änderungen wird nichts geschrieben (``(0, "")``).

    Wirft ``OSError``, wenn die Datei nicht gelesen oder geschrieben werden kann.
    """
    resolved = Path(path)
    wanted = {index: float(price) for index, price in updates.items() if price is not None}
    if not wanted:
        return 0, ""

    raw = resolved.read_bytes().decode("utf-8")
    lines = raw.splitlines(keepends=True)
    written = 0
    for position, (line_index, _name, _price, _section) in enumerate(_iter_price_entries(raw)):
        if position not in wanted:
            continue
        line = lines[line_index]
        lead = len(line) - len(line.lstrip())
        match = _ENTRY_RE.match(line.lstrip())
        if match is None:  # pragma: no cover - defensiv, kann nach dem Parser nicht passieren
            continue
        start = lead + match.start("price")
        end = lead + match.end("price")
        lines[line_index] = f"{line[:start]}{_format_price(wanted[position])}{line[end:]}"
        written += 1

    if not written:
        return 0, ""
    backup = _backup_path(resolved)
    backup.write_bytes(resolved.read_bytes())
    resolved.write_bytes("".join(lines).encode("utf-8"))
    # Cache leeren: Der Schlüssel enthält Änderungszeit und Größe; ein Wert mit
    # gleicher Ziffernzahl könnte sonst innerhalb derselben Sekunde als
    # "unverändert" gelesen werden und der Lauf mit alten Limits weiterrechnen.
    _CACHE.clear()
    return written, str(backup)


_CACHE: dict[tuple[str, float, int], PriceList] = {}


def load_price_list(path: str | Path | None = None) -> PriceList:
    """Lädt die Liste und hält sie aktuell (Cache über Pfad, Größe, Änderungszeit).

    So wirkt jede Änderung an ``Höchstpreise.txt`` sofort, ohne Neustart.
    """
    raw_path = str(path).strip() if path is not None else DEFAULT_PRICE_LIST_FILE
    if not raw_path:
        return PriceList(path="")
    resolved = Path(raw_path)
    try:
        stat = resolved.stat()
    except OSError:
        logger.info("Preisliste '%s' nicht gefunden - es gelten keine Kauf-Limits.", resolved)
        return PriceList(path=str(resolved))

    key = (str(resolved.resolve()), stat.st_mtime, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - Rechte-/Lesefehler
        logger.warning("Preisliste '%s' konnte nicht gelesen werden: %s", resolved, exc)
        return PriceList(path=str(resolved), errors=[f"Preisliste nicht lesbar: {exc}"])

    entries = parse_price_limits(text)
    price_list = PriceList(
        entries=entries,
        path=str(resolved),
        loaded_at=datetime.now(timezone.utc) if entries else None,
    )
    if not entries:
        price_list.errors.append(
            "In der Preisliste wurden keine Komponenten gefunden "
            "(erwartetes Format: 'Name = Preis €' unter einer Abschnittsüberschrift)."
        )
    _CACHE.clear()
    _CACHE[key] = price_list
    return price_list


__all__ = [
    "DEFAULT_PRICE_LIST_FILE",
    "GROUP_RULES",
    "MIN_PRICE_RATIO",
    "PriceLimit",
    "PriceLimitGroup",
    "PriceList",
    "load_price_list",
    "minimum_price",
    "parse_price_limits",
    "rewrite_price_limits",
    "search_term",
]
