"""Client config loading. The engine reads config only through ClientConfig —
client specifics never appear in engine or adapter code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CLIENTS_DIR = Path(__file__).resolve().parents[2] / "clients"


@dataclass
class ClientConfig:
    client: str
    raw: dict
    coa: dict
    rules: dict

    @property
    def data_root(self) -> Path:
        return Path(self.raw["data_root"])

    @property
    def close_data_dir(self) -> Path:
        return self.data_root / self.raw["close_data_dir"]

    @property
    def runs_dir(self) -> Path:
        return self.data_root / self.raw["runs_dir"]

    @property
    def coa_lines(self) -> list:
        return list(self.coa.get("coa_lines", []))

    def section(self, name: str) -> dict:
        return dict(self.raw.get(name, {}))


def load_client(client: str) -> ClientConfig:
    base = CLIENTS_DIR / client
    if not base.is_dir():
        raise FileNotFoundError(f"No client config at {base}")
    raw = yaml.safe_load((base / "client.yaml").read_text())
    coa = yaml.safe_load((base / "coa.yaml").read_text()) or {}
    rules_path = base / "rules.yaml"
    rules = yaml.safe_load(rules_path.read_text()) if rules_path.exists() else {}
    return ClientConfig(client=client, raw=raw, coa=coa, rules=rules or {})


def env(name: str) -> str:
    """Read a credential from the environment / .env file (never from config)."""
    val = os.environ.get(name, "")
    if not val:
        envfile = Path(__file__).resolve().parents[2] / ".env"
        if envfile.exists():
            for line in envfile.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip()
                    break
    return val
