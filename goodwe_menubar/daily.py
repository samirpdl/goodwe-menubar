"""Energy & cost tracking: daily / weekly / monthly / custom ranges.

The smart meter reports lifetime cumulative kWh (meter_e_total_imp /
meter_e_total_exp). We persist the value at the start of each local day plus a
running cost, so per-day import/export = (current - start-of-day). Each day is
written to a history file keyed by date, so any range can be summed.

Import cost uses a time-of-use tariff: each incremental bit of imported energy
is charged at whatever rate is active at that moment. Export credit is flat.
House usage = (generation - export) + import.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from .config import config_dir

HISTORY_FILE = "energy_history.json"
# Ignore implausible single-step jumps for *cost* (meter reset / long downtime);
# daily kWh still uses the robust start-of-day delta.
_MAX_DELTA_KWH = 2.0


def _hhmm(text: str) -> int:
    h, m = str(text).split(":")
    return int(h) * 60 + int(m)  # "24:00" -> 1440, "00:00" -> 0


def import_rate(now_min: int, tariff) -> float:
    """Return the import rate active at now_min (minutes since midnight)."""
    for period in tariff or ():
        start = _hhmm(period.get("from", "00:00"))
        end = _hhmm(period.get("to", "24:00")) or 1440
        if start <= now_min < end:
            return float(period.get("rate", 0.0))
    return 0.0


def _history_path():
    return config_dir() / HISTORY_FILE


def load_history() -> dict:
    try:
        with open(_history_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("state", {})
            data.setdefault("days", {})
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"state": {}, "days": {}}


def aggregate(records, currency: str = "A$") -> dict:
    """Sum a list of day-records into one summary."""
    records = list(records)

    def total(key):
        return sum(float(r.get(key) or 0.0) for r in records)

    import_cost = total("import_cost")
    export_credit = total("export_credit")
    supply_charge = total("supply_charge")
    return {
        "import_kwh": total("import_kwh"),
        "export_kwh": total("export_kwh"),
        "import_cost": import_cost,
        "export_credit": export_credit,
        "supply_charge": supply_charge,
        "net": import_cost + supply_charge - export_credit,
        "usage_kwh": total("usage_kwh"),
        "gen_kwh": total("gen_kwh"),
        "days": len(records),
        "currency": currency,
    }


def summarize_range(start: str, end: str, cfg: dict) -> dict:
    """Aggregate stored days with start <= date <= end (ISO strings).

    Reads the history file fresh, so it's safe to call from the UI thread.
    """
    days = load_history().get("days", {})
    recs = [r for d, r in days.items() if start <= d <= end]
    return aggregate(recs, cfg.get("currency_symbol", "A$"))


class EnergyTracker:
    """Live accumulator, driven from the poller thread (one update per poll)."""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        hist = load_history()
        self._state = hist.get("state", {}) or {}
        self._days = hist.get("days", {}) or {}

    def set_config(self, cfg: dict):
        self._cfg = cfg

    def _save(self):
        try:
            path = _history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"state": self._state, "days": self._days}, fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def update(self, meter_imp, meter_exp, e_day, now=None) -> dict:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        s = self._state

        if s.get("date") != today:
            # New local day (or first run): fresh accumulators.
            s = {"date": today, "import_cost": 0.0}
            self._state = s

        # Lazily set start-of-day baselines once meter values arrive.
        if meter_imp is not None and "imp_start" not in s:
            s["imp_start"] = meter_imp
            s["last_imp"] = meter_imp
        if meter_exp is not None and "exp_start" not in s:
            s["exp_start"] = meter_exp
            s["last_exp"] = meter_exp

        # Handle a meter counter reset (current < baseline).
        if meter_imp is not None and meter_imp < s.get("imp_start", meter_imp):
            s["imp_start"] = meter_imp
        if meter_exp is not None and meter_exp < s.get("exp_start", meter_exp):
            s["exp_start"] = meter_exp

        # Accumulate import cost at the current time-of-use rate.
        if meter_imp is not None and "last_imp" in s:
            d_imp = meter_imp - s["last_imp"]
            if 0 < d_imp <= _MAX_DELTA_KWH:
                rate = import_rate(now.hour * 60 + now.minute,
                                   self._cfg.get("import_tariff", []))
                s["import_cost"] = s.get("import_cost", 0.0) + d_imp * rate
            if d_imp != 0:
                s["last_imp"] = meter_imp
        if meter_exp is not None:
            s["last_exp"] = meter_exp

        rec = self._today_record(meter_imp, meter_exp, e_day)
        self._days[today] = rec
        self._save()
        return self._summary(rec)

    def _today_record(self, meter_imp, meter_exp, e_day):
        s = self._state
        imp_start, exp_start = s.get("imp_start"), s.get("exp_start")
        cur_imp = meter_imp if meter_imp is not None else s.get("last_imp")
        cur_exp = meter_exp if meter_exp is not None else s.get("last_exp")
        export_rate = float(self._cfg.get("export_rate", 0.0))

        import_kwh = (max(0.0, cur_imp - imp_start)
                      if cur_imp is not None and imp_start is not None else None)
        export_kwh = (max(0.0, cur_exp - exp_start)
                      if cur_exp is not None and exp_start is not None else None)
        export_credit = export_kwh * export_rate if export_kwh is not None else None
        usage_kwh = None
        if e_day is not None and export_kwh is not None and import_kwh is not None:
            usage_kwh = max(0.0, e_day - export_kwh) + import_kwh

        return {
            "import_kwh": import_kwh,
            "export_kwh": export_kwh,
            "import_cost": s.get("import_cost", 0.0),
            "export_credit": export_credit,
            "supply_charge": float(self._cfg.get("supply_charge", 0.0)),
            "gen_kwh": e_day,
            "usage_kwh": usage_kwh,
        }

    def _summary(self, rec):
        cur = self._cfg.get("currency_symbol", "A$")
        if not rec:
            return {"currency": cur}
        out = dict(rec)
        ic = rec.get("import_cost") or 0.0
        ec = rec.get("export_credit") or 0.0
        sc = rec.get("supply_charge") or 0.0
        out["net"] = ic + sc - ec
        out["currency"] = cur
        return out

    # --- period aggregates (poller thread) ---
    def summary_week(self, now=None):
        d = (now or datetime.now()).date()
        monday = d - timedelta(days=d.weekday())
        return self._between(monday.isoformat(), d.isoformat())

    def summary_month(self, now=None):
        d = (now or datetime.now()).date()
        return self._between(d.replace(day=1).isoformat(), d.isoformat())

    def _between(self, start, end):
        recs = [r for d, r in self._days.items() if start <= d <= end]
        return aggregate(recs, self._cfg.get("currency_symbol", "A$"))
