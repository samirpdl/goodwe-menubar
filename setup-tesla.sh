#!/usr/bin/env bash
# One-shot setup for solar-surplus Tesla charging over Bluetooth.
#
#   ./setup-tesla.sh [VIN]
#
# Installs Tesla's tesla-control, generates a key pair, enrols the public key in
# the car (you tap your NFC keycard), verifies it, and wires the environment
# into the LaunchAgent. Safe to re-run — every step is idempotent.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
TESLA_DIR="$HOME/.tesla"
KEY_FILE="$TESLA_DIR/key.pem"
PUB_FILE="$TESLA_DIR/pub.pem"
CACHE_FILE="$TESLA_DIR/cache.json"
ROLE="charging_manager"   # can set amps + start/stop charging; cannot unlock or drive

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

# --- 1. tesla-control ------------------------------------------------------
say "Checking for tesla-control"
TESLA_CONTROL="$(command -v tesla-control || true)"
[ -z "$TESLA_CONTROL" ] && [ -x "$HOME/go/bin/tesla-control" ] && TESLA_CONTROL="$HOME/go/bin/tesla-control"
if [ -z "$TESLA_CONTROL" ]; then
  command -v go  >/dev/null || fail "Go is not installed. Try: brew install go"
  command -v git >/dev/null || fail "git is not installed. Try: xcode-select --install"
  # `go install ...@latest` refuses this module: its go.mod carries replace
  # directives, which are only honoured for a main module. Build from a clone.
  GOBIN_DIR="$(go env GOPATH)/bin"
  SRC="$(mktemp -d)"
  trap 'rm -rf "$SRC"' EXIT
  echo "Cloning github.com/teslamotors/vehicle-command …"
  git clone --depth 1 https://github.com/teslamotors/vehicle-command.git "$SRC/vc" >/dev/null 2>&1 \
    || fail "clone failed — check your network"
  mkdir -p "$GOBIN_DIR"
  echo "Building tesla-control …"
  ( cd "$SRC/vc" && go build -o "$GOBIN_DIR/tesla-control" ./cmd/tesla-control ) \
    || fail "build failed"
  TESLA_CONTROL="$GOBIN_DIR/tesla-control"
fi
echo "Using $TESLA_CONTROL"

# --- 2. Key pair -----------------------------------------------------------
# Tesla uses ECDSA NIST P-256. openssl ships with macOS, so we generate the pair
# directly rather than installing tesla-keygen as well. Keeping the private key
# in a file (not the Keychain) matters: a launchd background job can't answer a
# Keychain unlock prompt.
say "Key pair"
mkdir -p "$TESLA_DIR"
chmod 700 "$TESLA_DIR"
if [ -f "$KEY_FILE" ]; then
  echo "Reusing existing $KEY_FILE"
else
  openssl ecparam -genkey -name prime256v1 -noout -out "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "Generated $KEY_FILE"
fi
openssl ec -in "$KEY_FILE" -pubout -out "$PUB_FILE" 2>/dev/null
echo "Public key at $PUB_FILE"

# --- 3. VIN ----------------------------------------------------------------
VIN="${1:-${TESLA_VIN:-}}"
if [ -z "$VIN" ]; then
  printf '\nVIN (Tesla app → your car → Service → the 17-character VIN): '
  read -r VIN
fi
[ ${#VIN} -eq 17 ] || fail "'$VIN' doesn't look like a 17-character VIN"
export TESLA_VIN="$VIN"
export TESLA_KEY_FILE="$KEY_FILE"
export TESLA_CACHE_FILE="$CACHE_FILE"

# Reads the charge state. BLE links drop routinely — especially straight after
# a previous session closed — so a "disconnected" is worth retrying, while an
# authorisation failure is final and shouldn't cost us three connect timeouts.
read_state() {
  local out i
  for i in 1 2 3 4; do
    if out=$("$TESLA_CONTROL" -ble -vin "$VIN" -key-file "$KEY_FILE" state charge 2>&1); then
      printf '%s' "$out"
      return 0
    fi
    case "$out" in
      *disconnected*|*"maximum number"*|*timeout*|*timed\ out*) sleep 3 ;;
      *) break ;;
    esac
  done
  printf '%s' "$out" >&2
  return 1
}

