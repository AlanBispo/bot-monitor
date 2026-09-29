from decimal import Decimal

import httpx
import pytest

from app.alerts import AlertEngine
from app.bot import TelegramBot, format_discovery_results
from app.database import Database
from app.discovery import (
    CandidateURL,
    DiscoveryOrchestrator,
    JsonWebDiscoveryProvider,
    StaticDiscoveryProvider,
)
from app.matching import match_offer
from app.models import StoreOffer
from app.monitor import Monitor
from app.query import parse_query
from app.stores.base import StoreAdapter


class FixtureAdapter(StoreAdapter):
    def __init__(self, name, domain, offers):
        self.name = name
        self.domains = {domain}
        self.offers = offers
        self.calls = []

    async def search(self, query):
        return []

    async def get_offer(self, url):
        self.calls.append(url)
        return self.offers[url]


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, message, **kwargs):
        self.replies.append(message)


class FakeUpdate:
    def __init__(self, chat_id=123):
        self.effective_chat = type("Chat", (), {"id": chat_id, "type": "private"})()
        self.effective_message = FakeMessage()


def candidate(store, url, source):
    return CandidateURL(url, source, store)


@pytest.mark.asyncio
async def test_mandatory_acceptance_scenario_is_deterministic_and_direct_monitoring(tmp_path):
    query = parse_query("celular modelo X 128GB")
    amazon_url = "https://amazon.fixture/item-x"
    mercado_url = "https://ml.fixture/item-x"
    unknown_url = "https://magalu.fixture/item-x"
    wrong_url = "https://amazon.fixture/item-x-256"
    amazon = FixtureAdapter(
        "amazon", "amazon.fixture", {
            amazon_url: StoreOffer(
                "amazon", "Celular modelo X 128GB", amazon_url,
                Decimal("100"), Decimal("10"),
            ),
            wrong_url: StoreOffer(
                "amazon", "Celular modelo X 256GB", wrong_url, Decimal("80"), Decimal("0"),
                attributes={"capacity": "256 gb"},
            ),
        },
    )
    mercado = FixtureAdapter(
        "mercadolivre", "ml.fixture", {
            mercado_url: StoreOffer(
                "mercadolivre", "Celular modelo X 128GB", mercado_url,
                Decimal("95"), Decimal("20"),
            ),
        },
    )
    magalu = FixtureAdapter(
        "magalu", "magalu.fixture", {
            unknown_url: StoreOffer(
                "magalu", "Celular modelo X 128GB", unknown_url,
                Decimal("90"), None,
            ),
        },
    )
    providers = [
        StaticDiscoveryProvider(
            "amazon-search",
            [
                candidate("amazon", amazon_url, "amazon-search"),
                candidate("amazon", wrong_url, "amazon-search"),
            ],
        ),
        StaticDiscoveryProvider(
            "mercado-search",
            [
                candidate("mercadolivre", mercado_url, "mercado-search"),
                candidate("amazon", amazon_url, "mercado-search"),
            ],
        ),
        StaticDiscoveryProvider(
            "magalu-search", [candidate("magalu", unknown_url, "magalu-search")]
        ),
        StaticDiscoveryProvider("web-timeout", [], TimeoutError("fixture timeout")),
    ]
    orchestrator = DiscoveryOrchestrator(
        {"amazon": amazon, "mercadolivre": mercado, "magalu": magalu}, providers,
        source_timeout=1, max_candidates=20,
    )

    outcome = await orchestrator.search(query)

    assert outcome.status == "partial"
    assert outcome.source_coverage["sources"]["web-timeout"]["status"] == "failed"
    assert [result.offer.url for result in outcome.results].count(amazon_url) == 1
    assert outcome.results[0].pricing.effective_price == Decimal("110.00")
    assert outcome.results[1].pricing.effective_price == Decimal("115.00")
    unknown = next(result for result in outcome.results if result.offer.url == unknown_url)
    assert unknown.decision.status == "matched"
    assert unknown.pricing.effective_price is None
    wrong = next(result for result in outcome.results if result.offer.url == wrong_url)
    assert wrong.decision.status == "rejected"
    assert wrong.decision.reason == "hard_mismatch_capacity"

    db = Database(tmp_path / "prices.db")
    db.initialize()
    session = db.create_search_session(
        123, query.original_text, query.to_json(), outcome.status, outcome.source_coverage,
        outcome.entries(),
    )
    selected = db.get_search_entry(session.id, "R1", chat_id=123)
    assert selected.url == amazon_url
    product, listing = db.register_product_listing(
        selected.title, Decimal("110"), selected.store, selected.url, selected.title,
        canonical_url=selected.canonical_url, discovery_source=selected.discovery_source,
    )
    amazon.calls.clear()
    monitor = Monitor(db, {"amazon": amazon}, AlertEngine(db))
    messages = []

    async def send(message):
        messages.append(message)

    assert await monitor.check_once(send) == 1
    assert await monitor.check_once(send) == 0
    assert amazon.calls == [amazon_url, amazon_url]
    assert len(db.history(listing.id)) == 2
    assert len(messages) == 1
    assert product.id == listing.product_id


