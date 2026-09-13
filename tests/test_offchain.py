import base64

from crawl.config import load_config
from web3 import Web3

import pytest
import httpx

from crawl import offchain
from crawl.offchain import _require_public_url, _retryable_http_error, classify_registration, content_hash_valid, evidence_class, evidence_fields, fetch_uri, uri_scheme


def test_inline_registration_and_evidence(tmp_path):
    app = load_config("config.yaml")
    object.__setattr__(app, "raw", tmp_path)
    payload = '{"type":"https://eips.ethereum.org/EIPS/eip-8004#registration-v1","services":[{"type":"mcp","endpoint":"https://example"}]}'
    uri = "data:application/json;base64," + base64.b64encode(payload.encode()).decode()
    record = fetch_uri(app, uri)
    quality, parsed = classify_registration(uri, record)
    assert quality == "valid_with_service"
    assert parsed["services"][0]["type"] == "mcp"
    assert fetch_uri(app, uri) == record


def test_uri_and_evidence_classification():
    assert uri_scheme("ipfs://abc") == "ipfs"
    assert classify_registration("", {"status": "failure"})[0] == "no_uri"
    assert evidence_class({"proofOfPayment": {"txHash": "0x1"}, "mcpTool": "x"}) == "payment_proof"
    assert evidence_class({"a2aTaskId": "task"}) == "task_linkage"
    assert evidence_class({}) == "no_evidence"
    record = {"status": "success", "body": "hello"}
    assert content_hash_valid(record, "0x" + Web3.keccak(text="hello").hex()) is True


def test_evidence_fields_cover_declared_parties_and_value():
    fields = evidence_fields(
        {
            "proofOfPayment": {"x402Nonce": "n", "txHash": "0xtx"},
            "a2a": {"taskId": "task"},
            "mcp": {"tool": "tool"},
            "timestamp": "now",
            "clientAddress": "client",
            "agentId": 7,
            "value": 90,
        }
    )
    assert fields == {
        "evidence_class": "payment_proof",
        "x402_nonce": "n",
        "payment_tx_hash": "0xtx",
        "a2a_task_id": "task",
        "mcp_tool": "tool",
        "declared_timestamp": "now",
        "declared_client": "client",
        "declared_agent": 7,
        "declared_value": 90,
    }


def test_nested_feedback_data_uses_the_same_evidence_fields():
    parsed = {
        "data": {
            "proofOfPayment": {"x402Nonce": "nonce"},
            "clientAddress": "client",
            "agentId": 7,
            "value": 90,
        }
    }
    fields = evidence_fields(parsed)
    assert evidence_class(parsed) == "payment_proof"
    assert fields["x402_nonce"] == "nonce"
    assert fields["declared_client"] == "client"
    assert fields["declared_agent"] == 7


def test_private_network_uris_are_rejected():
    with pytest.raises(ValueError):
        _require_public_url("http://127.0.0.1/secret")
    with pytest.raises(ValueError):
        _require_public_url("http://169.254.169.254/latest/meta-data")


def test_invalid_http_uri_is_cached_as_failure(tmp_path, monkeypatch):
    app = load_config("config.yaml")
    object.__setattr__(app, "raw", tmp_path)
    monkeypatch.setattr(offchain, "_http_get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.InvalidURL("bad")))
    record = fetch_uri(app, "https://example.com/bad")
    assert record["status"] == "failure"
    assert record["error"] == "InvalidURL: bad"


def test_expired_certificate_is_not_retried():
    assert not _retryable_http_error(httpx.ConnectError("CERTIFICATE_VERIFY_FAILED"))
    assert _retryable_http_error(httpx.ConnectTimeout("timed out"))
