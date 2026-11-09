"""CSV export of the price history, formatted for Excel in pt-BR."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from .database import Database

COLUMNS = (
    ("product_id", "produto_id"), ("product_name", "produto"), ("listing_id", "listing_id"),
    ("store", "loja"), ("url", "url"), ("captured_at", "capturado_em"),
    ("available", "disponivel"), ("product_price", "preco_produto"),
    ("shipping_price", "frete"), ("confirmed_discount", "desconto_confirmado"),
    ("possible_discount", "desconto_condicional"), ("effective_price", "preco_efetivo"),
    ("possible_effective_price", "preco_possivel"), ("seller", "vendedor"),
)
MONEY = {"product_price", "shipping_price", "confirmed_discount", "possible_discount",
         "effective_price", "possible_effective_price"}


def _cell(key: str, value) -> str:
    if value is None:
        return ""
    if key in MONEY:
        return str(value).replace(".", ",")
    if key == "available":
        return "sim" if value else "não"
    if key == "captured_at":
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    # Titles and sellers come from store pages; stop spreadsheets from running them as formulas.
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        text = "'" + text
    return text


def history_csv(database: Database, product_id: int | None = None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header for _, header in COLUMNS)
    for row in database.export_rows(product_id):
        writer.writerow(_cell(key, row[key]) for key, _ in COLUMNS)
    # BOM so Excel detects UTF-8 accents.
    return buffer.getvalue().encode("utf-8-sig")
