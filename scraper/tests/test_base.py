"""
Tests for the shared cookie helpers.

The cookie format on disk is Netscape (exported by Chrome extensions like
Cookie-Editor), so the parser under test is `cookie_dict_from_netscape`.
"""
from scraper.src.providers._base import (
    cookie_dict_from_netscape,
    cookies_for_playwright,
    read_cookie_file,
)


def test_parse_netscape_lines():
    text = (
        "opencode.ai\tFALSE\t/\tFALSE\t0\tauth\tFe26.2**token**\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\toc_locale\tzh\n"
    )
    d = cookie_dict_from_netscape(text)
    assert d == {"auth": "Fe26.2**token**", "oc_locale": "zh"}


def test_httponly_prefix_is_stripped_not_treated_as_comment():
    text = "#HttpOnly_opencode.ai\tFALSE\t/\tTRUE\t1800000000\tkimi-auth\tFe26.2**t**\n"
    d = cookie_dict_from_netscape(text)
    assert d == {"kimi-auth": "Fe26.2**t**"}


def test_real_kimi_cookie_shape():
    # Field names from the user's actual cookie file.
    text = (
        "www.kimi.com\tFALSE\t/\tFALSE\t0\tlang\tzh-CN\n"
        ".kimi.com\tFALSE\t/\tFALSE\t0\tHMACCOUNT\t189BDD207CA0272B\n"
        "#HttpOnly_www.kimi.com\tFALSE\t/\tTRUE\t0\tkimi-auth\tFe26.2**real_token**\n"
        "www.kimi.com\tFALSE\t/\tFALSE\t0\ttheme\tlight\n"
    )
    d = cookie_dict_from_netscape(text)
    assert "kimi-auth" in d
    assert d["kimi-auth"].startswith("Fe26.2")
    assert d["lang"] == "zh-CN"
    assert d["theme"] == "light"
    # long numeric values preserved
    assert d["HMACCOUNT"] == "189BDD207CA0272B"


def test_comments_and_blank_lines_are_skipped():
    text = (
        "# Netscape HTTP Cookie File\n"
        "# http://curl.haxx.se/rfc/cookie_spec.html\n"
        "\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\tauth\tvalue\n"
    )
    d = cookie_dict_from_netscape(text)
    assert d == {"auth": "value"}


def test_malformed_lines_are_skipped():
    text = "garbage\nonly\ttwo\tcolumns\n"
    assert cookie_dict_from_netscape(text) == {}


def test_empty_input_returns_empty_dict():
    assert cookie_dict_from_netscape("") == {}
    assert cookie_dict_from_netscape("   ") == {}


def test_last_wins_on_duplicates():
    text = (
        "opencode.ai\tFALSE\t/\tFALSE\t0\tauth\tfirst\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\tauth\tsecond\n"
    )
    assert cookie_dict_from_netscape(text)["auth"] == "second"


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
    p.write_text(
        "opencode.ai\tFALSE\t/\tFALSE\t0\tauth\tabc\n"
        "opencode.ai\tFALSE\t/\tFALSE\t0\tlang\ten\n",
        encoding="utf-8",
    )
    assert read_cookie_file(p) == {"auth": "abc", "lang": "en"}
