"""Solar-surplus Tesla charging over Bluetooth.

Drives `tesla-control` from Tesla's vehicle-command SDK — local BLE, no cloud,
no API fees, no Tesla developer account. The Mac must stay within ~10m of the
car. Run ./setup-tesla.sh once to install tesla-control, generate a key pair and
enrol it in the car. Without TESLA_VIN and TESLA_KEY_FILE in the environment
this module does nothing at all.

Control law: target = what the car is ACTUALLY drawing right now, plus however
many amps' worth we're exporting. Steering off the measured current rather than
our own last command means taper, charge limits and a driver poking the app in
the meantime all self-correct on the next tick.

Sleep policy: talking to the car over BLE keeps it awake, so this only does it
when there is something to gain. No surplus, no contact — which means zero
traffic overnight and on dull days. Once the car is known to be unplugged or
finished, it backs off to a half-hourly check, and `wake` is rate-limited hard.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

VOLTS = 240          # fallback only — the car reports its actual charger voltage
MIN_AMPS = 8         # don't bother charging below this — the car draws it
                     # inefficiently and it isn't worth the grid risk (5A is
                     # the car's own hard floor)
# Hard ceiling, in amps the charger is rated to deliver continuously. The car
# also reports its EVSE pilot limit and we always take the lower of the two.
MAX_AMPS = 15        # 15A charger
DEADBAND_W = 150     # ignore small imbalances so it stops fidgeting
BUFFER_W = 150       # aim to keep this much export in hand rather than eating
                     # every last watt — the kettle, the oven and a passing
                     # cloud all land faster than a BLE round-trip can answer
DECIDE_EVERY = 30    # seconds between decisions while actively charging
CHECK_EVERY = 20     # seconds between samples while waiting to start
IDLE_RECHECK = 1800  # unplugged / finished: only look again every 30 min
WAKE_COOLDOWN = 900  # at most one *successful* wake per 15 min
WAKE_SETTLE = 5      # seconds for infotainment to come up after a wake
URGENT_W = 500       # importing more than this while charging: don't sit out
URGENT_EVERY = 15    # the rest of DECIDE_EVERY, come back this soon instead
SUSTAINED = 3        # *distinct* meter readings of steady surplus before we
                     # disturb a sleeping car — so this is three inverter
                     # refreshes, not three UI ticks
STOP_AFTER = 60      # seconds of continuous grid import before we cut the
                     # charge outright (cloud, kettle, oven). Must span at
                     # least a couple of meter refreshes, or it fires on a
                     # reading that predates our own last amps change.
# How stale the car's own reported state may get while we are steering a
# charge. Every read is a BLE round-trip — the expensive, failure-prone part —
# so between reads we track amps from what we commanded and only touch the car
# when the target actually moves. A charge that finishes, unplugs or hits its
# limit goes unnoticed for at most this long, which the meter-driven STOP_AFTER
# cut-off covers in the meantime.
STATE_EVERY = 900    # 15 min
# tesla-control defaults to a 5s command timeout, which a car at the end of the
# driveway routinely misses — that's what "context deadline exceeded" is. Give
# it room; the 30s import cut-off is timed off the meter, not off BLE, so a
# slow read no longer delays the thing that protects the bill.
CMD_TIMEOUT = "15s"    # -command-timeout: per command, once connected
CONNECT_TIMEOUT = "30s"  # -connect-timeout: BLE link-up, default 20s is thin
                         # for a car at the end of the driveway
BLE_TIMEOUT = 75       # our own backstop; tesla-control's own timeouts (connect
                       # + command, above) should fire first and say why
RETRY_PAUSE = 3      # seconds before one retry of a dropped BLE link

# Below this there isn't enough sun to charge at even the minimum current, so
# there is no reason to wake the car up and ask.
START_W = MIN_AMPS * VOLTS + BUFFER_W

# Arming starts looking for sun immediately; the automation switches itself off
# at WINDOW_END rather than running into the evening.
WINDOW_END = 15
# Armed, in-window, but no usable surplus for this long: done for the day.
GIVE_UP_AFTER = 3600

# A GUI-launched app inherits a bare PATH, so ~/go/bin (where `go install` puts
# it) is invisible. Resolve once at import and fall back to the usual spot.
TESLA_CONTROL = (shutil.which("tesla-control")
                 or os.path.expanduser("~/go/bin/tesla-control"))

# `tesla-control state charge` prints protojson: camelCase keys, zero-valued
# fields omitted entirely, and enums that may render either as a bare string or
# as a oneof wrapper object. Rather than pin down which, match on the raw text.
CONNECTED_STATES = ("Charging", "Stopped", "Complete", "Starting",
                    "NoPower", "Calibrating")


def _local_hour() -> int:
    return time.localtime().tm_hour


def _log(msg: str) -> None:
    """launchd captures stdout into ~/Library/Logs/goodwe-menubar.log."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [tesla] {msg}", flush=True)


