"""CloakBrowser stealth fetcher plugin for changedetection.io.

CloakBrowser hands back ordinary Playwright browser objects, so this plugin does not fetch
anything itself: it subclasses changedetection.io's own Playwright fetcher and only answers "where
does the browser come from" (`_get_browser`) - and makes sure that browser is always closed again,
which matters more here than for the other browsers: it is a local process this plugin started,
and a CloakBrowser plan allows a limited number of them at once. Everything else - navigation, browser steps,
screenshots, the visual selector data, the memory handling around all of it, and every browser
setting changedetection.io offers (screen size, device emulation, language, timezone, blocked
assets, cookies from other plugins) - is the core fetcher's code, unchanged and kept up to date
with it.

Hooks used:
  register_content_fetcher      the fetcher itself, 'html_cloakbrowser'
  browser_config_form_section   a "CloakBrowser" section on the browser's edit page, so
                                human-like behaviour is chosen per browser instead of only by
                                the CLOAKBROWSER_HUMANIZE environment variable
  plugin_static_path            the icon
"""
import asyncio
import os
import re
from urllib.parse import urlparse

from loguru import logger

from changedetectionio.plugin_api import BrowserConfigSection, hookimpl

FETCHER_NAME = 'html_cloakbrowser'
# Key of this plugin's settings on a browser (browser_config.plugin_settings[PLUGIN_ID])
PLUGIN_ID = 'cloak_browser'

_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')

# Closing also stops CloakBrowser's Playwright instance and gives its licence session back
CLOSE_TIMEOUT_SECONDS = 10

# The fetcher class, built on first use: it subclasses the core Playwright fetcher, and
# changedetectionio.content_fetchers can't be imported while this module is being loaded (it is
# loaded BY the plugin manager that content_fetchers imports).
_fetcher_class = None


def humanize_default():
    """Human-like mouse/keyboard/scroll behaviour unless CLOAKBROWSER_HUMANIZE says otherwise."""
    return os.getenv('CLOAKBROWSER_HUMANIZE', 'true').lower() not in ('false', '0', 'no')


def humanize_for(browser_config):
    """Whether a browser runs with human-like behaviour: its own setting from the browser's edit
    page ('on' / 'off'), else the CLOAKBROWSER_HUMANIZE default."""
    settings = browser_config.plugin_config(PLUGIN_ID) if hasattr(browser_config, 'plugin_config') else {}
    choice = (settings or {}).get('humanize')
    if choice == 'on':
        return True
    if choice == 'off':
        return False
    return humanize_default()


def proxy_url_for_cloakbrowser(proxy):
    """A Playwright-style proxy dict ({'server', 'username', 'password'}) as the single URL
    CloakBrowser's launch takes, or None."""
    if not proxy:
        return None
    server = proxy.get('server', '')
    username = proxy.get('username')
    if username and server:
        parsed = urlparse(server)
        port = f":{parsed.port}" if parsed.port else ''
        return f"{parsed.scheme}://{username}:{proxy.get('password') or ''}@{parsed.hostname}{port}"
    return server or None


async def launch_cloakbrowser(proxy=None, browser_config=None):
    """Launch a local CloakBrowser. The result is a Playwright Browser."""
    from cloakbrowser import launch_async
    return await launch_async(
        headless=True,
        proxy=proxy_url_for_cloakbrowser(proxy),
        humanize=humanize_for(browser_config),
    )


def redact_credentials(text):
    """`scheme://user:password@host` -> `scheme://***@host`, anywhere in the text."""
    return re.sub(r'://[^@\s/]+@', '://***@', str(text))


def launch_error_message(error):
    """What to show as the watch's error when CloakBrowser could not be launched.

    A licence problem (too many sessions at once, an expired key) is CloakBrowser's own wording,
    which says what to do. Anything else gets a plain message: the launcher's errors carry the
    whole command line, proxy address and password included, and that must never reach the
    watch's error or the log.
    """
    if type(error).__name__ == 'CloakBrowserLicenseError':
        return f"CloakBrowser: {error}"
    # For whoever runs the server: the first line of the reason, with any credentials taken out
    first_line = (str(error).strip().splitlines() or [''])[0]
    logger.debug(f"CloakBrowser launch failed: {type(error).__name__}: {redact_credentials(first_line)}")
    return f"CloakBrowser could not be started ({type(error).__name__}). Is it installed? Try: python -m cloakbrowser info"


def close_only_once(browser):
    """Make browser.close() safe to call again once it has worked. CloakBrowser's close also
    stops its Playwright instance, and both the core fetcher and this plugin's own cleanup call it."""
    original_close = browser.close
    state = {'closed': False}

    async def close(*args, **kwargs):
        if state['closed']:
            return
        await original_close(*args, **kwargs)
        state['closed'] = True

    browser.close = close
    return browser


