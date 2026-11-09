"""Text helpers shared by Telegram commands and buttons."""

from __future__ import annotations

from ..discovery import SearchOutcome

MAX_EXTERNAL_TITLE_LENGTH = 300
TELEGRAM_TEXT_LIMIT = 4096
MAX_DISPLAY_URL_LENGTH = 1800
DATABASE_FAILURE_MESSAGE = "Falha no banco de dados; tente novamente."


def product_identifier(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("ID de produto inválido") from None
    if not 0 < value <= 2**63 - 1:
        raise ValueError("ID de produto inválido")
    return value


def short_text(value: str, limit: int = MAX_EXTERNAL_TITLE_LENGTH) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long responses at line boundaries, splitting only oversized records."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for record in text.splitlines():
        if len(record) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(record) > limit:
                chunks.append(record[:limit])
                record = record[limit:]
            current = record
            continue
        candidate = record if not current else f"{current}\n{record}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = record
    if current or not chunks:
        chunks.append(current)
    return chunks


def format_discovery_results(session_id: str, outcome: SearchOutcome) -> str:
    coverage = outcome.source_coverage
    sources = coverage.get("sources", {})
    completed = [name for name, data in sources.items() if data.get("status") == "completed"]
    failed = [name for name, data in sources.items() if data.get("status") == "failed"]
    lines = [
        f"Busca {session_id} ({outcome.status}); fontes concluídas: "
        f"{', '.join(completed) or 'nenhuma'}.",
    ]
    if failed:
        lines.append(f"Fontes com falha/timeout: {', '.join(failed)}. Resultado parcial.")
    lines.append(
        f"Candidatos: {coverage.get('candidates_discovered', 0)}; "
        f"validados: {coverage.get('validated', 0)}; "
        f"falhas de validação: {coverage.get('validation_failed', 0)}; "
        f"ambíguos: {coverage.get('ambiguous', 0)}; excluídos: {coverage.get('rejected', 0)}."
    )
    lines.append(
        "Selecione: /selecionar SESSAO ID PREÇO_ALVO (ou use um botão); "
        "para ambíguo, use /confirmar."
    )
    lines.append("")
    if not outcome.results:
        lines.append("Nenhuma oferta validada.")
        return "\n".join(lines)
    for result in outcome.results:
        if result.pricing is None:
            price = "sem preço confiável"
        elif result.pricing.effective_price is not None:
            price = f"efetivo R$ {result.pricing.effective_price:.2f}"
            if result.pricing.possible_effective_price is not None:
                possible = result.pricing.possible_effective_price
                price += f"; possível R$ {possible:.2f} (condicional)"
            if result.uncertainty:
                price += f" ({result.uncertainty})"
        else:
            price = (f"produto R$ {result.pricing.product_price:.2f}; "
                     "frete desconhecido; total não calculável; não é o menor garantido")
            if result.pricing.possible_effective_price is not None:
                price += f"; possível R$ {result.pricing.possible_effective_price:.2f}"
        marker = result.decision.status
        lines.append(
            f"{result.short_id}. [{result.offer.store}] "
            f"{short_text(result.offer.title, 180)} — {price} "
            f"[{marker}: {result.decision.reason}]\n"
            f"{short_text(result.offer.url, MAX_DISPLAY_URL_LENGTH)}"
        )
    if coverage.get("unknown_shipping"):
        lines.append(
            "Ofertas com frete desconhecido foram exibidas, mas não entram "
            "no menor preço garantido."
        )
    return "\n".join(lines)
