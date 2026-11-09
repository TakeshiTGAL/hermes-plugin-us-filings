"""SEC calls: the user's contact, no proxy, no redirect, no retry on 403 or 429."""
import json
import urllib.error
import urllib.request

import pytest

import client
from client import ContactInvalid, ContactUnreadable, ContactUnset, SecError
from support import CONTACT, OTHER_CONTACT, use_contact


def test_unset_contact_does_not_call_sec(capsys):
    with pytest.raises(ContactUnset) as caught:
        client.read_contact()
    assert caught.value.kind == "contact_unset"
    assert "US_FILINGS_CONTACT" in caught.value.next_step
    assert "accessing-edgar-data" in caught.value.next_step
    assert CONTACT not in str(caught.value)
    assert capsys.readouterr().out == ""


def test_invalid_contact_names_the_shape_and_hides_the_value():
    for bad in ("no-at", "a@b.co", "line\nbreak@example.com", "x" * 201 + "@example.com"):
        use_contact(bad)
        with pytest.raises(ContactInvalid) as caught:
            client.read_contact()
        assert bad not in str(caught.value)
        assert bad not in caught.value.next_step


def test_unreadable_helper_is_not_called_unset(monkeypatch):
    import agent.secret_scope as scope

    def explode(_name, default=None):
        raise RuntimeError("scope blew up near " + CONTACT)

    monkeypatch.setattr(scope, "get_secret", explode)
    with pytest.raises(ContactUnreadable) as caught:
        client.read_contact()
    assert caught.value.kind == "contact_unreadable"
    assert "unset" in str(caught.value)
    assert CONTACT not in str(caught.value)
    assert CONTACT not in (caught.value.next_step or "")


def test_user_agent_is_exactly_the_contact(monkeypatch):
    use_contact(CONTACT)
    seen = {}

    class Resp:
        status = 200

        def read(self, _n):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, timeout):
        seen["ua"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(client, "_opener", lambda: type("Op", (), {"open": staticmethod(open_request)})())
    client._transport.set(None)
    status, body = client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 100)
    assert status == 200
    assert body == b"{}"
    assert seen["ua"] == CONTACT
    assert not seen["ua"].lower().startswith("us-filings")
    assert seen["timeout"] == 30


def test_opener_drops_proxy_env(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    assert "127.0.0.1" in str(urllib.request.getproxies())
    opener = client._opener()
    assert not any(getattr(handler, "proxies", None) for handler in opener.handlers)
    assert any(isinstance(handler, client._NoRedirects) for handler in opener.handlers)


def test_redirect_is_refused():
    with pytest.raises(SecError) as caught:
        client._NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://evil.example/a")
    assert caught.value.kind == "redirect"
    assert "evil.example" not in str(caught.value)


def test_forbidden_hosts_are_refused_before_a_call():
    for url in (
        "http://data.sec.gov/submissions/CIK0000320193.json",
        "https://169.254.169.254/",
        "https://192.0.2.1/x",
        "https://0.0.0.0/",
        "https://user:secret@data.sec.gov/x",
        "https://evil.example/data.sec.gov",
    ):
        with pytest.raises(SecError) as caught:
            client.fetch(url, 100)
        assert caught.value.kind == "host"


def test_429_and_403_do_not_retry_or_return_html():
    use_contact()
    html = "<html>" + CONTACT + " Undeclared Automated Tool</html>"

    def once(url, contact):
        once.calls.append((url, contact))
        return 429, html.encode()

    once.calls = []
    client.set_transport(once)
    with pytest.raises(SecError) as caught:
        client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 1000)
    assert len(once.calls) == 1
    assert caught.value.kind == "http_429"
    assert "10 minutes" in caught.value.next_step
    assert "html" not in str(caught.value).lower()
    assert CONTACT not in str(caught.value)
    assert CONTACT not in caught.value.next_step

    def denied(url, contact):
        denied.calls += 1
        return 403, html.encode()

    denied.calls = 0
    client.set_transport(denied)
    with pytest.raises(SecError) as caught:
        client.fetch("https://www.sec.gov/files/company_tickers.json", 1000)
    assert denied.calls == 1
    assert caught.value.kind == "http_403"
    assert "no filings" not in str(caught.value).lower()


def test_other_http_errors_are_not_an_empty_list():
    use_contact()
    client.set_transport(lambda url, contact: (500, b"down"))
    with pytest.raises(SecError) as caught:
        client.get_json("https://data.sec.gov/submissions/CIK0000320193.json", 100)
    assert caught.value.kind == "http_error"
    assert "no filings" not in str(caught.value).lower()


def test_bad_json_and_oversize_do_not_look_like_zero():
    use_contact()
    client.set_transport(lambda url, contact: (200, b"not-json"))
    with pytest.raises(SecError) as caught:
        client.get_json("https://www.sec.gov/files/company_tickers.json", 100)
    assert caught.value.kind == "bad_json"
    client.set_transport(lambda url, contact: (200, b"12345"))
    with pytest.raises(SecError) as caught:
        client.fetch("https://www.sec.gov/files/company_tickers.json", 4)
    assert caught.value.kind == "too_large"


def test_budget_stops_and_the_gap_is_process_wide():
    use_contact()
    client.set_transport(lambda url, contact: (200, b"{}"))
    with client.call_budget(2):
        client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 10)
        client.fetch("https://www.sec.gov/files/company_tickers.json", 10)
        with pytest.raises(SecError) as caught:
            client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 10)
    assert caught.value.kind == "budget"
    slept = []
    client.GAP_SECONDS = 5
    client._sleep = slept.append
    client.reset_runtime()
    client.set_transport(lambda url, contact: (200, b"{}"))
    use_contact()
    client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 10)
    client.fetch("https://www.sec.gov/files/company_tickers.json", 10)
    assert slept and slept[0] > 0


def test_scrub_removes_the_url_and_the_contact():
    assert client.scrub("see https://data.sec.gov/a " + CONTACT, CONTACT) == "see <url> <contact>"


def test_contact_changes_are_read_on_the_next_call():
    seen = []

    def transport(url, contact):
        seen.append(contact)
        return 200, b"{}"

    client.set_transport(transport)
    use_contact(CONTACT)
    client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 10)
    use_contact(OTHER_CONTACT)
    client.fetch("https://www.sec.gov/files/company_tickers.json", 10)
    assert seen == [CONTACT, OTHER_CONTACT]


def test_network_error_hides_the_url(monkeypatch):
    use_contact()
    client._transport.set(None)

    def open_request(request, timeout):
        raise urllib.error.URLError("timed out talking to " + request.full_url)

    monkeypatch.setattr(client, "_opener", lambda: type("Op", (), {"open": staticmethod(open_request)})())
    with pytest.raises(SecError) as caught:
        client.fetch("https://data.sec.gov/submissions/CIK0000320193.json", 10)
    assert caught.value.kind == "network"
    assert caught.value.__cause__ is None
    assert "data.sec.gov" not in str(caught.value)
    text = json.dumps({"error": str(caught.value), "next": caught.value.next_step})
    assert CONTACT not in text
