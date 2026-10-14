"""Sammelt echte eBay-Titel als Prüfkorpus (einmalig, offline weiterverwendbar)."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3

from backend.config import get_settings
from backend.connectors.base import SearchQuerySpec
from backend.connectors.ebay_web import _clean_title
from backend.deps import build_state
from backend.domain.normalize import searchable_condition_ranks
from backend.domain.price_limits import load_price_list

TERMS = ["RTX 4070", "RX 6600", "RX 580", "RTX 3060", "GTX 1660 Super"]
OUT = pathlib.Path("data/title_corpus.json")


async def main() -> None:
    state = build_state(get_settings())
    connector = state.registry.get("ebay_web")
    assert connector is not None
    price_list = load_price_list(state.settings.price_list_path)
    gesammelt: list[dict] = []
    try:
        for term in TERMS:
            limit = price_list.limit_for(title=term)
            for page in (1, 2):
                spec = SearchQuerySpec(
                    term=term,
                    platform="ebay_web",
                    condition_texts=[str(rank) for rank in searchable_condition_ranks(None)],
                    fixed_price_only=True,
                    max_price=limit.max_price if limit else None,
                    min_price=None,
                    offset=(page - 1) * 60,
                )
                url = connector._build_url(spec, page_size=60, page=page)
                data = await connector._fetch(url)
                if data.get("blocked"):
                    print(f"{term} S.{page}: blockiert")
                    continue
                items = data.get("items") or []
                for item in items:
                    gesammelt.append(
                        {
                            "term": term,
                            "limit": limit.max_price if limit else None,
                            "title": _clean_title(item.get("title")),
                            "price": item.get("price"),
                            "subtitle": str(item.get("subtitle") or ""),
                        }
                    )
                print(f"{term} S.{page}: {len(items)} Titel")
    finally:
        await state.registry.aclose()
        state.engine.dispose()

    OUT.write_text(json.dumps(gesammelt, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(gesammelt)} Titel -> {OUT}")

    speicher = pathlib.Path("data/title_corpus_stored.json")
    con = sqlite3.connect("data/app.db")
    con.row_factory = sqlite3.Row
    rows = [
        {
            "title": r["title"],
            "price": r["total_price"],
            "category": r["category"],
            "model": r["model"],
        }
        for r in con.execute("select title, total_price, category, model from listings")
    ]
    speicher.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)} gespeicherte Angebote -> {speicher}")


if __name__ == "__main__":
    asyncio.run(main())
