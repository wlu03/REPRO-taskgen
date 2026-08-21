#!/usr/bin/env python3
"""One-time sign-in for the Chromium profile used by ``--browser`` downloads.

    export ICPSR_EMAIL='you@example.com'
    export ICPSR_PASSWORD='secret'
    ./.venv/bin/python browser_login.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from browser_download import (
    DEFAULT_PROFILE_DIRNAME,
    AeaIcpsrBrowserSession,
    AeaIcpsrBrowserUnavailable,
)

CHECK_URL = "https://www.openicpsr.org/openicpsr/project/111981/version/V1/view"
LOGIN_URL = (
    "https://login.icpsr.umich.edu/realms/icpsr/protocol/openid-connect/auth"
    "?client_id=openicpsr-archonnex-prod-authx&response_type=code&login=true"
    "&redirect_uri=https://www.openicpsr.org/openicpsr/oauth/callback"
)


def _auto_login(session: AeaIcpsrBrowserSession, email: str, password: str) -> None:
    """Navigate to the ICPSR Keycloak login page, fill credentials, click Sign In."""
    page = session.page

    # 1. Load the login page
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.wait_for_timeout(2000)

    # 2. Click "Sign in with email" to reveal the credential form.
    #    Use get_by_text which is case-insensitive and matches partial text.
    email_link = page.get_by_text("Sign in with email")
    if email_link.count() > 0:
        email_link.first.click()
        page.wait_for_timeout(2000)

    # 3. Fill email — Keycloak uses id="username"
    username_field = page.locator("#username")
    if username_field.count() == 0:
        username_field = page.locator('input[type="email"], input[type="text"]').first
    username_field.fill(email)

    # 4. Fill password — Keycloak uses id="password"
    password_field = page.locator("#password")
    if password_field.count() == 0:
        password_field = page.locator('input[type="password"]').first
    password_field.fill(password)

    # 5. Click Sign In — Keycloak uses id="kc-login"
    sign_in_btn = page.locator("#kc-login")
    if sign_in_btn.count() > 0:
        sign_in_btn.click()
    else:
        # Fallback: click whichever visible button says "Sign In"
        page.get_by_role("button", name="Sign In").click()

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=Path(__file__).resolve().parent / DEFAULT_PROFILE_DIRNAME,
        help="profile directory to sign in (default: browser-profile/)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=900.0,
        help="seconds to wait for the sign-in (default: 900)",
    )
    parser.add_argument("--url", default=CHECK_URL, help="page to open")
    parser.add_argument(
        "--email",
        default=None,
        help="ICPSR email (default: $ICPSR_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="ICPSR password (default: $ICPSR_PASSWORD env var)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="skip auto-login; sign in manually in the browser window",
    )
    args = parser.parse_args(argv)

    email = args.email or os.environ.get("ICPSR_EMAIL")
    password = args.password or os.environ.get("ICPSR_PASSWORD")
    auto_login = not args.manual and bool(email) and bool(password)

    if not args.manual and not auto_login:
        print(
            "No credentials supplied; opening the browser for a manual sign-in.\n"
            "Set ICPSR_EMAIL and ICPSR_PASSWORD, or pass --email/--password, to\n"
            "fill them automatically.\n",
            file=sys.stderr,
        )

    profile = args.browser_profile.expanduser().resolve()
    try:
        session = AeaIcpsrBrowserSession(profile, wait_seconds=args.wait)
        session.start()
    except AeaIcpsrBrowserUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if auto_login:
            print("Attempting automatic login ...")
            try:
                _auto_login(session, email, password)
            except Exception as exc:
                print(
                    f"Auto-login encountered an error: {exc}\n"
                    "Falling back to manual sign-in.",
                    file=sys.stderr,
                )
                auto_login = False

            session.navigate(args.url)
            if session.is_authenticated():
                print(f"Signed in automatically. Profile saved:\n  {profile}")
                print(
                    "\nNow run:\n"
                    "  ./run_scraper.sh --download-files --browser \\\n"
                    f"    --browser-profile {profile} --max-records 1 "
                    "--max-file-mb 500"
                )
                return 0
            print(
                "Auto-fill completed but the session is not authenticated.\n"
                "Waiting for the sign-in to finish in the browser window ...\n",
                flush=True,
            )

        session.navigate(args.url)
        if session.is_authenticated():
            print(f"Already signed in; profile is ready:\n  {profile}")
            return 0

        if not auto_login:
            print(
                "\nA Chromium window is open at:\n"
                f"  {args.url}\n\n"
                "Sign in to your ICPSR account in that window.\n"
                f"Waiting up to {int(args.wait)}s; this exits on its own once the\n"
                "page reports a signed-in session.\n",
                flush=True,
            )

        waited = 0.0
        while waited < args.wait:
            session.page.wait_for_timeout(2000)
            waited += 2.0
            if session.is_authenticated():
                print(f"Signed in after {int(waited)}s. Profile saved:\n  {profile}")
                print(
                    "\nNow run:\n"
                    "  ./run_scraper.sh --download-files --browser \\\n"
                    f"    --browser-profile {profile} --max-records 1 "
                    "--max-file-mb 500"
                )
                return 0

        print("Timed out without a signed-in session.", file=sys.stderr)
        return 2

    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())