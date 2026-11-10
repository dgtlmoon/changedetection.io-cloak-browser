"""Unit tests for the CloakBrowser plugin. They need changedetection.io installed, but neither the
cloakbrowser package nor a browser: its launch is replaced with a recorder.

    pytest tests/
"""
import asyncio
import sys
import types

import pytest

from changedetection_cloak_browser import fetcher as plugin


class _LaunchedBrowser:
    async def close(self):
        pass


# What the stand-in launch hands back
A_BROWSER = _LaunchedBrowser()


@pytest.fixture
def launches(monkeypatch):
    """Stand in for cloakbrowser.launch_async and record what it is asked for."""
    calls = []

    async def launch_async(**kwargs):
        calls.append(kwargs)
        return A_BROWSER

    fake = types.ModuleType('cloakbrowser')
    fake.launch_async = launch_async
    monkeypatch.setitem(sys.modules, 'cloakbrowser', fake)
    return calls


def _browser_config(**plugin_settings):
    from changedetectionio.model.browser_config import FetcherConfig
    return FetcherConfig(plugin_settings={plugin.PLUGIN_ID: plugin_settings} if plugin_settings else {})


def test_registers_a_playwright_based_fetcher():
    from changedetectionio.content_fetchers.base import FetcherCapabilities
    from changedetectionio.content_fetchers.playwright import fetcher as playwright_fetcher

    name, cls = plugin.register_content_fetcher()
    assert name == 'html_cloakbrowser'
    # The whole point: core's fetcher does the fetching. run() here only wraps it (see the
    # cleanup tests below), nothing of the fetch itself is reimplemented
    assert issubclass(cls, playwright_fetcher)
    for inherited in ('screenshot_step', 'save_step_html', 'iterate_browser_steps'):
        assert getattr(cls, inherited) is getattr(playwright_fetcher, inherited), inherited
    assert cls is plugin.register_content_fetcher()[1], "one class, however often it is asked for"

    caps = FetcherCapabilities.from_fetcher(cls)
    for flag in ('supports_browser_steps', 'supports_screenshots', 'supports_xpath_element_data',
                 'supports_page_settings', 'supports_device_emulation', 'supports_request_blocking', 'supports_cookies'):
        assert getattr(caps, flag), flag
    # One patched Chromium that launches locally: no engine to pick, no endpoint to enter
    assert not caps.supports_browser_type
    assert not caps.supports_connection_url
    assert cls.ready_to_use and cls.local_launch
    assert cls.get_status_icon_data()['filename'] == 'cloakbrowser-logo.svg'
    assert cls.get_status_icon_data(browser_config=_browser_config())['group'] == 'plugin'


@pytest.mark.parametrize('proxy, expected', [
    (None, None),
    ({}, None),
    ({'server': 'http://proxy.example:3128'}, 'http://proxy.example:3128'),
    ({'server': 'http://proxy.example:3128', 'username': 'user', 'password': 'secret'}, 'http://user:secret@proxy.example:3128'),
    # Credentials already in the address are replaced by the separate ones, not doubled
    ({'server': 'socks5://old:creds@proxy.example:1080', 'username': 'user', 'password': 'secret'}, 'socks5://user:secret@proxy.example:1080'),
    ({'server': 'http://proxy.example', 'username': 'user'}, 'http://user:@proxy.example'),
])
def test_proxy_url_for_cloakbrowser(proxy, expected):
    assert plugin.proxy_url_for_cloakbrowser(proxy) == expected


def test_redact_credentials():
    assert plugin.redact_credentials('chrome --proxy-server=http://user:hunter2@proxy.example:3128 --headless') == \
        'chrome --proxy-server=http://***@proxy.example:3128 --headless'
    assert plugin.redact_credentials('socks5://u:p@a.example and https://x:y@b.example/path') == \
        'socks5://***@a.example and https://***@b.example/path'
    assert plugin.redact_credentials('http://proxy.example:3128 has none') == 'http://proxy.example:3128 has none'


def test_humanize_is_per_browser_with_the_env_var_as_default(monkeypatch):
    monkeypatch.delenv('CLOAKBROWSER_HUMANIZE', raising=False)
    assert plugin.humanize_for(None) is True
    assert plugin.humanize_for(_browser_config()) is True
    assert plugin.humanize_for(_browser_config(humanize='off')) is False
    assert plugin.humanize_for(_browser_config(humanize='')) is True, "'' follows the default"

    monkeypatch.setenv('CLOAKBROWSER_HUMANIZE', 'false')
    assert plugin.humanize_for(None) is False
    assert plugin.humanize_for(_browser_config(humanize='on')) is True, "the browser's own choice wins"
    assert plugin.humanize_for(_browser_config(humanize='off')) is False


def test_a_check_launches_cloakbrowser_with_the_browsers_settings(launches, monkeypatch):
    monkeypatch.delenv('CLOAKBROWSER_HUMANIZE', raising=False)
    _name, cls = plugin.register_content_fetcher()
    instance = cls(proxy_override='http://user:secret@proxy.example:3128')
    instance.browser_config = _browser_config(humanize='off')

    assert asyncio.run(instance._get_browser(browser_type=object())) is A_BROWSER
    assert instance._cloak_browser is A_BROWSER, "kept, so quit() can close it"
    assert launches == [{'headless': True, 'proxy': 'http://user:secret@proxy.example:3128', 'humanize': False}]
    assert instance._resolve_browser_type_name() == 'chromium'


class _Browser:
    """A stand-in for the browser CloakBrowser launches: counts real closes."""
    def __init__(self, close_error=None, close_hangs=False):
        self.closes = 0
        self._close_error = close_error
        self._close_hangs = close_hangs

    async def close(self):
        if self._close_hangs:
            await asyncio.sleep(60)
        if self._close_error:
            raise self._close_error
        self.closes += 1


