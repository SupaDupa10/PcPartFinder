"""Messung der neuen Titelprüfung gegen echte Titel."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, ".")

from scripts._cases import MUST_ACCEPT, MUST_REJECT  # noqa: E402

from backend.domain.price_limits import load_price_list  # noqa: E402
from backend.search.titles import title_product_reason  # noqa: E402

price_list = load_price_list("Höchstpreise.txt")


def urteil(title: str, category: str = "gpu") -> tuple[str, str] | None:
    return title_product_reason(title, term=None, price_list=price_list, category=category)


def pruefe_faelle() -> int:
    fehler = 0
    print("=== Fallliste ===")
    for title, erwartet in MUST_REJECT:
        ergebnis = urteil(title)
        if ergebnis is None:
            print(f"  FEHLER: nicht aussortiert: {title[:80]}")
            fehler += 1
        elif ergebnis[0] != erwartet:
            print(f"  HINWEIS: Art {ergebnis[0]} statt {erwartet}: {title[:70]}")
    for title in MUST_ACCEPT:
        if urteil(title) is not None:
            print(f"  FEHLER: fälschlich aussortiert: {title[:80]} :: {urteil(title)[1][:60]}")
            fehler += 1
    print(f"  {len(MUST_REJECT)} Ausschluss- und {len(MUST_ACCEPT)} Karten-Fälle, {fehler} Fehler")
    return fehler


def zeige_korpus() -> None:
    stored = json.loads(pathlib.Path("data/title_corpus_stored.json").read_text(encoding="utf-8"))
    durch, zaehler = [], {}
    for row in stored:
        ergebnis = urteil(row["title"])
        if ergebnis is None:
            durch.append(row["title"])
        else:
            zaehler[ergebnis[0]] = zaehler.get(ergebnis[0], 0) + 1
    print(f"\n=== gespeicherte Angebote: {len(stored)} -> {len(durch)} durchgelassen ===")
    print("  aussortiert:", zaehler)
    for title in durch:
        print(f"    ✓ {title[:100]}")

    corpus = json.loads(pathlib.Path("data/title_corpus.json").read_text(encoding="utf-8"))
    print("\n=== eBay-Korpus ===")
    for term in sorted({c["term"] for c in corpus}):
        titel = [c["title"] for c in corpus if c["term"] == term]
        ok, rest = [], {}
        for title in titel:
            ergebnis = urteil(title)
            if ergebnis is None:
                ok.append(title)
            else:
                rest[ergebnis[0]] = rest.get(ergebnis[0], 0) + 1
        print(f"  {term:16s}: {len(titel):3d} -> {len(ok):3d} durch, aussortiert {rest}")
        for title in ok[:4]:
            print(f"        ✓ {title[:95]}")


if __name__ == "__main__":
    fehler = pruefe_faelle()
    zeige_korpus()
    raise SystemExit(1 if fehler else 0)