def _num(blob: str, snake: str) -> float:
    """Pull a numeric field, accepting either protojson spelling.

    Handles floats: charge_energy_added is one, and an int-only pattern would
    match its leading digits or miss it entirely."""
    camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), snake)
    m = re.search(rf'"(?:{snake}|{camel})"\s*:\s*"?(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)', blob)
    return float(m.group(1)) if m else 0.0


def _int(blob: str, snake: str) -> int:
    return int(_num(blob, snake))


def charge_summary(car: dict) -> str:
    """One line of human-readable charging stats for the menu."""
    bits = []
    if car["battery"]:
        bits.append(f"{car['battery']}%" +
                    (f" → {car['limit']}%" if car["limit"] else ""))
    if car["added_kwh"]:
        bits.append(f"+{car['added_kwh']:.1f} kWh")
    if car["power_kw"]:
        bits.append(f"{car['power_kw']} kW")
    if car["charging"] and car["minutes_left"]:
        h, m = divmod(car["minutes_left"], 60)
        bits.append(f"{h}h{m:02d}m left" if h else f"{m}m left")
    return " · ".join(bits)


def parse_charge_state(blob: str) -> dict:
    """Turn the protojson blob into the things the control loop needs."""
    volts = _int(blob, "charger_voltage")
    car_max = _int(blob, "charge_current_request_max") or _int(blob, "charger_pilot_current")
    battery = _int(blob, "battery_level")
    limit = _int(blob, "charge_limit_soc")
    return {
        "battery": battery,
        "limit": limit,
        # Already at the driver's set limit: nothing to gain by starting, and
        # the car would just refuse or immediately stop.
        "at_limit": bool(battery and limit and battery >= limit),
        "added_kwh": _num(blob, "charge_energy_added"),
        "power_kw": _int(blob, "charger_power"),
        "minutes_left": _int(blob, "minutes_to_full_charge"),
        "connected": any(f'"{s}"' in blob for s in CONNECTED_STATES),
        "charging": '"Charging"' in blob or '"Starting"' in blob,
        "complete": '"Complete"' in blob,
        "amps": _int(blob, "charger_actual_current"),
        # Never let the car's idea of its limit exceed the breaker's.
        "max_amps": min(car_max, MAX_AMPS) if car_max else MAX_AMPS,
        "volts": volts if 90 < volts < 300 else VOLTS,
    }


def next_amps(prev: int, export_w: float, ceiling: int = MAX_AMPS,
              volts: int = VOLTS) -> int:
    """Amps to command next, given what's currently going to the grid.

    Positive export means headroom, negative means we're pulling from the grid.
    Steers towards leaving BUFFER_W exported rather than towards zero, so the
    usual household steps don't put us into import before we can react.
    Truncating toward zero keeps both directions from overshooting."""
    export_w -= BUFFER_W
    if abs(export_w) < DEADBAND_W:
        return prev
    return max(0, min(ceiling, prev + int(export_w / volts)))