def get_fetcher_class():
    global _fetcher_class
    if _fetcher_class is not None:
        return _fetcher_class

    from changedetectionio.content_fetchers.exceptions import BrowserConnectError, BrowserFetchTimedOut
    from changedetectionio.content_fetchers.playwright import fetcher as playwright_fetcher

    class fetcher(playwright_fetcher):
        fetcher_description = "CloakBrowser - Stealth Chromium"
        fetcher_short_description = "CloakBrowser"

        # Launches its own local browser: nothing to connect to, and usable as it is
        local_launch = True
        ready_to_use = True
        # One patched Chromium, there is no browser engine to choose
        supports_browser_type = False

        # The browser this fetch launched, until quit() has closed it
        _cloak_browser = None

        def __init__(self, proxy_override=None, custom_browser_connection_url=None, **kwargs):
            if custom_browser_connection_url:
                logger.warning("CloakBrowser fetcher: custom_browser_connection_url is ignored - "
                               "CloakBrowser always launches a local browser")
            # The parent reads the proxy (the watch's, else the playwright_proxy_* env vars) into self.proxy
            super().__init__(proxy_override=proxy_override, custom_browser_connection_url=None, **kwargs)

        async def _get_browser(self, browser_type):
            """The one thing that differs from the core Playwright fetcher: launch CloakBrowser
            instead of connecting to a remote Chrome. `browser_type` (stock Playwright's) isn't used."""
            try:
                browser = await launch_cloakbrowser(proxy=self.proxy, browser_config=getattr(self, 'browser_config', None))
            except Exception as e:
                # `from None`: the original error's text and traceback hold the launch command line
                raise BrowserConnectError(msg=launch_error_message(e)) from None
            self._cloak_browser = close_only_once(browser)
            return browser

        async def run(self, *args, **kwargs):
            """The core Playwright fetcher's run(), unchanged, inside the same guard the Puppeteer
            fetcher has: a time limit on the whole fetch, and quit() whatever happens. The core
            run() doesn't close the browser on every way out (a 403, a failed navigation...) - with
            a remote browser the dropped connection does that, but this one is a local process
            that would be left running, holding its licence session."""
            watch_uuid = kwargs.get('watch_uuid')
            max_time = int(os.getenv('CLOAKBROWSER_MAX_PROCESSING_TIMEOUT_SECONDS', 180))
            try:
                await asyncio.wait_for(super().run(*args, **kwargs), timeout=max_time)
            except asyncio.TimeoutError:
                raise BrowserFetchTimedOut(msg=f"CloakBrowser was unable to process the page in {max_time} seconds.")
            finally:
                # The worker's own quit() afterwards is then a safety net, it does nothing twice
                try:
                    await self.quit(watch={'uuid': watch_uuid} if watch_uuid else None)
                except Exception as cleanup_error:
                    logger.error(f"[{watch_uuid}] CloakBrowser > Error during cleanup: {cleanup_error}")

        async def quit(self, watch=None):
            """Close the browser this fetch launched, if it is still open. Safe to call again."""
            watch_uuid = watch.get('uuid') if watch else 'unknown'
            browser = self._cloak_browser
            # Closing the browser takes its pages with it
            self.page = None
            try:
                if browser:
                    await asyncio.wait_for(browser.close(), timeout=CLOSE_TIMEOUT_SECONDS)
                    logger.debug(f"[{watch_uuid}] CloakBrowser > Browser closed")
            except asyncio.TimeoutError:
                logger.warning(f"[{watch_uuid}] CloakBrowser > Timed out closing the browser ({CLOSE_TIMEOUT_SECONDS}s), "
                               f"its licence session may stay taken for a few minutes")
            except Exception as e:
                logger.warning(f"[{watch_uuid}] CloakBrowser > Error closing the browser: {type(e).__name__}")
            finally:
                self._cloak_browser = None

        def _resolve_browser_type_name(self):
            # Always Chromium, whatever PLAYWRIGHT_BROWSER_TYPE says for the other browsers
            return 'chromium'

        @classmethod
        async def get_browsersteps_browser(cls, proxy=None, keepalive_ms=None, browser_config=None):
            """The browser for a live Browser Steps / Visual Selector session: the same local
            CloakBrowser a check uses, with the same per-browser settings. Returns (browser, None),
            there is no Playwright context to hand back, CloakBrowser manages its own. The session
            closes it when it ends."""
            try:
                return await launch_cloakbrowser(proxy=proxy, browser_config=browser_config), None
            except Exception as e:
                raise BrowserConnectError(msg=launch_error_message(e)) from None

        @classmethod
        def get_status_icon_data(cls, browser_config=None):
            return {
                'group': 'plugin',
                'filename': 'cloakbrowser-logo.svg',
                'alt': 'Using CloakBrowser (stealth)',
                'title': 'CloakBrowser - Stealth Chromium',
            }

        def is_ready(self):
            try:
                import cloakbrowser  # noqa: F401
                return True
            except ImportError:
                logger.error("CloakBrowser fetcher: 'cloakbrowser' package is not installed")
                return False

    _fetcher_class = fetcher
    return _fetcher_class


def _settings_form_class():
    from wtforms import Form, RadioField, validators

    class CloakBrowserSettingsForm(Form):
        humanize = RadioField(
            'Human-like behaviour',
            choices=[
                ('', f"Default ({'on' if humanize_default() else 'off'}, from CLOAKBROWSER_HUMANIZE)"),
                ('on', 'On'),
                ('off', 'Off'),
            ],
            default='',
            validators=[validators.Optional()],
            description='Human-like mouse, keyboard and scroll timing. Harder to detect, a little slower.',
        )

    return CloakBrowserSettingsForm


@hookimpl
def register_content_fetcher():
    """Register the CloakBrowser fetcher with changedetection.io."""
    return FETCHER_NAME, get_fetcher_class()


@hookimpl
def browser_config_form_section(base_fetcher, capabilities):
    """CloakBrowser's own settings, on the edit page of a browser built on it."""
    if base_fetcher != FETCHER_NAME:
        return None
    return BrowserConfigSection(plugin_id=PLUGIN_ID, label='CloakBrowser', form_class=_settings_form_class())


@hookimpl
def plugin_static_path():
    """Return the path to this plugin's static files directory."""
    return _STATIC_DIR
