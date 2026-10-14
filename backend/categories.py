"""Statische Definition der unterstützten Hardware-Kategorien.

Die Kategorien sind bewusst deterministisch im Code definiert (nicht aus dem Netz
erzeugt), damit Suchplanung, Filter und Wissensbasis immer dieselben,
stabilen Schlüssel verwenden.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    # Deterministische Basis-Suchbegriffe, die IMMER verwendet werden und
    # die Grundlage des Suchplans bilden.
    base_terms: tuple[str, ...]
    # eBay-Kategorie-IDs (Deutschland) zur Eingrenzung, soweit sinnvoll.
    # Quelle: oeffentliche eBay-Kategoriebaum-Struktur; bewusst konservativ
    # gehalten, damit keine falschen Kategorien gefiltert werden.
    ebay_category_ids: tuple[str, ...] = ()
    # Erwartete Groessenordnung für Preisfilter-Vorbelegung (EUR).
    default_max_price: float = 500.0
    # Rauschbegriffe: typische Zubehör- und Verpackungsanzeigen, die keine
    # Komponente verkaufen. Werden als Ausschlussbegriffe lokal angewendet.
    noise_terms: tuple[str, ...] = ()


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="gpu",
        label="GPU / Grafikkarte",
        base_terms=(
            "grafikkarte",
            "gpu",
            "geforce",
            "radeon",
            "rtx",
            "gtx",
            "rx",
            "intel arc",
        ),
        default_max_price=400.0,
        noise_terms=(
            "aufkleber",
            "sticker",
            "handbuch",
            "anleitung",
            "wasserblock",
            "kuehler für",
            "backplate",
            "slotblende",
            "retro",
            "sammler",
        ),
    ),
    Category(
        key="cpu",
        label="CPU / Prozessor",
        base_terms=(
            "prozessor",
            "cpu",
            "ryzen",
            "intel core",
            "xeon",
            "threadripper",
        ),
        default_max_price=300.0,
    ),
    Category(
        key="ram",
        label="RAM",
        base_terms=("arbeitsspeicher", "ram", "ddr4", "ddr5", "speicherriegel"),
        default_max_price=120.0,
        noise_terms=("kuehlkoerper", "heatspreader", "blende", "leer", "dummy"),
    ),
    Category(
        key="mainboard",
        label="Mainboard",
        base_terms=("mainboard", "motherboard", "am4", "am5", "lga1700", "lga1200"),
        default_max_price=200.0,
    ),
    Category(
        key="ssd",
        label="SSD",
        base_terms=("ssd", "nvme", "m.2", "solid state drive"),
        default_max_price=150.0,
        noise_terms=("gehaeuse", "kuehlkoerper", "adapter", "dummy"),
    ),
    Category(
        key="hdd",
        label="HDD",
        base_terms=("festplatte", "hdd", "hard disk"),
        default_max_price=100.0,
        noise_terms=("rahmen", "adapter", "dummy", "gehaeuse für"),
    ),
    Category(
        key="psu",
        label="Netzteil",
        base_terms=("netzteil", "psu", "atx netzteil", "watt netzteil"),
        default_max_price=120.0,
        noise_terms=("kabel", "adapter", "verlängerung", "sleeve"),
    ),
    Category(
        key="cooler",
        label="CPU-Kühler",
        base_terms=("cpu kühler", "cpu kuehler", "lüfter", "wasserkühlung", "aio"),
        default_max_price=100.0,
        noise_terms=("montagekit", "halterung", "schrauben", "adapter"),
    ),
    Category(
        key="case",
        label="Gehäuse",
        base_terms=("gehäuse", "gehaeuse", "pc case", "tower"),
        default_max_price=120.0,
        noise_terms=("seitenteil", "frontpanel", "standfuss", "schrauben"),
    ),
    Category(
        key="complete_pc",
        label="Komplett-PC",
        base_terms=("komplett pc", "komplettrechner", "gaming pc", "rechner"),
        default_max_price=800.0,
    ),
    Category(
        key="other",
        label="Sonstige PC-Komponenten",
        base_terms=("pc komponente", "pc zubehör"),
        default_max_price=100.0,
        noise_terms=("kabel", "schrauben", "anleitung", "handbuch"),
    ),
)

CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
CATEGORY_KEYS: tuple[str, ...] = tuple(c.key for c in CATEGORIES)


def get_category(key: str) -> Category | None:
    return CATEGORY_BY_KEY.get(key)