def _fetcher_that_launches(browser, monkeypatch, core_run):
    """A fetcher whose launch gives `browser` and whose core run() is `core_run(self, browser)`."""
    from changedetectionio.content_fetchers.playwright import fetcher as playwright_fetcher

    async def launch_async(**kwargs):
        return browser

    fake = types.ModuleType('cloakbrowser')
    fake.launch_async = launch_async
    monkeypatch.setitem(sys.modules, 'cloakbrowser', fake)

    async def run(self, *args, **kwargs):
        launched = await self._get_browser(browser_type=None)
        return await core_run(self, launched)

    monkeypatch.setattr(playwright_fetcher, 'run', run)
    _name, cls = plugin.register_content_fetcher()
    return cls()


def test_browser_is_closed_when_the_core_run_fails_before_its_own_cleanup(monkeypatch):
    """The case that left browsers running: the core run() raises (a 403, a failed navigation)
    on a path where it doesn't close the browser itself."""
    browser = _Browser()

    async def core_run(self, launched):
        raise RuntimeError("403 on a path with no cleanup")

    instance = _fetcher_that_launches(browser, monkeypatch, core_run)
    with pytest.raises(RuntimeError):
        asyncio.run(instance.run(url='https://example.org', watch_uuid='abc'))
    assert browser.closes == 1
    assert instance._cloak_browser is None

    # The worker's safety-net quit() afterwards closes nothing twice
    asyncio.run(instance.quit(watch={'uuid': 'abc'}))
    assert browser.closes == 1


def test_browser_already_closed_by_the_core_run_is_not_closed_twice(monkeypatch):
    browser = _Browser()

    async def core_run(self, launched):
        await launched.close()   # the core run()'s own cleanup, on its normal way out

    instance = _fetcher_that_launches(browser, monkeypatch, core_run)
    asyncio.run(instance.run(url='https://example.org', watch_uuid='abc'))
    assert browser.closes == 1


def test_a_fetch_that_never_finishes_is_stopped_and_its_browser_closed(monkeypatch):
    from changedetectionio.content_fetchers.exceptions import BrowserFetchTimedOut
    monkeypatch.setenv('CLOAKBROWSER_MAX_PROCESSING_TIMEOUT_SECONDS', '1')
    browser = _Browser()

    async def core_run(self, launched):
        await asyncio.sleep(60)

    instance = _fetcher_that_launches(browser, monkeypatch, core_run)
    with pytest.raises(BrowserFetchTimedOut):
        asyncio.run(instance.run(url='https://example.org', watch_uuid='abc'))
    assert browser.closes == 1


def test_a_browser_that_will_not_close_does_not_break_the_check(monkeypatch):
    """A failing or hanging close is logged, never raised over the fetch's own result."""
    monkeypatch.setattr(plugin, 'CLOSE_TIMEOUT_SECONDS', 0.2)

    async def core_run(self, launched):
        return None

    for browser in (_Browser(close_error=RuntimeError("boom")), _Browser(close_hangs=True)):
        instance = _fetcher_that_launches(browser, monkeypatch, core_run)
        asyncio.run(instance.run(url='https://example.org', watch_uuid='abc'))
        assert instance._cloak_browser is None


def test_launch_failures_become_a_clean_error_without_the_command_line(monkeypatch):
    """A licence problem keeps CloakBrowser's own explanation. Anything else must not carry the
    launcher's text into the watch's error: it holds the proxy address and password."""
    from changedetectionio.content_fetchers.exceptions import BrowserConnectError

    class CloakBrowserLicenseError(Exception):
        pass

    secret = 'http://user:hunter2@proxy.example:3128'
    for raised, expected, forbidden in (
        (CloakBrowserLicenseError("session limit reached for your plan."), "CloakBrowser: session limit reached for your plan.", None),
        (RuntimeError(f"launch failed: chrome --proxy-server={secret}"), "CloakBrowser could not be started (RuntimeError)", secret),
    ):
        async def launch_async(_raised=raised, **kwargs):
            raise _raised

        fake = types.ModuleType('cloakbrowser')
        fake.launch_async = launch_async
        monkeypatch.setitem(sys.modules, 'cloakbrowser', fake)
        _name, cls = plugin.register_content_fetcher()

        for launch in (cls()._get_browser(browser_type=None), cls.get_browsersteps_browser(proxy=None, keepalive_ms=1000)):
            with pytest.raises(BrowserConnectError) as caught:
                asyncio.run(launch)
            assert expected in caught.value.msg
            if forbidden:
                assert forbidden not in caught.value.msg and 'hunter2' not in caught.value.msg
            # Nothing of the original error travels with it either
            assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_live_session_launches_the_same_browser(launches, monkeypatch):
    monkeypatch.delenv('CLOAKBROWSER_HUMANIZE', raising=False)
    _name, cls = plugin.register_content_fetcher()
    result = asyncio.run(cls.get_browsersteps_browser(proxy={'server': 'http://proxy.example:3128'}, keepalive_ms=5000,
                                                      browser_config=_browser_config(humanize='off')))
    # (browser, no Playwright context of ours to stop)
    assert result == (A_BROWSER, None)
    assert launches == [{'headless': True, 'proxy': 'http://proxy.example:3128', 'humanize': False}]


def test_settings_section_only_on_its_own_browsers():
    assert plugin.browser_config_form_section(base_fetcher='html_webdriver', capabilities={}) is None
    section = plugin.browser_config_form_section(base_fetcher='html_cloakbrowser', capabilities={})
    assert section.plugin_id == plugin.PLUGIN_ID
    form = section.form_class()
    assert [value for value, _label in form.humanize.choices] == ['', 'on', 'off']
    assert form.humanize.data == ''
