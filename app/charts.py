"""PNG price chart for /historico. Matplotlib is imported lazily (it is slow to load)."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from .database import Database
from .models import Product


class NoChartData(ValueError):
    """There is no reliable price in the requested period."""


def render_price_chart(database: Database, product: Product, days: int = 90) -> bytes:
    """One line per listing; target-mode products plot the effective price when known.

    A listing without any known shipping falls back to the product price and says so in the
    legend, so values with and without shipping are never mixed on the same line.
    """
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker
    from matplotlib.figure import Figure

    since = datetime.now(UTC) - timedelta(days=days)
    series = []
    for listing in database.list_listings(active_only=False):
        if listing.product_id != product.id:
            continue
        rows = [row for row in database.history(listing.id, since) if row.available]
        label = f"{listing.store} #{listing.id}"
        points = []
        if product.monitoring_mode == "target":
            points = [(row.captured_at, row.effective_price) for row in rows
                      if row.effective_price is not None]
        if not points:
            points = [(row.captured_at, row.product_price) for row in rows
                      if row.product_price is not None]
            if product.monitoring_mode == "target":
                label += " (sem frete)"
        if points:
            series.append((label, points))
    if not series:
        raise NoChartData("Nenhum preço confiável no período para gerar o gráfico.")

    figure = Figure(figsize=(9, 4.8), dpi=120)
    axis = figure.subplots()
    for label, points in series:
        moments = [moment.astimezone() for moment, _ in points]
        axis.plot(moments, [float(price) for _, price in points],
                  marker="o", markersize=3, linewidth=1.6, label=label)
    if product.monitoring_mode == "target":
        axis.axhline(float(product.target_price), color="#c0392b", linestyle="--",
                     linewidth=1.2, label=f"alvo R$ {product.target_price:.2f}")
    axis.set_title(product.name[:80], fontsize=11)
    axis.set_ylabel("R$")
    axis.grid(True, alpha=0.3)
    axis.yaxis.set_major_formatter(
        mticker.FuncFormatter(
            lambda value, _: f"{value:,.2f}".replace(",", "X").replace(".", ",")
            .replace("X", ".")
        )
    )
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axis.legend(fontsize=8, loc="best")
    figure.autofmt_xdate()
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    return buffer.getvalue()
