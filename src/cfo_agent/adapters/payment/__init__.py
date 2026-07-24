"""Payment rails for reimbursements. `build_rail(cfg)` constructs the configured
rail (client-agnostic factory, mirrors cli._card_feed)."""
from __future__ import annotations


def build_rail(cfg):
    t = (cfg.section("reimbursements").get("payment", {}) or {}).get(
        "type", "justworks_manual")
    if t == "justworks_manual":
        from .justworks_manual import JustworksManual
        return JustworksManual(cfg)
    raise ValueError(f"unknown reimbursement payment rail: {t!r}")