verify() { read_state >/dev/null 2>&1; }

# --- 4. Enrol --------------------------------------------------------------
# The car accepts only a couple of simultaneous BLE connections and phone keys
# hold those slots open, so "maximum number of BLE devices" is the single most
# likely failure here. It's a connection problem, not a key problem — retry the
# same role rather than falling back to a wider one.
request_key() {
  local role="$1" out
  while true; do
    echo "Requesting enrolment as role '$role' …"
    if out=$("$TESLA_CONTROL" -ble -vin "$VIN" add-key-request "$PUB_FILE" "$role" cloud_key 2>&1); then
      return 0
    fi
    printf '\n\033[31m%s\033[0m\n' "$out"
    case "$out" in
      *"maximum number of BLE devices"*)
        cat <<'TXT'
The car's Bluetooth slots are full — every nearby phone key holds one open.

  • Turn Bluetooth OFF on all phones near the car (or walk them away)
  • Close the Tesla app on them
  • Wait ~10 seconds for the car to drop the connections

You can turn phone Bluetooth back on as soon as enrolment succeeds.
TXT
        ;;
      *"Bluetooth turned on"*)
        echo "This Mac can't reach its Bluetooth adapter — check System Settings"
        echo "→ Privacy & Security → Bluetooth and allow your terminal."
        ;;
      *)
        echo "Check the car is awake (open a door) and within ~10m."
        ;;
    esac
    read -r -p $'\nRetry? [Enter to retry, s to give up] ' ans
    [ "$ans" = s ] && return 1
  done
}

say "Enrolling the key in $VIN"
if verify; then
  echo "Already enrolled — skipping the keycard step."
else
  cat <<TXT

Before continuing:
  • park the car within ~10m of this Mac
  • wake it (open a door, or the Tesla app)
  • turn Bluetooth OFF on nearby phones — their phone keys occupy the car's
    limited BLE slots and will block enrolment
  • have your NFC keycard in hand
  • macOS may ask this terminal for Bluetooth permission — allow it

TXT
  read -r -p "Ready? [Enter] "
  for attempt in "$ROLE" owner; do
    request_key "$attempt" || fail "Gave up. Re-run ./setup-tesla.sh to try again."
    echo
    read -r -p "Now TAP YOUR KEYCARD on the centre console, then press [Enter] "
    if verify; then
      ROLE="$attempt"
      echo "Enrolled as '$attempt'."
      break
    fi
    # The request went through but the key can't read charge state: either the
    # tap was missed, or the role isn't permitted to read it. Either way this
    # app needs those reads, so owner is the role that actually works.
    [ "$attempt" = owner ] && fail "Enrolment failed. Make sure you tap the keycard while the request is pending."
    echo "Role '$attempt' didn't take — retrying as 'owner'."
  done
fi

# --- 5. Verify -------------------------------------------------------------
say "Reading charge state"
read_state || fail "state read failed — car asleep, out of range, or its BLE slots are full"
echo

# --- 6. Wire into the app --------------------------------------------------
say "Enabling it in the menubar app"
if [ -x "$PROJECT_DIR/install-launchagent.sh" ]; then
  "$PROJECT_DIR/install-launchagent.sh"
else
  echo "install-launchagent.sh not found — export these before launching the app:"
fi

cat <<TXT

Done. Role: $ROLE

  export TESLA_VIN=$VIN
  export TESLA_KEY_FILE=$KEY_FILE
  export TESLA_CACHE_FILE=$CACHE_FILE

Check MAX_AMPS in goodwe_menubar/tesla.py matches your circuit breaker
(currently $(grep -m1 '^MAX_AMPS' "$PROJECT_DIR/goodwe_menubar/tesla.py" | awk '{print $3}')A). The dropdown's "Tesla:" line shows what it's doing.
TXT
