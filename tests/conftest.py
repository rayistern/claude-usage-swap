"""Credential tests must stub HTTP explicitly, including recovery probes."""
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def incident_tests_never_contact_real_endpoints(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("test attempted unstubbed HTTP")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
