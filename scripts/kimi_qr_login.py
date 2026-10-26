#!/usr/bin/env python3
"""
Log in to Kimi from the terminal and save the session token.

The Kimi web app keeps its JWT in localStorage (`access_token`), not in a
cookie — so a Cookie-Editor export can never contain it, and the membership
API rejects cookie-only requests with 401.

This script uses Kimi's own **QR login** flow, which needs no browser
profile, no DevTools and no cookie export:

  1. POST auth.kimi.com  CreateLoginQRCode      -> a login code
  2. print / render a QR pointing at
             https://www.kimi.com/wechat/mp/auth?id=<code>&device_id=<webId>
  3. scan it with the Kimi app (or open it in a browser where you're logged
     in) and confirm
  4. poll GetLoginQRCodeStatus until STATUS_SUCCESS
  5. write access_token to cookie/kimi_token (mode 600)

The token is then what the scraper's membership source needs, and it is
verified against the real endpoint before it is written.

The access token is SHORT-LIVED (~15 minutes, `exp - iat == 900`). The
response also carries a `refresh_token`, which is saved alongside it as
`cookie/kimi_refresh_token` — the scraper exchanges it for a fresh access
token automatically, so the session survives indefinitely.

The login code and the poll MUST happen in the same HTTP session -- the token
is handed to the session that created the code. This script therefore creates
the code and polls it itself; use `--code` only to re-poll a code created by
this same script.

Usage:
    python scripts/kimi_qr_login.py            # print QR URL, poll
    python scripts/kimi_qr_login.py --no-qr    # print the URL only
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = PROJECT_ROOT / "cookie" / "kimi_token"

AUTH_BASE = "https://auth.kimi.com/api/account.gateway.v1.AuthService"
MEMBERSHIP_URL = (
    "https://www.kimi.com/apiv2/kimi.gateway.membership.v2."
    "MembershipService/GetSubscriptionStats"
)
SCAN_PAGE = "https://www.kimi.com/wechat/mp/auth"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.kimi.com",
    "Referer": "https://www.kimi.com/",
    "x-msh-platform": "web",
    "X-Language": "zh-CN",
}


def make_qr_text(url: str) -> str:
    """Render a QR code as ASCII art (no third-party dependency)."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return ""
    import io

    qr = qrcode.QRCode(border=1, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def write_qr_png(url: str, path: Path) -> Path | None:
    """Also write the QR as a PNG so it can be opened in a browser or an
    image viewer (the terminal ASCII art is hard to scan on a phone)."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(path)
    return path


def create_qr(client: httpx.Client) -> str:
    r = client.post(f"{AUTH_BASE}/CreateLoginQRCode", json={}, headers=HEADERS)
    r.raise_for_status()
    code = r.json().get("code")
    if not code:
        raise RuntimeError(f"no code in response: {r.text[:200]}")
    return code


def poll(client: httpx.Client, code: str, timeout: int):
    """Poll until the scan is confirmed. Yields the status string."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.post(f"{AUTH_BASE}/GetLoginQRCodeStatus",
                        json={"code": code}, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status != last:
            print(f"  status: {status}", file=sys.stderr)
            last = status
        if status == "STATUS_SUCCESS":
            return data
        if status in ("STATUS_EXPIRED",):
            raise RuntimeError("QR code expired — run again for a new one")
        time.sleep(1.2)
    raise TimeoutError(f"no scan within {timeout}s")


def verify(client: httpx.Client, token: str) -> tuple[bool, str]:
    """Check the token against the real membership endpoint."""
    try:
        r = client.post(
            MEMBERSHIP_URL, json={},
            headers={**HEADERS, "Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        return False, f"request failed: {e}"
    if r.status_code == 200:
        return True, "HTTP 200 — membership API accepted the token"
    if r.status_code in (401, 403):
        return False, f"HTTP {r.status_code} — token rejected"
    return False, f"HTTP {r.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--code", help="existing login code to poll "
                                      "(default: create a new one)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="seconds to wait for the scan (default 180)")
    ap.add_argument("--no-qr", action="store_true",
                    help="print only the URL (no ASCII QR)")
    ap.add_argument("--out", default=str(TOKEN_FILE))
    args = ap.parse_args()

    # IMPORTANT: create and poll MUST use the same HTTP session. The token is
    # returned to the session that created the login code, so creating the
    # code in one process and polling it in another loses the token.
    with httpx.Client(timeout=20) as client:
        code = args.code or create_qr(client)
        # device_id is the browser webId; Kimi accepts any stable id here.
        url = f"{SCAN_PAGE}?id={code}&device_id=kimi-monitor-scraper"

        print("\nScan this with the Kimi app (or open it in a logged-in browser):\n")
        if not args.no_qr:
            art = make_qr_text(url)
            if art:
                print(art)
        print(f"  {url}\n")

        png = write_qr_png(url, Path(args.out).with_suffix(".qr.png"))
        if png:
            # Also a stable name, so the URL served by
            # `python scripts/kimi_qr_png.py --serve` never changes.
            stable = png.with_name("kimi_login_qr.png")
            try:
                stable.write_bytes(png.read_bytes())
                print(f"  PNG: {png}")
                print(f"  PNG: {stable}")
            except Exception:
                print(f"  PNG: {png}")
            print()

        print("Waiting for confirmation...", file=sys.stderr)

        try:
            data = poll(client, code, args.timeout)
        except (TimeoutError, RuntimeError) as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            print("No token was written.", file=sys.stderr)
            return 1

    # The connect-rpc client is configured with useProtoFieldName, but accept
    # both spellings so a wire-format change can't silently lose the token.
    token = (data.get("access_token") or data.get("accessToken")
             or (data.get("result") or {}).get("access_token")
             or (data.get("result") or {}).get("accessToken"))
    refresh = (data.get("refresh_token") or data.get("refreshToken"))
    if not token:
        print("ERROR: scan succeeded but no access_token in the response",
              file=sys.stderr)
        print(f"  raw response: {json.dumps(data)[:600]}", file=sys.stderr)
        return 1

    with httpx.Client(timeout=20) as client:
        ok, detail = verify(client, token)

    if not ok:
        print(f"WARNING: the token did not authenticate: {detail}", file=sys.stderr)
        print("It was NOT written.", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(token, encoding="utf-8")
    out.chmod(0o600)
    print(f"\nToken written to {out} ({len(token)} chars, mode 600).")
    print(f"Verified: {detail}")

    # The access token is SHORT-LIVED (~15 min). The refresh token is what
    # keeps the session alive — save it, or the scraper will lose the
    # monthly balance after a quarter of an hour.
    if refresh:
        refresh_out = out.with_name("kimi_refresh_token")
        refresh_out.write_text(refresh, encoding="utf-8")
        refresh_out.chmod(0o600)
        print(f"Refresh token written to {refresh_out} ({len(refresh)} chars).")
    else:
        print("WARNING: no refresh_token in the response — the access token "
              "expires in ~15 minutes.")

    print("Next: docker compose up -d --force-recreate scraper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
