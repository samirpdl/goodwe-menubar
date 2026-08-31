"""Background poller that talks to the inverter over GoodWe's Modbus protocol.

All network I/O runs on a dedicated thread with its own asyncio event loop, so
the rumps/AppKit main thread never blocks. The main thread reads the latest
reading via ``snapshot()`` (cheap, lock-guarded) and pushes config changes via
``update_config()``.

The ``goodwe`` library handles the Modbus framing: ``connect()`` picks UDP
(port 8899) or Modbus TCP (port 502) based on the port, auto-detects the
inverter family, and ``read_runtime_data()`` returns a dict keyed by sensor id.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import goodwe

from .daily import EnergyTracker

# GoodWe Wi-Fi dongles sleep when the inverter is idle (e.g. at night) and stop
# answering Modbus. A UDP broadcast of this magic string on port 48899 rouses
# them — the same trick used in Home Assistant's shell_command.
WAKE_PORT = 48899
WAKE_PAYLOAD = "WIFIKIT-214028-READ"


def wake_dongle(host: str, port: int = WAKE_PORT, payload: str = WAKE_PAYLOAD) -> None:
    """Best-effort UDP wake/discovery broadcast (mirrors the HA nc command).

    Sent both as a broadcast and directly to the inverter, since some networks
    drop broadcast traffic. All socket errors are swallowed.
    """
    data = payload.encode("ascii", "ignore")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1)
        for target in ("255.255.255.255", host):
            if not target:
                continue
            try:
                sock.sendto(data, (target, port))
            except OSError:
                pass
        sock.close()
    except OSError:
        pass


class Poller:
    def __init__(self, config: dict):
        self._config = dict(config)
        self._lock = threading.Lock()

        # Shared state (read by the main thread via snapshot()).
        self._latest: dict = {}
        self._device: dict = {}
        self._status = "starting"   # starting | connecting | ok | error
        self._error = ""
        self._last_update = 0.0
        self._daily = None          # today's usage/cost summary
        self._week = None           # this week's aggregate
        self._month = None          # this month's aggregate
        self._tracker = EnergyTracker(dict(config))

        # Thread / loop plumbing.
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stop_flag = False
        self._reconnect_flag = False

    # ------------------------------------------------------------------ #
    # Public API (main thread)
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="goodwe-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag = True
        self._signal()

    def update_config(self, **changes) -> None:
        """Apply config changes and force a reconnect with the new settings."""
        with self._lock:
            self._config.update(changes)
        self._reconnect_flag = True
        self._signal()

    def request_refresh(self) -> None:
        """Wake the poll loop so it reads immediately instead of after the sleep."""
        self._signal()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._status,
                "error": self._error,
                "data": dict(self._latest),
                "device": dict(self._device),
                "last_update": self._last_update,
                "daily": dict(self._daily) if self._daily else None,
                "week": dict(self._week) if self._week else None,
                "month": dict(self._month) if self._month else None,
            }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass  # loop already closed

    def _get_cfg(self) -> dict:
        with self._lock:
            return dict(self._config)

    def _set_status(self, status: str, error: str = "") -> None:
        with self._lock:
            self._status = status
            self._error = error

    def _store_device(self, inverter) -> None:
        device = {
            "model_name": getattr(inverter, "model_name", None),
            "serial_number": getattr(inverter, "serial_number", None),
            "rated_power": getattr(inverter, "rated_power", None),
            "firmware": getattr(inverter, "firmware", None),
        }
        with self._lock:
            self._device = device

    def _store_data(self, data: dict, daily=None, week=None, month=None) -> None:
        with self._lock:
            self._latest = dict(data)
            if daily is not None:
                self._daily = daily
            if week is not None:
                self._week = week
            if month is not None:
                self._month = month
            self._status = "ok"
            self._error = ""
            self._last_update = time.time()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._set_status("error", f"poller crashed: {exc}")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        backoff = 2
        ever_connected = False

        while not self._stop_flag:
            cfg = self._get_cfg()
            host = (cfg.get("host") or "").strip()
            if not host:
                self._set_status("error", "No inverter IP set — use “Set inverter IP…”.")
                await self._sleep(2)
                continue

            # Rouse a sleeping dongle before (re)connecting.
            if cfg.get("wake_enabled", True):
                wake_dongle(host, int(cfg.get("wake_port", WAKE_PORT)),
                            str(cfg.get("wake_payload", WAKE_PAYLOAD)))
                await asyncio.sleep(0.6)

            self._set_status("connecting")
            try:
                inverter = await goodwe.connect(
                    host=host,
                    port=int(cfg.get("port", 8899)),
                    family=cfg.get("family") or None,
                    comm_addr=int(cfg.get("comm_addr", 0)),
                    timeout=int(cfg.get("timeout", 1)),
                    retries=int(cfg.get("retries", 3)),
                )
            except Exception as exc:
                if ever_connected:
                    # We've talked to it before, so a drop almost always means the
                    # inverter went idle/asleep rather than a real misconfig.
                    self._set_status(
                        "asleep", "No response — inverter may be idle/asleep. Retrying…"
                    )
                else:
                    self._set_status(
                        "error",
                        f"Can't reach {host}:{cfg.get('port')} — check it's powered on "
                        f"and on this network. ({_short(exc)})",
                    )
                backoff = min(backoff * 2, 30)
                await self._sleep(backoff)
                continue

            backoff = 2
            ever_connected = True
            self._store_device(inverter)
            self._reconnect_flag = False

            while not self._stop_flag and not self._reconnect_flag:
                try:
                    data = await inverter.read_runtime_data()
                except Exception:
                    self._set_status(
                        "asleep", "No response — inverter may be idle/asleep. Retrying…"
                    )
                    await self._sleep(3)
                    break  # drop out to reconnect (and re-wake the dongle)
                daily = week = month = None
                try:
                    self._tracker.set_config(self._get_cfg())
                    daily = self._tracker.update(
                        data.get("meter_e_total_imp"),
                        data.get("meter_e_total_exp"),
                        data.get("e_day"),
                    )
                    week = self._tracker.summary_week()
                    month = self._tracker.summary_month()
                except Exception:
                    pass
                self._store_data(data, daily, week, month)
                interval = max(2, int(self._get_cfg().get("poll_interval", 10)))
                await self._sleep(interval)

    async def _sleep(self, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early on stop/reconnect/refresh."""
        assert self._wake is not None
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def _short(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return msg if len(msg) <= 80 else msg[:77] + "…"
