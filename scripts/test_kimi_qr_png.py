"""
Tests for the Kimi QR-PNG helper.

The QR has to be *rendered* somewhere: Kimi's login page draws it into a
<canvas>, and the `/wechat/mp/auth` link the QR encodes is only a static
"open in WeChat" poster — opening that link in a browser shows no QR at all.
So we render it ourselves to a PNG the user can open or scan.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kimi_qr_png.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kimi_qr_png", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kqr = load_module()


def test_scan_url_matches_the_site():
    """The site encodes `${origin}/wechat/mp/auth?id=${loginId}&device_id=${webId}`."""
    url = kqr.scan_url("abc-123", "dev-9")
    assert url == (
        "https://www.kimi.com/wechat/mp/auth?id=abc-123&device_id=dev-9"
    )


def test_auth_base_is_the_oauth_namespace():
    """`oauth` namespace = `${AUTH_API_HOST}/api` = https://auth.kimi.com/api."""
    assert kqr.AUTH_BASE.startswith("https://auth.kimi.com/api/")


def test_render_png_writes_a_decodable_qr(tmp_path):
    """The PNG must contain the scan URL, not just look like a QR."""
    qrcode = pytest.importorskip("qrcode")
    pytest.importorskip("PIL")
    import cv2

    url = kqr.scan_url("code-42", "dev-1")
    out = tmp_path / "qr.png"
    kqr.render_png(url, out)

    assert out.exists() and out.stat().st_size > 0
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(
        cv2.imread(str(out))
    )
    assert decoded == url
