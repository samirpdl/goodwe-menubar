# GoodWe Solar — macOS menubar app

A tiny macOS menubar app that polls a **GoodWe grid-tie inverter** over its
local **Modbus** protocol and shows, at a glance:

- ☀︎ **Solar** — current PV production
- 🏠 **House** — current household consumption
- ⚡︎ **Grid** — importing from / exporting to the grid (with direction)
- Today's generation, AC output, grid voltage/frequency, inverter temperature

Built for a **solar + house + grid** setup with **no battery**. Data comes
straight off the inverter on your LAN — no cloud, no SEMS portal login, no
internet dependency.

The menubar shows solar, house, and **live grid direction** (`↑` exporting /
`↓` importing) at a glance. When the inverter idles at night it shows `☾ idle`
instead of an error, and sends GoodWe's UDP wake packet to keep the Wi-Fi
dongle reachable.

```
  menubar:  ☀︎ 2.41 kW   🏠 1.10 kW   ↑ 1.31 kW   🚗 12A

  dropdown: GW5000-DNS-30  (5000DSU000X0000)
            ────────────────────────────
            ☀︎ Solar (PV): 2.41 kW
            🏠 House load: 1.10 kW
            ⚡︎ Grid: ↑ exporting 1.31 kW
            AC out 2.40 kW · 241 V · 50.01 Hz · 38°C
            ────────────────────────────
            Today's generation: 14.2 kWh
            Today's usage: 8.5 kWh
               ↓ Imported: 3.2 kWh  ·  A$0.76
               ↑ Exported: 9.0 kWh  ·  A$0.90
               + Supply charge: A$1.37
            Net today:  A$1.23 cost
            ────────────────────────────
            This week:  52.1 kWh  ·  A$12.40 cost
            This month: 210.4 kWh ·  A$58.90 cost
            Custom range…
            ────────────────────────────
            Status: ok · updated 3s ago
            ...

  at night: ☾ idle      (inverter asleep — dongle gets woken automatically)
```

## How it connects

