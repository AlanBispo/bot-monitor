"""Validation for URLs and Telegram configuration."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .urls import host_matches


class UnsafeURL(ValueError):
    """Raised when a URL should not be requested by the application."""


def validate_url(url: str, allowed_domains: set[str] | None = None) -> str:
    """Return a normalized URL only when it is safe enough to fetch."""
    if len(url) > 2048:
        raise UnsafeURL("URL longa demais")
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("somente URLs http e https são aceitas")
    try:
        _port = parsed.port
    except ValueError:
        raise UnsafeURL("porta inválida") from None
    if parsed.username or parsed.password:
        raise UnsafeURL("URLs com credenciais não são aceitas")

    hostname = parsed.hostname.rstrip(".").lower()
    blocked_names = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if hostname in blocked_names or hostname.endswith(".local"):
        raise UnsafeURL("host local não é permitido")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise UnsafeURL("endereço de rede privada não é permitido")

    if allowed_domains and not host_matches(hostname, allowed_domains):
        raise UnsafeURL("domínio não suportado")
    return parsed.geturl()


def resolve_public_host(hostname: str, port: int) -> str:
    """Resolve and pin a host to one address after rejecting unsafe answers.

    All returned addresses must be globally routable. Rejecting a mixed DNS response
    avoids choosing a public address while another answer points at a private target.
    The caller uses the returned address for the actual TCP connection.
    """
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURL("não foi possível resolver o domínio") from exc
    addresses: list[str] = []
    for record in records:
        address = record[4][0]
        if address not in addresses:
            addresses.append(address)
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global:
            raise UnsafeURL("o domínio resolve para uma rede não pública")
    if not addresses:
        raise UnsafeURL("domínio sem endereço resolvido")
    return addresses[0]


def is_authorized_chat(chat_id: int | None, allowed_chat_id: int) -> bool:
    """Keep authorization in one testable function used by every bot command."""
    return chat_id is not None and chat_id == allowed_chat_id
