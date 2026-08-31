"""The rumps menubar app: live display + settings, driven by the Poller."""

from __future__ import annotations

import socket
import subprocess
import time
from datetime import date, datetime, timedelta

import rumps

from .config import config_path, load_config, save_config
from .daily import summarize_range
from .poller import Poller
from . import tesla
from .tesla import SolarCharger

SUN = "☀︎"   # ☀ (text-style, avoids a giant colour emoji in the menubar)
HOUSE = "\U0001f3e0"   # 🏠
BOLT = "⚡︎"  # ⚡
MOON = "☾"   # shown when the inverter is idle/asleep
EXPORT = "↑"  # outgoing to grid
IMPORT = "↓"  # incoming from grid
IDLE = "↕"   # balanced / negligible

# Localhost port used purely as a single-instance lock (see _single_instance_lock).
_LOCK_PORT = 49222


def fmt_power(watts, units: str = "kW") -> str:
    """Format a wattage. units='W' forces watts; 'kW' auto-switches at 1 kW."""
    if watts is None:
        return "—"
    try:
        w = float(watts)
    except (TypeError, ValueError):
        return "—"
    if units == "W" or abs(w) < 1000:
        return f"{w:.0f} W"
    return f"{w / 1000:.2f} kW"


def fmt_energy(kwh) -> str:
    if kwh is None:
        return "—"
    try:
        return f"{float(kwh):.1f} kWh"
    except (TypeError, ValueError):
        return "—"


def fmt_money(value, symbol: str = "A$") -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{symbol}{value:.2f}"


def fmt_net(value, symbol: str = "A$") -> str:
    """Signed daily/period balance: positive = you pay, negative = credit."""
    if not isinstance(value, (int, float)):
        return "—"
    if value < -0.005:
        return f"{symbol}{abs(value):.2f} credit"
    return f"{symbol}{value:.2f} cost"


