import pytest

from app.bot import parse_add_arguments
from app.security import UnsafeURL, is_authorized_chat, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://169.254.169.254/latest",
    ],
)
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_allowed_domains_are_enforced():
    with pytest.raises(UnsafeURL):
        validate_url("https://example.com/item", {"amazon.com.br"})


def test_chat_authorization():
    assert is_authorized_chat(123, 123)
    assert not is_authorized_chat(456, 123)
    assert not is_authorized_chat(None, 123)


def test_add_command_does_not_accept_shell_like_extra_arguments():
    target, url = parse_add_arguments("29,90 https://www.amazon.com.br/item")
    assert str(target) == "29.90"
    assert url == "https://www.amazon.com.br/item"