def test_query_parser_preserves_model_capacity_and_is_stable():
    first = parse_query('  celular modelo X-200 1TB "linha premium" até R$ 1.999,90 novo  ')
    second = parse_query(first.normalized_text)
    assert first.original_text.startswith("celular")
    assert first.model == "x-200"
    assert first.capacity == "1 tb"
    assert first.quoted_phrases == ("linha premium",)
    assert first.max_price == "1999.90"
    assert first == second


def test_query_parser_recognizes_volume_size_quantity_and_condition():
    query = parse_query("shampoo 500ml tamanho 10cm pack com 6 unidades usado")
    assert query.volume == "500 ml"
    assert query.size == "10 cm"
    assert query.package_quantity == 6
    assert query.condition == "usado"


def test_query_variants_keep_model_and_capacity_with_quoted_phrase():
    query = parse_query('celular modelo X 128GB "linha premium"')
    assert query.model == "x"
    assert query.variants
    assert all("modelo X 128GB" in variant for variant in query.variants)


def test_matching_rejects_conflicting_implicit_model_and_size():
    model_query = parse_query("Celular S24 128GB")
    model_offer = StoreOffer(
        "shop", "Celular S23 128GB", "https://shop.fixture/s23", Decimal("1"), Decimal("0")
    )
    assert match_offer(model_query, model_offer).reason == "hard_mismatch_model"
    size_query = parse_query("Caixa 10cm")
    size_offer = StoreOffer(
        "shop", "Caixa 12cm", "https://shop.fixture/12", Decimal("1"), Decimal("0")
    )
    assert match_offer(size_query, size_offer).reason == "hard_mismatch_size"
    clothing_query = parse_query("Camiseta tamanho G")
    clothing_offer = StoreOffer(
        "shop", "Camiseta tamanho M", "https://shop.fixture/m", Decimal("1"), Decimal("0")
    )
    assert match_offer(clothing_query, clothing_offer).reason == "hard_mismatch_size"
    color_query = parse_query("Celular azul")
    color_offer = StoreOffer(
        "shop", "Celular vermelho", "https://shop.fixture/red", Decimal("1"), Decimal("0")
    )
    assert match_offer(color_query, color_offer).reason == "hard_mismatch_color"


def test_matching_accepts_equivalent_units_and_condition_gender():
    query = parse_query("Shampoo 3L novo")
    offer = StoreOffer(
        "shop", "Shampoo 3000ml nova", "https://shop.fixture/shampoo",
        Decimal("1"), Decimal("0"),
    )
    assert match_offer(query, offer).status == "matched"
    color_query = parse_query("Celular azul")
    color_offer = StoreOffer(
        "shop", "Celular blue", "https://shop.fixture/blue", Decimal("1"), Decimal("0")
    )
    assert match_offer(color_query, color_offer).status == "matched"


@pytest.mark.parametrize("text", ["celular até R$ 1.2,34", "celular máximo abc"])
def test_query_rejects_malformed_explicit_max_price(text):
    from app.query import InvalidQuery

    with pytest.raises(InvalidQuery, match="preço máximo inválido"):
        parse_query(text)


