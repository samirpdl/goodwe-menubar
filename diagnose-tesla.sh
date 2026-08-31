#!/usr/bin/env bash
# Measure how well this Mac can actually reach the car over Bluetooth.
#
#   ./diagnose-tesla.sh [ATTEMPTS]
#
# Runs repeated charge-state reads, tallies what fails and how long it takes,
# then tells you whether the link is good, marginal, or blocked — and which of
# those it is (range, occupied BLE slots, permissions, or the car being asleep).

set -uo pipefail

N="${1:-10}"
KEY_FILE="${TESLA_KEY_FILE:-$HOME/.tesla/key.pem}"
VIN="${TESLA_VIN:-}"
TESLA_CONTROL="$(command -v tesla-control || echo "$HOME/go/bin/tesla-control")"

[ -x "$TESLA_CONTROL" ] || { echo "tesla-control not found — run ./setup-tesla.sh"; exit 1; }
[ -f "$KEY_FILE" ]      || { echo "no key at $KEY_FILE — run ./setup-tesla.sh"; exit 1; }
if [ -z "$VIN" ]; then
  VIN="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:TESLA_VIN" \
        "$HOME/Library/LaunchAgents/com.local.goodwe-menubar.plist" 2>/dev/null)"
fi
if [ ${#VIN} -ne 17 ]; then
  echo "couldn't find a valid VIN (got '${VIN:-nothing}')."
  echo "Run:  TESLA_VIN=<your 17-char VIN> $0"
  exit 1
fi

echo "Probing ${VIN:0:5}…${VIN: -4} · $N attempts · key $KEY_FILE"
echo

# VCSEC answers over BLE whenever the car is in range — asleep or not, key or
# not. Probing it first splits the two failures that look identical from
# infotainment: a car that is asleep, and a car the radio can't reach.
ok=0; slots=0; range=0; auth=0; bt=0; other=0; total_ms=0; asleep=0
for i in $(seq 1 "$N"); do
  t0=$(python3 -c 'import time; print(int(time.time()*1000))')
  vcsec=$("$TESLA_CONTROL" -ble -vin "$VIN" -key-file "$KEY_FILE" \
          -connect-timeout 20s body-controller-state 2>&1)
  if [ $? -ne 0 ]; then
    t1=$(python3 -c 'import time; print(int(time.time()*1000))')
    ms=$((t1 - t0))
    case "$vcsec" in
      *"maximum number"*)                       slots=$((slots+1)); tag="slots full" ;;
      *"Bluetooth turned on"*|*"invalid state"*) bt=$((bt+1));      tag="adapter off" ;;
      *)                                        range=$((range+1)); tag="VCSEC silent — out of range" ;;
    esac
    printf '  %2d. \033[31mfail\033[0m    %5dms  %s\n' "$i" "$ms" "$tag"
    sleep 2
    continue
  fi
  case "$vcsec" in *ASLEEP*) asleep=$((asleep+1)) ;; esac
  out=$("$TESLA_CONTROL" -ble -vin "$VIN" -key-file "$KEY_FILE" \
        -connect-timeout 20s state charge 2>&1)
  rc=$?
  t1=$(python3 -c 'import time; print(int(time.time()*1000))')
  ms=$((t1 - t0))
  if [ $rc -eq 0 ]; then
    ok=$((ok + 1)); total_ms=$((total_ms + ms))
    printf '  %2d. \033[32mok\033[0m      %5dms\n' "$i" "$ms"
  else
    case "$out" in
      *"maximum number"*)      slots=$((slots+1)); tag="slots full" ;;
      *disconnected*|*timeout*|*"timed out"*|*"not found"*|*"no device"*|*"deadline exceeded"*)
                               range=$((range+1)); tag="no answer / dropped" ;;
      *"Bluetooth turned on"*|*"invalid state"*) bt=$((bt+1)); tag="adapter off" ;;
      *nauthoriz*|*"not authorized"*|*whitelist*|*key*) auth=$((auth+1)); tag="key rejected" ;;
      *)                       other=$((other+1)); tag="$(echo "$out" | head -1)" ;;
    esac
    printf '  %2d. \033[31mfail\033[0m    %5dms  %s\n' "$i" "$ms" "$tag"
  fi
  sleep 2
done

echo
pct=$(( ok * 100 / N ))
echo "success: $ok/$N (${pct}%)"
[ $asleep -gt 0 ] && echo "car was in range but asleep on $asleep of them"
[ $ok -gt 0 ] && echo "mean latency of successful reads: $(( total_ms / ok ))ms"
echo

verdict() { printf '\033[1m%s\033[0m\n' "$1"; }

if   [ $bt -gt 0 ] && [ $ok -eq 0 ]; then
  verdict "Bluetooth adapter unavailable."
  echo "System Settings → Privacy & Security → Bluetooth: allow your terminal."
  echo "Also check Bluetooth is switched on at all."
elif [ $auth -gt 0 ] && [ $ok -eq 0 ]; then
  verdict "The car is rejecting this key."
  echo "Re-run ./setup-tesla.sh to enrol again (keycard tap required)."
elif [ $slots -gt $(( N / 2 )) ]; then
  verdict "The car's BLE slots are occupied, not a range problem."
  echo "Phone keys hold slots open. Turn Bluetooth off on phones near the car,"
  echo "or accept that the app only gets through when they're away."
elif [ $pct -ge 90 ]; then
  verdict "Link is good. Nothing to fix."
elif [ $pct -ge 50 ]; then
  verdict "Link is marginal — this matches your symptoms."
  echo "The app retries and backs off, so it will mostly work, but expect gaps."
  echo "Try: move the Mac toward the car (a window on the car's side helps a lot),"
  echo "or park closer. Brick, concrete and foil-backed insulation kill 2.4GHz;"
  echo "a single plasterboard wall usually doesn't."
elif [ $ok -gt 0 ]; then
  verdict "Link is poor — usable only occasionally."
  echo "Moving the Mac is worth trying, but at this rate you're near the edge of"
  echo "range. Note macOS 13+ doesn't support most USB Bluetooth dongles, so a"
  echo "small always-on machine nearer the car is the reliable fix."
else
  verdict "No contact at all."
  echo "Is the car awake and within ~10m? Open a door and try again."
fi
