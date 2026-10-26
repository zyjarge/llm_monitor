#!/usr/bin/env python3
"""
Extract the Kimi web access token from a logged-in browser session.

Modern Kimi web keeps its JWT in **localStorage** under `access_token`
(confirmed in the live app bundle: `localStorage.setItem('access_token', ...)`),
NOT in a `kimi-auth` cookie — so a Cookie-Editor cookie export does not
contain it and the membership API rejects cookie-only requests.

Run this on the machine where the browser is ALREADY logged in to Kimi.
If that browser is logged in, the token is already in localStorage and the
script needs no interaction at all.

    # reuse an existing (logged-in) browser profile — no interaction needed
    python scripts/fetch_kimi_token.py --profile ~/.config/google-chrome

    # ...or open a browser and log in by hand
    python scripts/fetch_kimi_token.py

    # print the token instead of writing a file (to paste into .env)
    python scripts/fetch_kimi_token.py --print

The token is a live account credential. It is written only to the local
file (mode 600) and never printed unless --print is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = PROJECT_ROOT / "cookie" / "kimi_token"

# The page that requires an authenticated session.
URL = "https://www.kimi.com/membership/subscription"

# Storage keys the Kimi web app has used for the session JWT.
TOKEN_KEYS = ("access_token", "accessToken", "kimi-auth")

# Common browser profile locations, in preference order.
PROFILE_CANDIDATES = [
    "~/.config/google-chrome",
    "~/.config/chromium",
    "~/.config/BraveSoftware/Brave-Browser",
    "~/.config/microsoft-edge",
    "~/.config/google-chrome-beta",
]

EXTRACT_JS = """(keys) => {
    // Direct keys first.
    for (const k of keys) {
        const v = localStorage.getItem(k);
        if (v && v.split('.').length === 3) return v;
    }
    // Some builds nest the token inside a JSON blob under a hashed key.
    for (const k of Object.keys(localStorage)) {
        const raw = localStorage.getItem(k) || '';
        if (raw.length < 4000 && raw.includes('.')) {
            try {
                const o = JSON.parse(raw);
                for (const key of keys) {
                    if (o[key] && String(o[key]).split('.').length === 3) {
                        return String(o[key]);
                    }
                }
            } catch (e) { /* not JSON */ }
        }
    }
    return null;
}"""


def find_profile() -> str | None:
    """Return an existing browser profile dir, if one is present."""
    for cand in PROFILE_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    return None


def resolve_profile(profile: str | None) -> tuple[str | None, str | None]:
    """Return (user_data_dir, cleanup_dir).

    Chrome locks its profile while it is running, so Playwright cannot open
    it directly. When the profile is locked we copy it to a temp dir and use
    the copy. (Copying is safe: the original is only read.)
    """
    if not profile:
        return None, None

    src = Path(profile).expanduser()
    if not src.exists():
        return None, None

    # A running Chrome leaves this lock file behind.
    lock = src / "SingletonLock"
    if not lock.exists():
        return str(src), None

    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="kimi-chrome-profile-"))
    dest = tmp / "profile"
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(
        "Singleton*", "lock", "Cache", "Code Cache", "GPUCache",
        "Service Worker/CacheStorage", "Service Worker/ScriptCache",
    ), symlinks=True, dirs_exist_ok=True)
    return str(dest), str(tmp)


def extract(page) -> str | None:
    """Read the session JWT out of localStorage."""
    return page.evaluate(EXTRACT_JS, list(TOKEN_KEYS))


MEMBERSHIP_URL = (
    "https://www.kimi.com/apiv2/kimi.gateway.membership.v2."
    "MembershipService/GetSubscriptionStats"
)


def validate_token(token: str) -> tuple[bool, str]:
    """Check the token against the live membership API.

    Returns (ok, detail). A 200 means the token is genuinely usable, so we
    never write a token that would just 401 at scrape time.
    """
    try:
        import httpx
    except ImportError:
        return True, "httpx not installed — skipped live verification"

    try:
        resp = httpx.post(
            MEMBERSHIP_URL,
            json={},
            timeout=20,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
    except Exception as e:
        return False, f"request failed: {e}"

    if resp.status_code == 200:
        return True, "HTTP 200"
    if resp.status_code in (401, 403):
        return False, f"HTTP {resp.status_code} — token rejected (not logged in?)"
    return False, f"HTTP {resp.status_code}"


def load_netscape_cookies(path: Path) -> list[dict]:
    """Parse a Netscape cookie file into Playwright cookie dicts."""
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, cpath, secure, expires, name, value = parts[:7]
        try:
            exp = int(expires)
        except ValueError:
            exp = 0
        out.append({
            "name": name,
            "value": value,
            "domain": domain.lstrip("."),
            "path": cpath or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": False,
            "sameSite": "Lax",
            **({"expires": exp} if exp > 0 else {}),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--profile", help="browser user-data-dir to reuse (logged-in session)")
    ap.add_argument("--cookies", help="Netscape cookie file to inject (optional)")
    ap.add_argument("--headless", action="store_true",
                    help="run headless (needs an already-valid session)")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="print the token to stdout instead of writing a file")
    ap.add_argument("--out", default=str(TOKEN_FILE), help="output file path")
    args = ap.parse_args()

    profile = args.profile
    if profile is None and args.headless:
        profile = find_profile()
    profile, _cleanup_dir = resolve_profile(profile)

    with sync_playwright() as p:
        ctx_args = {"user_data_dir": profile} if profile else {}
        browser = p.chromium.launch(
            headless=args.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(**ctx_args)
        if args.cookies:
            cookies = load_netscape_cookies(Path(args.cookies))
            if cookies:
                ctx.add_cookies(cookies)
                print(f"Injected {len(cookies)} cookies from {args.cookies}")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        token = extract(page)
        if not token and not args.headless:
            print("\nNo token found yet. Log in to Kimi in the browser window that "
                  "just opened,\nthen come back here and press Enter.")
            try:
                input()
            except EOFError:
                pass
            page.wait_for_timeout(1000)
            token = extract(page)

        browser.close()

    if not token:
        print("\nERROR: no access token found.")
        print("  The Kimi web app only writes `access_token` to localStorage after a")
        print("  real login, and there is no cookie->token exchange endpoint.")
        print()
        print("  Fix, in order of ease:")
        print("   1. On the machine where your browser is ALREADY logged in to Kimi:")
        print("        python scripts/fetch_kimi_token.py --profile <chrome profile dir>")
        print("      then copy the resulting cookie/kimi_token to this server.")
        print("   2. Or open the browser this script starts, log in, and press Enter.")
        print("   3. Or skip the script entirely -- in your browser DevTools:")
        print("        Application -> Local Storage -> https://www.kimi.com")
        print("        -> copy the `access_token` value")
        print("      and put it in .env as:")
        print("        KIMI_AUTH_TOKEN=eyJ...")
        return 1

    if _cleanup_dir:
        import shutil
        shutil.rmtree(_cleanup_dir, ignore_errors=True)

    if args.do_print:
        print(token)
        return 0

    ok, detail = validate_token(token)
    if not ok:
        print(f"\nWARNING: the extracted token did not authenticate: {detail}")
        print("It was NOT written. The browser session is probably not logged in.")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(token, encoding="utf-8")
    out.chmod(0o600)
    print(f"Token written to {out} ({len(token)} chars, mode 600).")
    print(f"Verified against the membership API: {detail}")
    print("Next: docker compose up -d --force-recreate scraper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
