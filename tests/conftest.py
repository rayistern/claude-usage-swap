"""Credential tests must stub HTTP explicitly, including recovery probes."""
import http.client
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def incident_tests_never_contact_real_endpoints(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("test attempted unstubbed HTTP")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    # Also stop a connection opened below urlopen (http.client directly, or
    # an opener that bypasses urlopen) before any name lookup or socket.
    monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", blocked)
