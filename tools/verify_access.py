"""Read-only anonymous versus saved-session check. Never prints Cookie or API key values."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.api import GameBananaClient, Submission
from client.common import SessionStore


def main():
    candidates = [ROOT / '.banana-index', Path.home() / '.banana-index']
    if os.environ.get('APPDATA'):
        candidates.append(Path(os.environ['APPDATA']) / 'banana-index')
    cookies = {}
    for directory in candidates:
        cookies = SessionStore(directory / 'session-cookies.json').load()
        if cookies:
            break
    results = []
    for mode in ['anonymous', 'saved-session']:
        client = GameBananaClient(timeout=25, request_delay_ms=0)
        if mode == 'saved-session':
            for name, value in cookies.items():
                client.session.cookies.set(name, value, domain='.gamebanana.com')
        detail = client.partition_detail(Submission(723417, 'Mod', 'access check', ''))
        assert detail.submission.name and detail.files
        results.append({'mode': mode, 'detail_loaded': True, 'files': len(detail.files),
                        'saved_cookies_present': bool(cookies) if mode == 'saved-session' else False})
    print(json.dumps({'public_detail_requires_login': False, 'checks': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
