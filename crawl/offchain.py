from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import socket
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

import httpx
import polars as pl
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from web3 import Web3

from .cache import cache_key, read_json, write_json
from .config import AppConfig, ChainConfig


REGISTRATION_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"


def _retryable_http_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPError) and "CERTIFICATE_VERIFY_FAILED" not in str(exc)


@retry(
    retry=retry_if_exception(_retryable_http_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)
def _http_get(
    url: str,
    timeout: float = 10,
    client: httpx.Client | None = None,
) -> httpx.Response:
    current = url
    for _ in range(6):
        _require_public_url(current)
        response = (client or httpx).get(current, timeout=timeout, follow_redirects=False)
        if response.has_redirect_location:
            location = response.headers["location"]
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        return response
    raise httpx.TooManyRedirects("more than five redirects")


def _require_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("unsafe HTTP URI")
    addresses = {
        item[4][0]
        for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    }
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("URI resolves to a non-public address")


def uri_scheme(uri: str) -> str:
    value = uri.strip()
    lower = value.lower()
    if not value:
        return "empty"
    if lower.startswith("https://"):
        return "https"
    if lower.startswith("http://"):
        return "http"
    if lower.startswith("ipfs://"):
        return "ipfs"
    if lower.startswith("data:"):
        return "data"
    if value.startswith("{") or value.startswith("["):
        return "raw_json"
    if lower.startswith("<svg") or lower.startswith("<?xml"):
        return "raw_xml_svg"
    return "unknown"


def _decode_data_uri(uri: str) -> bytes:
    header, payload = uri.split(",", 1)
    if ";base64" in header.lower():
        return base64.b64decode(payload, validate=True)
    return unquote_to_bytes(payload)


def _body_fields(raw: bytes) -> dict[str, str]:
    return {
        "body": raw.decode("utf-8", errors="replace"),
        "body_b64": base64.b64encode(raw).decode(),
    }


def fetch_uri(
    app: AppConfig,
    uri: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    path = app.raw / "offchain" / f"{cache_key(uri)}.json"
    cached = read_json(path)
    if cached is not None:
        return cached
    scheme = uri_scheme(uri)
    record: dict[str, Any] = {"uri": uri, "scheme": scheme, "status": "failure"}
    try:
        if scheme == "empty":
            record.update(error="empty URI")
        elif scheme == "raw_json":
            record.update(status="success", resolved_uri="inline:raw", content_type="application/json", **_body_fields(uri.encode()))
        elif scheme == "raw_xml_svg":
            record.update(status="success", resolved_uri="inline:raw", content_type="image/svg+xml", **_body_fields(uri.encode()))
        elif scheme == "data":
            raw = _decode_data_uri(uri)
            record.update(status="success", resolved_uri="inline:data", content_type=uri[5:].split(",", 1)[0], **_body_fields(raw))
        elif scheme == "ipfs":
            cid = uri[7:].lstrip("/")
            errors = []
            for template in app.ipfs_gateways:
                url = template.format(cid=cid)
                try:
                    response = _http_get(url, client=client)
                    record.update(
                        status="success",
                        resolved_uri=str(response.url),
                        content_type=response.headers.get("content-type", ""),
                        http_status=response.status_code,
                        **_body_fields(response.content),
                    )
                    break
                except (httpx.HTTPError, UnicodeError) as exc:
                    errors.append(str(exc))
            if record["status"] != "success":
                record["error"] = "; ".join(errors)
        elif scheme in {"http", "https"}:
            response = _http_get(uri, client=client)
            record.update(
                status="success",
                resolved_uri=str(response.url),
                content_type=response.headers.get("content-type", ""),
                http_status=response.status_code,
                **_body_fields(response.content),
            )
        else:
            record["error"] = "unsupported URI scheme"
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError, ValueError, binascii.Error) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    write_json(path, record)
    return record


def fetch_many(app: AppConfig, uris: list[str], workers: int = 512) -> dict[str, dict[str, Any]]:
    def group(uri: str) -> str:
        scheme = uri_scheme(uri)
        return (urlsplit(uri).hostname or scheme) if scheme in {"http", "https"} else scheme

    buckets: dict[str, deque[str]] = defaultdict(deque)
    for uri in sorted(set(uris)):
        buckets[group(uri)].append(uri)
    active = deque(buckets.values())
    unique = []
    while active:
        bucket = active.popleft()
        unique.append(bucket.popleft())
        if bucket:
            active.append(bucket)
    def host_limit(key: str) -> int:
        if key.endswith("amazonaws.com"):
            return 96
        if key == "ipfs" or key in {"ipfs.io", "cloudflare-ipfs.com", "gateway.pinata.cloud"}:
            return 64
        if key.endswith("vercel.app") or key in {"sentinelnet.gudman.xyz", "execution.market"}:
            return 32
        return 16

    semaphores = {
        key: BoundedSemaphore(host_limit(key))
        for key in buckets
        if key not in {"data", "raw_json", "raw_xml_svg", "empty", "unknown"}
    }

    def fetch(uri: str, client: httpx.Client) -> dict[str, Any]:
        semaphore = semaphores.get(group(uri))
        if semaphore is None:
            return fetch_uri(app, uri, client)
        with semaphore:
            return fetch_uri(app, uri, client)

    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    with httpx.Client(limits=limits) as client, ThreadPoolExecutor(max_workers=workers) as executor:
        records = executor.map(lambda uri: fetch(uri, client), unique)
        return dict(zip(unique, records, strict=True))


def parse_json(record: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    if record.get("status") != "success":
        return None
    try:
        value = json.loads(record.get("body", ""))
        return value if isinstance(value, (dict, list)) else None
    except (json.JSONDecodeError, TypeError):
        return None


def classify_registration(uri: str, record: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    if not uri:
        return "no_uri", None
    if record.get("status") != "success":
        return "uri_unresolvable", None
    parsed = parse_json(record)
    if not isinstance(parsed, dict) or parsed.get("type") != REGISTRATION_TYPE:
        return "retrieved_not_compliant", parsed if isinstance(parsed, dict) else None
    services = parsed.get("services", parsed.get("service", []))
    if not isinstance(services, list) or not services:
        return "valid_no_service", parsed
    return "valid_with_service", parsed


def evidence_class(parsed: object) -> str:
    # Xiong et al. §7.4 / Fig. 17 uses the strongest declared evidence class.
    if not isinstance(parsed, dict):
        return "no_evidence"
    payload = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    proof = payload.get("proofOfPayment")
    if isinstance(proof, dict) and (proof.get("x402Nonce") or proof.get("txHash")):
        return "payment_proof"
    a2a = payload.get("a2a") if isinstance(payload.get("a2a"), dict) else {}
    mcp = payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}
    if payload.get("a2aTaskId") or payload.get("mcpTool") or a2a.get("taskId") or mcp.get("tool"):
        return "task_linkage"
    return "no_evidence"


def evidence_fields(parsed: object) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {
            "evidence_class": "no_evidence",
            "x402_nonce": None,
            "payment_tx_hash": None,
            "a2a_task_id": None,
            "mcp_tool": None,
            "declared_timestamp": None,
            "declared_client": None,
            "declared_agent": None,
            "declared_value": None,
        }
    payload = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    proof = payload.get("proofOfPayment")
    a2a = payload.get("a2a") if isinstance(payload.get("a2a"), dict) else {}
    mcp = payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}
    return {
        "evidence_class": evidence_class(parsed),
        "x402_nonce": proof.get("x402Nonce") if isinstance(proof, dict) else None,
        "payment_tx_hash": proof.get("txHash") if isinstance(proof, dict) else None,
        "a2a_task_id": payload.get("a2aTaskId") or a2a.get("taskId"),
        "mcp_tool": payload.get("mcpTool") or mcp.get("tool"),
        "declared_timestamp": payload.get("createdAt", payload.get("timestamp")),
        "declared_client": payload.get("clientAddress", payload.get("client")),
        "declared_agent": payload.get("agentId", payload.get("agent")),
        "declared_value": payload.get("value", payload.get("feedbackValue")),
    }


def content_hash_valid(record: dict[str, Any], expected: object) -> bool | None:
    value = str(expected or "").lower()
    if value in {"", "0x", "0x0", "0x" + "0" * 64} or record.get("status") != "success":
        return None
    raw = base64.b64decode(record["body_b64"]) if record.get("body_b64") else record.get("body", "").encode()
    actual = "0x" + Web3.keccak(raw).hex()
    return actual.lower() == value


def _service_rows(parsed: dict[str, Any] | None, base: dict[str, Any]) -> list[dict[str, Any]]:
    if not parsed:
        return []
    services = parsed.get("services", parsed.get("service", []))
    if not isinstance(services, list):
        return []
    return [
        {
            **base,
            "service_type": str(item.get("type", item.get("name", ""))).lower(),
            "endpoint": item.get("endpoint", ""),
            "version": item.get("version", ""),
        }
        for item in services
        if isinstance(item, dict)
    ]


def ingest_offchain(app: AppConfig, chain: ChainConfig) -> None:
    directory = app.parquet / chain.name
    agents = pl.read_parquet(directory / "agents.parquet").to_dicts()
    registration_cache = fetch_many(
        app,
        [str(agent.get(column) or "") for agent in agents for column in ("current_uri", "current_uri_at_paper_end")],
    )
    registrations: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    cross_chain: list[dict[str, Any]] = []
    for agent in agents:
        snapshots = [("head", agent["current_uri"])]
        if int(agent["mint_block"]) <= chain.paper_end_block:
            snapshots.append(("paper_end", agent["current_uri_at_paper_end"]))
        for snapshot, uri in snapshots:
            record = registration_cache[uri or ""]
            quality, parsed = classify_registration(uri or "", record)
            base = {"chain": chain.name, "chain_id": chain.chain_id, "agent_id": agent["agent_id"], "snapshot": snapshot}
            registrations.append(
                {
                    **base,
                    "uri": uri or "",
                    "uri_scheme": uri_scheme(uri or ""),
                    "fetch_status": record.get("status"),
                    "quality": quality,
                    "name": parsed.get("name", "") if parsed else "",
                    "description": parsed.get("description", "") if parsed else "",
                    "supported_trust": json.dumps(parsed.get("supportedTrust", [])) if parsed else "[]",
                    "x402_support": bool(parsed.get("x402Support", False)) if parsed else False,
                }
            )
            services.extend(_service_rows(parsed, base))
            declared = parsed.get("registrations", []) if parsed else []
            if isinstance(declared, list):
                for item in declared:
                    if isinstance(item, dict):
                        cross_chain.append(
                            {
                                **base,
                                "agent_registry": item.get("agentRegistry", item.get("agent_registry", "")),
                                "declared_agent_id": item.get("agentId", item.get("agentID")),
                            }
                        )

    _write_rows(directory / "registration_files.parquet", registrations)
    _write_rows(directory / "services.parquet", services)
    _write_rows(directory / "declared_registrations.parquet", cross_chain)

    feedback = pl.read_parquet(directory / "feedback.parquet").to_dicts()
    feedback_cache = fetch_many(app, [str(row.get("feedback_uri") or "") for row in feedback])
    feedback_files = []
    for row in feedback:
        record = feedback_cache[row.get("feedback_uri") or ""]
        parsed = parse_json(record)
        feedback_files.append(
            {
                "chain": chain.name,
                "agent_id": row["agent_id"],
                "client_address": row["client_address"],
                "feedback_index": row["feedback_index"],
                "uri": row.get("feedback_uri", ""),
                "fetch_status": record.get("status"),
                "hash_valid": content_hash_valid(record, row.get("feedback_hash")),
                **evidence_fields(parsed),
            }
        )
    _write_rows(directory / "feedback_files.parquet", feedback_files)
    if feedback:
        source = pl.DataFrame(feedback, infer_schema_length=None)
        source = source.drop(
            [column for column in source.columns if column.startswith("evidence_class")]
        )
        enriched = source.join(
            pl.DataFrame(feedback_files, infer_schema_length=None).select(
                "agent_id", "client_address", "feedback_index", "evidence_class"
            ),
            on=["agent_id", "client_address", "feedback_index"],
            how="left",
        )
        enriched.write_parquet(directory / "feedback.parquet")

    responses = pl.read_parquet(directory / "responses.parquet").to_dicts()
    response_cache = fetch_many(app, [str(row.get("response_uri") or "") for row in responses])
    response_files = []
    for row in responses:
        record = response_cache[row.get("response_uri") or ""]
        parsed = parse_json(record)
        response_files.append(
            {
                "chain": chain.name,
                "agent_id": row["agent_id"],
                "client_address": row["client_address"],
                "feedback_index": row["feedback_index"],
                "responder": row["responder"],
                "uri": row.get("response_uri", ""),
                "fetch_status": record.get("status"),
                "hash_valid": content_hash_valid(record, row.get("response_hash")),
                "parsed_json": parsed is not None,
                **evidence_fields(parsed),
            }
        )
    _write_rows(directory / "response_files.parquet", response_files)


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(path)