@pytest.mark.asyncio
async def test_telegram_search_and_short_selection_use_persisted_session(tmp_path):
    url = "https://shop.fixture/item"
    offer = StoreOffer("shop", "Produto modelo A 1TB", url, Decimal("50"), Decimal("5"))
    adapter = FixtureAdapter("shop", "shop.fixture", {url: offer})
    discovery = DiscoveryOrchestrator(
        {"shop": adapter},
        [StaticDiscoveryProvider("shop-search", [candidate("shop", url, "shop-search")])],
    )
    db = Database(tmp_path / "prices.db")
    db.initialize()
    bot = TelegramBot(db, {"shop": adapter}, 123, discovery)

    search_update = FakeUpdate()
    search_context = type(
        "Context", (), {"args": ["produto", "modelo", "A", "1TB"]}
    )()
    await bot.search(search_update, search_context)
    assert "Busca " in search_update.effective_message.replies[0]
    assert "R1" in search_update.effective_message.replies[0]
    session_id = search_update.effective_message.replies[0].split()[1]

    select_update = FakeUpdate()
    await bot.select(
        select_update,
        type("Context", (), {"args": [session_id, "R1", "55"]})(),
    )
    assert "confirmado" in select_update.effective_message.replies[0]
    assert len(db.list_listings(active_only=False)) == 1


@pytest.mark.asyncio
async def test_selection_rejects_product_variant_changed_after_search(tmp_path):
    url = "https://shop.fixture/phone"
    adapter = FixtureAdapter(
        "shop", "shop.fixture",
        {url: StoreOffer("shop", "Celular modelo X 128GB", url, Decimal("100"), Decimal("0"))},
    )
    discovery = DiscoveryOrchestrator(
        {"shop": adapter},
        [StaticDiscoveryProvider("shop-search", [candidate("shop", url, "shop-search")])],
    )
    db = Database(tmp_path / "prices.db")
    db.initialize()
    bot = TelegramBot(db, {"shop": adapter}, 123, discovery)
    search_update = FakeUpdate()
    await bot.search(
        search_update,
        type("Context", (), {"args": ["Celular", "modelo", "X", "128GB"]})(),
    )
    session_id = search_update.effective_message.replies[0].split()[1]
    adapter.offers[url] = StoreOffer(
        "shop", "Celular modelo X 256GB", url, Decimal("80"), Decimal("0")
    )
    select_update = FakeUpdate()
    await bot.select(
        select_update,
        type("Context", (), {"args": [session_id, "R1", "100"]})(),
    )
    assert "hard_mismatch_capacity" in select_update.effective_message.replies[0]
    assert db.list_products() == []


@pytest.mark.asyncio
async def test_manual_ambiguous_confirmation_can_be_monitored_without_hard_mismatch(tmp_path):
    url = "https://shop.fixture/phone"
    adapter = FixtureAdapter(
        "shop", "shop.fixture",
        {url: StoreOffer("shop", "Celular linha X", url, Decimal("100"), Decimal("0"))},
    )
    discovery = DiscoveryOrchestrator(
        {"shop": adapter},
        [StaticDiscoveryProvider("shop-search", [candidate("shop", url, "shop-search")])],
    )
    db = Database(tmp_path / "prices.db")
    db.initialize()
    bot = TelegramBot(db, {"shop": adapter}, 123, discovery)
    search_update = FakeUpdate()
    await bot.search(
        search_update,
        type("Context", (), {"args": ["Celular", "128GB"]})(),
    )
    session_id = search_update.effective_message.replies[0].split()[1]
    confirm_update = FakeUpdate()
    await bot.confirm(
        confirm_update,
        type("Context", (), {"args": [session_id, "R1", "100"]})(),
    )
    assert "confirmado" in confirm_update.effective_message.replies[0]
    sent = []

    async def send(message):
        sent.append(message)

    monitor = Monitor(db, {"shop": adapter}, AlertEngine(db))
    assert await monitor.check_once(send) == 1
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_explicit_max_price_rejects_high_offers_and_marks_unknown_shipping_ambiguous():
    high_url = "https://shop.fixture/high"
    unknown_url = "https://shop.fixture/unknown"
    adapter = FixtureAdapter(
        "shop",
        "shop.fixture",
        {
            high_url: StoreOffer("shop", "fone modelo A", high_url, Decimal("120"), Decimal("0")),
            unknown_url: StoreOffer("shop", "fone modelo A", unknown_url, Decimal("90"), None),
        },
    )
    provider = StaticDiscoveryProvider(
        "fixture",
        [
            candidate("shop", high_url, "fixture"),
            candidate("shop", unknown_url, "fixture"),
        ],
    )
    outcome = await DiscoveryOrchestrator({"shop": adapter}, [provider]).search(
        parse_query("fone modelo A até R$ 100,00")
    )
    high = next(result for result in outcome.results if result.offer.url == high_url)
    unknown = next(result for result in outcome.results if result.offer.url == unknown_url)
    assert high.decision.status == "rejected"
    assert high.decision.reason == "over_max_price"
    assert unknown.decision.status == "ambiguous"
    assert unknown.decision.reason == "max_price_unverified"


