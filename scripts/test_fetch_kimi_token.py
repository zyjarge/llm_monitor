"""
End-to-end test of scripts/fetch_kimi_token.py.

Serves a local page that plants a fake JWT in localStorage and verifies the
script's extraction + cookie parsing work — no network, no Kimi account.
"""
import http.server
import importlib.util
import json
import os
import socketserver
import sys
import shutil
import tempfile
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_kimi_token.py"

# A real JWT is exactly three dot-separated segments:
#   header.payload.signature
FAKE = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.sig"

PAGE = (
    "<!doctype html><html><body>ok<script>"
    "localStorage.setItem('access_token', " + json.dumps(FAKE) + ");"
    "localStorage.setItem('noise_key', 'not-a-token');"
    "</script></body></html>"
)


def load_module():
    spec = importlib.util.spec_from_file_location("fkt", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    failures = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE.encode())

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    fkt = load_module()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_context().new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        page.wait_for_timeout(300)

        got = fkt.extract(page)
        if got == FAKE:
            print("PASS  extract() returns the planted JWT")
        else:
            failures.append(f"extract() returned {got!r}")
            print(f"FAIL  extract() returned {got!r}")
        browser.close()

    # Netscape cookie parsing
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t1800000000\tkimi-auth\tv1\n"
            "www.kimi.com\tFALSE\t/\tFALSE\t0\tlang\tzh-CN\n"
        )
        name = tf.name
    cks = fkt.load_netscape_cookies(Path(name))
    os.unlink(name)
    got_names = sorted(c["name"] for c in cks)
    if got_names == ["kimi-auth", "lang"]:
        print("PASS  Netscape cookie file parsed (incl. #HttpOnly_)")
    else:
        failures.append(f"cookie parse gave {got_names}")
        print(f"FAIL  cookie parse gave {got_names}")

    # Missing file must not explode
    if fkt.load_netscape_cookies(Path("/nonexistent/cookies.txt")) == []:
        print("PASS  missing cookie file returns []")
    else:
        failures.append("missing cookie file did not return []")
        print("FAIL  missing cookie file did not return []")

    # Token validation against the live membership API (mocked).
    import httpx
    import respx

    BOGUS = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.bogussig"
    with respx.mock:
        respx.post(fkt.MEMBERSHIP_URL).mock(
            return_value=httpx.Response(200, json={"code": 0}))
        ok, detail = fkt.validate_token(BOGUS)
        if ok and detail == "HTTP 200":
            print("PASS  validate_token() accepts a 200 response")
        else:
            failures.append(f"validate_token() 200 -> {ok} {detail}")
            print(f"FAIL  validate_token() 200 -> {ok} {detail}")

    with respx.mock:
        respx.post(fkt.MEMBERSHIP_URL).mock(
            return_value=httpx.Response(401, json={"code": "unauthenticated"}))
        ok, detail = fkt.validate_token(BOGUS)
        if not ok:
            print("PASS  validate_token() rejects a 401 response")
        else:
            failures.append("validate_token() accepted a 401")
            print("FAIL  validate_token() accepted a 401")

    # Browser-profile handling (locked Chrome profiles are the common case).
    prof = Path(tempfile.mkdtemp())
    (prof / "Default").mkdir()
    (prof / "Default" / "Cookies").write_text("secret")
    (prof / "Local State").write_text("{}")

    udd, cleanup = fkt.resolve_profile(str(prof))
    if udd == str(prof) and cleanup is None:
        print("PASS  unlocked profile is used in place")
    else:
        failures.append("unlocked profile was not used in place")
        print("FAIL  unlocked profile was not used in place")

    (prof / "SingletonLock").write_text("")
    udd2, cleanup2 = fkt.resolve_profile(str(prof))
    ok = (
        udd2 != str(prof)
        and (Path(udd2) / "Default" / "Cookies").exists()
        and not (Path(udd2) / "SingletonLock").exists()
        and cleanup2 is not None
    )
    if ok:
        print("PASS  locked profile is copied to a temp dir (SingletonLock skipped)")
    else:
        failures.append("locked profile copy misbehaved")
        print("FAIL  locked profile copy misbehaved")
    if cleanup2:
        shutil.rmtree(cleanup2, ignore_errors=True)
    shutil.rmtree(prof, ignore_errors=True)

    if fkt.resolve_profile("/nonexistent/profile") == (None, None):
        print("PASS  missing profile resolves to (None, None)")
    else:
        failures.append("missing profile did not resolve to (None, None)")
        print("FAIL  missing profile did not resolve to (None, None)")

    srv.shutdown()
    print()
    if failures:
        print("FAILURES:", failures)
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
