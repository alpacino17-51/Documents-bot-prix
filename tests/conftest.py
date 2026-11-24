import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from http_client import FetchError, FetchResult  # noqa: E402
from settings import load_config  # noqa: E402


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def idealo_search_html() -> str:
    """Reproduit la page de recherche Idealo : résultats JSON encodés en base64 dans un script."""
    payload = base64.b64encode(read_fixture("idealo_search_result.json").encode("utf-8")).decode("ascii")
    return (
        "<!DOCTYPE html><html><body><div data-searchterm=\"sony wh-1000xm5\"></div>"
        "<script data-csr-component=\"SearchResultNext\">\n"
        "window.csr = window.csr || {};\nwindow.csr.initialData = window.csr.initialData || {};\n"
        f"window.csr.initialData['searchResult'] = \"{payload}\";\n</script></body></html>"
    )


class FakeHttp:
    """Remplace HttpClient : associe un fragment d'URL à une réponse (texte, dict JSON ou code d'erreur)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []
        self.stats = {"requests": 0, "errors": 0, "blocked": 0}

    def _match(self, url: str):
        for fragment, response in self.routes.items():
            if fragment in url:
                return fragment, response
        raise FetchError(url, "HTTP 404 (route de test absente)", 404)

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        self.stats["requests"] += 1
        _, response = self._match(url)
        if isinstance(response, int):
            raise FetchError(url, f"HTTP {response}", response)
        final_url = url
        if isinstance(response, tuple):          # (url_finale, texte) pour simuler une redirection
            final_url, response = response
        text = json.dumps(response) if isinstance(response, (dict, list)) else response
        return FetchResult(url=final_url, status=200, text=text, headers={})

    def get_json(self, url: str, **kwargs):
        return json.loads(self.get(url, **kwargs).text)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.json")


@pytest.fixture
def fixture_text():
    return read_fixture
