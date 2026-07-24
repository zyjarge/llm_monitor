"""
Tests for cookie header parsing utilities.
"""
import pytest

from scraper.src.providers._base import (
    cookie_dict_from_header,
    cookies_for_playwright,
    read_cookie_file,
)


def test_parse_simple_header():
    h = "auth=abc123; session=xyz789; theme=light"
    d = cookie_dict_from_header(h)
    assert d == {"auth": "abc123", "session": "xyz789", "theme": "light"}


def test_parse_handles_spaces_around_pairs():
    h = " auth=abc ;  session=xyz "
    d = cookie_dict_from_header(h)
    assert d == {"auth": "abc", "session": "xyz"}


def test_parse_empty_string_returns_empty_dict():
    assert cookie_dict_from_header("") == {}
    assert cookie_dict_from_header("   ") == {}


def test_parse_quoted_values():
    h = 'name="quoted value"; other=plain'
    d = cookie_dict_from_header(h)
    assert d == {"name": "quoted value", "other": "plain"}


def test_parse_skips_pairs_without_equals():
    h = "garbage; auth=real; nochance"
    d = cookie_dict_from_header(h)
    assert d == {"auth": "real"}


def test_parse_real_kimi_cookie_shape():
    # Sample from user's actual file (truncated values, but real field names)
    h = (
        "lang=zh-CN;Hm_lpvt_358cae4815e85d48f7e8ab7f3680a74b=1784820337;"
        "_ga_Z0ZTEN03PZ=GSxxx;kimi-auth=Fe26.2**real_token**;"
        "_clck=1*abc;HMACCOUNT=189BDD207CA0272B;theme=light"
    )
    d = cookie_dict_from_header(h)
    assert "kimi-auth" in d
    assert d["kimi-auth"].startswith("Fe26.2")
    assert d["lang"] == "zh-CN"
    assert d["theme"] == "light"
    # long numeric values preserved
    assert d["HMACCOUNT"] == "189BDD207CA0272B"


def test_parse_real_opencode_cookie_shape():
    h = "auth=Fe26.2**truncated**; oc_locale=zh"
    d = cookie_dict_from_header(h)
    assert d["auth"].startswith("Fe26.2")
    assert d["oc_locale"] == "zh"


def test_last_wins_on_duplicates():
    h = "auth=first; auth=second"
    d = cookie_dict_from_header(h)
    assert d["auth"] == "second"


def test_cookies_for_playwright_attaches_domain():
    d = {"auth": "xyz", "session": "abc"}
    pw = cookies_for_playwright(d, host="example.com")
    assert len(pw) == 2
    for c in pw:
        assert c["domain"] == "example.com"
        assert c["secure"] is True
        assert c["httpOnly"] is True
        assert c["path"] == "/"


def test_read_cookie_file_missing_returns_empty(tmp_path):
    p = tmp_path / "nope.txt"
    assert read_cookie_file(p) == {}


def test_read_cookie_file_reads_and_parses(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("auth=abc; lang=en", encoding="utf-8")
    assert read_cookie_file(p) == {"auth": "abc", "lang": "en"}
