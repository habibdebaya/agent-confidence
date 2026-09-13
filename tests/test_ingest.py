from types import SimpleNamespace

from crawl import ingest


def test_blockscout_transactions_cache_and_range(tmp_path, monkeypatch):
    app = SimpleNamespace(raw=tmp_path)
    chain = SimpleNamespace(name="base", transaction_api_url="https://example/api/v2")
    pages = iter(
        [
            {
                "items": [{"block_number": 150}, {"block_number": 120}],
                "next_page_params": {"block_number": 120, "index": 1, "items_count": 50},
            },
            {"items": [{"block_number": 90}], "next_page_params": None},
        ]
    )
    monkeypatch.setattr(ingest, "_blockscout_transactions_request", lambda *args: next(pages))
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)

    rows = ingest.blockscout_transactions(app, chain, "0xabc", 100, 200)
    assert rows == [{"block_number": 150}, {"block_number": 120}]

    monkeypatch.setattr(
        ingest,
        "_blockscout_transactions_request",
        lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert ingest.blockscout_transactions(app, chain, "0xabc", 100, 200) == rows


def test_blockscout_transaction_context(monkeypatch):
    app = SimpleNamespace(identity="0x1", reputation="0x2")
    chain = SimpleNamespace(transaction_api_url="https://example/api/v2")
    row = {
        "hash": "0xABC",
        "from": {"hash": "0xDEF"},
        "gas_used": "21000",
        "gas_price": "1000000",
        "timestamp": "2026-05-13T21:49:47.000000Z",
    }
    monkeypatch.setattr(ingest, "blockscout_transactions", lambda *args: [row])

    context = ingest.explorer_transaction_context(app, chain, 1, 2)
    assert context["0xabc"]["tx_from"] == "0xdef"
    assert context["0xabc"]["gas_used"] == 21000
    assert context["0xabc"]["effective_gas_price"] == 1000000
    assert context["0xabc"]["block_timestamp"] == 1778708987
    assert context["0xabc"]["context_source"] == "blockscout_v2"
