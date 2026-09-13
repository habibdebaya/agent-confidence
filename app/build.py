from __future__ import annotations

import argparse
import os
import re
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'index.html': 'index.html',
    'technical-report.html': 'technical-report/index.html',
    **{name: 'static/' + name for name in ('site.css', 'site.js', 'report.js',
                                         'data/snapshot.json', 'data/catalogue.json', 'data/evidence.json')},
}


def build(destination: Path, repo_url: str = '') -> Path:
    source = ROOT / 'app/static'
    destination = destination.resolve()
    if destination == source or destination in source.parents:
        raise ValueError('the export directory must not contain application sources')
    if repo_url and not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?', repo_url):
        raise ValueError('repo URL must be an HTTPS GitHub repository URL')
    permitted = set(FILES.values()) | {'.nojekyll'}
    if destination.exists():
        unexpected = [str(path.relative_to(destination)) for path in destination.rglob('*')
                      if path.is_file() and str(path.relative_to(destination)) not in permitted]
        if unexpected:
            raise ValueError('export directory contains unexpected files; choose an empty directory')
    asset_versions = {name: sha256((source / name).read_bytes()).hexdigest()[:12]
                      for name in FILES if name.endswith(('.css', '.js'))}
    for name, target in FILES.items():
        content = (source / name).read_bytes()
        if name.endswith('.html'):
            for asset, version in asset_versions.items():
                content = content.replace(f'static/{asset}"'.encode(), f'static/{asset}?v={version}"'.encode())
            if repo_url:
                content = content.replace(b'data-repo-link hidden', f'data-repo-link href="{repo_url.rstrip("/")}"'.encode())
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    (destination / '.nojekyll').write_text('')
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, default=ROOT / 'dist')
    repository = os.environ.get('GITHUB_REPOSITORY', 'habibdebaya/erc8004-agent-confidence')
    parser.add_argument('--repo-url', default='https://github.com/' + repository if repository else '')
    args = parser.parse_args()
    print(build(args.destination, args.repo_url))


if __name__ == '__main__':
    main()
