#!/usr/bin/env python3
"""
Render the Kimi QR login as a PNG file you can open anywhere.

Why this exists: the Kimi login page draws its QR into a <canvas>, and the
`/wechat/mp/auth` link that the QR encodes is only a static "open in WeChat"
poster — opening that link in a browser shows no QR at all. So the QR has to
be *rendered*, and a PNG is the practical way to see it (open it in a browser,
an image viewer, or AirDrop/transfer it to your phone and scan from there).

The QR payload is the same URL the Kimi site itself encodes:

    https://www.kimi.com/wechat/mp/auth?id=<login code>&device_id=<webId>

Usage:
    python scripts/kimi_qr_png.py                 # create a code, write a PNG
    python scripts/kimi_qr_png.py --out qr.png
    python scripts/kimi_qr_png.py --code <uuid>    # re-render an existing code
    python scripts/kimi_qr_png.py --serve          # also serve it over HTTP

Then scan the PNG with the Kimi app and confirm on the phone.
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import sys
import threading
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]

AUTH_BASE = "https://auth.kimi.com/api/account.gateway.v1.AuthService"
SCAN_PAGE = "https://www.kimi.com/wechat/mp/auth"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.kimi.com",
    "Referer": "https://www.kimi.com/",
    "x-msh-platform": "web",
    "X-Language": "zh-CN",
}


def create_code() -> str:
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{AUTH_BASE}/CreateLoginQRCode", json={}, headers=HEADERS)
        r.raise_for_status()
        return r.json()["code"]


def scan_url(code: str, device_id: str) -> str:
    return f"{SCAN_PAGE}?id={code}&device_id={device_id}"


def render_png(url: str, out: Path) -> Path:
    """Write the QR as a PNG. Requires the `qrcode` package."""
    try:
        import qrcode
    except ImportError:
        raise SystemExit(
            "The 'qrcode' package is required.\n"
            "  pip install qrcode pillow"
        )
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def serve(directory: Path, port: int) -> None:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(directory), **kw
    )
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"Serving {directory} at http://<this-host>:{port}/", file=sys.stderr)
        httpd.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--code", help="existing login code (default: create a new one)")
    ap.add_argument("--device-id", default="kimi-monitor-scraper",
                    help="device_id to embed (the site's own webId is not required)")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "cookie" / "kimi_login_qr.png"))
    ap.add_argument("--serve", action="store_true",
                    help="also serve the directory over HTTP")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    code = args.code or create_code()
    url = scan_url(code, args.device_id)
    out = render_png(url, Path(args.out))

    print(f"Login code : {code}")
    print(f"QR PNG     : {out}")
    print(f"QR payload : {url}")
    print()
    print("Scan the PNG with the Kimi app and confirm on the phone.")
    print("Then run:  python scripts/kimi_qr_login.py --code " + code)

    if args.serve:
        t = threading.Thread(target=serve, args=(out.parent, args.port), daemon=True)
        t.start()
        print(f"\nServing on http://0.0.0.0:{args.port}/{out.name}")
        try:
            t.join()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
