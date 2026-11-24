import pytest

import http_client
from http_client import FetchError, HttpClient
from settings import Secrets


class Resp:
    def __init__(self, status, text="ok", headers=None, url="https://x.test/"):
        self.status_code, self.text, self.headers, self.url = status, text, headers or {}, url


class Session:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def request(self, method, url, headers=None, **kw):
        self.calls.append((url, headers, kw.get("timeout")))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)


def client(items, **cfg):
    base = {"max_retries": 2, "min_delay_seconds": 0, "max_delay_seconds": 0, "timeout_seconds": 25}
    base.update(cfg)
    secrets = Secrets(extra_headers={"X-Test": "1"})
    return HttpClient(base, secrets, session=Session(items))


def test_success_and_extra_headers():
    c = client([Resp(200, "hello")])
    assert c.get("https://a.test/x").text == "hello"
    url, headers, timeout = c.session.calls[0]
    assert headers["X-Test"] == "1" and "Accept-Language" in headers
    assert timeout == (10.0, 25.0)


def test_retry_on_429_then_ok():
    c = client([Resp(429, headers={"Retry-After": "1"}), Resp(200, "ok")])
    assert c.get("https://a.test/").text == "ok"
    assert len(c.session.calls) == 2


def test_403_not_retried():
    c = client([Resp(403)])
    with pytest.raises(FetchError) as exc:
        c.get("https://a.test/")
    assert exc.value.status == 403 and c.stats["blocked"] == 1
    assert len(c.session.calls) == 1


def test_network_errors_mark_host_down_until_next_run():
    c = client([OSError("reset"), OSError("reset"), OSError("reset"), Resp(200, "back")])
    with pytest.raises(FetchError):
        c.get("https://down.test/a")
    with pytest.raises(FetchError):
        c.get("https://down.test/b")          # ignoré sans nouvelle requête
    assert len(c.session.calls) == 3
    c.reset_run()
    assert c.get("https://down.test/c").text == "back"
