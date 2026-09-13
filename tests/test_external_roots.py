from crawl.provenance import external_roots


def test_external_root_labels(tmp_path):
    path = tmp_path / "roots.csv"
    path.write_text("address,label,type\n0xABC,Example Exchange,cex\n")
    assert external_roots(path) == {"0xabc": "Example Exchange"}
