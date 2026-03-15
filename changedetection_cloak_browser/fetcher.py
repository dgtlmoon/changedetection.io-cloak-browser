import asyncio
import gc
import json
import os
from urllib.parse import urlparse

from loguru import logger
from changedetectionio.pluggy_interface import hookimpl

_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


@hookimpl
def plugin_static_path():
    """Return the path to this plugin's static files directory."""
    return _STATIC_DIR


@hookimpl
def register_content_fetcher():
    """Register the CloakBrowser fetcher with changedetection.io.

    All changedetectionio.content_fetchers imports are deferred to here to avoid
    a circular import: pluggy_interface (line 215: load_setuptools_entrypoints) loads
    this module, which would otherwise import content_fetchers/__init__.py, which
    imports register_builtin_fetchers from pluggy_interface — but pluggy_interface
    is only partially initialised at that point.

    By the time this function is called (from content_fetchers.get_plugin_fetchers),
    pluggy_interface is fully initialised.
    """
    from changedetectionio.content_fetchers import (
        SCREENSHOT_MAX_HEIGHT_DEFAULT,
        visualselector_xpath_selectors,
        XPATH_ELEMENT_JS,
        INSTOCK_DATA_JS,
        FAVICON_FETCHER_JS,
    )
    from changedetectionio.content_fetchers.base import Fetcher, manage_user_agent
    from changedetectionio.content_fetchers.exceptions import (
        BrowserStepsStepException,
        EmptyReply,
        Non200ErrorCodeReceived,
        PageUnloadable,
        ScreenshotUnavailable,
    )
    # CloakBrowser pages are standard Playwright page objects, so we can reuse
    # the Playwright screenshot helper directly.
    from changedetectionio.content_fetchers.playwright import capture_full_page_async

    class fetcher(Fetcher):
        fetcher_description = "CloakBrowser - Stealth Chromium (anti-bot bypass)"

        # CloakBrowser pages are full Playwright pages — all features work unchanged
        supports_browser_steps = True
        supports_screenshots = True
        supports_xpath_element_data = True

        proxy = None

        def __init__(self, proxy_override=None, custom_browser_connection_url=None, **kwargs):
            super().__init__(**kwargs)

            # CloakBrowser launches a local browser; no remote CDP URL is used.
            # custom_browser_connection_url accepted for API compatibility but ignored.
            if custom_browser_connection_url:
                logger.warning(
                    "CloakBrowser fetcher: custom_browser_connection_url is ignored — "
                    "CloakBrowser always launches a local browser"
                )

            # Reuse the same playwright_proxy_* env vars for consistency
            proxy_args = {}
            for k in ['bypass', 'server', 'username', 'password']:
                v = os.getenv('playwright_proxy_' + k, False)
                if v:
                    proxy_args[k] = v.strip('"')

            if proxy_args:
                self.proxy = proxy_args

            if proxy_override:
                self.proxy = {'server': proxy_override}

            if self.proxy:
                parsed = urlparse(self.proxy.get('server', ''))
                if parsed.username:
                    self.proxy['username'] = parsed.username
                    self.proxy['password'] = parsed.password

        @classmethod
        def get_status_icon_data(cls):
            return {
                'group': 'plugin',
                'filename': 'cloakbrowser-logo.svg',
                'alt': 'Using CloakBrowser (stealth)',
                'title': 'CloakBrowser — Stealth Chromium with anti-bot bypass',
            }

        @classmethod
        async def get_browsersteps_browser(cls, proxy=None, keepalive_ms=None):
            """Launch a local CloakBrowser instance for the browser steps live UI.

            Called by browser_steps/__init__.py instead of the default CDP path when
            this fetcher is selected for the watch.  Returns (browser, None) — no
            playwright_context is needed because CloakBrowser manages its own process.
            """
            from cloakbrowser import launch_async

            proxy_url = cls._proxy_dict_to_url(proxy) if proxy else None

            humanize_raw = os.getenv('CLOAKBROWSER_HUMANIZE', 'true').lower()
            humanize = humanize_raw not in ('false', '0', 'no')

            browser = await launch_async(
                headless=True,
                proxy=proxy_url,
                humanize=humanize,
            )
            return (browser, None)

        @staticmethod
        def _proxy_dict_to_url(proxy):
            """Convert a Playwright-style proxy dict to a URL string for CloakBrowser."""
            if not proxy:
                return None
            server = proxy.get('server', '')
            username = proxy.get('username')
            password = proxy.get('password')
            if username and server:
                parsed = urlparse(server)
                return f"{parsed.scheme}://{username}:{password}@{parsed.hostname}:{parsed.port}"
            return server or None

        def _build_proxy_url(self):
            """Convert this instance's proxy dict to a URL string for CloakBrowser."""
            return self._proxy_dict_to_url(self.proxy)

        async def screenshot_step(self, step_n=''):
            super().screenshot_step(step_n=step_n)
            watch_uuid = getattr(self, 'watch_uuid', None)
            screenshot = await capture_full_page_async(
                page=self.page,
                screenshot_format=self.screenshot_format,
                watch_uuid=watch_uuid,
                lock_viewport_elements=self.lock_viewport_elements,
            )
            try:
                await self.page.request_gc()
            except Exception:
                pass

            if self.browser_steps_screenshot_path is not None:
                destination = os.path.join(self.browser_steps_screenshot_path, f'step_{step_n}.jpeg')
                logger.debug(f"Saving step screenshot to {destination}")
                with open(destination, 'wb') as f:
                    f.write(screenshot)
                del screenshot
                gc.collect()

        async def save_step_html(self, step_n):
            super().save_step_html(step_n=step_n)
            content = await self.page.content()
            try:
                await self.page.request_gc()
            except Exception:
                pass
            destination = os.path.join(self.browser_steps_screenshot_path, f'step_{step_n}.html')
            logger.debug(f"Saving step HTML to {destination}")
            with open(destination, 'w', encoding='utf-8') as f:
                f.write(content)
            del content
            gc.collect()

        async def run(
            self,
            fetch_favicon=True,
            current_include_filters=None,
            empty_pages_are_a_change=False,
            ignore_status_codes=False,
            is_binary=False,
            request_body=None,
            request_headers=None,
            request_method=None,
            screenshot_format=None,
            timeout=None,
            url=None,
            watch_uuid=None,
        ):
            from cloakbrowser import launch_async
            import time

            self.delete_browser_steps_screenshots()
            self.watch_uuid = watch_uuid

            browser = None
            context = None
            response = None

            proxy_url = self._build_proxy_url()

            humanize_raw = os.getenv('CLOAKBROWSER_HUMANIZE', 'true').lower()
            humanize = humanize_raw not in ('false', '0', 'no')

            try:
                browser = await launch_async(
                    headless=True,
                    proxy=proxy_url,
                    humanize=humanize,
                )

                # CloakBrowser returns standard Playwright browser objects —
                # new_context() and all page methods work identically to Playwright
                context = await browser.new_context(
                    accept_downloads=False,
                    bypass_csp=True,
                    extra_http_headers=request_headers or {},
                    ignore_https_errors=True,
                    service_workers=os.getenv('PLAYWRIGHT_SERVICE_WORKERS', 'allow'),
                    user_agent=manage_user_agent(headers=request_headers or {}),
                )

                self.page = await context.new_page()
                self.page.on(
                    "console",
                    lambda msg: logger.debug(f"CloakBrowser console: {url} {msg.type}: {msg.text} {msg.args}"),
                )

                # steppable_browser_interface works unchanged because CloakBrowser
                # pages are Playwright pages — same API, same error classes
                from changedetectionio.browser_steps.browser_steps import steppable_browser_interface
                browsersteps_interface = steppable_browser_interface(start_url=url)
                browsersteps_interface.page = self.page

                response = await browsersteps_interface.action_goto_url(value=url)

                if response is None:
                    raise EmptyReply(url=url, status_code=None)

                try:
                    self.headers = await response.all_headers()
                except TypeError:
                    self.headers = response.all_headers()

                try:
                    if self.webdriver_js_execute_code and len(self.webdriver_js_execute_code):
                        await browsersteps_interface.action_execute_js(
                            value=self.webdriver_js_execute_code, selector=None
                        )
                except Exception as e:
                    logger.debug(f"CloakBrowser > Error executing custom JS: {e}")
                    raise PageUnloadable(url=url, status_code=None, message=str(e))

                extra_wait = int(os.getenv("WEBDRIVER_DELAY_BEFORE_CONTENT_READY", 5)) + self.render_extract_delay
                await self.page.wait_for_timeout(extra_wait * 1000)

                try:
                    self.status_code = response.status
                except Exception as e:
                    logger.critical(f"CloakBrowser > Response had no status_code: {e}")
                    raise PageUnloadable(url=url, status_code=None, message=str(e))

                if fetch_favicon:
                    try:
                        self.favicon_blob = await self.page.evaluate(FAVICON_FETCHER_JS)
                        try:
                            await self.page.request_gc()
                        except Exception:
                            pass
                    except Exception as e:
                        logger.error(f"CloakBrowser > Error fetching favicon: {e}, continuing.")

                if self.status_code != 200 and not ignore_status_codes:
                    screenshot = await capture_full_page_async(
                        self.page,
                        screenshot_format=self.screenshot_format,
                        watch_uuid=watch_uuid,
                        lock_viewport_elements=self.lock_viewport_elements,
                    )
                    raise Non200ErrorCodeReceived(url=url, status_code=self.status_code, screenshot=screenshot)

                if not empty_pages_are_a_change and len((await self.page.content()).strip()) == 0:
                    raise EmptyReply(url=url, status_code=response.status)

                try:
                    if self.browser_steps:
                        try:
                            await self.iterate_browser_steps(start_url=url)
                        except BrowserStepsStepException:
                            raise
                        await self.page.wait_for_timeout(extra_wait * 1000)

                    now = time.time()
                    MAX_TOTAL_HEIGHT = int(os.getenv("SCREENSHOT_MAX_HEIGHT", SCREENSHOT_MAX_HEIGHT_DEFAULT))

                    if current_include_filters is not None:
                        await self.page.evaluate(f"var include_filters={json.dumps(current_include_filters)}")
                    else:
                        await self.page.evaluate("var include_filters=''")
                    try:
                        await self.page.request_gc()
                    except Exception:
                        pass

                    self.xpath_data = await self.page.evaluate(XPATH_ELEMENT_JS, {
                        "visualselector_xpath_selectors": visualselector_xpath_selectors,
                        "max_height": MAX_TOTAL_HEIGHT,
                    })
                    try:
                        await self.page.request_gc()
                    except Exception:
                        pass

                    self.instock_data = await self.page.evaluate(INSTOCK_DATA_JS)
                    try:
                        await self.page.request_gc()
                    except Exception:
                        pass

                    self.content = await self.page.content()
                    try:
                        await self.page.request_gc()
                    except Exception:
                        pass

                    logger.debug(f"CloakBrowser > Scraped xPath/instock data in {time.time() - now:.2f}s")

                    self.screenshot = await capture_full_page_async(
                        page=self.page,
                        screenshot_format=self.screenshot_format,
                        watch_uuid=watch_uuid,
                        lock_viewport_elements=self.lock_viewport_elements,
                    )
                    try:
                        await self.page.request_gc()
                    except Exception:
                        pass
                    gc.collect()

                except ScreenshotUnavailable:
                    raise ScreenshotUnavailable(url=url, status_code=self.status_code)

            finally:
                try:
                    if hasattr(self, 'page') and self.page:
                        try:
                            await self.page.request_gc()
                        except Exception:
                            pass
                        await asyncio.wait_for(self.page.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"CloakBrowser > Timed out closing page for {url}")
                except Exception as e:
                    logger.warning(f"CloakBrowser > Error closing page for {url}: {e}")
                finally:
                    self.page = None

                try:
                    if context:
                        await asyncio.wait_for(context.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"CloakBrowser > Timed out closing context for {url}")
                except Exception as e:
                    logger.warning(f"CloakBrowser > Error closing context for {url}: {e}")
                finally:
                    context = None

                try:
                    if browser:
                        await asyncio.wait_for(browser.close(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"CloakBrowser > Timed out closing browser for {url}")
                except Exception as e:
                    logger.warning(f"CloakBrowser > Error closing browser for {url}: {e}")
                finally:
                    browser = None

                gc.collect()

        async def quit(self, watch=None):
            pass

        def get_error(self):
            return self.error

        def get_last_status_code(self):
            return self.status_code

        def is_ready(self):
            try:
                import cloakbrowser  # noqa: F401
                return True
            except ImportError:
                logger.error("CloakBrowser fetcher: 'cloakbrowser' package is not installed")
                return False

    return ('html_cloakbrowser', fetcher)