class SolarCharger:
    def __init__(self):
        self.enabled = bool(os.environ.get("TESLA_VIN")
                            and os.environ.get("TESLA_KEY_FILE")
                            and os.path.exists(TESLA_CONTROL))
        # Nothing happens until you arm it from the menu. Deliberately not
        # persisted: a new day is a new decision.
        self.armed = False
        self.status = "idle" if self.enabled else "off"
        self.detail = ""        # battery / energy stats for the menu
        self.amps = 0           # live charge current, 0 when not charging
        self._next = 0.0        # earliest time we may touch the car again
        self._sun = 0           # consecutive ticks of usable surplus
        self._import_since = None  # when the house last started importing
        self._fails = 0         # consecutive BLE failures, for backoff
        self._charging = False  # last known state, so we keep polling mid-charge
        self._last_wake = 0.0
        self._busy = False      # a BLE round-trip is in flight
        self._last_decide = 0.0  # when we last dispatched one
        self._probe = False     # arm() wants one read before trusting the sun
        self._dry_since = None  # when usable surplus last ran out
        self._car = None        # last state the car reported
        self._state_at = 0.0    # when it reported it
        self._sample_at = None  # stamp of the last meter reading we acted on

    def arm(self) -> None:
        """Start managing the charge. Called from the menu."""
        self.armed = True
        # The car may already be plugged in and charging off the grid, so find
        # out before the no-sun gate below decides not to contact it at all.
        self._probe = True
        self._state_at = 0.0
        self._sun = self._fails = 0
        self._import_since = None
        self._dry_since = None
        self._next = 0.0
        self.status = "armed"
        _log(f"armed at hour {_local_hour()} (enabled={self.enabled})")

    def disarm(self, reason: str = "off") -> None:
        """Stop managing, and stop any charge we started.

        Leaving the car pulling whatever current we last set would keep it
        charging off the grid after sunset, so this actively stops it."""
        self.armed = False
        self._probe = False
        self._state_at = 0.0
        self._import_since = None
        self.status = reason
        _log(f"disarmed: {reason}")
        self._dry_since = None
        if self._charging:
            self._charging = False
            self.amps = 0
            self._busy = True
            threading.Thread(target=self._stop_bg, name="tesla-stop",
                             daemon=True).start()

    def _stop_bg(self) -> None:
        try:
            self._send("charging-stop")
        except Exception as exc:
            _log(f"stop failed: {exc}")
        finally:
            self._busy = False

    def tick(self, meter_w, sample_at=None) -> None:
        """Call from the UI loop with the grid meter (+ = exporting).

        `sample_at` is when the inverter produced this reading. The UI refreshes
        faster than the inverter does, so without it the same reading gets
        counted several times over — three "sustained" samples that are one
        sample, and an import timer that runs on a measurement taken before we
        last changed anything.

        Returns immediately: a BLE round-trip takes seconds to tens of seconds
        and this runs on the AppKit main thread, so the talking happens on a
        worker. Same reason poller.py keeps its Modbus I/O off this thread."""
        if not self.enabled or meter_w is None or self._busy:
            return
        # Same reading as last time: the inverter hasn't refreshed yet, so
        # there is genuinely nothing new to decide on.
        if sample_at is not None:
            if sample_at == self._sample_at:
                return
            self._sample_at = sample_at
        hour = _local_hour()

        # Past the window: shut down for the day, stopping any charge we began.
        if hour >= WINDOW_END:
            if self.armed:
                self.disarm(f"done for today (after {WINDOW_END}:00)")
            elif self.status != f"done for today (after {WINDOW_END}:00)":
                self.status = "off"
            return
        if not self.armed:
            self.status = "off — tick “Solar charging”"
            return

        now = time.time()
        export = float(meter_w)
        # Pulling real power off the grid while we're the one charging costs
        # money every second we wait, so don't serve out the rest of the
        # interval — come back at URGENT_EVERY instead. Not while backing off
        # from a failure (self._fails), or we'd hammer an unreachable car.
        # Importing while we are the ones charging: give the amps trim below a
        # few samples to fix it, then cut the charge. Timed off the meter, not
        # off decisions, because a BLE state read can take most of a minute and
        # the house imports for every second of it.
        if self._charging and export < -DEADBAND_W:
            if self._import_since is None:
                self._import_since = now
            elif now - self._import_since >= STOP_AFTER:
                self._import_since = None
                self._charging = False
                self.amps = 0
                self.status = "stopped (importing)"
                self._next = now + CHECK_EVERY
                _log(f"importing {export:.0f}W for {STOP_AFTER}s — stopping")
                self._busy = True
                threading.Thread(target=self._stop_bg, name="tesla-stop",
                                 daemon=True).start()
                return
        else:
            self._import_since = None

        due = self._next
        if self._probe:
            due = 0.0
        elif self._charging and not self._fails and export < -URGENT_W:
            due = min(due, self._last_decide + URGENT_EVERY)
        if now < due:
            return
        self._last_decide = now
        # Sample faster while waiting to start than while steering an active
        # charge: three quick samples still filter a passing sunny gap, but get
        # us charging in about a minute instead of three.
        self._next = now + (DECIDE_EVERY if self._charging else CHECK_EVERY)

        probe, self._probe = self._probe, False

        # Nothing worth waking the car for. This is the whole vampire-drain
        # defence: on dull days we never open a BLE session.
        if not probe and not self._charging and export < START_W:
            self._sun = 0
            if self._dry_since is None:
                self._dry_since = now
            elif now - self._dry_since >= GIVE_UP_AFTER:
                self.disarm("gave up — no sun for an hour")
                return
            self.status = "waiting for sun"
            return
        self._dry_since = None
        if not probe and not self._charging:
            self._sun += 1
            if self._sun < SUSTAINED:
                self.status = f"confirming surplus ({self._sun}/{SUSTAINED})"
                return

        _log(f"decide: export={export:.0f}W sun={self._sun} "
             f"charging={self._charging}{' probe' if probe else ''}")
        self._busy = True
        # A BLE round-trip can run to a couple of minutes. Say so, or the last
        # status sits there looking frozen.
        self.status = "contacting car…"
        threading.Thread(target=self._decide_bg, args=(export,),
                         name="tesla-ble", daemon=True).start()

    def _decide_bg(self, export_w: float) -> None:
        started = time.time()
        try:
            self._decide(export_w)
        except Exception as exc:            # never take the app down with us
            self.status = f"error: {exc}"
        finally:
            self._busy = False
            # Decisions land every max(DECIDE_EVERY, this). If it's routinely
            # the latter, the BLE round-trip is what's costing us reaction time.
            _log(f"decided in {time.time() - started:.0f}s -> {self.status}")

    def _decide(self, export_w: float) -> None:
        car = self._fresh_state()      # backs itself off if the car won't answer
        if car is None:
            self.status = "car not reachable"
            return

        self._charging = car["charging"]
        self.detail = charge_summary(car)
        # Track what the car reports, not what we asked for. On an unreachable
        # cycle this keeps the last known value rather than falsely showing 0.
        self.amps = car["amps"] if car["charging"] else 0

        # Slow-changing facts: stop pestering the car about them.
        if not car["connected"] or car["complete"] or car["at_limit"]:
            self._sun = 0
            self._next = time.time() + IDLE_RECHECK
            if not car["connected"]:
                self.status = "unplugged"
            elif car["at_limit"]:
                self.status = f"at charge limit ({car['battery']}%)"
            else:
                self.status = "charged"
            return

        want = next_amps(car["amps"], export_w, car["max_amps"], car["volts"])

        if want < MIN_AMPS:
            # Below the floor there is no point trickling: stop rather than
            # hold the car at a current the surplus can't cover.
            if car["charging"]:
                if not self._send("charging-stop"):
                    self._state_at = 0.0     # we no longer know where it is
                car["charging"] = False
                car["amps"] = 0
                self._charging = False
                self.amps = 0
                self.status = f"stopped (under {MIN_AMPS}A)"
            else:
                self.status = "waiting for sun"
            return

        cmds = []
        if want != car["amps"]:
            cmds.append(f"charging-set-amps {want}")
        if not car["charging"]:
            cmds.append("charging-start")
        if cmds and not self._send(*cmds):
            # Half the batch may have landed, so the cached amps are no longer
            # trustworthy: force a real read next time round.
            self._state_at = 0.0
            self.status = "car not reachable"
            return
        car["charging"] = True
        car["amps"] = want
        # The meter can't have seen this yet, so any import measured up to now
        # describes the old current. Restart the clock rather than cut a charge
        # we just corrected.
        self._import_since = None
        self._charging = True
        self.amps = want
        self.status = f"charging {want}A"

    def _send(self, *cmds: str) -> bool:
        """Send write commands, one BLE session each.

        tesla-control's stdin mode would share a single connection across them,
        but it can't know which commands are coming so it loads OAuth
        credentials up front and dies with "could not load token" on a BLE-only
        setup. Separate invocations measure fine anyway — the car happily
        accepts a reconnect a couple of seconds after the last one closed."""
        for i, cmd in enumerate(cmds):
            if i:
                time.sleep(RETRY_PAUSE)   # let the previous session close
            argv = self._cmd(*cmd.split())
            if self._exec(argv) is None:
                time.sleep(RETRY_PAUSE)
                if self._exec(argv) is None:
                    return False
        return True

    # argv is [bin, -ble, -vin, V, -key-file, K, -command-timeout, T,
    # -connect-timeout, T, -domain D, *args]; everything from here on is the
    # command itself, which is all we log.
    CMD_AT = 12

    @staticmethod
    def _cmd(*args: str) -> list:
        """Full argv. The key is passed explicitly rather than relying on the
        environment reaching us intact through launchd.

        Pinning -domain halves the handshake: without it tesla-control connects
        to every domain the car offers and waits on all of them. Charging lives
        in infotainment; only `wake` is a VCSEC command."""
        domain = ("vcsec" if args and args[0] in ("wake", "body-controller-state")
                  else "infotainment")
        return [TESLA_CONTROL, "-ble",
                "-vin", os.environ["TESLA_VIN"],
                "-key-file", os.environ["TESLA_KEY_FILE"],
                "-command-timeout", CMD_TIMEOUT,
                "-connect-timeout", CONNECT_TIMEOUT,
                "-domain", domain, *args]

    def _fresh_state(self):
        """The car's charge state, re-reading it only when it has gone stale.

        Reading costs a BLE round-trip, which is the part that fails; between
        reads the cache is kept honest by writing our own accepted commands
        back into it. Always re-read before starting a charge — that is when
        "is it even plugged in?" actually matters."""
        now = time.time()
        if (self._car is not None and self._charging
                and now - self._state_at < STATE_EVERY):
            return self._car

        blob = self._read("state", "charge")
        if blob is None:
            # Car out of range or refusing to answer. Back off rather than
            # burning a BLE timeout every half-minute.
            self._fails += 1
            self._next = now + min(IDLE_RECHECK,
                                   DECIDE_EVERY * 2 ** self._fails)
            return None
        self._fails = 0
        self._car = parse_charge_state(blob)
        self._state_at = now
        return self._car

    def _asleep(self):
        """True asleep, False awake, None not reachable at all.

        VCSEC stays listening when infotainment doesn't, and answers without a
        key, so this is the one question we can always ask. Asking it first
        turns the two failures that look identical from infotainment — a car
        that is asleep and a car that isn't there — into different answers,
        for one cheap round-trip instead of a 30s connect that tells us
        nothing either way."""
        out = self._exec(self._cmd("body-controller-state"))
        if out is None:
            return None
        return "VEHICLE_SLEEP_STATUS_AWAKE" not in out

    def _wake(self) -> bool:
        """Wake infotainment, at most once per WAKE_COOLDOWN."""
        now = time.time()
        if now - self._last_wake < WAKE_COOLDOWN:
            _log("asleep, but woken too recently — leaving it alone")
            return False
        if self._exec(self._cmd("wake")) is None:
            return False
        # Only a wake that worked starts the cooldown; otherwise one timeout
        # locks out every retry for the next 15 minutes.
        self._last_wake = now
        time.sleep(WAKE_SETTLE)
        return True

    def _read(self, *cmd: str):
        """Run one read command; returns stdout, or None if the car didn't answer.

        Ask infotainment first and only diagnose when it doesn't answer. On a
        marginal link every connect is another chance to fail, so probing VCSEC
        up front would double the cost of the path that was working — the probe
        has to earn its place, and it only does once we already have a failure
        to explain."""
        out = self._exec(self._cmd(*cmd))
        if out is not None:
            return out
        asleep = self._asleep()
        if asleep is None:
            _log("VCSEC silent too — car out of range")
            return None
        if asleep and not self._wake():
            return None
        return self._exec(self._cmd(*cmd))

    @staticmethod
    def _exec(argv: list[str]):
        try:
            p = subprocess.run(argv, capture_output=True, timeout=BLE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"{' '.join(argv[SolarCharger.CMD_AT:])}: {type(exc).__name__}: {exc}")
            return None
        if p.returncode == 0:
            return p.stdout.decode("utf-8", "replace")
        _log(f"{' '.join(argv[SolarCharger.CMD_AT:])}: rc={p.returncode} "
             f"{p.stderr[:300].decode('utf-8', 'replace').strip()}")
        return None