It uses the [`goodwe`](https://github.com/mletenay/home-assistant-goodwe-inverter)
Python library, which speaks GoodWe's Modbus protocol over the inverter's
Wi-Fi/LAN dongle and auto-detects your inverter family (DT / MS / NS / XS / ET…).
Two transports are supported, selected by **port**:

| Port | Transport | When to use |
|------|-----------|-------------|
| **8899** | UDP (Modbus over GoodWe's UDP) | **Default.** Works with most GoodWe Wi-Fi/LAN dongles. |
| **502**  | **Modbus TCP** | Use if your dongle/datalogger exposes a standard Modbus TCP port. |

> **Smart meter note:** House load and grid import/export require a GoodWe
> **smart meter (GM-series)** wired to the inverter. Solar production and
> energy-today always work. Without a meter, those rows show
> "— (needs a GoodWe smart meter)".

## Setup

```bash
# 1. Create the virtualenv (Python 3.13 or 3.14)
/opt/homebrew/bin/python3 -m venv .venv

# 2. Install dependencies
./.venv/bin/python -m pip install -r requirements.txt

# 3. Run it
./run.sh
```

On first launch it asks for your inverter's **local IP address**. Find it in:
- your router's DHCP / connected-clients list, or
- the **SolarGo** / **PV Master** mobile app (device info), or
- `arp -a` on your Mac while on the same network.

The app appears in the menubar (no Dock icon). All settings are reachable from
its dropdown.

## Configuration

Settings are stored at `~/.config/goodwe-menubar/config.json` and are editable
from the menu (**Set inverter IP…**, **Connection port…**, **Poll interval…**)
or directly via **Edit config file…**. See [`config.example.json`](config.example.json).

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `""` | Inverter local IP, e.g. `192.168.1.50` |
| `port` | `8899` | `8899` = UDP, `502` = Modbus TCP |
| `family` | `null` | `null` = auto-detect, or force `"ET"` / `"DT"` / `"ES"` |
| `comm_addr` | `0` | Modbus unit/slave address (`0` = library default) |
| `timeout` | `1` | Seconds per request |
| `retries` | `3` | Retries per request |
| `poll_interval` | `10` | Seconds between reads |
| `units` | `"kW"` | `"kW"` (auto kW/W) or `"W"` (always watts) |
| `flip_meter_sign` | `false` | Flip if import/export shows reversed |
| `wake_enabled` | `true` | Send GoodWe's UDP wake packet before (re)connecting |
| `wake_port` | `48899` | UDP port for the wake/discovery broadcast |
| `wake_payload` | `"WIFIKIT-214028-READ"` | Magic string the dongle listens for |
| `currency_symbol` | `"A$"` | Shown before money amounts |
| `supply_charge` | `1.3684` | Fixed cost per day (applies regardless of usage) |
| `export_rate` | `0.10` | Flat credit per kWh exported |
| `import_tariff` | *(see below)* | Time-of-use import rates |

Environment overrides `GOODWE_HOST` / `GOODWE_PORT` take precedence over the file.

**Grid direction:** the app treats positive meter power as *exporting* (matching
the library's convention). If your install reads reversed, toggle
**Flip grid direction** in the menu.

## Usage & cost

Below the live readings the dropdown shows energy **usage and cost**, broken down
by **today / this week / this month**, plus a **Custom range…** picker:

- **Today** — generation, usage, imported (kWh + cost), exported (kWh + credit),
  the daily supply charge, and the **net** (cost or credit).
- **This week / This month** — usage and net for the calendar week (Mon→today)
  and month (1st→today).
- **Custom range…** — enter a start and end date for any total.

How the numbers are derived:
- The smart meter's lifetime import/export counters are sampled each poll; the
  **per-day** figures are the delta since local midnight, stored in a history file
  (`~/.config/goodwe-menubar/energy_history.json`) so any range can be summed.
- **Import cost** is time-of-use: each bit of imported energy is charged at the
  rate active at that moment.
- **Export credit** = exported kWh × `export_rate`. **Supply charge** is added
  once per day. **Net** = import cost + supply charge − export credit
  (negative net = you're in credit).
- **Usage** (house consumption) = generation − export + import.

`import_tariff` is a list of time windows in local 24-hour time (`"24:00"` means
midnight); they should cover the whole day:

```json
"import_tariff": [
  { "from": "00:00", "to": "06:00", "rate": 0.1365 },
  { "from": "06:00", "to": "15:00", "rate": 0.2365 },
  { "from": "15:00", "to": "21:00", "rate": 0.40392 },
  { "from": "21:00", "to": "24:00", "rate": 0.2365 }
]
```

> History accumulates from when the app first runs, so weekly/monthly totals only
> include days the app was running (it's meant to run continuously via the
> LaunchAgent). Today's supply charge is counted in full from the start of the day.

## Run it as a real app (no shell command)

Two one-time steps:

```bash
./install-launchagent.sh     # registers a LaunchAgent → starts at login
./build-launcher-app.sh      # builds the clickable "GoodWe Solar.app"
cp -R "GoodWe Solar.app" /Applications/
```

Now you have:

- **Auto-start at login** — the LaunchAgent launches the menubar app directly
  via launchd, which is the *only* way it reliably gets menubar access (see the
  note below). Restarts itself on a crash; a clean **Quit** stays quit.
- **A clickable `/Applications/GoodWe Solar.app`** — double-click it (or launch
  from Spotlight) anytime to **start or restart** the app, e.g. after you Quit
  it. It doesn't run Python itself; it just asks launchd to start the menubar
  instance, so the icon always shows. No Dock icon, no Terminal, no Rosetta.

Only one instance ever runs (a localhost lock on port 49222), so launching it
twice never duplicates the icon. Logs: `~/Library/Logs/goodwe-menubar.log`.

To stop it for good (no restart at login):

```bash
launchctl bootout gui/$(id -u)/com.local.goodwe-menubar
```

> **Why the LaunchAgent and not a plain `.app` wrapper?** A `.app` whose launcher
> `exec`s into Python loses its window-server/menubar connection when launched by
> Finder/LaunchServices — the process runs but no icon appears. Only a *direct*
> Python process (what launchd runs) gets menubar access on this setup. The
> clickable app above sidesteps this by delegating to launchd. A `py2app` bundle
> (`setup.py`) would also work since it runs Python in-process, if you prefer a
> fully self-contained app.

## Charging a Tesla from surplus solar

Optional. When enabled, the app matches your Tesla's charge current to whatever
you're currently exporting, so the car soaks up surplus instead of it going to
the grid at 10c/kWh. It deliberately leaves `BUFFER_W` (150 W) of export in
hand rather than eating every last watt — the kettle, the oven and a passing
cloud all land faster than a Bluetooth round-trip can answer.

Before each read it asks VCSEC (`body-controller-state`) whether the car is
awake — VCSEC answers over BLE even when infotainment is asleep, so a car that
is merely sleeping gets woken instead of timing out, and a car that is out of
range is recognised without spending a connect timeout finding out.

It talks to the car over **Bluetooth**, using Tesla's official
[`vehicle-command`](https://github.com/teslamotors/vehicle-command) SDK. No
cloud, no subscription, no Tesla developer account, no internet. The Mac needs
to stay within about 10m of the car.

> The old **Owner API** is dead — Tesla shut it down for third-party use. The
> official replacement, the Fleet API, needs a developer account, a domain you
> own hosting a public key, and a local proxy to sign every command. BLE avoids
> all of it.

### Setup

```bash
./setup-tesla.sh
```

It installs `tesla-control`, generates an ECDSA P-256 key pair in `~/.tesla/`,
enrols the public half in the car, verifies it, and updates the LaunchAgent.
Re-running it is safe — every step is idempotent.

Have the car **parked within ~10m, awake, with your NFC keycard in hand**: the
script pauses and asks you to tap the card on the centre console, which is what
authorises the key. macOS will also ask the terminal for Bluetooth permission
the first time.

> **Turn Bluetooth off on nearby phones first.** The car accepts only a couple
> of simultaneous BLE connections and every phone key holds one open, so
> enrolment fails with *"the vehicle is already connected to the maximum number
> of BLE devices"*. Switch phone Bluetooth back on once enrolment succeeds — the
> enrolled key reconnects on demand and doesn't need a permanent slot.

> **There is no key to obtain from Tesla.** You generate the pair yourself; the
> private half never leaves this Mac. The script enrols it with the
> **`charging_manager`** role, which can set the charge current and start/stop
> charging but *cannot unlock or drive the car* — worth having on an always-on
> machine. If your firmware won't accept that role over BLE it falls back to
> `owner` and tells you.
>
> The private key lives in a file rather than the macOS Keychain on purpose: a
> launchd background job can't answer a Keychain unlock prompt.

If you'd rather do it by hand:

```bash
# `go install ...@latest` fails on this module — its go.mod has replace
# directives, which Go only honours for a main module. Build from a clone:
git clone --depth 1 https://github.com/teslamotors/vehicle-command.git
(cd vehicle-command && go build -o "$(go env GOPATH)/bin/tesla-control" ./cmd/tesla-control)

mkdir -p ~/.tesla && chmod 700 ~/.tesla
openssl ecparam -genkey -name prime256v1 -noout -out ~/.tesla/key.pem
openssl ec -in ~/.tesla/key.pem -pubout -out ~/.tesla/pub.pem
tesla-control -ble -vin $TESLA_VIN add-key-request ~/.tesla/pub.pem charging_manager cloud_key
# …tap the keycard on the centre console…
tesla-control -ble -vin $TESLA_VIN -key-file ~/.tesla/key.pem state charge
```

`tesla-control` also picks these up from the environment directly: `TESLA_VIN`,
`TESLA_KEY_FILE`, and `TESLA_CACHE_FILE` (a session cache that saves a
handshake on every call).

### Turning it on

`setup-tesla.sh` does this for you. Manually, the app reads `TESLA_VIN`,
`TESLA_KEY_FILE` and `TESLA_CACHE_FILE` from its environment — with the first
two unset it does nothing at all, so nothing changes until you opt in. A GUI
job under launchd inherits no shell profile, so export them and re-run the
installer, which bakes them into the `.plist`:

```bash
export TESLA_VIN=5YJ... TESLA_KEY_FILE=$HOME/.tesla/key.pem
./install-launchagent.sh
```

Then check **`MAX_AMPS`** in `goodwe_menubar/tesla.py` matches what your
charger is rated to deliver continuously (`15` for a 15A charger). The car
reports its own limit from the cable's pilot signal and the lower of the two
always wins, but the car can't know what else shares that circuit — so this
number is yours to get right.

A `🚗 Tesla:` line in the dropdown shows what it's doing — `waiting for sun`,
`confirming surplus`, `contacting car…`, `charging 12A`,
`stopped (importing)`, `stopped (under 8A)`,
`unplugged`, `at charge limit (80%)`, `charged`, or `car not reachable` — with a
second line of stats beneath it, and `🚗 12A` in the menubar itself while a
charge is actually running:

```
🚗 Tesla: charging 12A
   62% → 80% · +4.3 kWh · 3 kW · 1h35m left
```

Battery level, the driver's charge limit, energy added this session, charger
power and time remaining all come from the same `state charge` read the control
loop already makes, so showing them costs no extra Bluetooth traffic.

### It only runs when you say so

Nothing touches the car until you click **Start solar charging** in the
dropdown. Until then there is no polling, no Bluetooth, nothing — the line reads
`off — click Start solar charging`.

Once armed:

| | |
|---|---|
| Immediately | the control loop runs as described below, whatever the hour |
| No usable sun for a solid hour | switches itself off for the day — `gave up — no sun for an hour` |
| After 15:00 | keeps going while there's sun; once the sun is gone (no surplus, not charging) it switches itself off and **stops any charge it started** |

Arming is deliberately not remembered across restarts or days: a new day is a
new decision. Click **Stop solar charging** to disarm early; that also stops a
charge in progress, since leaving the car pulling the current we last set would
have it charging off the grid after sunset.

The 15:00 mark and the give-up timer are `WINDOW_END` and `GIVE_UP_AFTER` at the top of `goodwe_menubar/tesla.py`.

**It won't charge a full car.** If the battery is already at the charge limit
set in the Tesla app, it reports `at charge limit` and backs off to a half-hourly
check instead of waking the car every minute. Raise the limit in the Tesla app
and it picks up on the next check.

`context deadline exceeded` in the log is a `tesla-control` timeout, not a
refusal. Its own default is 5s per command, which a car parked at the end of a
driveway misses often; `CMD_TIMEOUT` raises it to 15s.

### When it can't reach the car

```bash
./diagnose-tesla.sh
```

Runs repeated charge-state reads and reports a success rate, classifying each
failure as occupied BLE slots, dropped link, adapter permissions, or a rejected
key — the four causes look identical from a single failed command.

Bluetooth is 2.4GHz: a plasterboard wall costs little, but **brick, concrete and
foil-backed insulation are close to opaque**. If reads succeed sometimes and drop
mid-session, that's a marginal link rather than a configuration problem. Moving
the Mac toward the car — ideally a window on the car's side — helps more than
anything else. macOS 13+ dropped support for most USB Bluetooth dongles, so
adding an external antenna generally isn't an option; a small always-on machine
nearer the car is the reliable fix if moving the Mac isn't possible.

The app degrades quietly either way: it retries once, then backs off up to 30
minutes, and the dropdown shows `car not reachable`. Nothing is left in a bad
state — the car keeps whatever charge current it was last given.

### Will it keep waking the car up?

No — BLE contact is gated on there being something to gain:

| Situation | What it does |
|---|---|
| Arming | One state read straight away (waking the car if it doesn't answer) — it may already be plugged in and charging off the grid. |
| Night, or under ~2.3 kW export | **Never contacts the car** after that first read. No BLE session at all. |
| Surplus appears | Wants 3 steady samples (~1 min) before disturbing a sleeping car — a passing sunny gap won't trigger it. |
| Charging | Polls every 30s, and immediately (min 15s apart) if the house starts importing more than 500 W — the car is awake anyway while charging, so this is free. |
| Unplugged, or finished charging | Backs off to one check every 30 minutes. |
| Out of Bluetooth range | Exponential backoff up to 30 minutes, instead of a 25s timeout every 30s. |
| Asleep with real surplus | One `wake`, at most once per 15 minutes. |

The tuning knobs are the constants at the top of `goodwe_menubar/tesla.py`
(`MIN_AMPS`, `START_W`, `SUSTAINED`, `IDLE_RECHECK`, `WAKE_COOLDOWN`,
`DECIDE_EVERY`, `STATE_EVERY`, `URGENT_W`, `URGENT_EVERY`, `STOP_AFTER`, `CMD_TIMEOUT`, `CONNECT_TIMEOUT`,
`BUFFER_W`). If you still see
vampire drain, raise `SUSTAINED` and `START_W` first.

## Project layout

```
goodwe_menubar/
  app.py       rumps menubar UI + settings + single-instance lock (main thread)
  poller.py    background asyncio thread: connect, read, wake dongle, reconnect
  daily.py     energy/cost history: daily/weekly/monthly/custom + ToU tariffs
  config.py    JSON config load/save
  tesla.py     optional: match Tesla charge current to surplus solar over BLE
  __main__.py  entry point (python -m goodwe_menubar)
diagnose.py             dump every sensor your inverter reports
install-launchagent.sh  register the start-at-login LaunchAgent
setup-tesla.sh          one-shot Tesla BLE key setup (install, keygen, enrol)
diagnose-tesla.sh       measure Bluetooth link quality to the car
build-launcher-app.sh   build the clickable "GoodWe Solar.app" (start/restart)
run.sh                  run in the foreground from a terminal (dev)
```

## Troubleshooting

- **"Connect failed"** — verify the IP, make sure your Mac is on the same LAN/Wi-Fi
  as the inverter, and try the other port (8899 ↔ 502). Some dongles only accept
  one client connection at a time, so close the SolarGo/PV Master app while testing.
- **House/Grid show "needs a GoodWe smart meter"** — your inverter doesn't have a
  GM-series meter reporting consumption; solar + energy-today still work.
- **Import/export reversed** — toggle **Flip grid direction**.
- **`☾ idle` shown** — normal at night/low light: the inverter has powered down.
  The app keeps waking the dongle (UDP `WIFIKIT-214028-READ` on port 48899) and
  reconnects automatically once the inverter comes back. Disable the wake packet
  by setting `wake_enabled` to `false` if you don't want the broadcast.
```

## Tests

```
python3 tests/test_tesla.py
```

Plain asserts, no framework. Everything runs against stubs — no car, no
inverter, no network.

## License

MIT — see [LICENSE](LICENSE).
