import sys

from crawl.cli import _parser


def test_payments_accepts_incremental_block_range(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "erc8004",
            "payments",
            "--chain",
            "base",
            "--start-block",
            "45959523",
            "--end-block",
            "50815929",
        ],
    )
    args = _parser().parse_args()
    assert args.start_block == 45_959_523
    assert args.end_block == 50_815_929