@pytest.mark.asyncio
async def test_conditional_price_is_displayed_separately_from_guaranteed_price():
    url = "https://shop.fixture/item"
    adapter = FixtureAdapter(
        "shop", "shop.fixture",
        {url: StoreOffer(
            "shop", "Produto teste", url, Decimal("100"), Decimal("0"),
            possible_discount=Decimal("10"),
        )},
    )
    outcome = await DiscoveryOrchestrator(
        {"shop": adapter},
        [StaticDiscoveryProvider("fixture", [candidate("shop", url, "fixture")])],
    ).search(parse_query("Produto teste"))
    rendered = format_discovery_results("SESSION", outcome)
    assert "efetivo R$ 100.00" in rendered
    assert "possível R$ 90.00 (condicional)" in rendered


@pytest.mark.asyncio
async def test_canonical_urls_collapse_tracking_variants():
    first_url = "https://shop.fixture/item?campaign=first"
    second_url = "https://shop.fixture/item?campaign=second"
    canonical = "https://shop.fixture/item"
    adapter = FixtureAdapter(
        "shop",
        "shop.fixture",
        {
            first_url: StoreOffer(
                "shop", "produto fone", first_url, Decimal("20"), Decimal("0"),
                canonical_url=canonical,
            ),
            second_url: StoreOffer(
                "shop", "produto fone", second_url, Decimal("20"), Decimal("0"),
                canonical_url=canonical,
            ),
        },
    )
    provider = StaticDiscoveryProvider(
        "fixture",
        [
            candidate("shop", first_url, "first"),
            candidate("shop", second_url, "second"),
        ],
    )
    outcome = await DiscoveryOrchestrator({"shop": adapter}, [provider]).search(
        parse_query("produto fone")
    )
    assert len(outcome.results) == 1
    assert outcome.results[0].offer.canonical_url == canonical


@pytest.mark.asyncio
async def test_one_candidate_parser_crash_does_not_discard_other_valid_offer():
    good_url = "https://shop.fixture/good"
    bad_url = "https://shop.fixture/bad"

    class PartiallyBrokenAdapter(FixtureAdapter):
        async def get_offer(self, url):
            if url == bad_url:
                raise RuntimeError("changed external page")
            return await super().get_offer(url)

    adapter = PartiallyBrokenAdapter(
        "shop", "shop.fixture",
        {good_url: StoreOffer(
            "shop", "Produto teste", good_url, Decimal("10"), Decimal("0")
        )},
    )
    outcome = await DiscoveryOrchestrator(
        {"shop": adapter},
        [StaticDiscoveryProvider(
            "fixture", [candidate("shop", bad_url, "fixture"),
                        candidate("shop", good_url, "fixture")]
        )],
    ).search(parse_query("Produto teste"))
    assert [result.offer.url for result in outcome.results] == [good_url]
    assert outcome.source_coverage["validation_failed"] == 1


@pytest.mark.asyncio
async def test_web_provider_discovers_urls_but_snippet_price_is_not_ranked():
    product_url = "https://shop.fixture/item"
    adapter = FixtureAdapter(
        "shop", "shop.fixture",
        {product_url: StoreOffer(
            "shop", "Produto teste", product_url, Decimal("50"), Decimal("0")
        )},
    )

    def handler(request):
        assert request.url.params["q"] == "Produto teste"
        return httpx.Response(
            200,
            json={"results": [{"url": product_url, "store": "shop", "price": "1.00"}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JsonWebDiscoveryProvider("https://search.example/api", client=client)
        outcome = await DiscoveryOrchestrator({"shop": adapter}, [provider]).search(
            parse_query("Produto teste")
        )
    assert outcome.results[0].pricing.effective_price == Decimal("50.00")
    assert outcome.source_coverage["sources"]["web"]["status"] == "completed"


@pytest.mark.asyncio
async def test_telegram_commands_reject_authorized_id_in_group_chat(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    bot = TelegramBot(db, {}, 123)
    update = FakeUpdate(123)
    update.effective_chat.type = "group"
    await bot.status(update, type("Context", (), {"args": []})())
    assert update.effective_message.replies == ["Acesso não autorizado."]
