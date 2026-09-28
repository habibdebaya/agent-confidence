import asyncio
import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

import app.build as static_build
from app.build import build, published
from app.main import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_static_export_is_allowlisted_and_supports_repo_links(tmp_path):
    destination = build(tmp_path / 'site', 'https://github.com/example/research')
    actual = {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()}
    assert actual == set(published().values()) | {'.nojekyll'}
    assert any(name.startswith('static/data/feedback/') for name in actual)
    page = (destination / 'index.html').read_text()
    assert 'href="https://github.com/example/research"' in page
    assert 'href="https://github.com/example/research/issues/new"' in page
    (destination / 'unexpected.txt').write_text('must not publish')
    with pytest.raises(ValueError):
        build(destination)


@pytest.mark.parametrize('asset', ['site.css', 'site.js'])
def test_static_asset_urls_change_with_content(tmp_path, monkeypatch, asset):
    root = tmp_path / 'project'
    source = root / 'app/static'
    shutil.copytree(ROOT / 'app/static', source)
    monkeypatch.setattr(static_build, 'ROOT', root)
    destination = tmp_path / 'site'

    def references():
        build(destination)
        return {page: re.findall(r'(?:href|src)="([^" ]+\.(?:css|js)\?v=[a-f0-9]{12})"',
                                 (destination / page).read_text())
                for page in ('index.html',)}

    before = references()
    assert all(len(urls) == 2 for urls in before.values())
    assert references() == before
    with (source / asset).open('a') as handle:
        handle.write('\n/* updated asset */\n')
    after = references()
    changed = 0
    for page in before:
        for old_url, new_url in zip(before[page], after[page], strict=True):
            path = new_url.split('?')[0]
            published = (destination / page).parent / path
            assert published.read_bytes() == (source / published.name).read_bytes()
            if path.endswith('/' + asset):
                assert new_url != old_url
                changed += 1
            else:
                assert new_url == old_url
    assert changed == 1


def test_public_app_starts_without_crawl_or_explorer(tmp_path):
    static = tmp_path / 'app/static'
    static.mkdir(parents=True)
    (static / 'index.html').write_text('public demo')

    async def run():
        app = create_app(tmp_path)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                assert (await client.get('/')).text == 'public demo'
                assert (await client.get('/api/agent/1')).status_code == 404
                assert (await client.get('/static/%2e%2e/%2e%2e/config.yaml')).status_code == 404

    asyncio.run(run())