def _is_date(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def pick(data: dict, *keys):
    """First non-None value among the given sensor ids (handles model differences)."""
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


class GoodWeApp(rumps.App):
    def __init__(self):
        super().__init__("GoodWe", title=f"{SUN} …", quit_button=None)
        self._set_accessory_policy()

        self.config = load_config()
        self._asked_ip = False

        # Info rows (a no-op callback keeps the text crisp rather than greyed out).
        self.item_model = rumps.MenuItem("Connecting…", callback=self._noop)
        self.item_solar = rumps.MenuItem(f"{SUN} Solar (PV): —", callback=self._noop)
        self.item_house = rumps.MenuItem(f"{HOUSE} House load: —", callback=self._noop)
        self.item_grid = rumps.MenuItem(f"{BOLT} Grid: —", callback=self._noop)
        self.item_extra = rumps.MenuItem("—", callback=self._noop)
        # Daily figures.
        self.item_today = rumps.MenuItem("Today's generation: —", callback=self._noop)
        self.item_usage = rumps.MenuItem("Today's usage: —", callback=self._noop)
        self.item_import = rumps.MenuItem(f"   {IMPORT} Imported: —", callback=self._noop)
        self.item_export = rumps.MenuItem(f"   {EXPORT} Exported: —", callback=self._noop)
        self.item_supply = rumps.MenuItem("   + Supply charge: —", callback=self._noop)
        self.item_net = rumps.MenuItem("Net today: —", callback=self._noop)
        # Period aggregates.
        self.item_week = rumps.MenuItem("This week: —", callback=self._noop)
        self.item_month = rumps.MenuItem("This month: —", callback=self._noop)
        self.item_custom = rumps.MenuItem("Custom range…", callback=self.on_custom_range)
        self.item_status = rumps.MenuItem("Status: starting…", callback=self._noop)

        self.item_tesla = rumps.MenuItem("🚗 Tesla: —", callback=self._noop)
        self.item_tesla_stats = rumps.MenuItem("   —", callback=self._noop)
        # Constant title + checkmark, like "Flip grid direction" below: the
        # other clickable items here never mutate their titles.
        self.item_tesla_go = rumps.MenuItem("Solar charging",
                                            callback=self.on_tesla_toggle)

        self.item_flip = rumps.MenuItem("Flip grid direction", callback=self.on_flip)
        self.item_flip.state = 1 if self.config.get("flip_meter_sign") else 0

        self.menu = [
            self.item_model,
            None,
            self.item_solar,
            self.item_house,
            self.item_grid,
            self.item_extra,
            None,
            self.item_tesla,
            self.item_tesla_stats,
            self.item_tesla_go,
            self.item_today,
            self.item_usage,
            self.item_import,
            self.item_export,
            self.item_supply,
            self.item_net,
            None,
            self.item_week,
            self.item_month,
            self.item_custom,
            None,
            self.item_status,
            None,
            rumps.MenuItem("Refresh now", callback=self.on_refresh),
            rumps.MenuItem("Set inverter IP…", callback=self.on_set_ip),
            rumps.MenuItem("Connection port…", callback=self.on_set_port),
            rumps.MenuItem("Poll interval…", callback=self.on_set_interval),
            self.item_flip,
            rumps.MenuItem("Edit config file…", callback=self.on_edit_config),
            rumps.MenuItem("Reload config", callback=self.on_reload),
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        self.poller = Poller(self.config)
        self.poller.start()

        self.solar_charger = SolarCharger()
        self.ui_timer = rumps.Timer(self.update_ui, 1)
        self.ui_timer.start()

    # ---- helpers -------------------------------------------------------- #
    @staticmethod
    def _set_accessory_policy() -> None:
        """Hide the Dock icon so it lives only in the menubar, even from a terminal."""
        try:
            from AppKit import (
                NSApplication,
                NSApplicationActivationPolicyAccessory,
            )

            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except Exception:
            pass

    def _noop(self, _):
        pass

    def _prompt(self, title: str, message: str, default: str, width: int = 240) -> str | None:
        window = rumps.Window(
            title=title,
            message=message,
            default_text=str(default),
            ok="Save",
            cancel="Cancel",
            dimensions=(width, 22),
        )
        response = window.run()
        if response.clicked:
            return response.text.strip()
        return None

    # ---- menu callbacks ------------------------------------------------- #
    def on_refresh(self, _):
        self.poller.request_refresh()

    def on_set_ip(self, _):
        text = self._prompt(
            "GoodWe inverter IP",
            "Enter the inverter's local IP address (e.g. 192.168.1.50).\n"
            "Find it in your router's DHCP/client list or the SolarGo / PV Master app.",
            self.config.get("host", ""),
        )
        if text:
            self.config["host"] = text
            save_config(self.config)
            self.poller.update_config(host=text)

    def on_set_port(self, _):
        text = self._prompt(
            "Connection port",
            "8899 = UDP (default; most GoodWe Wi-Fi/LAN dongles)\n"
            "502 = Modbus TCP (use if your dongle exposes it).",
            self.config.get("port", 8899),
            width=120,
        )
        if text and text.isdigit():
            self.config["port"] = int(text)
            save_config(self.config)
            self.poller.update_config(port=int(text))

    def on_set_interval(self, _):
        text = self._prompt(
            "Poll interval",
            "Seconds between inverter reads (minimum 2).",
            self.config.get("poll_interval", 10),
            width=120,
        )
        if text and text.isdigit():
            interval = max(2, int(text))
            self.config["poll_interval"] = interval
            save_config(self.config)
            self.poller.update_config(poll_interval=interval)

    def on_tesla_toggle(self, sender):
        """Arm/disarm solar charging. Nothing touches the car until armed."""
        charger = self.solar_charger
        tesla._log(f"menu clicked: enabled={charger.enabled} "
                   f"armed={charger.armed}")
        if not charger.enabled:
            rumps.alert("Tesla charging not set up",
                        "Run ./setup-tesla.sh to pair this Mac with the car.")
            return
        if charger.armed:
            charger.disarm()
        else:
            charger.arm()
        sender.state = 1 if charger.armed else 0

    def on_flip(self, sender):
        flipped = not self.config.get("flip_meter_sign", False)
        self.config["flip_meter_sign"] = flipped
        save_config(self.config)
        sender.state = 1 if flipped else 0

    def on_edit_config(self, _):
        save_config(self.config)  # make sure the file exists before opening it
        subprocess.run(["open", "-t", str(config_path())], check=False)

    def on_reload(self, _):
        self.config = load_config()
        self.item_flip.state = 1 if self.config.get("flip_meter_sign") else 0
        self.poller.update_config(**self.config)

    def on_custom_range(self, _):
        today = date.today()
        default_start = (today - timedelta(days=6)).isoformat()
        start = self._prompt(
            "Custom range — start", "Start date (YYYY-MM-DD)", default_start, width=160
        )
        if not start:
            return
        end = self._prompt(
            "Custom range — end", "End date (YYYY-MM-DD)", today.isoformat(), width=160
        )
        if not end:
            return
        start, end = start.strip(), end.strip()
        if not (_is_date(start) and _is_date(end)):
            rumps.alert("GoodWe — Custom range", "Please use dates in YYYY-MM-DD format.")
            return
        if start > end:
            start, end = end, start

        agg = summarize_range(start, end, self.config)
        cur = agg.get("currency", "A$")
        message = (
            f"{start}  →  {end}   ({agg.get('days', 0)} days with data)\n\n"
            f"Generation:   {fmt_energy(agg.get('gen_kwh'))}\n"
            f"Usage:        {fmt_energy(agg.get('usage_kwh'))}\n"
            f"Imported:     {fmt_energy(agg.get('import_kwh'))}  ·  {fmt_money(agg.get('import_cost'), cur)}\n"
            f"Exported:     {fmt_energy(agg.get('export_kwh'))}  ·  {fmt_money(agg.get('export_credit'), cur)}\n"
            f"Supply:       {fmt_money(agg.get('supply_charge'), cur)}\n"
            f"Net:          {fmt_net(agg.get('net'), cur)}"
        )
        rumps.alert(title="GoodWe — Custom range", message=message)

    def on_quit(self, _):
        try:
            self.poller.stop()
        except Exception:
            pass
        rumps.quit_application()

    # ---- UI refresh ----------------------------------------------------- #
    def update_ui(self, _):
        # First launch with no IP saved: prompt once, then let the menu handle it.
        if not (self.config.get("host") or "").strip() and not self._asked_ip:
            self._asked_ip = True
            self.on_set_ip(None)
            return

        snap = self.poller.snapshot()
        status = snap["status"]
        data = snap["data"]
        device = snap["device"]

        model = device.get("model_name") or "GoodWe"
        serial = device.get("serial_number")
        self.item_model.title = f"{model}  ({serial})" if serial else model

        host = (self.config.get("host") or "").strip()

        if status in ("starting", "connecting"):
            self.title = f"{SUN} …"
            self.item_status.title = f"Status: connecting to {host or '—'}…"
            return

        if status == "error":
            self.title = f"⚠︎ {SUN}"
            self.item_status.title = f"Status: {snap['error']}"
            return

        if status == "asleep":
            self.title = f"{MOON} idle"
            self.item_status.title = f"Status: {snap['error'] or 'inverter idle / asleep'}"
            self.item_solar.title = f"{SUN} Solar (PV): idle (no production)"
            self.item_house.title = f"{HOUSE} House load: —"
            self.item_grid.title = f"{BOLT} Grid: —"
            return

        # status == ok
        units = self.config.get("units", "kW")
        solar = pick(data, "ppv", "pv_power")
        house = pick(data, "house_consumption")
        meter = pick(data, "meter_active_power_total", "meter_active_power")
        ac_out = pick(data, "active_power", "total_inverter_power")
        today = pick(data, "e_day")
        vgrid = pick(data, "vgrid", "vgrid1")
        fgrid = pick(data, "fgrid", "fgrid1")
        temp = pick(data, "temperature")

        if meter is not None and self.config.get("flip_meter_sign"):
            meter = -float(meter)
        self.solar_charger.tick(meter, snap["last_update"])
        self.item_tesla.title = f"🚗 Tesla: {self.solar_charger.status}"
        self.item_tesla_stats.title = f"   {self.solar_charger.detail or '—'}"
        self.item_tesla_go.state = 1 if self.solar_charger.armed else 0

        # Menubar title: solar, house and live grid direction at a glance.
        parts = [f"{SUN} {fmt_power(solar, units)}"]
        if house is not None:
            parts.append(f"{HOUSE} {fmt_power(house, units)}")
        grid_seg = self._grid_segment(meter, units)
        if grid_seg:
            parts.append(grid_seg)
        if self.solar_charger.amps:
            parts.append(f"🚗 {self.solar_charger.amps}A")
        self.title = "  ".join(parts)

        # Dropdown rows.
        self.item_solar.title = f"{SUN} Solar (PV): {fmt_power(solar, units)}"
        self.item_house.title = (
            f"{HOUSE} House load: {fmt_power(house, units)}"
            if house is not None
            else f"{HOUSE} House load: — (needs a GoodWe smart meter)"
        )
        self.item_grid.title = self._grid_label(meter, units)
        self.item_today.title = f"Today's generation: {fmt_energy(today)}"

        # Daily usage & cost (today) + weekly/monthly aggregates.
        daily = snap.get("daily") or {}
        week = snap.get("week") or {}
        month = snap.get("month") or {}
        cur = daily.get("currency") or self.config.get("currency_symbol", "A$")
        self.item_usage.title = f"Today's usage: {fmt_energy(daily.get('usage_kwh'))}"
        self.item_import.title = (
            f"   {IMPORT} Imported: {fmt_energy(daily.get('import_kwh'))}"
            f"  ·  {fmt_money(daily.get('import_cost'), cur)}"
        )
        self.item_export.title = (
            f"   {EXPORT} Exported: {fmt_energy(daily.get('export_kwh'))}"
            f"  ·  {fmt_money(daily.get('export_credit'), cur)}"
        )
        self.item_supply.title = f"   + Supply charge: {fmt_money(daily.get('supply_charge'), cur)}"
        self.item_net.title = f"Net today:  {fmt_net(daily.get('net'), cur)}"
        self.item_week.title = (
            f"This week:  {fmt_energy(week.get('usage_kwh'))}  ·  {fmt_net(week.get('net'), cur)}"
        )
        self.item_month.title = (
            f"This month:  {fmt_energy(month.get('usage_kwh'))}  ·  {fmt_net(month.get('net'), cur)}"
        )

        extras = []
        if ac_out is not None:
            extras.append(f"AC out {fmt_power(ac_out, units)}")
        if vgrid is not None:
            extras.append(f"{float(vgrid):.0f} V")
        if fgrid is not None:
            extras.append(f"{float(fgrid):.2f} Hz")
        if temp is not None:
            extras.append(f"{float(temp):.0f}°C")
        self.item_extra.title = "   ·   ".join(extras) if extras else "—"

        last = snap["last_update"]
        age = int(time.time() - last) if last else None
        self.item_status.title = (
            f"Status: ok · updated {age}s ago" if age is not None else "Status: ok"
        )

    @staticmethod
    def _grid_label(meter, units: str) -> str:
        if meter is None:
            return f"{BOLT} Grid: — (needs a GoodWe smart meter)"
        value = float(meter)
        if value > 20:
            return f"{BOLT} Grid: {EXPORT} exporting {fmt_power(value, units)}"
        if value < -20:
            return f"{BOLT} Grid: {IMPORT} importing {fmt_power(-value, units)}"
        return f"{BOLT} Grid: idle"

    @staticmethod
    def _grid_segment(meter, units: str):
        """Compact grid segment for the menubar title: ↑/↓ + value."""
        if meter is None:
            return None
        value = float(meter)
        if value > 20:
            return f"{EXPORT} {fmt_power(value, units)}"
        if value < -20:
            return f"{IMPORT} {fmt_power(-value, units)}"
        return f"{IDLE} {fmt_power(0, units)}"


def _single_instance_lock():
    """Bind a localhost port as a lock so only one menubar instance runs.

    Returns the bound socket (keep a reference for the process lifetime) or
    None if another instance already holds it. The OS frees the port if the
    process dies, so there is no stale lock to clean up.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", _LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def main():
    lock = _single_instance_lock()
    if lock is None:
        print("GoodWe menubar is already running — exiting.")
        return
    app = GoodWeApp()
    app._instance_lock = lock  # keep the socket alive for the whole process
    app.run()


if __name__ == "__main__":
    main()
