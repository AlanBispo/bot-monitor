"""Listing keys that discard tracking while retaining variant and seller parameters."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = {"ref", "ref_", "tag", "linkcode", "ascsubtag", "campaign", "matt_tool", "matt_word"}


def listing_url(url: str, canonical: str | None = None) -> str:
    source = urlsplit(url)
    chosen = urlsplit(canonical or url)
    # A canonical hint is not allowed to change a generic store's identity.
    if source.hostname != chosen.hostname:
        chosen = source
    host = source.hostname or ""
    known_store = any(
        host == domain or host.endswith(f".{domain}")
        for domain in (
            "amazon.com.br",
            "amazon.com",
            "mercadolivre.com.br",
            "mercadolibre.com.br",
            "magazineluiza.com.br",
            "magalu.com",
        )
    )
    canonical_changed = bool(canonical) and (source.path, source.query) != (
        chosen.path,
        chosen.query,
    )
    params = parse_qsl(source.query, keep_blank_values=True)
    source_keys = {key for key, _ in params}
    params += [
        (key, value)
        for key, value in parse_qsl(chosen.query, keep_blank_values=True)
        if key not in source_keys
    ]
    params = [
        (key, value)
        for key, value in params
        if not ((known_store or canonical_changed) and key.casefold() in TRACKING)
        and not key.casefold().startswith("utm_")
    ]
    path = chosen.path or "/"
    if (source.hostname or "").removeprefix("www.") in {"amazon.com.br", "amazon.com"}:
        asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", path, re.I)
        if asin:
            path = f"/dp/{asin.group(1).upper()}"
    return urlunsplit(
        (chosen.scheme.lower(), chosen.netloc.lower(), path, urlencode(sorted(params)), "")
    )


def listing_key(store: str, url: str, canonical: str | None = None) -> str:
    return f"{store}:{listing_url(url, canonical)}"
