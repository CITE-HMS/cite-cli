"""Test-wide safety nets."""

import pytest
import requests

_REAL_NIKON_DOMAIN = "nikon-instruments.jp"


@pytest.fixture(autouse=True)
def _block_real_nikon_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard-fail any test that tries to POST to the real Nikon endpoint."""
    real_post = requests.post

    def guarded_post(url, *args, **kwargs):  # type: ignore[no-untyped-def]
        if _REAL_NIKON_DOMAIN in str(url):
            raise RuntimeError(
                f"Test attempted to hit the real Nikon endpoint: {url!r}. "
                "Use '--url test' (the mock_server fixture remaps it to a "
                "local mock)."
            )
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(requests, "post", guarded_post)
