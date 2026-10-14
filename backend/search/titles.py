"""Titelprüfung: Was wird in dieser Anzeige eigentlich verkauft?

Warum es diesen Schritt gibt
----------------------------
Marktplätze liefern zu einem Modellnamen alles, was das Modell *erwähnt*: das
Produkt selbst, Einzelteile ("Lüfter für RTX 3060"), Zubehör (Wasserblock,
NVLink-Bridge, Aufkleber), Verpackung ("Karton", "BOX ONLY"), Laptop-Netzteile
für Geräte *mit* dieser Karte, fremde Geräte (Laptop, Server-Baugruppe) und
Suchgesuche ("Suche RTX 3070"). Ohne diesen Schritt landen davon vor allem die
billigen Fälle in der Ergebnisliste - genau die, die niemand sucht.

Der Ansatz
----------
Statt einer wachsenden Liste einzelner Verbote wird je Titel die **Angebotsart**
bestimmt. Jede Art hat eine eigene, klar benannte Begründung, und die Prüfung
geht von den eindeutigen Fällen zu den feinen:

1. ``ABSOLUTE_PHRASES`` - Wendungen, die für sich stehen ("box only",
   "ohne Karte", "bitte um Hilfe"). Sie sind nie eine Produkteigenschaft.
2. ``FOREIGN_PHRASES`` - fremdes Gerät oder fremde Baugruppe (Laptop, Notebook,
   Server-Backplane, Docking-Station).
3. ``PART_PHRASES`` - eindeutige Teile und Zubehör (Wasserblock, Heat sink,
   Thermal module, NVLink-Bridge, Kühlkörper, Riser, Adapter, Trim-Kit). Dazu
   ``PART_SUFFIXES`` für Teilwörter, die nur als Bestandteil eines
   zusammengesetzten Wortes stehen ("Ersatzhalterung", "Riserkabel") - die
   Tabellen mit ganzen Wörtern finden sie sonst nicht.
4. **Kopfwort**: Das Hauptwort am Ende eines Titels benennt das Verkaufte
   ("... RTX 5700(XT) Lüfter", "... Reference Cooler", "RTX5060ti Karton").
   Nur wenn das Kopfwort ein Teil, eine Verpackung oder eine Dienstleistung ist,
   fällt der Titel - ein "Dual Fan" mitten im Titel bleibt erlaubt, weil dort
   die Karte das Produkt ist.
5. **Teil + Verbindung**: "Lüfter für RTX 3060" verkauft einen Lüfter, egal
   welches Wort davor steht.
6. **Teilwort am Titelanfang**: "Graka Lüfter Zotac RTX 2060 ..." - dort
   benennt das erste inhaltstragende Wort das Verkaufte. Ein Zählwort davor
   ("Dual Fan") macht es zur Eigenschaft.
7. **Ausdrücklich nicht dabei**: "Ohne RTX 5090", "keine Grafikkarte" - das
   Modell wird nur verneint. Solche Anzeigen verkaufen ein anderes Gerät.
8. **Komplette Geräte**: Notebook- und PC-Baureihen ("Razer Blade", "Legion
   Pro", "Gaming Laptop"), Server-Baugruppen, Docking-/eGPU-Gehäuse und
   Laufwerksgrößen ("1TB") gibt es an einer Karte nicht.
9. **Suchgesuche**: "Suche", "gesucht", "Tausch gegen", "kaufe".
10. **Sammelanzeige**: drei oder mehr verschiedene Modelle der Liste in einem
    Titel - damit lässt sich kein Angebot vergleichen.

**Beigaben**: Steht vor einem Teil- oder Verpackungswort ein "mit", "inkl.",
"inklusive", "in" oder "ohne", beschreibt es die Karte und schließt nicht aus
("RTX 3070 Ti ... inklusive OVP/Originalverpackung"). "nur Verpackung" und
"BOX ONLY" schlagen jede solche Ausstattungsangabe.

Zum Schluss greift wie bisher der Modell-Abgleich mit der Kauf-Limit-Liste: Der
Titel muss eine Komponente der Liste nennen (und die muss zur gesuchten Gruppe
gehören). Ist das gesuchte Produkt selbst das Teil ("Gehäuse", "Kühler"),
schützen ``CATEGORY_OWN_NOUNS`` und die Vorgabe dessen Wörter.

Beschreibung als Rückfall
-------------------------
Der Titel genügt in der Regel - aber nicht immer: Kleinanzeigen zeigt unter dem
Titel häufig nur "Grafikkarte" und nennt das Modell im Text; die eBay-API
liefert eine Kurzbeschreibung mit. Nennt der Titel die Komponente **nicht**
(Urteil ``ART_MODELL``), entscheidet deshalb die **Beschreibung** - beurteilt
mit denselben Regeln (``match_product``). Die Rettung greift ausdrücklich nur
dort: Verkauft der Titel erkennbar ein Teil, Zubehör, eine Verpackung, ein
fremdes Gerät oder ist es ein Suchgesuch, bleibt es dabei.

Alles ist nachvollziehbar und reproduzierbar: keine Antwort, keine Wartezeit,
kein Modell. Defekte und Neuware prüft ``ingest.defect_or_new_reason`` - dort
steht auch der Zustand des Angebots zur Verfügung.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domain.normalize import compact_key, match_key_contains, normalize_text, tokenize
from ..domain.price_limits import PriceList

logger = logging.getLogger(__name__)

# Was als "breit" gilt: Bei solchen Anfragen liefert die Plattform deutlich mehr
# Treffer, als eine Suchansicht abdeckt.
BROAD_FAMILIES: frozenset[str] = frozenset(
    {"generic", "title_keyword", "description_hint", "incomplete_title"}
)

# Quellen, die keine Marktplatz-Suche sind: Der lokale Import enthält bereits
# ausgewählte Angebote des Benutzers. Dort wird nicht gefiltert.
SKIP_PLATFORMS: frozenset[str] = frozenset({"local_import"})

QUELLE_REGEL = "regel"

# Die Art des Angebots. Sie steht neben der Begründung in den Zählern, damit in
# der Oberfläche sichtbar ist, *warum* die große Mehrheit der geprüften Titel
# ausscheidet (Teile, Verpackung, fremde Geräte, Suchgesuche).
ART_TEIL = "teil"
ART_VERPACKUNG = "verpackung"
ART_FREMDGERAET = "fremdgeraet"
ART_SUCHGESUCH = "suchgesuch"
ART_SAMMELANZEIGE = "sammelanzeige"
ART_MODELL = "modell"

ART_LABELS: dict[str, str] = {
    ART_TEIL: "Teile/Zubehör",
    ART_VERPACKUNG: "Verpackung",
    ART_FREMDGERAET: "fremdes Gerät",
    ART_SUCHGESUCH: "Suchgesuche",
    ART_SAMMELANZEIGE: "Sammelanzeigen",
    ART_MODELL: "anderes Modell",
}

# --------------------------------------------------------------------------
# 1. Wendungen, die für sich stehen
# --------------------------------------------------------------------------
# Diese Wendungen benennen selbst, was verkauft wird. Sie stehen in echten
# Anzeigen nie als Eigenschaft einer Karte.
ABSOLUTE_PHRASES: tuple[tuple[str, str], ...] = (
    # Verpackung
    ("box only", "es wird nur die Verpackung verkauft ('box only')"),
    ("only the box", "es wird nur die Verpackung verkauft ('only the box')"),
    ("empty box", "es wird nur die leere Verpackung verkauft ('empty box')"),
    ("no graphics card", "es wird nur die Verpackung verkauft (ohne Grafikkarte)"),
    ("ohne grafikkarte", "es wird nur die Verpackung verkauft (ohne Grafikkarte)"),
    ("ohne gpu", "es wird nur die Verpackung verkauft (ohne GPU)"),
    ("ohne karte", "es wird nur die Verpackung verkauft (ohne Karte)"),
    ("nur originalverpackung", "es wird nur die Verpackung verkauft (nur Originalverpackung)"),
    ("nur verpackung", "es wird nur die Verpackung verkauft"),
    ("ovp ohne gpu", "es wird nur die Verpackung verkauft (OVP ohne GPU)"),
    ("keine grafikkarte", "es wird nur die Verpackung verkauft (ohne Grafikkarte)"),
    ("keine gpu", "es wird nur die Verpackung verkauft (ohne GPU)"),
    ("nur ovp", "es wird nur die Verpackung verkauft ('nur OVP')"),
    ("nur karton", "es wird nur die Verpackung verkauft ('nur Karton')"),
    ("nur anleitung", "es wird nur Verpackung oder Papiere verkauft ('nur Anleitung')"),
    ("nur handbuch", "es wird nur Verpackung oder Papiere verkauft ('nur Handbuch')"),
    ("nur rechnung", "es wird nur Verpackung oder Papiere verkauft ('nur Rechnung')"),
    ("leere verpackung", "es wird nur die Verpackung verkauft (leer)"),
    ("leerer karton", "es wird nur die Verpackung verkauft (leerer Karton)"),
    ("nur das gehäuse", "es wird nur die Abdeckung verkauft"),
    # Dienstleistungen, Tausch, Beratung
    ("bitte um hilfe", "es ist eine Frage oder ein Hilfegesuch"),
    ("wer kann helfen", "es ist eine Frage oder ein Hilfegesuch"),
    ("reparatur service", "es ist eine Dienstleistung"),
    ("tausch gegen", "es ist ein Tauschangebot"),
    ("zum tausch", "es ist ein Tauschangebot"),
    ("nachbau", "es ist ein Nachbau oder eine Attrappe"),
    ("attrappe", "es ist ein Nachbau oder eine Attrappe"),
    ("replika", "es ist ein Nachbau oder eine Attrappe"),
)

# --------------------------------------------------------------------------
# 2. Fremdes Gerät oder fremde Baugruppe
# --------------------------------------------------------------------------
# Diese Wörter benennen ein Gerät, in dem ein solches Modell steckt - oder eine
# Baugruppe, die zufällig ähnlich heißt. Keines davon ist das gesuchte Produkt.
FOREIGN_PHRASES: tuple[tuple[str, str], ...] = (
    ("gaming laptop", "es ist ein komplettes Notebook"),
    ("notebook", "es ist ein komplettes Notebook"),
    ("laptop", "es ist ein komplettes Notebook"),
    ("netbook", "es ist ein komplettes Notebook"),
    ("ultrabook", "es ist ein komplettes Notebook"),
    ("macbook", "es ist ein komplettes Notebook"),
    ("thinkpad", "es ist ein komplettes Notebook"),
    ("ideapad", "es ist ein komplettes Notebook"),
    ("vivobook", "es ist ein komplettes Notebook"),
    ("zephyrus", "es ist ein Notebook-Modell (kein Einzelteil)"),
    ("helios", "es ist ein Notebook-Modell (kein Einzelteil)"),
    ("nitro 5", "es ist ein Notebook-Modell (kein Einzelteil)"),
    ("legion 5", "es ist ein Notebook-Modell (kein Einzelteil)"),
    ("raider", "es ist ein Notebook-Modell (kein Einzelteil)"),
    ("trident 3", "es ist ein Komplett-PC (kein Einzelteil)"),
    ("alienware", "es ist ein Komplett-Gerät (kein Einzelteil)"),
    ("pavilion", "es ist ein Komplett-Gerät (kein Einzelteil)"),
    ("aspire", "es ist ein Komplett-Gerät (kein Einzelteil)"),
    ("docking", "es ist eine Docking-Station"),
    ("dock station", "es ist eine Docking-Station"),
    ("egpu", "es ist ein externes Gehäuse (eGPU)"),
    ("server", "es ist eine Server-Baugruppe"),
    ("integrity", "es ist eine Server-Baugruppe (HP Integrity)"),
    ("backplane", "es ist eine Server-Baugruppe (Backplane)"),
    ("pci-x", "es ist eine Server-Baugruppe"),
    ("core i/o", "es ist eine Server-Baugruppe"),
    ("i/o upgrade", "es ist eine Server-Baugruppe"),
    ("speicher erweiterung", "es ist eine Speicher-Erweiterung"),
    ("48-slot", "es ist eine Server-Baugruppe"),
    ("8-slot", "es ist eine Server-Baugruppe"),
    ("sas ", "es ist eine Server-Baugruppe (SAS)"),
    # Netzteile und Ladegeräte: "Netzteil für Dell XPS mit RTX 3060" nennt die
    # Karte, verkauft aber das Netzteil.
    ("netzteil", "es ist ein Netzteil"),
    ("power supply", "es ist ein Netzteil"),
    ("ladegerät", "es ist ein Ladegerät"),
    ("ladegeraet", "es ist ein Ladegerät"),
    ("charger", "es ist ein Ladegerät"),
    ("adp", "es ist ein Ladegerät (Delta ADP)"),
    # Komplette Geräte: Ein PC oder Notebook enthält die Karte, verkauft wird
    # aber das ganze Gerät. Diese Wörter benennen das Gerät selbst und stehen in
    # einer Karten-Anzeige nie.
    ("gaming pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("gaming-pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("gaming laptop", "es ist ein komplettes Notebook"),
    ("gaming-laptop", "es ist ein komplettes Notebook"),
    ("komplett-pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("komplett pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("komplettsystem", "es ist ein komplettes System (kein Einzelteil)"),
    ("komplettrechner", "es ist ein kompletter Rechner (kein Einzelteil)"),
    ("desktop-pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("desktop pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("mini-pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("mini pc", "es ist ein kompletter PC (kein Einzelteil)"),
    ("all-in-one", "es ist ein Komplettgerät (kein Einzelteil)"),
    ("workstation", "es ist eine Workstation (kein Einzelteil)"),
    ("imac", "es ist ein Komplettgerät (kein Einzelteil)"),
    ("mac pro", "es ist ein Komplettgerät (kein Einzelteil)"),
    # Notebook-Baureihen: Sie benennen ein Gerät, in dem das Modell steckt.
    ("razer blade", "es ist ein komplettes Notebook"),
    ("strix scar", "es ist ein komplettes Notebook"),
    ("legion pro", "es ist ein komplettes Notebook"),
    ("lenovo legion", "es ist ein komplettes Notebook"),
    ("helios neo", "es ist ein komplettes Notebook"),
    ("predator helios", "es ist ein komplettes Notebook"),
    ("nitro 5", "es ist ein komplettes Notebook"),
    ("nitro v", "es ist ein komplettes Notebook"),
    ("ideapad", "es ist ein komplettes Notebook"),
    ("probook", "es ist ein komplettes Notebook"),
    ("elitebook", "es ist ein komplettes Notebook"),
    ("inspiron", "es ist ein komplettes Notebook"),
    ("surface pro", "es ist ein Komplettgerät (kein Einzelteil)"),
    ("surface laptop", "es ist ein komplettes Notebook"),
    ("komplettgerät", "es ist ein Komplettgerät (kein Einzelteil)"),
    ("komplettgeraet", "es ist ein Komplettgerät (kein Einzelteil)"),
)

# Speichergrößen, die es an einer Grafikkarte nicht gibt: 1 TB, 2 TB oder
# 500 GB sind Laufwerke eines Komplettgeräts. Eine Karte nennt höchstens ihren
# Grafikspeicher (2, 4, 6, 8, 10, 12, 16, 20, 24, 32 GB).
STORAGE_RE = re.compile(
    r"\b\d{1,2}\s?(?:tb)\b|\b(?:120|128|240|250|256|480|500|512)\s?gb\b"
)

# Kategorien, in denen ein Laufwerk das gesuchte Produkt ist.
STORAGE_CATEGORIES: frozenset[str] = frozenset({"ssd", "hdd"})

# Hauptwort-Treffer für Laptop-Prozessoren ("i7-9750H", "i7-13700HX"). Ein
# Komplett-Gerät nennt einen solchen Prozessor, eine Karte nie.
LAPTOP_CPU_RE = re.compile(r"\b(?:i[3579]|ryzen\s?[3579])-?\d{3,5}(?:h|hq|hx|hs|u)\b")

# --------------------------------------------------------------------------
# 3. Eindeutige Teile und Zubehör
# --------------------------------------------------------------------------
PART_PHRASES: tuple[tuple[str, str], ...] = (
    ("water block", "es ist ein Wasserblock"),
    ("waterblock", "es ist ein Wasserblock"),
    ("wasserblock", "es ist ein Wasserblock"),
    ("wasserkühl", "es ist eine Wasserkühlung"),
    ("wasserkuehl", "es ist eine Wasserkühlung"),
    ("heatkiller", "es ist ein Wasserblock (Heatkiller)"),
    ("heat sink", "es ist ein Kühler (heat sink)"),
    ("heatsink", "es ist ein Kühler (heatsink)"),
    ("kühlkörper", "es ist ein Kühler (Kühlkörper)"),
    ("kuehlkoerper", "es ist ein Kühler (Kühlkörper)"),
    ("kühlblock", "es ist ein Kühlblock"),
    ("kuehlblock", "es ist ein Kühlblock"),
    ("kühler für", "es ist ein Kühler für ein Modell"),
    ("kuehler fuer", "es ist ein Kühler für ein Modell"),
    ("kühler fur", "es ist ein Kühler für ein Modell"),
    ("kuehler fur", "es ist ein Kühler für ein Modell"),
    ("lüfter für", "es ist ein Lüfter für ein Modell"),
    ("luefter fuer", "es ist ein Lüfter für ein Modell"),
    ("lüfter fur", "es ist ein Lüfter für ein Modell"),
    ("luefter fur", "es ist ein Lüfter für ein Modell"),
    ("cooler for", "es ist ein Kühler für ein Modell"),
    ("fan for", "es ist ein Lüfter für ein Modell"),
    ("graphics card fan", "es ist ein Lüfter (kein Grafikchip)"),
    ("gpu fan", "es ist ein Lüfter (kein Grafikchip)"),
    ("card fan", "es ist ein Lüfter (kein Grafikchip)"),
    ("ersatzlüfter", "es ist ein Ersatzlüfter"),
    ("ersatzluefter", "es ist ein Ersatzlüfter"),
    ("reference cooler", "es ist der Referenzkühler"),
    ("dual fan assembly", "es ist eine Lüfter-Baugruppe"),
    ("fan assembly", "es ist eine Lüfter-Baugruppe"),
    ("thermal module", "es ist ein Kühlmodul"),
    ("thermal pad", "es ist ein Wärmeleitpad"),
    ("wärmeleitpad", "es ist ein Wärmeleitpad"),
    ("waermeleitpad", "es ist ein Wärmeleitpad"),
    ("stromadapter", "es ist ein Stromadapter"),
    ("powerlink", "es ist ein Stromadapter (EVGA PowerLink)"),
    ("nvlink", "es ist eine NVLink-Brücke"),
    ("sli bridge", "es ist eine SLI-Brücke"),
    ("riser", "es ist ein Riser-Kabel"),
    ("backplate", "es ist eine Backplate"),
    ("back plate", "es ist eine Backplate"),
    ("halterung", "es ist eine Halterung"),
    ("bracket", "es ist eine Halterung"),
    ("stütze", "es ist eine Stütze"),
    ("stuetze", "es ist eine Stütze"),
    ("aufkleber", "es sind Aufkleber"),
    ("sticker", "es sind Aufkleber"),
    ("plakette", "es ist eine Plakette"),
    ("schrauben", "es sind Schrauben"),
    ("screws", "es sind Schrauben"),
    ("trim kit", "es ist ein Zier-Kit"),
    ("mod kit", "es ist ein Umbau-Kit"),
    ("zubehör", "es ist als Zubehör ausgewiesen"),
    ("zubehoer", "es ist als Zubehör ausgewiesen"),
    ("ersatzteil", "es ist als Ersatzteil ausgewiesen"),
    ("bastler", "es ist als Bastlerware ausgewiesen"),
    ("gehäuse für", "es ist ein Gehäuse für ein Modell"),
    ("gehaeuse fuer", "es ist ein Gehäuse für ein Modell"),
    ("speicherkühler", "es ist ein Speicherkühler"),
    ("speicherkuehler", "es ist ein Speicherkühler"),
    ("copper mod", "es ist ein Kühl-Umbau (Copper Mod)"),
    ("plaque", "es ist eine Plakette"),
    ("collectible", "es ist ein Sammlerstück"),
    ("sammlerstück", "es ist ein Sammlerstück"),
    ("sammlerstueck", "es ist ein Sammlerstück"),
    # Bei einer Grafikkarte ist "Gehäuse" die Blende mit Lüfter. Eine
    # Gehäuse-Suche schützt ihr eigenes Wort (siehe ``CATEGORY_OWN_NOUNS``).
    ("gehäuse", "es ist die Abdeckung oder Blende (kein Grafikchip)"),
    ("gehaeuse", "es ist die Abdeckung oder Blende (kein Grafikchip)"),
)

# Teilwörter, die als *Ende* eines zusammengesetzten Wortes stehen: "Ersatz-
# halterung", "Kartenhalter", "Ersatzbackplate". Aufgenommen sind nur Wörter,
# die in einer Karten-Anzeige kein Ausstattungsmerkmal sind - "Ersatzkühler"
# oder "Dual Fan" stehen hier deshalb nicht (dafür gibt es ``PART_STEMS`` bzw.
# die Zählwort-Regel). Ein "mit"/"inkl." davor macht sie zur Beigabe.
PART_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("halterung", "es ist eine Halterung"),
    ("halter", "es ist eine Halterung"),
    ("bracket", "es ist eine Halterung"),
    ("backplate", "es ist eine Backplate"),
    ("stuetze", "es ist eine Stütze"),
    ("adapter", "es ist ein Adapter"),
    ("kabel", "es ist ein Kabel"),
    ("aufkleber", "es sind Aufkleber"),
)

# Wortstämme, die *im* Wort stecken dürfen (Schreibvarianten und Tippfehler):
# "Wasskühlblock", "Wasserkühlung", "Heatkiller".
PART_STEMS: tuple[tuple[str, str], ...] = (
    ("wasserkuehl", "es ist eine Wasserkühlung"),
    ("wasskuehl", "es ist eine Wasserkühlung"),
    ("wasserblock", "es ist ein Wasserblock"),
    ("waterblock", "es ist ein Wasserblock"),
    ("heatkiller", "es ist ein Wasserblock (Heatkiller)"),
    ("kuehlblock", "es ist ein Kühlblock"),
    ("kuehlkoerper", "es ist ein Kühler (Kühlkörper)"),
    ("heatsink", "es ist ein Kühler (heatsink)"),
)

# Weiche Stämme: Sie stehen auch in Anzeigen echter Karten, die ihr Kühlsystem
# nur beschreiben. Sie schließen deshalb nur aus, wenn der Titel keine echte
# Karte mit Angaben nennt (siehe ``SOFT_PART_TOKENS``).
SOFT_PART_STEMS: frozenset[str] = frozenset({"kuehlblock", "kuehlkoerper", "heatsink"})

# Weiche Teil-Wörter: Sie stehen auch in Anzeigen echter Karten, die das
# Kühlsystem nur erwähnen ("Grafikkarte PCIe x16 Lüfter mit Kühlkörper"). Sie
# schließen deshalb nur aus, wenn der Titel keine echte Karte mit Angaben nennt.
SOFT_PART_TOKENS: tuple[str, ...] = (
    "kühlkörper",
    "kuehlkoerper",
    "heat sink",
    "heatsink",
    "thermal module",
    "thermal pad",
    "wärmeleitpad",
    "waermeleitpad",
)

# Teilwörter, die als *Kopfwort* am Titelende das Verkaufte benennen. Als
# Wortbestandteil eines längeren Wortes zählen sie nicht: "Single-Lüfter" oder
# "Dual Fan" sind Eigenschaften einer echten Karte, "Lüfter" allein nicht.
PART_HEADS: tuple[str, ...] = (
    "lüfter",
    "luefter",
    "lüfters",
    "fan",
    "fans",
    "kühler",
    "kuehler",
    "cooler",
    "coolers",
    "kühlung",
    "kuehlung",
    "radiator",
    "shroud",
    "blende",
    "abdeckung",
    "backplate",
    "halter",
    "halterung",
    "bracket",
    "stütze",
    "stuetze",
    "standfuß",
    "standfuss",
    "adapter",
    "konverter",
    "kabel",
    "cable",
    "riser",
    "modul",
    "module",
    "platine",
    "pcb",
    "chip",
    "spule",
    "kondensator",
    "cover",
    "tray",
    "support",
    "schrauben",
    "screws",
    "aufkleber",
    "sticker",
)

# Wörter, die Verpackung verkaufen. Sie stehen an jeder Stelle des Titels: Wer
# einen "Karton" nennt, verkauft ihn - es sei denn, ein "mit"/"inkl." stellt
# klar, dass die Verpackung nur Beigabe zur Karte ist.
PACKAGING_HEADS: tuple[str, ...] = (
    "karton",
    "kartons",
    "kartönchen",
    "ovp",
    "originalverpackung",
    "umkarton",
    "versandkarton",
    "verpackung",
    "verpackungen",
    "verpackungsset",
    "box",
    "boxes",
    "schachtel",
    "anleitung",
    "handbuch",
    "manual",
    "rechnung",
    "poster",
)

# Wörter, die nach einem Kopfwort noch stehen dürfen, ohne dessen Aussage zu
# ändern ("Lüfter (Non RGB)", "Heatsink Dual Fan #zn", "Karton 6GB").
HEAD_IGNORED: frozenset[str] = frozenset(
    {
        "rgb",
        "non",
        "argb",
        "led",
        "neu",
        "neues",
        "angebot",
        "original",
        "inkl",
        "inklusive",
        "mit",
        "ohne",
        "und",
        "oder",
        "u",
        "a",
        "zubehör",
        "zubehoer",
        "stück",
        "stueck",
        "stk",
        "set",
        "kit",
        "gpu",
        "grafikkarte",
        "graphics",
        "card",
        "vga",
        "ti",
        "super",
        "oc",
        "evga",
        "asus",
        "msi",
        "gigabyte",
        "sapphire",
        "zotac",
        "palit",
        "gainward",
        "xfx",
        "powercolor",
        "inno3d",
        "kfa2",
        "pny",
        "manli",
        "gaming",
        "pc",
        "desktop",
        "originalverpackung",
        "artikel",
        "ware",
        # Hersteller- und Modellwörter: Sie tragen keine Aussage darüber, *was*
        # verkauft wird. Ohne sie wäre bei "NUR KÜHLER!! MSI RTX 3060 GAMING X 12G"
        # das Kopfwort "rtx" statt "kühlkörper".
        "rtx",
        "gtx",
        "rx",
        "geforce",
        "radeon",
        "nvidia",
        "amd",
        "intel",
        "arc",
        "quadro",
        "titan",
        "vega",
        "gddr6x",
        "gddr6",
        "gddr5",
        "lhr",
        "rev",
    }
)

# "Nur" am Titelanfang kündigt an, dass ausdrücklich *nur* das folgende Teil
# verkauft wird: "NUR KÜHLER!! MSI RTX 3060 GAMING X 12G".
ONLY_HEAD_TOKENS: frozenset[str] = frozenset({"nur", "only"})

# Verneinungen, die das gesuchte Modell ausschließen: "PC ohne RTX 5090",
# "Gehäuse, keine Grafikkarte".
MISSING_TOKENS: frozenset[str] = frozenset(
    {"ohne", "kein", "keine", "keinen", "keiner", "without", "no", "not"}
)

# Ein Modellname, wie er in Anzeigen steht: "rtx5090", "rtx 5090", "rx 7900 xt".
MODEL_IN_TEXT_RE = re.compile(
    r"\b(?:rtx|gtx|rx|quadro|titan|arc|geforce|radeon)\s?\d{3,4}\b"
)

# Wörter, die ein Teil oder eine Verpackung zur *Eigenschaft* einer echten Karte
# machen: "RTX 3070 OC 8GB GDDR6 Dual Fan", "RTX 3080 mit OVP".
WITH_MARKERS: frozenset[str] = frozenset(
    {
        "mit",
        "inkl",
        "inklusive",
        "incl",
        "dabei",
        "vorhanden",
        "plus",
        "und",
        "im",
        "in",
        # "ohne OVP" heißt: die Karte wird ohne Verpackung verkauft - dann ist
        # die Verpackung gerade *nicht* das Verkaufte.
        "ohne",
        "&",
        "+",
    }
)
COUNT_QUALIFIERS: frozenset[str] = frozenset(
    {"dual", "triple", "single", "twin", "zwei", "drei", "vier", "2x", "3x", "4x", "2", "3"}
)

# Wörter, die ein Teilwort auch dann als Beigabe ausweisen, wenn sie *danach*
# stehen. "mit"/"inkl." hinter dem Wort kündigen dagegen nichts an: "Gehäuse mit
# Lüfter" verkauft das Gehäuse. "und"/"dabei" schon: "OVP und Rechnung dabei".
FOLLOW_MARKERS: frozenset[str] = frozenset(
    {
        "und",
        "&",
        "+",
        "dabei",
        "inklusive",
        "incl",
        "mitgeliefert",
        "vorhanden",
        "beiliegend",
    }
)

# Echte Hardware-Angaben. Sie unterscheiden eine Karte mit Verpackung von einer
# Verpackung: Kapazitäten ("16GB") zählen bewusst *nicht*, die stehen auch auf
# Karton-Anzeigen.
HARDWARE_SPEC_RE = re.compile(
    r"(gddr\d*x?|ddr\d|pcie|pci-e|hdmi|displayport|\bdvi\b|\bmhz\b|\bghz\b|\bbit\b)"
)

# Wörter, die es nur bei Grafikkarten gibt. Zusammen mit technischen Angaben
# belegen sie, dass ein Teil im Titel eine *Eigenschaft* der Karte ist.
CARD_SIGNATURE_RE = re.compile(
    r"\b(geforce|rtx|gtx|rx|radeon|grafikkarte|grafikkarten|graka|gpu|quadro|titan|arc)\b"
)

# Technische Angaben, die eine echte Karte beschreiben. Sie unterscheiden
# "Grafikkarte mit OVP" von "nur Verpackung".
SPEC_TOKENS: frozenset[str] = frozenset(
    {
        "gb",
        "gddr",
        "gddr5",
        "gddr6",
        "gddr6x",
        "ddr",
        "ddr3",
        "ddr4",
        "ddr5",
        "pcie",
        "pci",
        "e",
        "hdmi",
        "dvi",
        "dp",
        "displayport",
        "mhz",
        "ghz",
        "bit",
        "mb",
        "tb",
        "core",
        "cuda",
        "stream",
        "raytracing",
        "gaming",
        "oc",
        "vram",
    }
)

# Verbindungen, die ein Teil mit dem Modell verknüpfen, für das es gedacht ist.
PART_CONNECTORS: tuple[str, ...] = (
    " for ",
    " fuer ",
    " fur ",
    " passend ",
    " geeignet ",
    " kompatibel ",
    " zu ",
)

# Teil-Wörter, die nur zusammen mit einer Verbindung ausschließen. Ein "Fan"
# mitten im Titel ("ASUS GTX 1660 Dual Fan") ist eine Eigenschaft der Karte.
PART_NOUNS: tuple[str, ...] = (
    "fan",
    "fans",
    "lüfter",
    "luefter",
    "cooler",
    "coolers",
    "cooling",
    "kühler",
    "kuehler",
    "kühlkörper",
    "heatsink",
    "shroud",
    "backplate",
    "bracket",
    "halterung",
    "kabel",
    "cable",
    "adapter",
    "riser",
    "wasserblock",
    "waterblock",
    "thermalpad",
    "waermeleitpad",
    "leitpad",
    "schrauben",
    "screws",
    "aufkleber",
    "sticker",
)

# Alle Wörter, die ein Teil, ein Zubehör oder eine Verpackung benennen. Sie
# werden zusammen geprüft, weil mehrere davon nebeneinander stehen können
# ("inkl. OVP und Rechnung").
TEILWOERTER: frozenset[str] = (
    frozenset(PACKAGING_HEADS) | frozenset(PART_HEADS) | frozenset(PART_NOUNS)
)

# Wörter, die in einer Kategorie das *gesuchte Produkt* sind und dort nicht als
# Zubehör gelten. Sonst würde eine Gehäuse-Suche jedes "Gehäuse" verwerfen -
# genau das Wort, nach dem sie sucht.
CATEGORY_OWN_NOUNS: dict[str, frozenset[str]] = {
    "case": frozenset({"gehäuse", "gehaeuse", "tower", "hülle", "huelle", "shroud"}),
    "cooler": frozenset({"kühler", "kuehler", "lüfter", "luefter", "fan", "radiator", "aio"}),
    "psu": frozenset({"netzteil", "psu", "power supply"}),
}

# Wörter, die ein Suchgesuch oder einen Ankauf ankündigen ("Suche RTX 3070",
# "RTX 4070 gesucht", "kaufe/ Suche RTX 4070 Ti"). Ein Verkauf formuliert das
# nie so. "abzugeben" fehlt bewusst: Das ist ein Verkaufsangebot.
WANTED_TOKENS: frozenset[str] = frozenset(
    {
        "suche",
        "sucht",
        "gesucht",
        "gesuche",
        "kaufe",
        "ankauf",
        "tausche",
        "tausch",
        "wanted",
        "looking",
        "brauche",
    }
)

# Ab dieser Anzahl verschiedener Modelle der Kauf-Limit-Liste im Titel ist es
# eine Sammelanzeige ("Grafikkarten: RTX 3080Ti 2060 GTX1080 1060 RX580 uvm").
BUCKET_MODEL_LIMIT = 3


def target_text(target: "TitleTarget") -> str:
    """Die Vorgabe als Text - Grundlage fuer den Ziel-Abgleich der Regeln."""
    beschreibung = target.beschreibung
    return " ".join(
        str(beschreibung.get(key) or "") for key in ("gesucht", "produkt", "kategorie")
    )


def _phrase_in(text: str, phrase: str) -> bool:
    """Steht die Wendung als eigene Wörter im (normalisierten) Titel?

    Wortgrenzen sind hier entscheidend: Ohne sie fände "egpu" ein "coverage
    gpu" und "fan" ein "infantil". ``match_key_contains`` prüft zusätzlich
    kompaktiert und ist damit für diese Tabellen zu großzügig.
    """
    if not phrase:
        return False
    # Ein angehängtes "s" zählt als Mehrzahl mit: Der Markt schreibt
    # "Wärmeleitpads", "Backplates", "Kartons".
    muster = rf"(?<![a-z0-9]){re.escape(phrase)}s?(?![a-z0-9])"
    return re.search(muster, text) is not None


def _find_phrase(text: str, table: tuple[tuple[str, str], ...]) -> tuple[str, str] | None:
    """Erste passende Phrase einer Tabelle (normalisiert, umlautfest)."""
    normalisiert = normalize_text(text)
    for phrase, grund in table:
        if _phrase_in(normalisiert, phrase):
            return phrase, grund
    return None


def _head_noun(title: str) -> tuple[str, int]:
    """Das Hauptwort am Ende des Titels - es benennt, was verkauft wird.

    Rückgabe ist ``(Wort, Position)``: das letzte inhaltstragende Wort und
    seine Stelle in der Wortliste. Technische Angaben ("8GB", "GDDR6", "OC"),
    Marken und Füllwörter werden übersprungen, ebenso angehängte eBay-Zusätze
    ("Wird in neuem Fenster geöffnet").
    """
    tokens = tokenize(title)
    if not tokens:
        return "", -1
    # eBay hängt an Links den Hinweistext an; er steht in manchen Quellen im
    # Titel und sagt nichts aus.
    for stopper in ("wird", "fenster", "tab", "geoeffnet", "geöffnet"):
        while tokens and tokens[-1] == stopper:
            tokens.pop()
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token in HEAD_IGNORED or token in SPEC_TOKENS:
            continue
        if token.isdigit() or _is_spec_token(token):
            continue
        if len(token) <= 1:
            continue
        return token, index
    return "", -1


def _head_is_property(title: str, index: int, *, verpackung: bool) -> bool:
    """Ist das Kopfwort nur eine *Eigenschaft* der Karte?

    "RTX 3070 OC 8GB GDDR6 Dual **Fan**" und "RTX 3080 **mit OVP**" nennen das
    Teil bzw. die Verpackung als Ausstattung - verkauft wird die Karte.

    * Verpackung: nur ein "mit"/"inkl"/"dabei" oder eine echte Hardware-Angabe
      ("DDR6 OVP") spricht für eine echte Karte. Bloße Speichergrößen nicht:
      "Asus Prime RTX5060ti 16gb Karton" bleibt ein Karton.
    * Teile: zusätzlich zählt ein Zählwort ("Dual Fan", "3x Lüfter") und jede
      Karte mit Angaben ("PNY GeForce GTX 1660 Ti 6GB Arctic-Kühler – Lüfter").
    """
    tokens = tokenize(title)
    # "nur Platine", "nur Kühler": Der Titel sagt selbst, dass ausschließlich
    # dieses Teil verkauft wird - dann ist das Wort keine Eigenschaft.
    if any(token in ONLY_HEAD_TOKENS for token in tokens[:index]):
        return False
    # Verbindungs- und Zählwörter zählen mit, auch wenn sie sonst als Füllwort
    # gelten: "... mit OVP und Rechnung" muss das "und" sehen.
    davor = [
        tokens[pos]
        for pos in range(0, index)
        if tokens[pos] not in HEAD_IGNORED or tokens[pos] in WITH_MARKERS
    ]
    if davor and davor[-1] in WITH_MARKERS:
        return True
    if _has_hardware_spec(title):
        return True
    if verpackung:
        return False
    if davor and davor[-1] in COUNT_QUALIFIERS:
        return True
    return _is_card_with_spec(title)


def _is_spec_token(token: str) -> bool:
    """Erkennt Angaben wie "12gb", "8g", "gddr6x", "pci-e", "3x"."""
    if re.fullmatch(r"\d+\s?(?:gb|g|mb|tb|bit|x|w)", token):
        return True
    if re.fullmatch(r"(?:gddr|ddr|pcie|pci|usb|hdmi|dvi|dp)\d*\w*", token):
        return True
    return False


def _has_spec(title: str) -> bool:
    """Nennt der Titel technische Angaben einer Karte (Speicher, Bus, Anschluss)?"""
    tokens = set(tokenize(title))
    if tokens & SPEC_TOKENS:
        return True
    return any(_is_spec_token(token) for token in tokens)


def _has_hardware_spec(title: str) -> bool:
    """Nennt der Titel echte Hardware-Angaben (GDDR6, PCIe, HDMI, DDR5)?

    Speichergrößen ("16GB") zählen hier bewusst *nicht*: "Asus Prime RTX5060ti
    16gb Karton" bleibt eine Karton-Anzeige.
    """
    return HARDWARE_SPEC_RE.search(normalize_text(title)) is not None


def _is_card_with_spec(title: str) -> bool:
    """Ist der Titel eine echte Karte mit Angaben? Dann sind Teile Ausstattung."""
    return bool(CARD_SIGNATURE_RE.search(normalize_text(title))) and _has_spec(title)


def _head_reason(title: str, *, skip_nouns: frozenset[str]) -> tuple[str, str] | None:
    """Verkauft der Titel laut Kopfwort ein Teil oder eine Verpackung?

    Ein qualifiziertes Kopfwort ("Dual Fan", "mit OVP") ist Ausstattung einer
    echten Karte und schließt nicht aus.
    """
    head, index = _head_noun(title)
    if not head or index < 0 or head in skip_nouns:
        return None
    if head not in PACKAGING_HEADS and head not in PART_HEADS:
        return None
    ist_verpackung = head in PACKAGING_HEADS
    if _head_is_property(title, index, verpackung=ist_verpackung):
        return None
    if ist_verpackung:
        return ART_VERPACKUNG, f"das Hauptwort ist '{head}' - es wird nur die Verpackung angeboten"
    return ART_TEIL, f"das Hauptwort ist '{head}' - es wird ein Teil oder Zubehör angeboten"


def _ist_beigabe(tokens: list[str], index: int) -> bool:
    """Steht das Wort an ``index`` nur als Beigabe in einer Karten-Anzeige?

    Eine Beigabe kündigt ein "mit"/"inkl."/"dabei"/"und" oder ein "ohne" an -
    direkt daneben oder über eine Kette benachbarter Teilwörter hinweg
    ("inkl. OVP und Rechnung", "mit Netzteil und Lüfter"). Ohne eine solche
    Ankündigung benennt das Wort das Verkaufte.
    """
    links = index - 1
    while links >= 0 and tokens[links] in TEILWOERTER:
        links -= 1
    if links >= 0 and tokens[links] in WITH_MARKERS:
        return True
    rechts = index + 1
    while rechts < len(tokens) and tokens[rechts] in TEILWOERTER:
        rechts += 1
    return rechts < len(tokens) and tokens[rechts] in FOLLOW_MARKERS


def _packaging_reason(title: str, *, skip_nouns: frozenset[str]) -> tuple[str, str] | None:
    """Verpackung: führende Verpackung, "nur <Verpackung>" oder Verpackung ohne Beigabe."""
    tokens = tokenize(title)

    # Verpackungswort am Anfang: "KARTON ASUS GeForce RTX 2060 ...".
    fuehrend = {"karton", "kartons", "verpackung", "ovp", "box", "originalverpackung"}
    for index, token in enumerate(tokens[:3]):
        if token in fuehrend and not _ist_beigabe(tokens, index):
            return (
                ART_VERPACKUNG,
                f"der Titel beginnt mit '{token}' - es wird die Verpackung angeboten",
            )

    for index, token in enumerate(tokens):
        if token not in PACKAGING_HEADS or token in skip_nouns:
            continue
        # "nur Verpackung"/"**NUR VERPACKUNG**" schlägt jede Ausstattungsangabe:
        # Der Verkäufer sagt selbst, dass nur die Verpackung verkauft wird.
        if index >= 1 and tokens[index - 1] in ONLY_HEAD_TOKENS:
            return ART_VERPACKUNG, f"der Titel sagt ausdrücklich 'nur {token}'"
        if _ist_beigabe(tokens, index):
            continue
        if _has_hardware_spec(title):
            continue
        return (
            ART_VERPACKUNG,
            f"Titel nennt Verpackung ('{token}') ohne Beigabe-Angabe",
        )
    return None


def _missing_model_reason(title: str) -> tuple[str, str] | None:
    """"ohne RTX 5090", "keine Grafikkarte": Das Produkt ist ausdrücklich nicht dabei.

    Ein Titel, der das gesuchte Modell mit "ohne"/"kein" nennt, verkauft den
    Rest des Geräts - die Karte fehlt gerade. Ohne diese Prüfung landen
    Komplett-PCs unter "ohne RTX 5090" in der Ergebnisliste.
    """
    tokens = tokenize(title)
    for index, token in enumerate(tokens):
        if token not in MISSING_TOKENS:
            continue
        fenster = " ".join(tokens[index + 1 : index + 4])
        if MODEL_IN_TEXT_RE.search(fenster):
            return (
                ART_VERPACKUNG,
                f"der Titel nennt das Modell nur mit '{token}' - es wird ohne das Produkt verkauft",
            )
    return None


def _phrase_ist_beigabe(title: str, phrase: str) -> bool:
    """Steht die Wendung nur als Beigabe im Titel ("inkl. OVP & Zubehör")?"""
    tokens = tokenize(title)
    erste = normalize_text(phrase).split()[0] if normalize_text(phrase) else ""
    if not erste:
        return False
    for index, token in enumerate(tokens):
        if token == erste and _ist_beigabe(tokens, index):
            return True
    return False


def _part_reason(title: str, *, skip_nouns: frozenset[str]) -> tuple[str, str] | None:
    """Teile und Zubehör: Phrasen, Wortstämme, "nur"-Ankündigung, Kopfwort."""
    phrase = _find_phrase(title, PART_PHRASES)
    if phrase is not None and not any(
        teil in skip_nouns for teil in phrase[0].replace("-", " ").split()
    ):
        # Weiche Wörter gelten nur, wenn der Titel keine echte Karte mit Angaben
        # nennt ("Grafikkarte PCIe x16 Lüfter mit Kühlkörper").
        if phrase[0] not in SOFT_PART_TOKENS or not _is_card_with_spec(title):
            # "inkl. OVP & Zubehör", "mit Netzteil": Das Wort ist Beigabe der
            # Karte, nicht das Verkaufte.
            if not _phrase_ist_beigabe(title, phrase[0]):
                return ART_TEIL, phrase[1]
    normalisiert = normalize_text(title)
    for stamm, grund in PART_STEMS:
        if stamm in skip_nouns:
            continue
        if not re.search(rf"(?<![a-z0-9]){re.escape(stamm)}", normalisiert):
            continue
        # Weiche Stämme ("Kühlkörper") stehen auch in Anzeigen echter Karten.
        if stamm in SOFT_PART_STEMS and _is_card_with_spec(title):
            continue
        # "mit Wasserblock und Kühler" beschreibt die Karte, verkauft sie nicht.
        if _phrase_ist_beigabe(title, stamm):
            continue
        return ART_TEIL, grund
    ende = _compound_part_reason(title, skip_nouns=skip_nouns)
    if ende is not None:
        return ende
    nur_grund = _only_reason(title, skip_nouns=skip_nouns)
    if nur_grund is not None:
        return nur_grund
    # Ein Teilwort am Titelanfang benennt das Verkaufte: "Graka Lüfter Zotac
    # RTX 2060 ...". Ein Zählwort davor ("Dual Fan") macht es zur Eigenschaft.
    tokens = tokenize(title)
    for index, token in enumerate(tokens[:3]):
        if token not in PART_HEADS and token not in PACKAGING_HEADS:
            continue
        if token in skip_nouns:
            continue
        if index >= 1 and tokens[index - 1] in COUNT_QUALIFIERS | WITH_MARKERS:
            continue
        if _ist_beigabe(tokens, index):
            continue
        return ART_TEIL, f"der Titel beginnt mit '{token}' - es wird ein Teil angeboten"
    head_reason = _head_reason(title, skip_nouns=skip_nouns)
    if head_reason is not None:
        return head_reason
    text = f" {normalize_text(title)} "
    if not any(conn in text for conn in PART_CONNECTORS):
        return None
    for noun in PART_NOUNS:
        if noun in skip_nouns:
            continue
        if f" {noun} " in text:
            return ART_TEIL, f"es wird ein Teil oder Zubehör angeboten ('{noun}' für ein Modell)"
    return None


def _compound_part_reason(
    title: str, *, skip_nouns: frozenset[str]
) -> tuple[str, str] | None:
    """Teilwort am *Ende* eines zusammengesetzten Wortes.

    "Yeston RTX 3050 Ersatzhalterung für Grafikkarte" verkauft eine Halterung -
    das Wort steht aber nur als Bestandteil des Kompositums im Titel, deshalb
    greifen die Tabellen mit ganzen Wörtern (``PART_PHRASES``, ``PART_HEADS``)
    nicht. Geprüft wird wortweise, damit ein "mit"/"inkl." davor weiterhin als
    Beigabe erkannt wird ("RTX 3060 mit Halterung").
    """
    tokens = tokenize(title)
    for index, token in enumerate(tokens):
        for ende, grund in PART_SUFFIXES:
            if token == ende or not token.endswith(ende):
                continue
            # Wer selbst eine Halterung sucht, verliert "Kartenhalter" nicht.
            if token in skip_nouns or _nennt_dasselbe(ende, skip_nouns):
                continue
            if _ist_beigabe(tokens, index):
                continue
            return ART_TEIL, f"{grund} ('{token}')"
    return None


def _nennt_dasselbe(ende: str, skip_nouns: frozenset[str]) -> bool:
    """Nennt ein geschütztes Wort (Vorgabe oder eigene Kategorie) dasselbe?

    "Halterung" in der Suche schützt auch "Kartenhalter", "halter" allein nicht
    umgekehrt. Gemeinsamer Wortstamm genügt: Kurze Wörter ("rtx") können kein
    zufälliges Teilwort treffen.
    """
    return any(
        wort and len(wort) >= 4 and (wort.startswith(ende) or ende in wort)
        for wort in skip_nouns
    )


def _only_reason(title: str, *, skip_nouns: frozenset[str]) -> tuple[str, str] | None:
    """"Nur Kühler", "nur Lüfter": Es wird ausdrücklich nur ein Teil verkauft."""
    tokens = tokenize(title)
    if not tokens or tokens[0] not in ONLY_HEAD_TOKENS:
        return None
    for token in tokens[1:6]:
        if token in skip_nouns:
            return None
        if token in PACKAGING_HEADS:
            return ART_VERPACKUNG, f"der Titel beginnt mit 'nur {token}' - nur die Verpackung"
        if token in PART_HEADS:
            return ART_TEIL, f"der Titel beginnt mit 'nur {token}' - nur ein Teil"
    return None


def _wanted_reason(title: str) -> str | None:
    """Suchgesuche und Ankäufe - es wird nichts verkauft."""
    tokens = set(tokenize(title))
    treffer = tokens & WANTED_TOKENS
    if treffer:
        return f"es ist ein Suchgesuch ('{sorted(treffer)[0]}')"
    return None


def _foreign_reason(
    title: str, *, skip_nouns: frozenset[str], category: str | None = None
) -> str | None:
    """Fremdes Gerät, fremde Baugruppe, Netzteil oder komplettes Gerät."""
    phrase = _find_phrase(title, FOREIGN_PHRASES)
    if phrase is not None:
        treffer, grund = phrase
        if any(teil in skip_nouns for teil in treffer.split()):
            return None
        return grund
    if LAPTOP_CPU_RE.search(normalize_text(title)):
        return "es ist ein komplettes Notebook (Notebook-Prozessor im Titel)"
    # Eine Laufwerksgröße ("1TB", "500GB") gibt es an einer Grafikkarte nicht.
    # Sie steht nur in Anzeigen kompletter Rechner - in einer Laufwerkssuche ist
    # sie dagegen das gesuchte Produkt.
    if category not in STORAGE_CATEGORIES and _hat_laufwerk(title):
        return "es ist ein komplettes Gerät (Laufwerksgröße im Titel)"
    return None


def _hat_laufwerk(title: str) -> bool:
    return STORAGE_RE.search(normalize_text(title)) is not None


def _bucket_reason(title: str, price_list: PriceList | None) -> str | None:
    """Sammelanzeige: mehrere Modelle der Liste in einem Titel.

    Gezählt werden *Modelle*, nicht Listeneinträge: "RTX 3080 Ti 12GB" ist eine
    Karte, auch wenn die Liste für sie drei Einträge führt ("RTX 3080 10GB",
    "RTX 3080 12GB", "RTX 3080 Ti"). Sonst fällt jede echte Karte mit Variante
    im Titel hier heraus (beobachtet bei RTX 3080 Ti, RTX 4060 Ti, RTX 5060 Ti).
    """
    if price_list is None or not price_list.available:
        return None
    familien = price_list.model_families_in_title(title)
    if len(familien) >= BUCKET_MODEL_LIMIT:
        beispiele = ", ".join(sorted(familien.values())[:4])
        return (
            f"es ist eine Sammelanzeige mit {len(familien)} Modellen "
            f"({beispiele}) - kein einzelnes Angebot"
        )
    return None


def classify_title(
    title: str,
    *,
    vorgabe: str = "",
    price_list: PriceList | None = None,
    category: str | None = None,
) -> tuple[str, str] | None:
    """Bestimmt die Angebotsart eines Titels.

    Rückgabe ``(art, begründung)`` oder ``None`` (= der Titel kann das gesuchte
    Produkt sein). ``vorgabe`` schützt das gesuchte Produkt selbst: Wer einen
    Lüfter sucht, verliert ihn nicht (siehe ``CATEGORY_OWN_NOUNS``).
    """
    text = (title or "").strip()
    if not text:
        return ART_MODELL, "Anzeige ohne Titel"

    vorgabe_tokens = set(tokenize(vorgabe))
    # Die Tabellen der geschützten Wörter stehen in Schreibweise mit Umlauten
    # ("gehäuse"); verglichen wird aber der transliterierte Titel ("gehaeuse").
    skip_nouns = {normalize_text(wort) for wort in CATEGORY_OWN_NOUNS.get(category or "", frozenset())}
    skip_nouns |= vorgabe_tokens
    skip = frozenset(skip_nouns)

    treffer = _find_phrase(text, ABSOLUTE_PHRASES)
    if treffer is not None:
        phrase, grund = treffer
        art = ART_VERPACKUNG if any(
            wort in phrase
            for wort in ("box", "ovp", "verpackung", "karton", "grafikkarte", "gpu", "karte")
        ) else ART_TEIL
        return art, grund

    missing = _missing_model_reason(text)
    if missing is not None:
        return missing

    wanted = _wanted_reason(text)
    if wanted is not None:
        return ART_SUCHGESUCH, wanted

    foreign = _foreign_reason(text, skip_nouns=skip, category=category)
    if foreign is not None:
        return ART_FREMDGERAET, foreign

    packaging = _packaging_reason(text, skip_nouns=skip)
    if packaging is not None:
        return packaging

    part = _part_reason(text, skip_nouns=skip)
    if part is not None:
        return part

    bucket = _bucket_reason(text, price_list)
    if bucket is not None:
        return ART_SAMMELANZEIGE, bucket

    return None


def accessory_reason(
    title: str,
    *,
    vorgabe: str = "",
    skip_nouns: frozenset[str] = frozenset(),
    price_list: PriceList | None = None,
    category: str | None = None,
) -> str | None:
    """Begründung, wenn der Titel ein Teil, eine Verpackung oder ein Suchgesuch ist.

    Beibehaltener Name aus der ersten Fassung: Der Aufrufer bekommt nur die
    Begründung (oder ``None``). ``skip_nouns`` ergänzt die geschützten Wörter.
    """
    if not (title or "").strip():
        return None
    ergebnis = classify_title(
        title,
        vorgabe=vorgabe,
        price_list=price_list,
        category=category,
    )
    if ergebnis is None:
        return None
    art, grund = ergebnis
    if skip_nouns:
        geschuetzt = {normalize_text(wort).strip() for wort in skip_nouns}
        if any(wort and wort in normalize_text(title) for wort in geschuetzt):
            # Ein geschütztes Wort steht im Titel: dann nicht allein deshalb
            # ausschließen. Die übrigen Prüfungen laufen im Aufrufer weiter.
            return None
    if art == ART_MODELL:
        return None
    return grund


def keyword_match(title: str, term: str) -> bool:
    """Stehen die Schlagwörter des Suchbegriffs im Titel?

    Zwei Schreibweisen zählen: die kompakte Form ('RTX3080' in 'RTX3080 Gaming')
    und die Wortfolge ('RTX 3080' als einzelne Wörter). Kurze Bestandteile wie
    '3080' werden als ganze Wörter verlangt, damit '30800' kein Treffer ist.
    """
    if not (title or "").strip() or not (term or "").strip():
        return False
    if compact_key(term) in compact_key(title):
        return True
    hay_tokens = set(tokenize(title))
    need = [token for token in tokenize(term) if token]
    return bool(need) and all(token in hay_tokens for token in need)


def title_product_reason(
    title: str,
    *,
    term: str | None = None,
    price_list: PriceList | None = None,
    category: str | None = None,
) -> tuple[str, str] | None:
    """Passt der Titel zum gesuchten Produkt? ``None`` heißt: ja.

    Reihenfolge:

    1. Angebotsart (``classify_title``): Teile, Verpackung, fremde Geräte,
       Suchgesuche. 'Lüfter für RTX 3060' nennt das Produkt, verkauft aber ein
       Teil.
    2. Modell-Abgleich mit der Kauf-Limit-Liste: Steht in Höchstpreise.txt eine
       Komponente, muss der Titel eine davon nennen - und sie muss zur gesuchten
       Gruppe gehören.
    3. Ohne Liste: die Schlagwörter des Suchbegriffs.

    Rückgabe: ``(art, begründung)`` oder ``None`` (= passt).
    """
    art = classify_title(
        title,
        vorgabe=term or "",
        price_list=price_list,
        category=category,
    )
    if art is not None:
        return art
    if price_list is not None and price_list.available:
        limit = price_list.limit_for(title=title)
        if limit is None:
            grund = (
                "Titel nennt keine Komponente aus der Kauf-Limit-Liste "
                f"('{term}')"
                if term
                else "Titel nennt keine Komponente aus der Kauf-Limit-Liste"
            )
            return ART_MODELL, grund
        if category and limit.category and limit.category != category:
            return (
                ART_MODELL,
                "Titel nennt eine Komponente aus einer anderen Gruppe "
                f"({limit.group_label} statt der gesuchten Kategorie)",
            )
        return None
    if term and not keyword_match(title, term):
        return (
            ART_MODELL,
            f"Titel enthält nicht die Schlagwörter des Suchbegriffs ('{term}')",
        )
    return None


def match_product(
    title: str,
    description: str | None = None,
    *,
    term: str | None = None,
    price_list: PriceList | None = None,
    category: str | None = None,
) -> tuple[tuple[str, str] | None, bool]:
    """Prüft, ob eine Anzeige das gesuchte Produkt verkauft - Titel, dann Beschreibung.

    Rückgabe ``(ergebnis, aus_beschreibung)``. ``ergebnis is None`` heißt
    "passt"; sonst ``(art, begründung)``. ``aus_beschreibung`` ist nur dann
    ``True``, wenn erst der Text die Zuordnung gerettet hat.

    Die Rettung ist eng gefasst und bleibt ehrlich:

    * Sie greift **nur** bei ``ART_MODELL`` - also wenn allein die
      Produktzuordnung fehlt. Ein Titel, der erkennbar ein Teil, Zubehör, eine
      Verpackung, ein fremdes Gerät oder ein Suchgesuch verkauft, bleibt
      verworfen; die Beschreibung hebt das nicht auf.
    * Die Beschreibung wird mit **denselben** Regeln beurteilt wie der Titel.
      Nennt sie das Produkt nicht, ändert sich nichts.
    """
    ergebnis = title_product_reason(
        title, term=term, price_list=price_list, category=category
    )
    if ergebnis is None or ergebnis[0] != ART_MODELL:
        return ergebnis, False
    text = " ".join((description or "").split())
    if not text:
        return ergebnis, False
    if title_product_reason(text, term=term, price_list=price_list, category=category) is None:
        return None, True
    return ergebnis, False


def title_key(title: str) -> str:
    """Stabiler Schluessel eines Anzeigentitels (Gross/Klein, Umlaute, Abstaende)."""
    normalized = normalize_text(title)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class TitleTarget:
    """Was der Benutzer sucht - die Vorgabe, an der Titel gemessen werden."""

    key: str
    beschreibung: dict[str, Any]
    category: str | None = None

    @classmethod
    def from_query(
        cls,
        *,
        term: str,
        platform: str,
        category: str | None = None,
        category_label: str | None = None,
        product: str | None = None,
        max_price: float | None = None,
    ) -> "TitleTarget":
        beschreibung: dict[str, Any] = {"gesucht": term}
        if category_label or category:
            beschreibung["kategorie"] = category_label or category
        if product:
            beschreibung["produkt"] = product
        if max_price is not None:
            beschreibung["budget_eur"] = round(float(max_price), 2)
        key = "|".join(
            [
                normalize_text(product or ""),
                normalize_text(category or ""),
                normalize_text(term),
            ]
        )
        return cls(key=key, beschreibung=beschreibung, category=category)

    @property
    def term(self) -> str:
        """Der Suchbegriff, an dem Titel gemessen werden."""
        return str(
            self.beschreibung.get("produkt") or self.beschreibung.get("gesucht") or ""
        )


@dataclass(frozen=True)
class TitleVerdict:
    """Urteil zu einem Titel. ``passt is False`` ist der einzige Ausschluss."""

    passt: bool
    grund: str = ""
    quelle: str = QUELLE_REGEL
    art: str = ""
    # Nur der Beschreibungstext hat die Zuordnung gerettet (siehe
    # ``match_product``) - wird getrennt gezählt und nicht verschwiegen.
    aus_beschreibung: bool = False


@dataclass
class TitleCheckStats:
    """Zähler der Titelprüfung eines Laufs (für Fortschritt und Abdeckung)."""

    geprueft: int = 0
    passt: int = 0
    passt_nicht: int = 0
    zubehoer: int = 0
    # Angebote, bei denen erst die Beschreibung die Zuordnung gerettet hat.
    aus_beschreibung: int = 0
    aktiv: bool = True
    nach_art: dict[str, int] = field(default_factory=dict)

    def zaehle(self, art: str) -> None:
        if not art:
            return
        self.nach_art[art] = self.nach_art.get(art, 0) + 1
        if art == ART_TEIL:
            self.zubehoer += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "aktiv": self.aktiv,
            "geprueft": self.geprueft,
            "passt": self.passt,
            "passt_nicht": self.passt_nicht,
            "zubehoer": self.zubehoer,
            "aus_beschreibung": self.aus_beschreibung,
            # Aufschluesselung nach Angebotsart - in der Oberflaeche sichtbar,
            # damit klar ist, was die grosse Menge der geprueften Titel ist.
            "gruende": [
                {"art": art, "label": ART_LABELS.get(art, art), "anzahl": anzahl}
                for art, anzahl in sorted(
                    self.nach_art.items(), key=lambda item: -item[1]
                )
            ],
        }

    @property
    def total(self) -> int:
        return self.geprueft


@dataclass
class TitleFilter:
    """Prüft Anzeigentitel rein über Schlagwörter - ohne KI.

    Die Instanz lebt genau einen Suchlauf lang. Jede Anzeige bekommt ein
    Urteil; ein Titel ohne Urteil gibt es nicht, weil die Prüfung rein
    deterministisch arbeitet.

    Es gibt bewusst keinen Bestandsschutz: Ein bereits gespeichertes Angebot
    wird bei jedem Lauf erneut geprüft. Sonst bliebe alles, was eine frühere,
    schwächere Fassung dieser Regeln durchgelassen hat, für immer in der
    Ergebnisliste stehen.
    """

    price_list: PriceList | None = None
    on_limitation: Any | None = None
    stats: TitleCheckStats = field(default_factory=TitleCheckStats)

    def active_for(self, platform: str) -> bool:
        """Wird in dieser Quelle geprüft? Der lokale Import ist ausgenommen."""
        return platform not in SKIP_PLATFORMS

    def review(self, target: TitleTarget, candidates: Sequence[Any]) -> list[TitleVerdict]:
        """Beurteilt die Titel der übergebenen Roh-Treffer.

        Rückgabe: je Treffer ein Urteil. Jeder Titel wird jedes Mal neu geprüft -
        auch ein bereits gespeichertes Angebot (siehe Klassendokumentation).
        """
        results: list[TitleVerdict] = []
        for raw in candidates:
            title = str(getattr(raw, "title", "") or "").strip()
            # Der Beschreibungstext rettet nur, wenn der Titel die Komponente
            # nicht nennt (siehe ``match_product``).
            description = str(getattr(raw, "description", "") or "").strip()
            ergebnis, gerettet = match_product(
                title,
                description,
                term=target.term,
                price_list=self.price_list,
                category=target.category,
            )
            if ergebnis is None:
                verdict = TitleVerdict(True, aus_beschreibung=gerettet)
                self.stats.passt += 1
                if gerettet:
                    self.stats.aus_beschreibung += 1
            else:
                art, grund = ergebnis
                verdict = TitleVerdict(False, grund, art=art)
                self.stats.passt_nicht += 1
                self.stats.zaehle(art)
            results.append(verdict)
            self.stats.geprueft += 1
        return results

    def summary(self) -> str:
        """Kurze, wahrheitsgemäße Beschreibung für die Oberfläche."""
        stats = self.stats
        if stats.geprueft == 0:
            return "Titelprüfung: keine neuen Titel zu beurteilen."
        teile = [
            f"{anzahl} {ART_LABELS.get(art, art)}"
            for art, anzahl in sorted(stats.nach_art.items(), key=lambda item: -item[1])
            if anzahl
        ]
        aufschluesselung = f" ({', '.join(teile)})" if teile else ""
        gerettet = (
            f" {stats.aus_beschreibung} davon wurden über die Beschreibung "
            "zugeordnet."
            if stats.aus_beschreibung
            else ""
        )
        return (
            f"Titelprüfung (Schlagwörter): {stats.geprueft} Titel geprüft "
            f"({stats.passt} passend, {stats.passt_nicht} nicht passend{aufschluesselung})."
            + gerettet
        )


__all__ = [
    "ABSOLUTE_PHRASES",
    "ART_LABELS",
    "ART_FREMDGERAET",
    "ART_MODELL",
    "ART_SAMMELANZEIGE",
    "ART_SUCHGESUCH",
    "ART_TEIL",
    "ART_VERPACKUNG",
    "BROAD_FAMILIES",
    "BUCKET_MODEL_LIMIT",
    "CATEGORY_OWN_NOUNS",
    "FOREIGN_PHRASES",
    "PACKAGING_HEADS",
    "PART_CONNECTORS",
    "PART_HEADS",
    "PART_NOUNS",
    "PART_PHRASES",
    "SKIP_PLATFORMS",
    "TitleCheckStats",
    "TitleFilter",
    "TitleTarget",
    "TitleVerdict",
    "WANTED_TOKENS",
    "accessory_reason",
    "classify_title",
    "keyword_match",
    "match_product",
    "target_text",
    "title_key",
    "title_product_reason",
]
