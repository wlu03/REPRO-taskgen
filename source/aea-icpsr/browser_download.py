#!/usr/bin/env python3
"""Human-in-the-loop browser session for openICPSR package downloads.

Every page under ``www.openicpsr.org`` sits behind a Cloudflare managed
challenge, which cannot be satisfied by an HTTP client: clearance is granted
only after a browser executes the challenge script, and the resulting
``cf_clearance`` cookie is bound to the client's address, User-Agent, and TLS
fingerprint.  This module therefore opens a persistent Chromium profile, hands
control to the operator so a person can solve the challenge, sign in, and
accept each study's terms, and then transfers packages through that same
browser so the session that earned clearance is the one that downloads.

Nothing here attempts to defeat, forge, or replay a challenge.  The profile
directory persists between runs only so an operator does not have to repeat
work the site already granted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

# Detected from the response status and the document title only.  Cloudflare
# injects /cdn-cgi/challenge-platform/ scripts into *successful* responses as
# well, so matching that string in the body reports a cleared page as blocked.
CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "access denied",
)
# Terms and login are matched on the URL only.  A healthy project page shows a
# "Terms of Use" footer link and a "Log in" control, so body-text matching
# reports every working page as blocked.
TERMS_PATH = "/download/terms"
LOGIN_PATH_SEGMENTS = {
    "login", "signin", "sign-in", "sso", "oauth", "oauth2", "cas", "saml",
}
# openICPSR serves the project page and its file manifest to anyone, but sends
# every actual transfer through an OAuth login.  A cleared page is therefore
# not a downloadable one, and the signed-in state has to be checked separately.
SIGNED_OUT_MARKERS = ("log in/create account", "log in / create account")
SIGNED_IN_MARKERS = ("my account", "logout", "log out")
DEFAULT_PROFILE_DIRNAME = "browser-profile"
# openICPSR's own session cookie (JSESSIONID) is a browser-session cookie, so it
# is gone every time Chromium restarts even though the Keycloak SSO cookie
# persists on disk.  Re-entering the OAuth flow rebuilds the site session
# without a password whenever that SSO cookie is still valid.
OAUTH_ENTRY_URL = (
    "https://login.icpsr.umich.edu/realms/icpsr/protocol/openid-connect/auth"
    "?client_id=openicpsr-archonnex-prod-authx&response_type=code"
    "&redirect_uri=https://www.openicpsr.org/openicpsr/oauth/callback"
)


class AeaIcpsrBrowserUnavailable(RuntimeError):
    """Playwright or its Chromium build is not installed."""


class AeaIcpsrBrowserSession:
    """A persistent Chromium profile driven with an operator in the loop."""

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        wait_seconds: float = 600.0,
        download_timeout: float = 1800.0,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.wait_seconds = wait_seconds
        self.download_timeout = download_timeout
        self.announce = announce or (lambda message: print(message, file=sys.stderr, flush=True))
        self._playwright = None
        self._context = None
        self._page = None
        self._last_status: int | None = None
        self._last_cf_mitigated: str = ""

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "AeaIcpsrBrowserSession":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - import guard
            raise AeaIcpsrBrowserUnavailable(
                "playwright is not installed; run "
                "`.venv/bin/python -m pip install -r requirements-browser.txt` "
                "and `.venv/bin/python -m playwright install chromium`"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                accept_downloads=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:  # pragma: no cover - launch guard
            self._playwright.stop()
            self._playwright = None
            raise AeaIcpsrBrowserUnavailable(
                "could not launch Chromium; run "
                "`.venv/bin/python -m playwright install chromium`"
            ) from exc
        self._context.set_default_timeout(60_000)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def close(self) -> None:
        for closer in (self._context, self._playwright):
            if closer is None:
                continue
            try:
                closer.close() if closer is self._context else closer.stop()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._context = None
        self._playwright = None
        self._page = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("browser session is not started")
        return self._page

    # -- challenge handling ------------------------------------------------

    def _record_response(self, response: Any) -> None:
        """Remember the status and Cloudflare verdict of the last navigation."""
        if response is None:
            self._last_status, self._last_cf_mitigated = None, ""
            return
        try:
            self._last_status = response.status
            headers = response.headers or {}
            self._last_cf_mitigated = str(headers.get("cf-mitigated", "")).lower()
        except Exception:  # pragma: no cover - transient navigation
            self._last_status, self._last_cf_mitigated = None, ""

    def page_state(self) -> str:
        """Classify the current page as challenge/terms/login/ready.

        Terms and login are decided from the URL, never from body text: an
        ordinary project page carries a "Terms of Use" footer link and a "Log
        in" control, so matching those words in the body reports every healthy
        page as blocked.  Only the challenge is detected by content, because
        Cloudflare serves it in place of the page rather than redirecting.
        """
        try:
            url = (self.page.url or "").lower()
            title = (self.page.title() or "").lower()
        except Exception:  # pragma: no cover - transient navigation
            return "unknown"
        if any(marker in title for marker in CHALLENGE_TITLE_MARKERS):
            return "challenge"
        if self._last_status == 403 or self._last_cf_mitigated == "challenge":
            return "challenge"
        host = urlsplit(url).hostname or ""
        path = urlsplit(url).path
        if host.startswith(("login.", "signin.", "sso.")) or any(
            segment in LOGIN_PATH_SEGMENTS or segment.endswith("login")
            for segment in path.split("/")
            if segment
        ):
            return "login"
        if TERMS_PATH in path:
            return "terms"
        return "ready"

    def is_authenticated(self) -> bool | None:
        """Whether the page shows a signed-in openICPSR session.

        Returns None when the page says nothing either way, so an unfamiliar
        layout does not stall a session that would have worked.
        """
        try:
            body = (self.page.content() or "").lower()
        except Exception:  # pragma: no cover - transient navigation
            return None
        if any(marker in body for marker in SIGNED_OUT_MARKERS):
            return False
        if any(marker in body for marker in SIGNED_IN_MARKERS):
            return True
        return None

    def establish_session(self) -> bool:
        """Rebuild the openICPSR session from a persisted Keycloak SSO cookie.

        Returns True when the round-trip lands back on openicpsr.org signed in.
        No credential is entered: if the realm asks for one, this reports False
        and leaves the sign-in to the operator.
        """
        try:
            self.navigate(OAUTH_ENTRY_URL)
        except Exception:  # pragma: no cover - needs a browser
            return False
        self.page.wait_for_timeout(3000)
        host = (urlsplit(self.page.url or "").hostname or "").lower()
        if host.endswith("openicpsr.org"):
            return self.is_authenticated() is not False
        return False

    def navigate(self, url: str, attempts: int = 3) -> None:
        """Open ``url``, retrying the aborted navigations Chromium emits.

        A freshly launched persistent context can abort its first navigation
        (``net::ERR_ABORTED``) while the profile is still settling, so a single
        goto is not a reliable signal that the site is unreachable.
        """
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.page.goto(
                    url, wait_until="domcontentloaded", timeout=60_000
                )
                self._record_response(response)
                return
            except Exception as exc:  # pragma: no cover - needs a browser
                last_error = exc
                self.page.wait_for_timeout(2000 * (attempt + 1))
        try:  # last resort: accept the navigation as soon as it commits
            self._record_response(
                self.page.goto(url, wait_until="commit", timeout=60_000)
            )
            return
        except Exception:  # pragma: no cover - needs a browser
            pass
        if last_error is not None:
            raise last_error

    def ensure_clearance(self, url: str) -> str:
        """Open ``url`` and block until a person has cleared the way.

        Returns the final page state.  This deliberately does not automate the
        challenge: it waits for the operator and reports what it sees.
        """
        self.navigate(url)
        state = self.page_state()
        signed_in = self.is_authenticated()
        if state == "ready" and signed_in is False:
            # A persisted SSO cookie can rebuild the site session silently.
            if self.establish_session():
                self.navigate(url)
                state = self.page_state()
                signed_in = self.is_authenticated()
        if state == "ready" and signed_in is not False:
            return state

        needs = []
        if state != "ready":
            needs.append("solve the Cloudflare challenge")
        if signed_in is False:
            needs.append("sign in to your ICPSR account")
        self.announce(
            "\n"
            "  openICPSR needs a person here.\n"
            f"  A Chromium window is open at:\n    {url}\n"
            "  Please, in that window: " + ", then ".join(needs) + ".\n"
            "  (Accept the study's terms too if it asks.)\n"
            f"  Waiting up to {int(self.wait_seconds)}s; the run continues "
            "automatically once you are through.\n"
        )

        deadline = self.wait_seconds
        waited = 0.0
        step = 2.0
        while waited < deadline:
            self.page.wait_for_timeout(int(step * 1000))
            waited += step
            if not any(
                marker in (self.page.title() or "").lower()
                for marker in CHALLENGE_TITLE_MARKERS
            ):
                self._last_status, self._last_cf_mitigated = None, ""
            state = self.page_state()
            if state == "ready" and self.is_authenticated() is not False:
                self.announce(f"  cleared after {int(waited)}s; continuing.\n")
                return state
        if state == "ready":
            return "login"
        return state

    # -- transfer ----------------------------------------------------------

    def download(self, url: str, destination: Path) -> dict[str, Any]:
        """Transfer ``url`` through the browser and save it to ``destination``.

        Chromium streams the body to disk itself, so a multi-gigabyte package
        never has to be held in memory.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.page.expect_download(
                timeout=self.download_timeout * 1000
            ) as info:
                self.page.evaluate(
                    """(target) => {
                        const anchor = document.createElement('a');
                        anchor.href = target;
                        anchor.download = '';
                        document.body.appendChild(anchor);
                        anchor.click();
                        anchor.remove();
                    }""",
                    url,
                )
            download = info.value
            failure = download.failure()
            if failure:
                return {"status": "failed", "error": f"browser download failed: {failure}"}
            download.save_as(str(destination))
            return {
                "status": "complete",
                "suggested_filename": download.suggested_filename,
            }
        except Exception as exc:
            state = self.page_state()
            if state in {"terms", "login", "challenge"}:
                return {
                    "status": {
                        "terms": "terms_required",
                        "login": "auth_required",
                        "challenge": "access_blocked",
                    }[state],
                    "error": f"browser stopped at {state} page for {url}",
                }
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
