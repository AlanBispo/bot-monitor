"""Daily summary: queued secondary alerts, current prices and store health in one message."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from .alerts import describe_queued_alert
from .database import Database
from .models import Alert
from .pricing import format_brl


def build_daily_summary(database: Database, now: datetime | None = None) -> tuple[str, list[Alert]]:
    """Return the summary text and the queued alerts it contains (to mark as sent later)."""
    now = now or datetime.now(UTC)
    queued = database.queued_alerts()
    listings = {listing.id: listing for listing in database.list_listings(active_only=False)}
    products = {product.id: product for product in database.list_products()}
    lines = [f"📊 Resumo diário — {now.astimezone().strftime('%d/%m/%Y')}"]

    by_listing: dict[int, list[Alert]] = defaultdict(list)
    for alert in queued:
        by_listing[alert.listing_id].append(alert)
    lines.append("")
    if by_listing:
        lines.append("Novidades desde o último resumo:")
        for listing_id, alerts in by_listing.items():
            listing = listings[listing_id]
            product = products[listing.product_id]
            lines.append(f"• {product.id}. {product.name} [{listing.store}]")
            # Repeated events of one type in the same day: the latest is the relevant one.
            latest = {alert.alert_type: alert for alert in alerts}
            lines.extend(f"   {describe_queued_alert(alert)}" for alert in latest.values())
            lines.append(f"   {listing.url}")
    else:
        lines.append("Nenhuma novidade de preço ou estoque desde o último resumo.")

    active = [product for product in products.values() if product.active]
    if active:
        lines.append("")
        lines.append("Produtos monitorados (preço do produto, sem frete):")
        day_ago = now - timedelta(days=1)
        for product in active:
            prices = []
            for listing in listings.values():
                if listing.product_id != product.id or not listing.active:
                    continue
                rows = [row for row in database.history(listing.id)
                        if row.available and row.product_price is not None]
                if rows and not listing.last_error:
                    previous = [row for row in rows if row.captured_at <= day_ago]
                    prices.append((rows[-1].product_price,
                                   previous[-1].product_price if previous else None))
            if not prices:
                lines.append(f"• {product.id}. {product.name}: sem preço atual confirmado")
                continue
            current, before = min(prices, key=lambda item: item[0])
            change = ""
            if before is not None and before != current:
                sign = "+" if current > before else "-"
                change = f" · 24h: {sign}{format_brl(abs(current - before))}"
            target = (f" (alvo {format_brl(product.target_price)})"
                      if product.monitoring_mode == "target" else "")
            lines.append(f"• {product.id}. {product.name}: {format_brl(current)}{target}{change}")

    failing = [health for health in database.store_health() if health.consecutive_failures]
    if failing:
        lines.append("")
        lines.append("Lojas com falha na última verificação:")
        for health in failing:
            lines.append(
                f"• {health.store}: {health.consecutive_failures} ciclo(s) seguidos — "
                f"{health.last_error or 'erro desconhecido'}"
            )
    return "\n".join(lines), queued
