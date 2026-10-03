"""Anonymous access, transient replies, explicit login and browser-cookie regression."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import requests

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.pop('DEEPSEEK_API_KEY', None)
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.api import API_BASE, GameBananaClient, Submission
from client.common import ApiError, LoginRequiredError, SiteVerificationRequiredError, DiskCache, SessionStore
from client.ui.web_view import WebViewWidget
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWidgets import QMessageBox
from test_direct_download import DirectDownload


def response(body, status=200, url=API_BASE + '/Mod/723417/ProfilePage', headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode('utf-8')
    r.url = url
    r.headers.update(headers or {})
    return r


class ApiAccess(unittest.TestCase):
    def setUp(self):
        self.client = GameBananaClient(request_delay_ms=0)
        self.sleep = patch('client.common.time.sleep')
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_public_detail_does_not_require_cookies(self):
        with patch.object(self.client.session, 'get', return_value=response('{"_sName":"public","_aFiles":[]}')):
            detail = self.client.partition_detail(Submission(723417, 'Mod', 'old', ''))
        self.assertEqual(detail.submission.name, 'public')
        self.assertFalse(self.client.session.cookies)

    def test_empty_then_valid_response_retries_with_fresh_url(self):
        with patch.object(self.client.session, 'get', side_effect=[response(''), response('{"_sName":"recovered"}')]) as get:
            self.assertEqual(self.client._get_json('Mod/723417/ProfilePage')['_sName'], 'recovered')
        self.assertIn('_banana_retry=', get.call_args_list[1].args[0])
        self.assertEqual(get.call_count, 2)

    def test_incomplete_json_is_retried(self):
        with patch.object(self.client.session, 'get', side_effect=[response('{'), response('{}')]) as get:
            self.assertEqual(self.client._get_json('Mod/723417/ProfilePage'), {})
        self.assertEqual(get.call_count, 2)

    def test_ordinary_html_with_login_link_is_not_login_requirement(self):
        with patch.object(self.client.session, 'get', return_value=response('<html><a href="/members/account/login">Login</a></html>')) as get:
            with self.assertRaises(ApiError) as caught:
                self.client._get_json('Mod/723417/ProfilePage')
        self.assertNotIsInstance(caught.exception, LoginRequiredError)
        self.assertEqual(get.call_count, 3)
        self.assertNotIn('路由不可用', str(caught.exception))

    def test_only_explicit_authentication_triggers_login(self):
        for r in [response('', 401), response('{"_sErrorCode":"LOGIN_REQUIRED"}', 403),
                  response('<html>login</html>', url='https://gamebanana.com/members/account/login')]:
            with self.subTest(status=r.status_code, url=r.url):
                with patch.object(self.client.session, 'get', return_value=r) as get:
                    with self.assertRaises(LoginRequiredError):
                        self.client._get_json('Mod/723417/ProfilePage')
                    self.assertEqual(get.call_count, 1)

    def test_permission_denied_or_deleted_is_not_login(self):
        for status in [403, 404]:
            with patch.object(self.client.session, 'get', return_value=response('denied', status)):
                with self.assertRaises(ApiError) as caught:
                    self.client._get_json('Mod/723417/ProfilePage')
                self.assertNotIsInstance(caught.exception, LoginRequiredError)

    def test_challenge_is_distinct_from_login(self):
        for status in [401, 403]:
            with patch.object(self.client.session, 'get', return_value=response('<html><title>Just a moment</title></html>', status)):
                with self.assertRaises(SiteVerificationRequiredError):
                    self.client._get_json('Mod/723417/ProfilePage')

    def test_foreign_login_redirect_is_not_gamebanana_login(self):
        with patch.object(self.client.session, 'get', return_value=response('<html>login</html>', url='https://portal.example/login')):
            with self.assertRaises(ApiError) as caught:
                self.client._get_json('Mod/723417/ProfilePage')
            self.assertNotIsInstance(caught.exception, LoginRequiredError)

    def test_browser_session_updates_next_request_and_cache_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(pathlib.Path(tmp) / 'session.json')
            client = GameBananaClient(request_delay_ms=0, session_store=store,
                                      cache=DiskCache(pathlib.Path(tmp) / 'cache.json'))
            with patch.object(client.session, 'get', side_effect=[response('{"name":"public"}'), response('{"name":"member"}')]) as get:
                self.assertEqual(client._get_json('fixture')['name'], 'public')
                store.save({'session': 'fake-login-cookie'})
                self.assertEqual(client._get_json('fixture')['name'], 'member')
                self.assertEqual(client.session.cookies.get('session'), 'fake-login-cookie')
                store.save({})
                self.assertEqual(client._get_json('fixture')['name'], 'public')
                self.assertFalse(client.session.cookies.get('session'))
                self.assertEqual(get.call_count, 2)

    def test_cookie_bridge_only_persists_gamebanana_and_handles_logout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(pathlib.Path(tmp) / 'session.json')
            class Bridge:
                session_store = store
            bridge = Bridge()
            cookie = QNetworkCookie(b'session', b'fake-login-cookie')
            for domain in ['evilgamebanana.com', 'gamebanana.com.evil.test', 'other.test']:
                cookie.setDomain(domain)
                WebViewWidget._persist_site_cookie(bridge, cookie)
                self.assertFalse(store.load())
            cookie.setDomain('.gamebanana.com')
            WebViewWidget._persist_site_cookie(bridge, cookie)
            self.assertEqual(store.load(), {'session': 'fake-login-cookie'})
            WebViewWidget._persist_site_cookie(bridge, cookie, removed=True)
            self.assertFalse(store.load())

    def test_browser_login_replaces_same_name_anonymous_api_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(pathlib.Path(tmp) / 'session.json')
            client = GameBananaClient(session_store=store)
            client.session.cookies.set('session', 'fake-anonymous-cookie', domain='gamebanana.com', path='/')
            store.save({'session': 'fake-browser-login-cookie'})
            client.apply_session_cookies()
            matching = [c for c in client.session.cookies if c.name == 'session']
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0].value, 'fake-browser-login-cookie')
            store.save({})
            original_clear = client.session.cookies.clear
            def concurrently_expired(domain, path, name):
                original_clear(domain, path, name)
                raise KeyError(name)
            with patch.object(client.session.cookies, 'clear', side_effect=concurrently_expired):
                client.apply_session_cookies()
            self.assertFalse(client.session.cookies.get('session'))


class AccessDialog(DirectDownload):
    def test_detail_failure_only_prompts_for_confirmed_access_requirement(self):
        item = Submission(723417, 'Mod', 'fixture', 'https://gamebanana.com/mods/723417')
        for message, expected in [('ApiError: empty response', None),
                                  ('LoginRequiredError: login', True),
                                  ('SiteVerificationRequiredError: challenge', False)]:
            with patch.object(self.window.tasks, 'run', side_effect=lambda work, done, failed: failed(message)):
                with patch.object(self.window, '_prompt_site_access') as prompt:
                    self.window.show_detail(item)
                    if expected is None:
                        prompt.assert_not_called()
                    else:
                        prompt.assert_called_once_with(item, login=expected)

    def test_dismissing_login_dialog_does_not_navigate(self):
        with patch.object(QMessageBox, 'exec', return_value=0):
            self.window._prompt_site_access(Submission(723417, 'Mod', 'fixture', ''), login=True)
        self.assertFalse(self.browser.pages)
        self.assertFalse(self.window._site_access_prompt_open)

    def test_login_dialog_opens_embedded_login_page(self):
        item = Submission(723417, 'Mod', 'fixture', 'https://gamebanana.com/mods/723417')
        def click(dialog):
            button = next(b for b in dialog.buttons() if b.text() == '网页登录')
            button.click()
            return 0
        with patch.object(QMessageBox, 'exec', click):
            self.window._prompt_site_access(item, login=True)
        self.assertEqual(self.browser.pages, ['https://gamebanana.com/members/account/login'])
        self.assertIs(self.window.right_tabs.currentWidget(), self.browser)

    def test_verification_dialog_is_not_labeled_login(self):
        item = Submission(723417, 'Mod', 'fixture', 'https://gamebanana.com/mods/723417')
        def click(dialog):
            self.assertIn('网页验证', dialog.windowTitle())
            self.assertFalse(any(b.text() == '网页登录' for b in dialog.buttons()))
            next(b for b in dialog.buttons() if b.text() == '打开验证页').click()
            return 0
        with patch.object(QMessageBox, 'exec', click):
            self.window._prompt_site_access(item, login=False)
        self.assertEqual(self.browser.pages, [item.profile_url])


if __name__ == '__main__':
    unittest.main()
