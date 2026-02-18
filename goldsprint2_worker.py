# Воркер для одного BLE-датчика: один процесс = одно подключение (обходит ограничение Windows).
# Запуск: python goldsprint2_worker.py <BLE_address> <rider_number>
# Пишет состояние в cadence_rider1.json / cadence_rider2.json в той же папке, что и goldsprint2.py.

import sys
import os
import asyncio
import json
import time

# Путь к папке основного скрипта (для cadence_riderN.json)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = SCRIPT_DIR
WHEEL_CIRCUMFERENCE_M = 2.1

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
if sys.platform == "win32":
    try:
        from bleak.backends.winrt.scanner import RawAdvData
    except Exception:
        RawAdvData = None
else:
    RawAdvData = None

HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR_UUID = "00002a5b-0000-1000-8000-00805f9b34fb"
FD09_UUID = "0000fd09-0000-1000-8000-00805f9b34fb"
FD19_UUID = "0000fd19-0000-1000-8000-00805f9b34fb"


def _ble_device_from_address(address):
    if not address or sys.platform != "win32" or RawAdvData is None:
        return address
    addr_clean = address.replace(":", "").replace("-", "")
    if len(addr_clean) != 12:
        return address
    try:
        bluetooth_address_int = int(addr_clean, 16)
    except ValueError:
        return address
    class _Adv:
        bluetooth_address = bluetooth_address_int
    details = RawAdvData(adv=_Adv(), scan=None)
    return BLEDevice(address, "ThinkRider", details)


def _find_all_notify_uuids(client):
    uuids = []
    try:
        for s in client.services:
            for c in s.characteristics:
                if "notify" in (c.properties or []):
                    uuids.append(str(c.uuid))
        return uuids
    except Exception:
        pass
    return []


def _parse_thinkrider_7byte(data):
    if not data or len(data) != 7:
        return None, None
    try:
        revs = int.from_bytes(data[1:3], byteorder="little")
        wheel_time = int.from_bytes(data[5:7], byteorder="little")
        return revs, wheel_time
    except Exception:
        return None, None


def _parse_csc_speed(data):
    if not data or len(data) < 7:
        return None, None
    flags = data[0]
    if (flags & 0x01) == 0:
        return None, None
    revs = int.from_bytes(data[1:5], byteorder="little")
    wheel_time = int.from_bytes(data[5:7], byteorder="little")
    return revs, wheel_time


def _compute_speed_from_csc(last_revs, last_time, revs, wheel_time):
    if last_revs is None or last_time is None:
        return None
    delta_revs = revs - last_revs
    if delta_revs < 0:
        return None
    delta_t = wheel_time - last_time
    if delta_t < 0:
        delta_t += 65536
    if delta_t == 0:
        return None
    delta_t_sec = delta_t / 1024.0
    return (delta_revs * WHEEL_CIRCUMFERENCE_M) / delta_t_sec


def _write_state(rider_number, speed, max_speed, revs, connected):
    path = os.path.join(STATE_DIR, f"cadence_rider{rider_number}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "speed": round(speed, 4),
                "max_speed": round(max_speed, 4),
                "revs": revs,
                "connected": connected,
                "ts": time.time(),
            }, f, ensure_ascii=False)
    except Exception:
        pass


async def run_ble_worker_main(address, rider_number):
    state = {"last_revs": None, "last_time": None, "speed": 0.0, "max_speed": 0.0, "revs": 0}
    client_arg = _ble_device_from_address(address)

    def on_data(sender, data):
        if data is not None and not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        revs, wheel_time = _parse_csc_speed(data)
        if revs is None:
            revs, wheel_time = _parse_thinkrider_7byte(data)
        speed = None
        if revs is not None:
            speed = _compute_speed_from_csc(
                state["last_revs"], state["last_time"], revs, wheel_time
            )
            state["last_revs"] = revs
            state["last_time"] = wheel_time
        if speed is not None and 0 <= speed <= 100:
            state["speed"] = speed
            state["revs"] = revs
            if speed > state["max_speed"]:
                state["max_speed"] = speed
        _write_state(rider_number, state["speed"], state["max_speed"], state["revs"], True)

    try:
        async with BleakClient(client_arg) as client:
            await asyncio.sleep(1.2)
            notify_uuids = _find_all_notify_uuids(client)
            if HR_CHAR_UUID not in notify_uuids and len(notify_uuids) > 0:
                await asyncio.sleep(1.2)
                notify_uuids = _find_all_notify_uuids(client)
            if HR_CHAR_UUID in notify_uuids:
                notify_uuids.remove(HR_CHAR_UUID)
                notify_uuids.insert(0, HR_CHAR_UUID)
            for char_uuid in notify_uuids:
                try:
                    await client.start_notify(char_uuid, on_data)
                except Exception:
                    pass
            for fallback_uuid in (HR_CHAR_UUID, FD09_UUID, FD19_UUID):
                if fallback_uuid in notify_uuids:
                    continue
                try:
                    await client.start_notify(fallback_uuid, on_data)
                except Exception:
                    pass
            while True:
                await asyncio.sleep(1)
    except Exception as e:
        print(f"Worker rider {rider_number} error: {e}", file=sys.stderr)
    finally:
        _write_state(rider_number, 0.0, state["max_speed"], state["revs"], False)


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--worker":
        sys.exit(1)
    address = sys.argv[2]
    rider_num = int(sys.argv[3])
    asyncio.run(run_ble_worker_main(address, rider_num))
