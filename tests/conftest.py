"""Test wiring: import the flat server module and stand in for the e-Gov API."""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from tests import fixtures  # noqa: E402


@pytest.fixture(autouse=True)
def clear_caches():
    """Caches are process-global, so tests would otherwise leak into each other."""
    server.RESOLVE_LAW_CACHE.clear()
    server.LAW_XML_CACHE.clear()
    server.ELEMENT_CACHE.clear()
    yield


@pytest.fixture
def egov(requests_mock):
    """Serve the fixture statute for every e-Gov endpoint the server calls.

    ``requests_mock.request_history`` is left intact so tests can assert on the
    exact ``elm`` path that was requested.
    """

    def law_data(request, context):
        # requests_mock lower-cases request.path/.qs, which would mangle law ids
        # and elm paths, so the original URL is parsed directly.
        parts = urlsplit(request.url)
        key = unquote(parts.path.rsplit("/", 1)[-1])
        if key not in (fixtures.LAW_ID, fixtures.LAW_NUM) and "_" not in key:
            context.status_code = 404
            return fixtures.NOT_FOUND_BODY
        elm = parse_qs(parts.query).get("elm", [""])[0]
        xml = fixtures.ELEMENTS.get(elm)
        if xml is None:
            context.status_code = 400
            return fixtures.NO_ELEMENT_BODY
        return fixtures.law_data_response(xml)

    def laws(request, context):
        title = parse_qs(urlsplit(request.url).query).get("law_title", [""])[0]
        if title and title not in (fixtures.LAW_TITLE, "労働", "労働基準"):
            context.status_code = 404
            return fixtures.NOT_FOUND_BODY
        return fixtures.LAWS_RESPONSE

    base = re.escape(server.EGOV_BASE)
    requests_mock.get(re.compile(rf"{base}/law_data/"), json=law_data)
    requests_mock.get(re.compile(rf"{base}/law_revisions/"),
                      json=fixtures.REVISIONS_RESPONSE)
    requests_mock.get(re.compile(rf"{base}/laws"), json=laws)
    requests_mock.get(re.compile(rf"{base}/keyword"), json=fixtures.KEYWORD_RESPONSE)
    return requests_mock
