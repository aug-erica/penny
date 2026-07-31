"""Currency detection + conversion (FX API mocked — no network in tests)."""
import httpx

from cfo_agent.engine import fx


def test_detect_currency():
    assert fx.detect_currency("these are all in CAD") == "CAD"
    assert fx.detect_currency("C$40 for parking") == "CAD"
    assert fx.detect_currency("€20 lunch") == "EUR"
    assert fx.detect_currency("£12 tube") == "GBP"
    assert fx.detect_currency("just $40 for lunch") is None


def test_to_usd_passthrough_usd():
    assert fx.to_usd_cents(5000, "USD", "2026-07-15") == (5000, 1.0)


def test_to_usd_converts(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rates": {"USD": 0.73}}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    usd, rate = fx.to_usd_cents(1479, "CAD", "2026-07-15")
    assert usd == round(1479 * 0.73) and rate == 0.73


def test_to_usd_failure_degrades(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "get", boom)
    assert fx.to_usd_cents(1479, "CAD", "2026-07-15") == (None, None)


def test_to_usd_none_amount():
    assert fx.to_usd_cents(None, "CAD", "2026-07-15") == (None, None)
