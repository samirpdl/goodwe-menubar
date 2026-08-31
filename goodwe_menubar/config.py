"""Configuration loading/saving for the GoodWe menubar app.

Config lives at ~/.config/goodwe-menubar/config.json. Environment variables
GOODWE_HOST / GOODWE_PORT override the file when set (handy for testing).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Defaults double as the schema: only these keys are ever read from / written
# to disk, so unknown keys in a hand-edited file are ignored on load.
DEFAULTS: dict = {
    "host": "",            # inverter local IP; empty prompts on first run
    "port": 8899,          # 8899 = UDP (most GoodWe Wi-Fi/LAN dongles); 502 = Modbus TCP
    "family": None,        # None = auto-detect; or "ET" / "DT" / "ES" to force
    "comm_addr": 0,        # Modbus unit/slave address; 0 = library default per family
    "timeout": 1,          # seconds per request
    "retries": 3,          # retries per request
    "poll_interval": 10,   # seconds between reads
    "units": "kW",         # "kW" (auto kW/W) or "W" (always watts)
    "flip_meter_sign": False,  # flip if import/export appears reversed for your meter
    "wake_enabled": True,  # nudge the Wi-Fi dongle awake before (re)connecting
    "wake_port": 48899,    # UDP port for GoodWe's wake/discovery broadcast
    "wake_payload": "WIFIKIT-214028-READ",  # magic string the dongle listens for
    # --- Tariffs (for the daily usage/cost figures) ---
    "currency_symbol": "A$",
    "supply_charge": 1.3684,  # fixed charge per day (applies regardless of usage)
    "export_rate": 0.10,   # flat credit per kWh exported to the grid
    # Time-of-use import rates. Each window is [from, to) in local 24h time;
    # use "24:00" for midnight. Windows should cover the whole day.
    "import_tariff": [
        {"from": "00:00", "to": "06:00", "rate": 0.1365},
        {"from": "06:00", "to": "15:00", "rate": 0.2365},
        {"from": "15:00", "to": "21:00", "rate": 0.40392},
        {"from": "21:00", "to": "24:00", "rate": 0.2365},
    ],
}


def config_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "goodwe-menubar"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """Return a full config dict (defaults merged with the on-disk file)."""
    cfg = dict(DEFAULTS)
    path = config_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass  # fall back to defaults on a corrupt/unreadable file

    if os.environ.get("GOODWE_HOST"):
        cfg["host"] = os.environ["GOODWE_HOST"].strip()
    if os.environ.get("GOODWE_PORT", "").strip().isdigit():
        cfg["port"] = int(os.environ["GOODWE_PORT"])

    return cfg


def save_config(cfg: dict) -> None:
    """Write config atomically, keeping only known keys."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
