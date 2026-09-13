from trustlayer.semantics import TagSemantics, _response_text, build_catalog, normalize, rule_classify, validate_proposal


def test_rule_mapping_and_canonical_scale():
    assert normalize(4, TagSemantics("rating_0_5")) == 80
    assert normalize(2, TagSemantics("rating_0_10", "lower_better")) == 80
    assert normalize(1, TagSemantics("boolean")) == 100
    assert normalize(500, TagSemantics("open_metric")) is None


def test_empirical_distribution_rejects_mixed_scale():
    semantic = rule_classify("review", "", [4, 5, 86, 90])
    assert semantic.kind == "unknown"
    assert semantic.mixed is True
    proposal = validate_proposal(TagSemantics("rating_0_5", source="test"), [4, 5, 86, 90])
    assert proposal.kind == "unknown"
    assert proposal.mixed is True


def test_llm_proposals_are_cached(tmp_path):
    calls = []

    def propose(tag1, tag2, stats, model):
        calls.append((tag1, tag2, model))
        return TagSemantics("rating_0_10", source=f"openai:{model}")

    rows = [{"tag1": "novel", "tag2": "metric", "normalized_value": 8.0}]
    path = tmp_path / "tag_map.json"
    build_catalog(rows, path, use_llm=True, proposer=propose)
    build_catalog(rows, path, use_llm=True, proposer=propose)
    assert calls == [("novel", "metric", "gpt-5.4")]


def test_responses_api_wire_format_text_extraction():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"kind":"boolean"}'}],
            }
        ]
    }
    assert _response_text(payload) == '{"kind":"boolean"}'
