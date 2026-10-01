"""Fixtures shared by the test modules."""

import pytest


@pytest.fixture
def no_sleep(monkeypatch):
    """Record the client's sleeps instead of taking them; returns the list of waits."""
    slept = []
    monkeypatch.setattr("image2ppt.client.time.sleep", slept.append)
    return slept
