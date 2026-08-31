"""Connect to the inverter and dump everything it reports.

Useful for verifying the connection and seeing exactly which sensors your
inverter/meter exposes. Run with the project venv:

    ./.venv/bin/python diagnose.py [host] [port]

Defaults come from goodwe_menubar.config, i.e. whatever the app is using.
"""

import asyncio
import sys

import goodwe

from goodwe_menubar.config import load_config

# Sensor ids the menubar app cares about.
HEADLINE = [
    "ppv", "pv_power", "house_consumption",
    "meter_active_power_total", "meter_active_power",
    "active_power", "total_inverter_power",
    "e_day", "e_total",
    "vgrid", "vgrid1", "fgrid", "fgrid1", "temperature",
]


async def main():
    cfg = load_config()
    host = sys.argv[1] if len(sys.argv) > 1 else cfg["host"]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else cfg["port"]

    print(f"Connecting to {host}:{port} "
          f"({'Modbus TCP' if port == 502 else 'UDP'})…")
    inverter = await goodwe.connect(host=host, port=port,
                                    timeout=2, retries=3)

    print("\n=== Device ===")
    for attr in ("model_name", "serial_number", "rated_power", "firmware"):
        print(f"  {attr:14} {getattr(inverter, attr, None)}")

    data = await inverter.read_runtime_data()

    print("\n=== Headline sensors used by the menubar ===")
    present = {s.id_: s for s in inverter.sensors()}
    for sid in HEADLINE:
        if sid in data:
            unit = present[sid].unit if sid in present else ""
            print(f"  {sid:28} {data[sid]} {unit}")

    print("\n=== All runtime sensors ===")
    for sensor in inverter.sensors():
        if sensor.id_ in data:
            print(f"  {sensor.id_:28} {data[sensor.id_]!r:>14}  "
                  f"{sensor.unit:>5}   {sensor.name}")


if __name__ == "__main__":
    asyncio.run(main())
