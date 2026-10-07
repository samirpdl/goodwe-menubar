"""Runnable checks for the solar-charging control loop.

    python3 tests/test_tesla.py

No framework on purpose: plain asserts, no fixtures, no plugins. Everything
here runs against stubs — it never touches a real car or a real inverter.

Anything that stubs module-level state (RETRY_PAUSE, WAKE_SETTLE, _local_hour)
writes it back onto the module, since that is what the code under test reads.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import goodwe_menubar.tesla as tesla
from goodwe_menubar.tesla import (
    BUFFER_W, DECIDE_EVERY, GIVE_UP_AFTER, MAX_AMPS, MIN_AMPS, RETRY_PAUSE,
    START_W, STATE_EVERY, STOP_AFTER, SUSTAINED, TESLA_CONTROL, SolarCharger,
    URGENT_EVERY, VOLTS, WAKE_SETTLE, charge_summary, next_amps,
    parse_charge_state,
)

_local_hour = tesla._local_hour


def test():
    assert next_amps(0, 0) == 0
    assert next_amps(0, BUFFER_W) == 0               # exactly on target
    assert next_amps(8, BUFFER_W) == 8               # ...so hold, don't fidget
    assert next_amps(8, BUFFER_W + 100) == 8         # inside the deadband
    assert next_amps(0, START_W) == MIN_AMPS         # enough sun to start on
    assert next_amps(10, 1200, ceiling=32) == 14     # 1050W over target -> +4A
    assert next_amps(10, -500) == 8                  # importing 500W -> back off
    assert next_amps(10, -9000) == 0                 # kettle on -> floor
    assert next_amps(30, 9000, ceiling=32) == 32
    assert next_amps(16, -1900, ceiling=32) < 16     # car tapering -> walk down
    assert next_amps(20, 5000, ceiling=15) == 15     # car/EVSE limit wins
    assert next_amps(0, 99999) == MAX_AMPS           # default ceiling holds
    # The buffer must cost amps, never add them: same sun, one step lower.
    assert next_amps(10, 2000) < 10 + int(2000 / VOLTS)

    # protojson, camelCase with the enum wrapped in a oneof object
    plugged = """{"chargeState":{"chargingState":{"Charging":{}},
      "chargerActualCurrent":16,"chargerVoltage":241,
      "chargeCurrentRequestMax":40,"batteryLevel":62}}"""
    st = parse_charge_state(plugged)
    assert st["connected"] and st["charging"] and not st["complete"]
    assert st["amps"] == 16 and st["volts"] == 241
    assert st["max_amps"] == MAX_AMPS                # car says 40A, our ceiling wins

    # snake_case with a bare enum string, and zero-valued fields omitted
    st = parse_charge_state('{"charge_state": {"charging_state": "Disconnected"}}')
    assert not st["connected"] and not st["charging"]
    assert st["amps"] == 0 and st["volts"] == VOLTS and st["max_amps"] == MAX_AMPS

    # Stats come off the same read; charge_energy_added is a float.
    st = parse_charge_state("""{"chargingState":{"Charging":{}},
      "batteryLevel":62,"chargeLimitSoc":80,"chargeEnergyAdded":4.35,
      "chargerPower":3,"minutesToFullCharge":95}""")
    assert st["battery"] == 62 and st["limit"] == 80 and not st["at_limit"]
    assert abs(st["added_kwh"] - 4.35) < 1e-9
    assert charge_summary(st) == "62% → 80% · +4.3 kWh · 3 kW · 1h35m left"

    # At the driver's limit: don't start, whatever the sun is doing.
    st = parse_charge_state('{"chargingState":"Stopped","batteryLevel":80,"chargeLimitSoc":80}')
    assert st["at_limit"] and st["connected"] and not st["charging"]
    st = parse_charge_state('{"chargingState":"Stopped","batteryLevel":79,"chargeLimitSoc":80}')
    assert not st["at_limit"]

    st = parse_charge_state('{"chargingState":"Complete","chargerPilotCurrent":10}')
    assert st["connected"] and st["complete"] and st["max_amps"] == 10

    # Every write must actually reach the car; a failed one must not be
    # reported as success, and a transient failure gets exactly one retry.
    os.environ.setdefault("TESLA_VIN", "TESTVIN0000000000")
    os.environ.setdefault("TESLA_KEY_FILE", "/dev/null")
    c = SolarCharger()
    c.enabled = True
    real_pause, tesla.RETRY_PAUSE = tesla.RETRY_PAUSE, 0

    sent = []
    c._exec = lambda argv: (sent.append(argv[SolarCharger.CMD_AT]), "")[1]
    # Reading the car is the expensive, failure-prone part, so a steady charge
    # must not re-read it every decision.
    charging_blob = ('{"chargingState":{"Charging":{}},"chargerActualCurrent":10,'
                     '"chargerVoltage":240,"chargeCurrentRequestMax":32,'
                     '"batteryLevel":50,"chargeLimitSoc":80}')
    sc = SolarCharger()
    sc.enabled = True
    sc_reads, sc_sent = [], []
    sc._read = lambda *cmd: sc_reads.append(cmd) or charging_blob
    sc._send = lambda *cmds: sc_sent.append(cmds) or True

    # export_w is the surplus left over *while the car is already drawing*, so
    # BUFFER_W + n*VOLTS is "n amps' worth of room above where we want to sit".
    room = lambda n: BUFFER_W + n * VOLTS

    # Steady sun, mid-charge: one read, and no further reads inside STATE_EVERY.
    sc._decide(room(0))
    assert len(sc_reads) == 1 and sc.amps == 10 and sc_sent == [], (sc_reads, sc_sent)
    for _ in range(5):
        sc._decide(room(0))
    assert len(sc_reads) == 1, sc_reads          # still just the one
    assert sc_sent == [], sc_sent                # target never moved, so no commands

    # Sun rises: the amps command goes out, still without re-reading the car,
    # and the new current becomes the baseline the next decision steers from.
    sc._decide(room(4))
    assert sc_sent == [("charging-set-amps 14",)], sc_sent
    assert len(sc_reads) == 1 and sc.amps == 14, (sc_reads, sc.amps)
    sc._decide(room(0))
    assert len(sc_sent) == 1, sc_sent            # already there, don't re-send

    # Once the cache goes stale we ask the car again.
    sc._state_at -= STATE_EVERY + 1
    sc._decide(room(0))
    assert len(sc_reads) == 2, sc_reads

    # A command that fails leaves us unsure what the car is doing, so the next
    # decision must re-read rather than trust our own bookkeeping.
    sc._send = lambda *cmds: False
    sc._decide(room(1))
    assert sc._state_at == 0.0 and sc.status == "car not reachable", sc.status
    sc._send = lambda *cmds: sc_sent.append(cmds) or True
    sc._decide(room(1))
    assert len(sc_reads) == 3, sc_reads

    # An unreachable car still backs off instead of retrying every tick.
    sc._read = lambda *cmd: None
    sc._state_at = 0.0
    sc._decide(5000)
    assert sc.status == "car not reachable" and sc._fails == 1
    assert sc._next > time.time()

    # CMD_AT must track _cmd's flags, or every log line lies about what ran.
    assert SolarCharger._cmd("state", "charge")[SolarCharger.CMD_AT:] == ["state", "charge"]
    assert "infotainment" in SolarCharger._cmd("state", "charge")
    assert "vcsec" in SolarCharger._cmd("wake")

    assert c._send("charging-set-amps 11", "charging-start") is True
    assert sent == ["charging-set-amps", "charging-start"], sent
    assert c._cmd("wake")[:2] == [TESLA_CONTROL, "-ble"]
    assert "-key-file" in c._cmd("wake")
    assert c._cmd("wake")[SolarCharger.CMD_AT:] == ["wake"]   # CMD_AT vs _cmd
    assert "-command-timeout" in c._cmd("wake")

    # The VCSEC probe decides which failure we are in, so infotainment only
    # gets bothered when there is something there to answer.
    real_settle, tesla.WAKE_SETTLE = tesla.WAKE_SETTLE, 0
    ASLEEP = '{"vehicleSleepStatus": "VEHICLE_SLEEP_STATUS_ASLEEP"}'
    AWAKE = '{"vehicleSleepStatus": "VEHICLE_SLEEP_STATUS_AWAKE"}'

    def stub(replies):
        """An _exec that answers per command name and records what was asked."""
        asked = []
        def _exec(argv):
            cmd = argv[SolarCharger.CMD_AT]
            asked.append(cmd)
            return replies.get(cmd)
        return asked, _exec

    # The link is working: one round-trip, and VCSEC is never bothered.
    seen, c._exec = stub({"state": "{}"})
    assert c._read("state", "charge") == "{}"
    assert seen == ["state"], seen

    # Nothing answers at all: probe once to find out, then stop. No wake — you
    # cannot wake a car that isn't there.
    seen, c._exec = stub({})
    c._last_wake = 0.0
    assert c._read("state", "charge") is None
    assert seen == ["state", "body-controller-state"], seen

    # In range and awake: a dropped read is just a dropped read, so retry it.
    seen, c._exec = stub({"body-controller-state": AWAKE})
    assert c._read("state", "charge") is None
    assert seen == ["state", "body-controller-state", "state"], seen

    # Asleep: wake, then read. A wake that failed must not start the cooldown,
    # or one timeout locks out the next 15 minutes of retries.
    seen, c._exec = stub({"body-controller-state": ASLEEP})
    c._last_wake = 0.0
    assert c._read("state", "charge") is None
    assert seen == ["state", "body-controller-state", "wake"], seen
    assert c._last_wake == 0.0

    # Wake worked but the car is still mute: the cooldown starts, and blocks
    # the next attempt from waking it again.
    seen, c._exec = stub({"body-controller-state": ASLEEP, "wake": ""})
    assert c._read("state", "charge") is None
    assert seen == ["state", "body-controller-state", "wake", "state"], seen
    assert c._last_wake > 0
    seen, c._exec = stub({"body-controller-state": ASLEEP, "wake": ""})
    assert c._read("state", "charge") is None
    assert seen == ["state", "body-controller-state"], seen
    tesla.WAKE_SETTLE = real_settle

    tries = []
    c._exec = lambda argv: tries.append(argv[SolarCharger.CMD_AT]) or None       # always fails
    assert c._send("charging-start") is False
    assert tries == ["charging-start"] * 2, tries              # one retry, no more

    flaky = [None, ""]                                          # fails once, then works
    c._exec = lambda argv: flaky.pop(0)
    assert c._send("charging-start") is True
    tesla.RETRY_PAUSE = real_pause

    real_hour = _local_hour
    tesla._local_hour = lambda: 12          # don't depend on the wall clock
    c = SolarCharger()
    c.enabled = True
    c._decide = lambda *a: (_ for _ in ()).throw(AssertionError("touched the car"))

    # The UI refreshes faster than the inverter does, so a reading already
    # acted on must not be acted on twice — that is what turned one stale
    # sample into a "sustained" import and cut the charge.
    stamped = SolarCharger()
    stamped.enabled = True
    stamped.arm()
    stamped._decide = lambda *a: (_ for _ in ()).throw(AssertionError("touched the car"))
    stamped._probe = False
    stamped._next = 0
    stamped.tick(0, sample_at=100.0)
    assert stamped._dry_since is not None
    first = stamped._dry_since
    for _ in range(5):                      # same reading, over and over
        stamped._next = 0
        stamped.tick(0, sample_at=100.0)
    assert stamped._dry_since == first      # nothing advanced
    stamped._next = 0
    stamped.tick(0, sample_at=130.0)        # a genuinely new one does
    assert stamped._sample_at == 130.0
    stamped.disarm()

    # Disarmed: nothing happens, whatever the sun is doing.
    tesla._local_hour = lambda: 12
    for _ in range(5):
        c._next = 0
        c.tick(5000)
    assert c.status.startswith("off"), c.status

    # Armed early in the morning: still reads the car straight away, no
    # waiting for a start hour.
    c.arm()
    tesla._local_hour = lambda: 7
    probed = []
    c._decide = probed.append
    c.tick(0)
    while c._busy:
        time.sleep(0.01)
    assert probed == [0] and not c._probe, probed
    c._decide = lambda *a: (_ for _ in ()).throw(AssertionError("touched the car"))

    # After that one read it is back to waiting for real surplus.
    c._next = 0
    c.tick(0)
    assert c.status == "waiting for sun", c.status

    # Surplus still has to hold for SUSTAINED samples before we disturb the car.
    c._sun = 0
    for _ in range(SUSTAINED - 1):
        c._next = 0
        c.tick(5000)
    assert c._sun == SUSTAINED - 1

    # Importing for STOP_AFTER seconds: cut the charge, without waiting on a
    # state read first.
    c._charging = True
    c.amps = 12
    c._import_since = None
    stopped = []
    c._send = lambda *cmds: stopped.extend(cmds) or True
    c.tick(-400)                       # first sample of import: starts the clock
    assert c._import_since and not stopped and c._charging
    c.tick(-50)                        # inside the deadband: clock resets
    assert c._import_since is None
    c._import_since = time.time() - STOP_AFTER - 1
    c.tick(-400)
    while c._busy:
        time.sleep(0.01)
    assert stopped == ["charging-stop"], stopped
    assert not c._charging and c.amps == 0 and c.status == "stopped (importing)"

    # Charging and importing hard: don't wait out the interval.
    c._charging = True
    c._next = time.time() + DECIDE_EVERY
    c._last_decide = time.time() - URGENT_EVERY - 1
    hit = []
    c._decide = hit.append
    c.tick(-2000)
    while c._busy:
        time.sleep(0.01)
    assert hit == [-2000], hit
    # Same gap, but only a trickle of import: the interval still holds.
    c._next = time.time() + DECIDE_EVERY
    c._last_decide = time.time() - URGENT_EVERY - 1
    c.tick(-100)
    assert hit == [-2000], hit
    # Urgent, but too soon after the last decision: still holds.
    c._last_decide = time.time()
    c.tick(-2000)
    assert hit == [-2000], hit
    c._decide = lambda *a: (_ for _ in ()).throw(AssertionError("touched the car"))
    c._charging = False

    # No usable sun must not start the give-up clock while a charge is running.
    c._charging = True
    c._dry_since = None
    c._next = 0
    c.tick(0)
    assert c._dry_since is None
    c._charging = False

    # An hour of no sun disarms for the day.
    c._next = 0
    c.tick(200)
    assert c._dry_since is not None and c.armed
    c._dry_since = time.time() - GIVE_UP_AFTER - 1
    c._next = 0
    c.tick(200)
    assert not c.armed and "gave up" in c.status, c.status

    # Past the window it keeps going while there's sun, then switches off.
    c.arm()
    tesla._local_hour = lambda: 15
    c._next = 0
    c.tick(5000)
    assert c.armed, c.status
    c.tick(0)
    assert not c.armed and "done for today" in c.status, c.status
    tesla._local_hour = real_hour


if __name__ == "__main__":
    test()
    print("ok")
