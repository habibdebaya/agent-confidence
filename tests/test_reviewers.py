from web3 import Web3

from crawl.reviewers import decode_reviewers


def test_decode_reviewers_collects_successful_multicall_results():
    codec = Web3().codec
    one = codec.encode(["address[]"], [["0x" + "1" * 40, "0x" + "2" * 40]])
    two = codec.encode(["address[]"], [["0x" + "2" * 40, "0x" + "3" * 40]])
    encoded = "0x" + codec.encode(
        ["(bool,bytes)[]"],
        [[(True, one), (False, b""), (True, two)]],
    ).hex()
    assert decode_reviewers(encoded) == {
        "0x" + "1" * 40,
        "0x" + "2" * 40,
        "0x" + "3" * 40,
    }
