"""Append reviewer/pal corrections to the client's rules.yaml as exact merchant
rules (the flywheel). Skips rules already present."""
from __future__ import annotations

import yaml

from ..config import CLIENTS_DIR


def merge_rules(client: str, new_rules: dict) -> int:
    if not new_rules:
        return 0
    path = CLIENTS_DIR / client / "rules.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    rules = data.get("merchant_rules") or []
    existing = {(r.get("match", "").upper(), r.get("coa_line")) for r in rules}
    added = 0
    for merch, coa in new_rules.items():
        key = (merch.upper(), coa)
        if key in existing or not merch:
            continue
        rules.append({"match": merch, "kind": "exact", "coa_line": coa,
                      "source": "reviewer-correction"})
        existing.add(key)
        added += 1
    if added:
        data["merchant_rules"] = rules
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return added
