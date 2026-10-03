"""Windowed frozen entry point and an offline release health check."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import sys


def release_self_test(report_path: Path) -> int:
    """Exercise the bundled GUI/browser/download with an empty, isolated profile."""
    import hashlib
    import io
    import threading
    import zipfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from client import __version__, app as app_module
    from client.api import SearchOutcome
    from client.settings import Settings
    from client.translate import DeepSeekTranslator
    from client.ui.environment import integrity_level

    config = Path(os.environ['BANANA_INDEX_CONFIG_DIR'])
    assert not (config / 'settings.json').exists()
    assert not (config / 'session-cookies.json').exists()
    assert not os.environ.get('DEEPSEEK_API_KEY')
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w') as archive:
        archive.writestr('fixture.txt', 'offline release check')
        archive.writestr('payload.bin', b'x' * 1_048_576)
    payload = memory.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/fixture.zip':
                body, kind = payload, 'application/zip'
            else:
                body, kind = b'<html><body>offline release check</body></html>', 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert app_module.initialize(), 'Bundled QtWebEngine is unavailable'
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from client.ui.main_window import MainWindow
    from client.ui.web_view import create_web_widget

    app = QApplication([sys.argv[0]])
    app.setApplicationName('banana-index-release-check')
    app.setApplicationVersion(__version__)
    settings = Settings(config_dir=str(config), session_profile_dir=str(config / 'webprofile'),
                        download_dir=str(config / 'downloads'), web_home_url='about:blank',
                        request_delay_ms=0)
    assert not settings.resolved_api_key()
    client = app_module.build_client(settings)
    client.list_games = lambda *a, **kw: []
    client.browse_model = lambda *a, **kw: SearchOutcome(
        submissions=[], total=0, page=1, per_page=24, is_complete=True)
    window = MainWindow(settings, client, DeepSeekTranslator(settings),
                        web_widget_factory=lambda s: create_web_widget(s, client.session_store))
    window.show()
    from client.ui.about_dialog import AboutDialog, SPONSOR_URL, show_first_launch_notice
    from client.settings import load_settings
    QTimer.singleShot(0, lambda: app.activeModalWidget().confirm_button.click())
    show_first_launch_notice(window)
    assert load_settings(config).open_source_notice_acknowledged
    about = AboutDialog(window)
    assert 'Hzans' in about.notice.text() and SPONSOR_URL in about.notice.text()
    assert '没有任何付费购买渠道' in about.notice.text()
    show_first_launch_notice(window)  # Acknowledged: must return without another modal.
    browser = window.web_view
    result = {'version': __version__, 'webengine': True,
              'integrity_level': integrity_level(), 'fresh_profile': True, 'about_notice': True}
    state = {'finished': False, 'started': False}

    def finish(error=None):
        if state['finished']:
            return
        state['finished'] = True
        if error:
            result['error'] = str(error)
        result['ok'] = not error
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()  # Exactly one close operation must exit the real event loop.

    def downloaded(name, filename):
        if name != 'DownloadCompleted':
            return
        try:
            saved = Path(settings.download_dir) / filename
            assert hashlib.sha256(saved.read_bytes()).digest() == hashlib.sha256(payload).digest()
            assert window.download_progress.value() == 100
            window.close_download_notice_button.click()
            assert window.download_progress.isHidden()
            assert window.close_download_notice_button.isHidden()
            assets = Path(app_module.__file__).parent
            for relative in ['webpage/translate.js', 'translate/glossary.json', 'translate/skip_rules.json']:
                assert (assets / relative).is_file(), relative
            assert __version__ in window.windowTitle()
            result.update(native_download=True, download_controls=True, bundled_assets=True,
                          window_version=True, single_close=True)
            QTimer.singleShot(200, finish)
        except Exception as exc:
            finish(exc)

    def loaded(ok):
        if state['started']:
            return
        state['started'] = True
        if not ok:
            finish('Local browser page could not load')
            return
        browser.download_url(f'http://127.0.0.1:{server.server_port}/fixture.zip')

    browser.download_state_changed.connect(downloaded)
    browser.view.loadFinished.connect(loaded)
    browser.load_url(f'http://127.0.0.1:{server.server_port}/')
    QTimer.singleShot(20000, lambda: finish('Release self-test timed out'))
    code = app.exec()
    server.shutdown()
    server.server_close()
    return code if result.get('ok') else 1


def main() -> int:
    multiprocessing.freeze_support()
    # Windowed PyInstaller apps have no attached stdout/stderr.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    if len(sys.argv) == 3 and sys.argv[1] == '--release-access-check':
        # Anonymous read-only diagnostic; never load settings or a browser profile.
        from client.api import GameBananaClient, Submission
        result = {'anonymous': True}
        try:
            client = GameBananaClient(timeout=25, request_delay_ms=0)
            detail = client.partition_detail(Submission(id=723417, model='Mod',
                                                       name='release check', profile_url=''))
            result.update(ok=True, detail_loaded=bool(detail.submission.name), files=len(detail.files))
        except Exception as exc:
            result.update(ok=False, error_type=type(exc).__name__, error=str(exc)[:250])
        Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return 0 if result.get('ok') else 1
    if len(sys.argv) == 3 and sys.argv[1] == '--release-self-test':
        report = Path(sys.argv[2])
        try:
            return release_self_test(report)
        except Exception as exc:
            report.write_text(json.dumps({'ok': False, 'error': str(exc)}), encoding='utf-8')
            return 1
    from client.app import main as run_app
    return run_app()


if __name__ == '__main__':
    raise SystemExit(main())
