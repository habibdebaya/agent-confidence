from crawl.provenance import (
    _normalize_indexed_transfer,
    classify_code,
    funding_groups,
    funding_roots,
    reviewer_flag_rows,
)


def test_code_classification():
    assert classify_code("0x") == "eoa"
    assert classify_code("0xef0100" + "1" * 40) == "delegated_eoa"
    assert classify_code("0x60016000") == "contract"


def test_funding_roots_and_cycle_are_deterministic():
    edges = [
        {"funder": "a", "address": "b"},
        {"funder": "b", "address": "c"},
        {"funder": "y", "address": "x"},
        {"funder": "x", "address": "y"},
    ]
    roots = funding_roots(edges)
    assert roots["b"] == "a"
    assert roots["c"] == "a"
    assert roots["x"] == "x"
    assert roots["y"] == "x"


def test_contract_operator_can_replace_contract_funder():
    edge = {
        "funder": "contract",
        "address": "reviewer",
        "funder_type": "contract",
        "operator": "operator",
    }
    effective = {**edge, "funder": edge["operator"]}
    assert funding_roots([effective])["reviewer"] == "operator"


def test_paper_flag_excludes_contract_funders():
    edges = [
        {"funder": "root", "address": "a", "funder_type": "eoa", "external_root": False},
        {"funder": "root", "address": "b", "funder_type": "eoa", "external_root": False},
        {"funder": "contract", "address": "c", "funder_type": "contract", "external_root": False},
        {"funder": "contract", "address": "d", "funder_type": "contract", "external_root": False},
        {"funder": "exchange", "address": "e", "funder_type": "eoa", "external_root": True},
        {"funder": "exchange", "address": "f", "funder_type": "eoa", "external_root": True},
    ]
    rows = reviewer_flag_rows(edges, {"base": {"a", "b", "c", "d", "e", "f"}})["base"]
    flags = {row["reviewer"]: row["sybil_flag"] for row in rows}
    assert flags == {"a": True, "b": True, "c": False, "d": False, "e": True, "f": True}


def test_funding_groups_merge_cross_chain_paths():
    edges = [
        {"funder": "root", "address": "bridge", "funder_type": "eoa", "external_root": False},
        {"funder": "bridge", "address": "reviewer-a", "funder_type": "eoa", "external_root": False},
        {"funder": "bridge", "address": "reviewer-b", "funder_type": "delegated_eoa", "external_root": False},
    ]
    groups = funding_groups(edges)
    assert groups["reviewer-a"] == "root"
    assert groups["reviewer-b"] == "root"


def test_cross_chain_flags_match_chain_local_roots():
    edges = [
        {"chain": "eth", "funder": "root", "address": "a", "funder_type": "eoa"},
        {"chain": "eth", "funder": "root", "address": "b", "funder_type": "eoa"},
        {"chain": "base", "funder": "root", "address": "c", "funder_type": "eoa"},
    ]
    rows = reviewer_flag_rows(edges, {"eth": {"a", "b"}, "base": {"c"}})
    eth = {row["reviewer"]: row for row in rows["eth"]}
    base = rows["base"][0]
    assert eth["a"]["same_chain_flag"] is True
    assert eth["a"]["cross_chain_flag"] is True
    assert base["same_chain_flag"] is False
    assert base["cross_chain_flag"] is True
    assert base["sybil_flag"] is True


def test_indexed_native_transfer_normalization():
    row = _normalize_indexed_transfer(
        {
            "blockNum": "0x10",
            "uniqueId": "0xhash:internal_0_1",
            "hash": "0xHASH",
            "from": "0xFUNDER",
            "to": "0xTARGET",
            "category": "internal",
            "rawContract": {"value": "0x2a"},
        }
    )
    assert row["blockNumber"] == 16
    assert row["value"] == 42
    assert row["source"] == "txlistinternal"
