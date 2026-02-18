import sys
import tkinter as tk
from tkinter import ttk, messagebox
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
import asyncio
if sys.platform == "win32":
    try:
        from bleak.backends.winrt.scanner import RawAdvData
    except Exception:
        RawAdvData = None
else:
    RawAdvData = None
import os
import json
import queue
import threading
import time

# Базовая папка: при сборке в exe — папка с exe, иначе — папка скрипта
def _base_path():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _debug_log(msg, data, hypothesis_id):
    pass
if sys.platform == "win32":
    try:
        import winsound
    except ImportError:
        winsound = None
else:
    winsound = None

# Адреса Bluetooth датчиков ThinkRider (по умолчанию; загружаются из config при наличии)
CONFIG_PATH = os.path.join(_base_path(), "cadence_config.json")
DEFAULT_RIDER1 = "01:55:16.6840"
DEFAULT_RIDER2 = "01:58:00.9120"


def _read_config():
    """Прочитать весь config. Возвращает dict или пустой dict."""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения настроек: {e}")


def load_sensor_addresses():
    data = _read_config()
    return data.get("sensor1", DEFAULT_RIDER1), data.get("sensor2", DEFAULT_RIDER2)


def save_sensor_addresses(addr1, addr2):
    data = _read_config()
    data["sensor1"] = addr1.strip()
    data["sensor2"] = addr2.strip()
    _write_config(data)


def save_scanned_devices(devices_list):
    """Сохранить список отсканированных устройств в cadence_config.json. devices_list: [(display_name, address), ...]"""
    try:
        data = _read_config()
        data["scanned_devices"] = [{"name": n, "address": a} for n, a in devices_list]
        _write_config(data)
    except Exception as e:
        print(f"Ошибка сохранения списка устройств: {e}")


def load_scanned_devices():
    """Загрузить список устройств из cadence_config.json. Возвращает [(display_name, address), ...] или []."""
    data = _read_config()
    raw = data.get("scanned_devices", [])
    if not isinstance(raw, list):
        return []
    return [(item["name"], item["address"]) for item in raw if isinstance(item, dict) and "name" in item and "address" in item]


def clear_config():
    """Сбросить конфиг: адреса по умолчанию, пустой список устройств."""
    _write_config({
        "sensor1": DEFAULT_RIDER1,
        "sensor2": DEFAULT_RIDER2,
        "scanned_devices": []
    })


def _format_ble_address(addr_int):
    """Форматировать адрес BLE (int, 6 байт big-endian) в строку XX:XX:XX:XX:XX:XX."""
    return ":".join(f"{(addr_int >> (5 - i) * 8) & 0xFF:02X}" for i in range(6))


async def get_paired_ble_devices_win():
    """Получить BLE-устройства, известные Windows (в т.ч. сопряжённые). Только Windows. [(name, address), ...]."""
    if sys.platform != "win32":
        return []
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.enumeration import DeviceInformation
        infos = await DeviceInformation.find_all_async()
        seen = set()
        result = []
        for i in range(infos.size):
            info = infos.get_at(i)
            id_upper = (info.id or "").upper()
            if "BTHLE" not in id_upper and "BluetoothLE" not in id_upper:
                continue
            try:
                ledevice = await BluetoothLEDevice.from_id_async(info.id)
                if not ledevice:
                    continue
                addr_int = ledevice.bluetooth_address
                address = _format_ble_address(addr_int)
                if address in seen:
                    continue
                seen.add(address)
                name = (info.name or getattr(ledevice, "name", None) or "").strip()
                if not name:
                    name = f"(Сопряжено) {address}"
                result.append((name, address))
            except Exception:
                continue
        return result
    except Exception:
        return []


async def scan_ble_devices_async(timeout=8.0):
    """Сканирование BLE-устройств (реклама) + уже сопряжённые с ПК. Возвращает список (display_name, address)."""
    # Устройства из рекламы (сканирование)
    devices = await BleakScanner.discover(timeout=timeout)
    by_addr = {}
    for d in devices:
        display_name = (d.name or "").strip()
        if not display_name:
            display_name = f"(Без имени) {d.address}"
        by_addr[d.address.upper()] = (display_name, d.address)
    # Добавить сопряжённые с ПК (Windows) — они могут не рекламироваться
    paired = await get_paired_ble_devices_win()
    for name, address in paired:
        key = address.upper().replace("-", ":")
        if key not in by_addr:
            by_addr[key] = (name, address)
    return list(by_addr.values())


def run_ble_scan_then_callback(callback):
    """Запускает сканирование в отдельном потоке и вызывает callback(devices) в главном потоке."""
    def scan_thread():
        try:
            devices = asyncio.run(scan_ble_devices_async())
            root.after(0, lambda: callback(devices))
        except Exception as e:
            root.after(0, lambda: callback([], error=str(e)))

    threading.Thread(target=scan_thread, daemon=True).start()


RIDER1_ADDRESS, RIDER2_ADDRESS = load_sensor_addresses()

# Cycling Speed and Cadence (CSC) — стандартная характеристика скорости/каденса
# 0x2A5C = CSC Measurement (revs + event time; speed вычисляется по дельте)
SPEED_CHARACTERISTIC_UUID = "00002a5c-0000-1000-8000-00805f9b34fb"
# Длина окружности колеса в метрах (для перевода оборотов в м/с). Типично ~2.1 м для 700c.
WHEEL_CIRCUMFERENCE_M = 2.1
# Интервал обновления интерфейса (мс) — поток и таймер главного потока
UI_REFRESH_MS = 16
# Реальный рендер (полоски скорости, дистанции, подписи)
UI_RENDER_MS = 16
# Если дольше этого времени (сек) нет данных с датчика — показываем скорость 0 (колесо остановилось)
SPEED_STALE_SEC = 1.2
# Ниже этой скорости (км/ч) на UI показываем 0 (шум датчика при стоящем колесе)
SPEED_DISPLAY_THRESHOLD_KMH = 0.6
# Показывать 0 только после стольких подряд именно нулей из state (не «ниже порога»)
CONSECUTIVE_ZEROS_REQUIRED = 3
# Интервал пинга датчиков (сек), чтобы они не уходили в сон
SENSOR_PING_INTERVAL_SEC = 15

# Ссылки на цикл и задачи BLE для перезапуска без перезагрузки приложения
ble_loop = None
ble_tasks = []

# Единственное общее хранилище данных (без разбросанных глобальных переменных).
# Данные каждого датчика защищены своим замком: чтение/запись гонщика 1 и гонщика 2 могут идти в разных потоках без блокировки друг друга.
class AppState:
    """Потокобезопасное состояние: данные датчиков (по замку на гонщика) и гонки (общий замок)."""
    __slots__ = (
        "_lock_rider1", "_lock_rider2", "_lock_race",
        "distance", "rider1_speed", "rider2_speed", "rider1_max_speed", "rider2_max_speed",
        "_last_speed_time", "_csc_state", "_debug_raw_last",
        "rider1_distance_traveled", "rider2_distance_traveled",
        "race_start_time", "rider1_start_revs", "rider2_start_revs",
        "rider1_finished_time", "rider2_finished_time", "_finished_beeped",
        "_countdown_visible", "_countdown_after_ids", "_stopwatch_after_id",
    )

    def __init__(self):
        self._lock_rider1 = threading.Lock()
        self._lock_rider2 = threading.Lock()
        self._lock_race = threading.Lock()
        self.distance = 250
        self.rider1_speed = 0
        self.rider2_speed = 0
        self.rider1_max_speed = 0
        self.rider2_max_speed = 0
        self._last_speed_time = {1: 0, 2: 0}
        self._csc_state = {}
        self._debug_raw_last = {1: 0, 2: 0}
        self.rider1_distance_traveled = 0.0
        self.rider2_distance_traveled = 0.0
        self.race_start_time = None
        self.rider1_start_revs = None
        self.rider2_start_revs = None
        self.rider1_finished_time = None
        self.rider2_finished_time = None
        self._finished_beeped = {1: False, 2: False}
        self._countdown_visible = 0
        self._countdown_after_ids = []
        self._stopwatch_after_id = None

    def _lock_for_rider(self, rider_number):
        return self._lock_rider1 if rider_number == 1 else self._lock_rider2

    def get_snapshot(self):
        """Снять снимок для UI. Читает данные каждого датчика под своим замком — можно вызывать из любого потока."""
        now = time.time()
        with self._lock_rider1:
            t1 = self._last_speed_time.get(1, 0)
            r1 = self.rider1_speed
            stale_1 = t1 > 0 and (now - t1) > SPEED_STALE_SEC
            if stale_1:
                r1 = 0
            r1_max = self.rider1_max_speed
        with self._lock_rider2:
            t2 = self._last_speed_time.get(2, 0)
            r2 = self.rider2_speed
            stale_2 = t2 > 0 and (now - t2) > SPEED_STALE_SEC
            if stale_2:
                r2 = 0
            r2_max = self.rider2_max_speed
        # #region agent log
        if stale_1 or stale_2:
            _debug_log("get_snapshot stale zero", {"stale_1": stale_1, "stale_2": stale_2, "r1": r1, "r2": r2, "now": now, "t1": t1, "t2": t2}, "H5")
        # #endregion
        with self._lock_race:
            dist_target = self.distance
            rider1_distance = min(self.rider1_distance_traveled, dist_target) if dist_target > 0 else 0.0
            rider2_distance = min(self.rider2_distance_traveled, dist_target) if dist_target > 0 else 0.0
            r1ft = self.rider1_finished_time
            r2ft = self.rider2_finished_time
            start = self.race_start_time
            return {
                "rider1_speed": r1, "rider2_speed": r2,
                "rider1_max_speed": r1_max, "rider2_max_speed": r2_max,
                "race_start_time": start,
                "rider1_finished_time": r1ft, "rider2_finished_time": r2ft,
                "countdown_visible": self._countdown_visible, "finished_beeped": dict(self._finished_beeped),
                "distance": dist_target, "rider1_distance": rider1_distance, "rider2_distance": rider2_distance,
            }

    def reset_race_state(self):
        """Сброс гонки. Вызывать из главного потока (держит _lock_race и кратко замки гонщиков)."""
        with self._lock_race:
            self.race_start_time = None
            self.rider1_start_revs = None
            self.rider2_start_revs = None
            self.rider1_finished_time = None
            self.rider2_finished_time = None
            self._finished_beeped = {1: False, 2: False}
            self._countdown_visible = 0
            self.rider1_distance_traveled = 0.0
            self.rider2_distance_traveled = 0.0
        with self._lock_rider1:
            self.rider1_max_speed = 0
        with self._lock_rider2:
            self.rider2_max_speed = 0


app_state = AppState()

# Очередь запросов на обновление UI: отдельный поток кладёт метки, главный поток забирает и обновляет виджеты
_ui_update_queue = queue.Queue()
# Состояние для фильтра фантомного нуля и редкого рендера (только главный поток)
_display_speed_1 = 0.0
_display_speed_2 = 0.0
_consecutive_zeros_1 = 0
_consecutive_zeros_2 = 0
_last_render_time = 0.0

DEBUG_RAW_BLE = True  # выводить в консоль сырые данные, если скорость не распознана
COUNTDOWN_SEC = 3

# Material Design colors
COLORS = {
    "primary": "#1976D2",
    "primary_dark": "#1565C0",
    "primary_light": "#42A5F5",
    "secondary": "#FF5722",
    "secondary_dark": "#E64A19",
    "secondary_light": "#FF8A65",
    "surface": "#FAFAFA",
    "surface_card": "#FFFFFF",
    "background": "#EEEEEE",
    "on_primary": "#FFFFFF",
    "on_surface": "#212121",
    "on_surface_secondary": "#757575",
    "divider": "#E0E0E0",
    "success": "#4CAF50",
    "error": "#D32F2F",
    "rider1": "#1976D2",
    "rider2": "#FF5722",
}

# Парсинг CSC Measurement (0x2A5C): флаги, cumulative wheel revs (uint32 LE), last wheel event time (uint16 LE, 1/1024 s)
def _parse_csc_speed(data):
    """Возвращает (revs, wheel_time) или (None, None) если нет wheel data."""
    if not data or len(data) < 2:
        return None, None
    flags = data[0]
    has_wheel = (flags & 0x01) != 0
    if not has_wheel or len(data) < 7:
        return None, None
    revs = int.from_bytes(data[1:5], byteorder="little")
    wheel_time = int.from_bytes(data[5:7], byteorder="little")  # 1/1024 s, 16-bit rollover 64 s
    return revs, wheel_time


def _parse_indoor_bike_speed(data):
    """Indoor Bike Data (0x2AD2): флаги 2 байта, затем скорость 2 байта LE, 0.01 км/ч. Возвращает speed (м/с) или None."""
    if not data or len(data) < 4:
        return None
    try:
        speed_kmh = int.from_bytes(data[2:4], byteorder="little") / 100.0
        speed_ms = speed_kmh / 3.6
        if 0 <= speed_ms <= 50:
            return speed_ms
    except Exception:
        pass
    return None


def _parse_speed_fallback(data):
    """Если датчик шлёт не стандартный CSC: попытка прочитать скорость. Возвращает speed (м/с) или None."""
    if not data or len(data) < 1:
        return None
    try:
        if len(data) >= 2:
            # Вариант 1: 2 байта LE = скорость в 0.01 км/ч (часто в велокомпах)
            raw_kmh = int.from_bytes(data[:2], byteorder="little")
            speed = (raw_kmh / 100.0) / 3.6
            if 0 <= speed <= 50:
                return speed
            # Вариант 2: 2 байта LE = скорость в 0.01 м/с
            speed = raw_kmh / 100.0
            if 0 <= speed <= 50:
                return speed
        if len(data) >= 1:
            speed = data[0] / 10.0
            if 0 <= speed <= 50:
                return speed
    except Exception:
        pass
    return None


def _compute_speed_from_csc(last_revs, last_time, revs, wheel_time):
    """Скорость в м/с: (delta_revs * circumference) / delta_t_sec. Учитываем 16-bit rollover времени."""
    if last_revs is None or last_time is None:
        return None
    delta_revs = revs - last_revs
    if delta_revs < 0:
        return None  # или сброс счётчика
    delta_t = wheel_time - last_time
    if delta_t < 0:
        delta_t += 65536  # rollover 16-bit
    if delta_t == 0:
        return None
    if delta_revs == 0:
        return None
    delta_t_sec = delta_t / 1024.0
    speed_m_s = (delta_revs * WHEEL_CIRCUMFERENCE_M) / delta_t_sec
    return speed_m_s


# Обработка данных с датчика скорости (CSC), вызывается из BLE-потока; пишет в state
def handle_speed_data(sender, data, rider_number, state):
    """Вызывается из BLE-потока (или из потока датчика). Данные гонщика пишутся под своим замком — датчики не блокируют друг друга."""
    try:
        revs, wheel_time = _parse_csc_speed(data)
        speed = None
        lock_rider = state._lock_for_rider(rider_number)
        with lock_rider:
            if revs is not None:
                csc = state._csc_state.setdefault(rider_number, {"last_revs": None, "last_time": None})
                speed = _compute_speed_from_csc(csc["last_revs"], csc["last_time"], revs, wheel_time)
                csc["last_revs"] = revs
                csc["last_time"] = wheel_time
        if speed is None:
            speed = _parse_indoor_bike_speed(data)
        if speed is None:
            speed = _parse_speed_fallback(data)
        if speed is None:
            if DEBUG_RAW_BLE and data:
                now = time.time()
                with lock_rider:
                    if now - state._debug_raw_last.get(rider_number, 0) >= 2.0:
                        state._debug_raw_last[rider_number] = now
                        raw_hex = data.hex() if hasattr(data, "hex") else "".join(f"{b:02x}" for b in data)
                        print(f"[BLE rider {rider_number}] не распознано: len={len(data)} hex={raw_hex}")
            return
        if speed < 0 or speed > 100:
            return
        threshold_ms = SPEED_DISPLAY_THRESHOLD_KMH / 3.6
        with lock_rider:
            now = time.time()
            prev_time = state._last_speed_time.get(rider_number, now)
            if speed >= threshold_ms:
                state._last_speed_time[rider_number] = now
                if rider_number == 1:
                    state.rider1_speed = speed
                    if speed > state.rider1_max_speed:
                        state.rider1_max_speed = speed
                else:
                    state.rider2_speed = speed
                    if speed > state.rider2_max_speed:
                        state.rider2_max_speed = speed
        with state._lock_race:
            if speed >= threshold_ms and state.race_start_time is not None and state.distance > 0 and prev_time > 0:
                dt = now - prev_time
                if 0 < dt < 60:
                    if rider_number == 1:
                        state.rider1_distance_traveled = min(state.rider1_distance_traveled + speed * dt, state.distance)
                        dist_now = state.rider1_distance_traveled
                        finished = state.rider1_finished_time
                    else:
                        state.rider2_distance_traveled = min(state.rider2_distance_traveled + speed * dt, state.distance)
                        dist_now = state.rider2_distance_traveled
                        finished = state.rider2_finished_time
                    if finished is None and dist_now >= state.distance:
                        elapsed = time.time() - state.race_start_time
                        if rider_number == 1:
                            state.rider1_finished_time = elapsed
                        else:
                            state.rider2_finished_time = elapsed
        try:
            _ui_update_queue.put_nowait(True)
        except queue.Full:
            pass
    except (ValueError, IndexError, TypeError):
        return

def _format_stopwatch(seconds):
    """Форматирование времени для секундомера: ММ:СС.сс"""
    if seconds is None or seconds < 0:
        return "0.00 сек"
    m = int(seconds // 60)
    s = seconds % 60
    if m > 0:
        return f"{m}:{s:05.2f}"
    return f"{s:.2f} сек"


def _beep_finish():
    """Звук при финише гонщика (отличается от стартового)."""
    if winsound:
        try:
            winsound.Beep(660, 200)
        except Exception:
            pass
    else:
        try:
            print("\a", end="")
        except Exception:
            pass


def _update_stopwatch_labels(state):
    """Обновить обратный отсчёт, «кто финишировал / кто первый», и подписи времени секундомера. Вызывается из главного потока."""
    try:
        with state._lock_race:
            cd = state._countdown_visible
            race_start = state.race_start_time
            r1ft = state.rider1_finished_time
            r2ft = state.rider2_finished_time
            f1 = r1ft is not None
            f2 = r2ft is not None
            finished_beeped = dict(state._finished_beeped)
        if cd > 0:
            countdown_label.config(text=str(cd), font=("Segoe UI", 56, "bold"), fg=COLORS["primary"])
        elif race_start is not None:
            if not finished_beeped.get(1, False) and f1:
                _beep_finish()
                with state._lock_race:
                    state._finished_beeped[1] = True
            if not finished_beeped.get(2, False) and f2:
                _beep_finish()
                with state._lock_race:
                    state._finished_beeped[2] = True
            if f1 and f2:
                if r1ft < r2ft:
                    countdown_label.config(text="Гонщик 1 — первый!", font=("Segoe UI", 20, "bold"), fg=COLORS["rider1"])
                elif r2ft < r1ft:
                    countdown_label.config(text="Гонщик 2 — первый!", font=("Segoe UI", 20, "bold"), fg=COLORS["rider2"])
                else:
                    countdown_label.config(text="Ничья!", font=("Segoe UI", 20, "bold"), fg=COLORS["on_surface"])
            elif f1:
                countdown_label.config(text="Гонщик 1 финишировал!", font=("Segoe UI", 20, "bold"), fg=COLORS["rider1"])
            elif f2:
                countdown_label.config(text="Гонщик 2 финишировал!", font=("Segoe UI", 20, "bold"), fg=COLORS["rider2"])
            else:
                countdown_label.config(text="", font=("Segoe UI", 56, "bold"), fg=COLORS["primary"])
        else:
            countdown_label.config(text="", font=("Segoe UI", 56, "bold"), fg=COLORS["primary"])
        if race_start is None:
            time_label1.config(text="0.00 сек", font=("Segoe UI", 14, "bold"))
            time_label2.config(text="0.00 сек", font=("Segoe UI", 14, "bold"))
            return
        now = time.time()
        elapsed = now - race_start
        t1 = r1ft if f1 else elapsed
        t2 = r2ft if f2 else elapsed
        time_label1.config(text=_format_stopwatch(t1), font=("Segoe UI", 14, "bold"))
        time_label2.config(text=_format_stopwatch(t2), font=("Segoe UI", 14, "bold"))
        with state._lock_race:
            state._stopwatch_after_id = None
    except NameError:
        pass


def _beep():
    """Короткий звуковой сигнал по окончании обратного отсчёта."""
    if winsound:
        try:
            winsound.Beep(880, 300)
        except Exception:
            pass
    else:
        try:
            print("\a", end="")
        except Exception:
            pass


def _countdown_finished(state):
    """По окончании обратного отсчёта: звук и старт секундомера."""
    _beep()
    with state._lock_race:
        state._countdown_visible = 0
        state._countdown_after_ids = []
        state._finished_beeped = {1: False, 2: False}
        state.race_start_time = time.time()
        state.rider1_start_revs = state._csc_state.get(1, {}).get("last_revs")
        state.rider2_start_revs = state._csc_state.get(2, {}).get("last_revs")
        state.rider1_finished_time = None
        state.rider2_finished_time = None
        state.rider1_distance_traveled = 0.0
        state.rider2_distance_traveled = 0.0
    try:
        start_race_button.config(state=tk.NORMAL)
    except NameError:
        pass
    _ui_update_queue.put_nowait(True)


def start_race(state):
    """Старт гонки: обратный отсчёт 3–2–1, затем звук и секундомеры."""
    with state._lock_race:
        dist = state.distance
        after_ids = list(state._countdown_after_ids)
        stopwatch_id = state._stopwatch_after_id
    if dist <= 0:
        try:
            messagebox.showinfo("Гонка", "Сначала установите расстояние (м).", parent=root)
        except Exception:
            pass
        return
    for aid in after_ids:
        try:
            root.after_cancel(aid)
        except Exception:
            pass
    if stopwatch_id is not None:
        try:
            root.after_cancel(stopwatch_id)
        except Exception:
            pass
    with state._lock_race:
        state._countdown_after_ids = []
        state._countdown_visible = 3
    try:
        start_race_button.config(state=tk.DISABLED)
    except NameError:
        pass
    _ui_update_queue.put_nowait(True)
    a1 = root.after(1000, lambda: _set_countdown(2, state))
    a2 = root.after(2000, lambda: _set_countdown(1, state))
    a3 = root.after(3000, lambda: _countdown_finished(state))
    with state._lock_race:
        state._countdown_after_ids = [a1, a2, a3]


def _set_countdown(n, state):
    with state._lock_race:
        state._countdown_visible = n
    _ui_update_queue.put_nowait(True)


def _ui_refresh_worker(state):
    """Поток обновления UI: непрерывно кладёт запрос на обновление в очередь каждые UI_REFRESH_MS мс."""
    while True:
        try:
            time.sleep(UI_REFRESH_MS / 1000.0)
            _ui_update_queue.put_nowait(True)
        except queue.Full:
            pass
        except Exception:
            break


def _build_display_snapshot(snapshot):
    """Порог 0.6 км/ч. Ноль на UI только после N подряд именно нулей из state (ниже порога — не считаем)."""
    global _display_speed_1, _display_speed_2, _consecutive_zeros_1, _consecutive_zeros_2
    threshold_ms = SPEED_DISPLAY_THRESHOLD_KMH / 3.6
    raw_r1, raw_r2 = snapshot["rider1_speed"], snapshot["rider2_speed"]
    # Для отображения: ниже порога показываем как 0 (или держим предыдущее)
    r1 = raw_r1 if raw_r1 * 3.6 >= SPEED_DISPLAY_THRESHOLD_KMH else 0
    r2 = raw_r2 if raw_r2 * 3.6 >= SPEED_DISPLAY_THRESHOLD_KMH else 0
    # Считаем подряд только «настоящие» нули (state прислал 0), не «ниже порога»
    real_zero_1 = raw_r1 <= 0 or raw_r1 * 3.6 < 0.01
    real_zero_2 = raw_r2 <= 0 or raw_r2 * 3.6 < 0.01

    if real_zero_1:
        _consecutive_zeros_1 += 1
        if _consecutive_zeros_1 >= CONSECUTIVE_ZEROS_REQUIRED:
            _display_speed_1 = 0
        elif _display_speed_1 >= threshold_ms:
            r1 = _display_speed_1
    else:
        _consecutive_zeros_1 = 0
        if r1 >= threshold_ms:
            _display_speed_1 = r1
        elif _display_speed_1 >= threshold_ms:
            r1 = _display_speed_1

    if real_zero_2:
        _consecutive_zeros_2 += 1
        if _consecutive_zeros_2 >= CONSECUTIVE_ZEROS_REQUIRED:
            _display_speed_2 = 0
        elif _display_speed_2 >= threshold_ms:
            r2 = _display_speed_2
    else:
        _consecutive_zeros_2 = 0
        if r2 >= threshold_ms:
            _display_speed_2 = r2
        elif _display_speed_2 >= threshold_ms:
            r2 = _display_speed_2

    return {**snapshot, "rider1_speed": r1, "rider2_speed": r2}


def _ui_drain_and_update(state):
    """Главный поток: забирает запросы, тянет данные; рендер полос/подписей — реже (UI_RENDER_MS)."""
    global _last_render_time
    try:
        while True:
            try:
                _ui_update_queue.get_nowait()
            except queue.Empty:
                break
        snapshot = state.get_snapshot()
        _update_stopwatch_labels(state)
        now = time.time()
        if (now - _last_render_time) * 1000 >= UI_RENDER_MS:
            _last_render_time = now
            display_snapshot = _build_display_snapshot(snapshot)
            max_speed_label1.config(text=f"{snapshot['rider1_max_speed'] * 3.6:.2f} км/ч макс.")
            max_speed_label2.config(text=f"{snapshot['rider2_max_speed'] * 3.6:.2f} км/ч макс.")
            update_projection(display_snapshot)
    except (NameError, tk.TclError):
        pass
    try:
        root.after(UI_REFRESH_MS, lambda: _ui_drain_and_update(state))
    except (NameError, tk.TclError):
        pass


def reset_race(state):
    """Сброс гонки: остановить обратный отсчёт/секундомеры, обнулить время, финиш и макс. скорость."""
    with state._lock_race:
        after_ids = list(state._countdown_after_ids)
        stopwatch_id = state._stopwatch_after_id
        state._countdown_after_ids = []
        state._stopwatch_after_id = None
    for aid in after_ids:
        try:
            root.after_cancel(aid)
        except Exception:
            pass
    if stopwatch_id is not None:
        try:
            root.after_cancel(stopwatch_id)
        except Exception:
            pass
    state.reset_race_state()
    try:
        start_race_button.config(state=tk.NORMAL)
    except NameError:
        pass
    _update_stopwatch_labels(state)
    try:
        max_speed_label1.config(text="0.00 км/ч макс.")
        max_speed_label2.config(text="0.00 км/ч макс.")
    except NameError:
        pass


def update_gui(state):
    """Обновить интерфейс по снимку state (вызывается из главного потока)."""
    snapshot = state.get_snapshot()
    _update_stopwatch_labels(state)
    max_speed_label1.config(text=f"{snapshot['rider1_max_speed'] * 3.6:.2f} км/ч макс.")
    max_speed_label2.config(text=f"{snapshot['rider2_max_speed'] * 3.6:.2f} км/ч макс.")
    update_projection(_build_display_snapshot(snapshot))

# Функция для обновления проекции гонки
def _draw_round_rect(c, x1, y1, x2, y2, radius, **kw):
    rad = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    if rad <= 0:
        c.create_rectangle(x1, y1, x2, y2, **kw)
        return
    fill = kw.get("fill", "")
    outline = kw.get("outline", fill)
    c.create_arc(x1, y1, x1 + 2*rad, y1 + 2*rad, start=90, extent=90, fill=fill, outline=outline)
    c.create_arc(x2 - 2*rad, y1, x2, y1 + 2*rad, start=0, extent=90, fill=fill, outline=outline)
    c.create_arc(x2 - 2*rad, y2 - 2*rad, x2, y2, start=270, extent=90, fill=fill, outline=outline)
    c.create_arc(x1, y2 - 2*rad, x1 + 2*rad, y2, start=180, extent=90, fill=fill, outline=outline)
    c.create_rectangle(x1 + rad, y1, x2 - rad, y2, fill=fill, outline=outline)
    c.create_rectangle(x1, y1 + rad, x1 + rad, y2 - rad, fill=fill, outline=outline)
    c.create_rectangle(x2 - rad, y1 + rad, x2, y2 - rad, fill=fill, outline=outline)
    c.create_rectangle(x1 + rad, y1, x2 - rad, y1 + rad, fill=fill, outline=outline)
    c.create_rectangle(x1 + rad, y2 - rad, x2 - rad, y2, fill=fill, outline=outline)


def update_projection(snapshot_or_event=None):
    """Перерисовать полоски: при установленном расстоянии — прогресс 0..расстояние (м), иначе — скорость (км/ч)."""
    if isinstance(snapshot_or_event, dict):
        snapshot = snapshot_or_event
    else:
        try:
            snapshot = _build_display_snapshot(app_state.get_snapshot())
        except NameError:
            return
    canvas.delete("all")
    w = max(canvas.winfo_width(), 360)
    h = max(canvas.winfo_height(), 180)
    pad = 24
    bar_h = min(28, (h - 80) // 2 - 10)
    gap = 20
    label_w = 140
    max_bar_w = w - pad * 2 - label_w
    dist_target = snapshot.get("distance") or 0
    d1 = snapshot.get("rider1_distance") or 0
    d2 = snapshot.get("rider2_distance") or 0
    r1 = snapshot["rider1_speed"]
    r2 = snapshot["rider2_speed"]

    if dist_target > 0:
        # Полоски по пройденному расстоянию (накопление по приходящей скорости): от 0 до установленного (м)
        progress1 = min(d1 / dist_target, 1.0)
        progress2 = min(d2 / dist_target, 1.0)
        bar1_w = max_bar_w if progress1 >= 1.0 else int(progress1 * max_bar_w)
        bar2_w = max_bar_w if progress2 >= 1.0 else int(progress2 * max_bar_w)
        label1 = f"{d1:.1f} / {dist_target:.0f} м   {r1 * 3.6:.1f} км/ч"
        label2 = f"{d2:.1f} / {dist_target:.0f} м   {r2 * 3.6:.1f} км/ч"
    else:
        # Без установленного расстояния — по скорости (как раньше)
        scale = max((r1, r2), default=1) or 1
        bar_scale = max_bar_w / max(scale * 1.2, 1)
        bar1_w = min(int(r1 * bar_scale), max_bar_w)
        bar2_w = min(int(r2 * bar_scale), max_bar_w)
        label1 = f"{r1 * 3.6:.1f} км/ч"
        label2 = f"{r2 * 3.6:.1f} км/ч"

    bar_right = pad + max_bar_w
    y1 = pad + 30
    _draw_round_rect(canvas, pad, y1, bar_right, y1 + bar_h, 14, fill=COLORS["divider"], outline="")
    if bar1_w > 0:
        _draw_round_rect(canvas, pad, y1, pad + bar1_w, y1 + bar_h, 14, fill=COLORS["rider1"], outline="")
    canvas.create_text(bar_right + 8, y1 + bar_h // 2, text=label1, fill=COLORS["on_surface"], font=("Segoe UI", 10, "bold"), anchor="w")

    y2 = y1 + bar_h + gap + 30
    _draw_round_rect(canvas, pad, y2, bar_right, y2 + bar_h, 14, fill=COLORS["divider"], outline="")
    if bar2_w > 0:
        _draw_round_rect(canvas, pad, y2, pad + bar2_w, y2 + bar_h, 14, fill=COLORS["rider2"], outline="")
    canvas.create_text(bar_right + 8, y2 + bar_h // 2, text=label2, fill=COLORS["on_surface"], font=("Segoe UI", 10, "bold"), anchor="w")

def set_distance(state):
    try:
        with state._lock_race:
            state.distance = float(distance_entry.get())
        _ui_update_queue.put_nowait(True)
    except ValueError:
        pass

# Минимальный размер окна настроек
SETTINGS_MIN_WIDTH = 600
SETTINGS_MIN_HEIGHT = 600

# Окно настроек (датчики 1 и 2) с выпадающим списком BLE-устройств
def open_settings():
    win = tk.Toplevel(root)
    win.title("Настройки")
    win.configure(bg=COLORS["background"])
    win.minsize(SETTINGS_MIN_WIDTH, SETTINGS_MIN_HEIGHT)
    win.transient(root)
    win.grab_set()

    # Позиция: по центру над главным окном; начальный размер не меньше минимума
    root.update_idletasks()
    rx = root.winfo_x()
    ry = root.winfo_y()
    rw = root.winfo_width()
    rh = root.winfo_height()
    sw = SETTINGS_MIN_WIDTH
    sh = SETTINGS_MIN_HEIGHT
    x = rx + (rw - sw) // 2
    y = ry + (rh - sh) // 2
    win.geometry(f"{sw}x{sh}+{max(0, x)}+{max(0, y)}")

    # Храним отсканированные устройства (display_name, address)
    devices_list = []
    spinner_id = None

    def log_msg(msg):
        try:
            log_text.insert(tk.END, msg + "\n")
            log_text.see(tk.END)
        except tk.TclError:
            pass

    def set_loading(loading):
        """Показать/скрыть загрузку и заблокировать/разблокировать форму."""
        if loading:
            try:
                content_frame.pack_forget()
            except tk.TclError:
                pass
            loading_frame.pack(fill=tk.BOTH, expand=True)
            win.update_idletasks()
            _start_spinner()
        else:
            _stop_spinner()
            loading_frame.pack_forget()
            content_frame.pack(fill=tk.BOTH, expand=True)

    def _start_spinner():
        nonlocal spinner_id
        def tick(angle=0):
            nonlocal spinner_id
            spinner_canvas.delete("spinner")
            # дуга по кругу (индикатор загрузки)
            x, y, r = 40, 40, 32
            extent = 80
            spinner_canvas.create_arc(x - r, y - r, x + r, y + r, start=angle, extent=extent, outline=COLORS["primary"], width=4, tags="spinner")
            spinner_id = spinner_canvas.after(50, lambda: tick((angle + 24) % 360))
        tick()

    def _stop_spinner():
        nonlocal spinner_id
        if spinner_id:
            spinner_canvas.after_cancel(spinner_id)
            spinner_id = None
        spinner_canvas.delete("spinner")

    def _enable_content(enable):
        state_ttk = "readonly" if enable else "disabled"
        state_tk = tk.NORMAL if enable else tk.DISABLED
        combo1.configure(state=state_ttk)
        combo2.configure(state=state_ttk)
        entry1.configure(state=state_tk)
        entry2.configure(state=state_tk)
        btn_apply.configure(state=state_tk)
        btn_close.configure(state=state_tk)
        btn_rescan.configure(state=state_tk)
        btn_verify.configure(state=state_tk)
        btn_clear_config.configure(state=state_tk)

    def apply_devices_to_ui():
        """Заполнить выпадающие списки из devices_list и синхронизировать с сохранённым адресом."""
        nonlocal devices_list
        if not devices_list:
            names = ["— Устройства не найдены —"]
        else:
            names = [d[0] for d in devices_list]
        combo1["values"] = names
        combo2["values"] = names
        addr1 = entry1.get().strip()
        addr2 = entry2.get().strip()
        name1 = next((n for n, a in devices_list if a == addr1), None)
        name2 = next((n for n, a in devices_list if a == addr2), None)
        combo1.set(name1 if name1 else "— Выберите устройство —")
        combo2.set(name2 if name2 else "— Выберите устройство —")

    def on_scan_done(devices, error=None):
        nonlocal devices_list
        set_loading(False)
        _enable_content(True)
        if error:
            devices_list = []
            combo1["values"] = ["— Ошибка сканирования —"]
            combo2["values"] = ["— Ошибка сканирования —"]
            combo1.set("— Ошибка сканирования —")
            combo2.set("— Ошибка сканирования —")
            return
        devices_list = devices
        save_scanned_devices(devices_list)
        apply_devices_to_ui()

    def on_rescan():
        set_loading(True)
        _enable_content(False)
        run_ble_scan_then_callback(on_scan_done)

    def on_combo1_select(_event=None):
        sel = combo1.get()
        addr = next((a for n, a in devices_list if n == sel), None)
        if addr:
            entry1.delete(0, tk.END)
            entry1.insert(0, addr)

    def on_combo2_select(_event=None):
        sel = combo2.get()
        addr = next((a for n, a in devices_list if n == sel), None)
        if addr:
            entry2.delete(0, tk.END)
            entry2.insert(0, addr)

    # === Блок загрузки (спиннер) — только при сканировании ===
    loading_frame = tk.Frame(win, bg=COLORS["background"])
    spinner_canvas = tk.Canvas(loading_frame, width=80, height=80, bg=COLORS["background"], highlightthickness=0)
    spinner_canvas.pack(pady=(40, 8))
    tk.Label(loading_frame, text="Сканирование BLE...", font=("Segoe UI", 11), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack()

    # === Контент (форма) ===
    content_frame = tk.Frame(win, bg=COLORS["background"], padx=24, pady=20)

    tk.Label(content_frame, text="Настройки датчиков", font=("Segoe UI", 14, "bold"), fg=COLORS["on_surface"], bg=COLORS["background"]).pack(anchor="w", pady=(0, 12))

    # Датчик 1
    tk.Label(content_frame, text="Датчик 1", font=("Segoe UI", 10), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w")
    row1 = tk.Frame(content_frame, bg=COLORS["background"])
    row1.pack(fill=tk.X, pady=(2, 4))
    combo1 = ttk.Combobox(row1, font=("Segoe UI", 10), width=28, state="readonly")
    combo1.pack(side=tk.LEFT, padx=(0, 8))
    combo1.set("— Загрузка... —")
    combo1.bind("<<ComboboxSelected>>", on_combo1_select)
    tk.Label(content_frame, text="Адрес BLE", font=("Segoe UI", 9), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w")
    entry1 = tk.Entry(content_frame, font=("Segoe UI", 10), width=32, relief=tk.FLAT, highlightthickness=1, highlightcolor=COLORS["primary"], highlightbackground=COLORS["divider"])
    entry1.pack(fill=tk.X, pady=(2, 12), ipady=6, ipadx=8)
    stored_addr1, stored_addr2 = load_sensor_addresses()
    entry1.insert(0, stored_addr1)

    # Датчик 2
    tk.Label(content_frame, text="Датчик 2", font=("Segoe UI", 10), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w")
    row2 = tk.Frame(content_frame, bg=COLORS["background"])
    row2.pack(fill=tk.X, pady=(2, 4))
    combo2 = ttk.Combobox(row2, font=("Segoe UI", 10), width=28, state="readonly")
    combo2.pack(side=tk.LEFT, padx=(0, 8))
    combo2.set("— Загрузка... —")
    combo2.bind("<<ComboboxSelected>>", on_combo2_select)
    tk.Label(content_frame, text="Адрес BLE", font=("Segoe UI", 9), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w")
    entry2 = tk.Entry(content_frame, font=("Segoe UI", 10), width=32, relief=tk.FLAT, highlightthickness=1, highlightcolor=COLORS["primary"], highlightbackground=COLORS["divider"])
    entry2.pack(fill=tk.X, pady=(2, 12), ipady=6, ipadx=8)
    entry2.insert(0, stored_addr2)

    tk.Label(content_frame, text="Выберите устройство по имени или введите адрес. Применить — сохранить и перезапустить подключение без закрытия окна.", font=("Segoe UI", 9), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w", pady=(0, 8))

    # Окно лога (проверка подключения и действия)
    tk.Label(content_frame, text="Лог", font=("Segoe UI", 10), fg=COLORS["on_surface_secondary"], bg=COLORS["background"]).pack(anchor="w", pady=(4, 2))
    log_frame = tk.Frame(content_frame, bg=COLORS["surface_card"])
    log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    log_text = tk.Text(log_frame, font=("Consolas", 9), height=8, wrap=tk.WORD, state=tk.NORMAL, bg="#f5f5f5", fg=COLORS["on_surface"], relief=tk.FLAT, padx=8, pady=8)
    log_scroll = tk.Scrollbar(log_frame)
    log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.configure(command=log_text.yview)
    log_msg("Откройте настройки. Введите адреса и нажмите «Проверить подключение» или «Применить».")

    def on_apply():
        a1, a2 = entry1.get().strip(), entry2.get().strip()
        if not a1 or not a2:
            log_msg("[Применить] Ошибка: оба адреса должны быть заполнены.")
            return
        log_msg("[Применить] Сопряжение с выбранными устройствами (при необходимости появится окно Windows)...")
        btn_apply.configure(state=tk.DISABLED)

        async def pair_then_save():
            r1_ok, r1_msg = await verify_connection_async(a1)
            r2_ok, r2_msg = await verify_connection_async(a2)
            return (r1_ok, r1_msg), (r2_ok, r2_msg)

        def run_pair_thread():
            try:
                (r1_ok, r1_msg), (r2_ok, r2_msg) = asyncio.run(pair_then_save())
                def update_ui():
                    global RIDER1_ADDRESS, RIDER2_ADDRESS
                    log_msg(f"  Датчик 1: {'OK' if r1_ok else 'Ошибка'} — {r1_msg}")
                    log_msg(f"  Датчик 2: {'OK' if r2_ok else 'Ошибка'} — {r2_msg}")
                    RIDER1_ADDRESS = a1
                    RIDER2_ADDRESS = a2
                    save_sensor_addresses(a1, a2)
                    restart_bleak_tasks(app_state)
                    log_msg("[Применить] Адреса сохранены, подключение перезапущено.")
                    btn_apply.configure(state=tk.NORMAL)
                root.after(0, update_ui)
            except Exception as e:
                def err_ui():
                    btn_apply.configure(state=tk.NORMAL)
                    log_msg(f"  Ошибка сопряжения: {e}")
                root.after(0, err_ui)

        threading.Thread(target=run_pair_thread, daemon=True).start()

    def run_verify():
        a1, a2 = entry1.get().strip(), entry2.get().strip()
        if not a1 or not a2:
            log_msg("[Проверка] Заполните оба адреса.")
            return
        log_msg("[Проверка] Проверка подключения...")
        btn_verify.configure(state=tk.DISABLED)

        async def do_verify():
            r1_ok, r1_msg = await verify_connection_async(a1)
            r2_ok, r2_msg = await verify_connection_async(a2)
            return (r1_ok, r1_msg), (r2_ok, r2_msg)

        def on_verify_done():
            try:
                (r1_ok, r1_msg), (r2_ok, r2_msg) = asyncio.run(do_verify())
                def update_ui():
                    btn_verify.configure(state=tk.NORMAL)
                    log_msg(f"  Датчик 1 ({a1}): {'OK' if r1_ok else 'Ошибка'} — {r1_msg}")
                    log_msg(f"  Датчик 2 ({a2}): {'OK' if r2_ok else 'Ошибка'} — {r2_msg}")
                root.after(0, update_ui)
            except Exception as e:
                root.after(0, lambda: (btn_verify.configure(state=tk.NORMAL), log_msg(f"  Ошибка проверки: {e}")))

        threading.Thread(target=on_verify_done, daemon=True).start()

    btn_row = tk.Frame(content_frame, bg=COLORS["background"])
    btn_row.pack(fill=tk.X)

    btn_apply = tk.Button(btn_row, text="Применить", font=("Segoe UI", 10), fg=COLORS["on_primary"], bg=COLORS["primary"], relief=tk.FLAT, padx=16, pady=8, cursor="hand2", command=on_apply)
    btn_apply.pack(side=tk.LEFT, padx=(0, 8))
    btn_close = tk.Button(btn_row, text="Закрыть", font=("Segoe UI", 10), fg=COLORS["on_surface"], bg=COLORS["divider"], relief=tk.FLAT, padx=16, pady=8, cursor="hand2", command=win.destroy)
    btn_close.pack(side=tk.LEFT, padx=(0, 8))
    btn_verify = tk.Button(btn_row, text="Проверить подключение", font=("Segoe UI", 10), fg=COLORS["on_surface"], bg=COLORS["divider"], relief=tk.FLAT, padx=10, pady=8, cursor="hand2", command=run_verify)
    btn_verify.pack(side=tk.LEFT, padx=(0, 8))
    btn_rescan = tk.Button(btn_row, text="Сканировать снова", font=("Segoe UI", 10), fg=COLORS["on_surface"], bg=COLORS["divider"], relief=tk.FLAT, padx=14, pady=8, cursor="hand2", command=on_rescan)
    btn_rescan.pack(side=tk.LEFT, padx=(0, 8))

    def on_clear_config():
        if messagebox.askyesno("Очистить конфиг", "Сбросить адреса датчиков и список устройств? При следующем открытии настроек будет запущено сканирование."):
            nonlocal devices_list
            clear_config()
            devices_list = []
            entry1.delete(0, tk.END)
            entry2.delete(0, tk.END)
            entry1.insert(0, DEFAULT_RIDER1)
            entry2.insert(0, DEFAULT_RIDER2)
            combo1["values"] = ["— Устройства не найдены —"]
            combo2["values"] = ["— Устройства не найдены —"]
            combo1.set("— Выберите устройство —")
            combo2.set("— Выберите устройство —")
            log_msg("[Конфиг] Конфиг очищен. Адреса сброшены, список устройств пуст.")

    btn_clear_config = tk.Button(btn_row, text="Очистить конфиг", font=("Segoe UI", 10), fg=COLORS["on_surface"], bg=COLORS["divider"], relief=tk.FLAT, padx=10, pady=8, cursor="hand2", command=on_clear_config)
    btn_clear_config.pack(side=tk.LEFT)

    # При открытии: если в config есть список устройств — показать форму сразу; иначе запустить сканирование
    saved_devices = load_scanned_devices()
    if saved_devices:
        devices_list = saved_devices
        content_frame.pack(fill=tk.BOTH, expand=True)
        apply_devices_to_ui()
        _enable_content(True)
    else:
        _enable_content(False)
        set_loading(True)
        run_ble_scan_then_callback(on_scan_done)

# На Windows при подключении по строковому адресу Bleak ищет устройство через сканер (только реклама).
# Уже сопряжённые устройства часто не рекламируются → "Device was not found".
# Передаём BleakClient BLEDevice с details.adv.bluetooth_address (int), чтобы использовался
# FromBluetoothAddressAsync без сканирования — тогда сопряжённое устройство находится.
def _ble_device_from_address(address):
    """Для Windows: BLEDevice по адресу, чтобы подключение шло через FromBluetoothAddressAsync, без скана."""
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


def _is_valid_ble_address(address):
    """Проверка, что строка похожа на BLE MAC (12 hex-символов, разделители : или - допускаются)."""
    if not address or not isinstance(address, str):
        return False
    clean = address.replace(":", "").replace("-", "")
    return len(clean) == 12 and all(c in "0123456789ABCDEFabcdef" for c in clean)


# Обновление статуса датчика (вызывать через root.after(0, ...) из BLE-потока)
def set_sensor_status(rider_number, status):
    """status: 'Подключён', 'Не подключён', 'Ошибка', 'Подключён (нет данных)'"""
    try:
        fg = COLORS["success"] if status == "Подключён" else (COLORS["error"] if status == "Ошибка" else COLORS["on_surface_secondary"])
        if rider_number == 1:
            sensor_status_label1.config(text=status, fg=fg)
        elif rider_number == 2:
            sensor_status_label2.config(text=status, fg=fg)
    except NameError:
        pass

def _find_csc_measurement_uuid(client):
    """Найти UUID характеристики скорости: CSC (0x2A5C), Indoor Bike (0x2AD2) или любой с notify. Возвращает str или None."""
    try:
        found_any = None
        for s in client.services:
            for c in s.characteristics:
                uuid_str = str(c.uuid).lower()
                if "notify" not in (c.properties or []):
                    continue
                if "2a5c" in uuid_str:
                    return str(c.uuid)
                if "2ad2" in uuid_str:
                    found_any = str(c.uuid)
                elif found_any is None:
                    found_any = str(c.uuid)
        return found_any
    except Exception:
        pass
    return None


# Асинхронная функция для подключения к датчику
# rider_numbers: один int (1 или 2) или список [1, 2] — один датчик на двух гонщиков
async def connect_to_rider(address, rider_numbers, state):
    if isinstance(rider_numbers, int):
        rider_numbers = [rider_numbers]
    client_arg = _ble_device_from_address(address)
    try:
        async with BleakClient(client_arg) as client:
            for r in rider_numbers:
                root.after(0, lambda rn=r: set_sensor_status(rn, "Подключён"))
            char_uuid = _find_csc_measurement_uuid(client) or SPEED_CHARACTERISTIC_UUID
            def callback(sender, data, riders=rider_numbers, st=state):
                for r in riders:
                    handle_speed_data(sender, data, r, st)
            try:
                await client.start_notify(char_uuid, callback)
                if DEBUG_RAW_BLE:
                    print(f"[BLE] Подписка на {char_uuid} для гонщика(ов) {rider_numbers}")
            except Exception as ex:
                if DEBUG_RAW_BLE:
                    print(f"[BLE] Ошибка подписки для {rider_numbers}: {ex}")
                for r in rider_numbers:
                    root.after(0, lambda rn=r: set_sensor_status(rn, "Подключён (нет данных)"))
            async def ping_sensor():
                """Пинг датчика каждые 15 сек (чтение характеристики), чтобы он не засыпал."""
                while True:
                    await asyncio.sleep(SENSOR_PING_INTERVAL_SEC)
                    print(f"[BLE] Пинг датчика для гонщика(ов) {rider_numbers}")
                    try:
                        await client.read_gatt_char(char_uuid)
                    except Exception as ex:
                        print(f"[BLE] Пинг датчика {rider_numbers} — ошибка: {ex}")
            ping_task = asyncio.create_task(ping_sensor())
            try:
                while True:
                    await asyncio.sleep(1)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
    except Exception as e:
        for r in rider_numbers:
            root.after(0, lambda rn=r: set_sensor_status(rn, "Ошибка"))
        print(f"Rider(s) {rider_numbers} sensor error: {e}")

# Проверка подключения к одному адресу (без подписки на данные)
async def verify_connection_async(address):
    """Возвращает (success: bool, message: str)."""
    client_arg = _ble_device_from_address(address)
    try:
        async with BleakClient(client_arg, timeout=8.0) as client:
            return True, "OK"
    except Exception as e:
        return False, str(e)


# Задержка (сек) перед подключением второго датчика — на Windows два одновременных BLE-подключения часто конфликтуют
SECOND_SENSOR_DELAY_SEC = 2.0


async def _connect_second_rider_delayed(state):
    """Подключить второй датчик с задержкой, чтобы первый успел установиться."""
    await asyncio.sleep(SECOND_SENSOR_DELAY_SEC)
    await connect_to_rider(RIDER2_ADDRESS, 2, state)


def start_bleak_tasks(state):
    """Запуск асинхронных задач BLE (использует текущие RIDER1_ADDRESS, RIDER2_ADDRESS)."""
    global ble_loop, ble_tasks
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ble_loop = loop
    valid1 = _is_valid_ble_address(RIDER1_ADDRESS)
    valid2 = _is_valid_ble_address(RIDER2_ADDRESS)
    tasks = []
    if valid1 and valid2 and RIDER1_ADDRESS.strip() == RIDER2_ADDRESS.strip():
        tasks.append(loop.create_task(connect_to_rider(RIDER1_ADDRESS, [1, 2], state)))
    else:
        if valid1:
            tasks.append(loop.create_task(connect_to_rider(RIDER1_ADDRESS, 1, state)))
        if valid2:
            tasks.append(loop.create_task(_connect_second_rider_delayed(state)))
    ble_tasks = tasks
    try:
        loop.run_forever()
    finally:
        ble_loop = None
        ble_tasks = []


def restart_bleak_tasks(state):
    old_loop = ble_loop
    old_tasks = list(ble_tasks) if ble_tasks else []

    def stop_loop():
        for t in old_tasks:
            t.cancel()
        old_loop.stop()

    root.after(0, lambda: (set_sensor_status(1, "Не подключён"), set_sensor_status(2, "Не подключён")))
    if old_loop and not old_loop.is_closed():
        old_loop.call_soon_threadsafe(stop_loop)
        root.after(400, lambda: threading.Thread(target=start_bleak_tasks, args=(state,), daemon=True).start())
    else:
        threading.Thread(target=start_bleak_tasks, args=(state,), daemon=True).start()

# Минимальный размер окна
MIN_WIDTH = 700
MIN_HEIGHT = 800

# Создание графического интерфейса (Material-style)
root = tk.Tk()
root.title("Гонка на время")
root.configure(bg=COLORS["background"])
root.minsize(MIN_WIDTH, MIN_HEIGHT)
root.geometry(f"{MIN_WIDTH}x{MIN_HEIGHT}")

# Use Segoe UI on Windows for a clean look
font_title = ("Segoe UI", 18, "bold")
font_subtitle = ("Segoe UI", 11)
font_body = ("Segoe UI", 10)
font_caption = ("Segoe UI", 9)

# Main container with padding
main = tk.Frame(root, bg=COLORS["background"], padx=20, pady=20)
main.pack(fill=tk.BOTH, expand=True)

# Title
title = tk.Label(main, text="Гонка на время", font=font_title, fg=COLORS["on_surface"], bg=COLORS["background"])
title.pack(pady=(0, 20))

# Card: Distance
card_dist = tk.Frame(main, bg=COLORS["surface_card"], padx=20, pady=16)
card_dist.pack(fill=tk.X, pady=(0, 12))
tk.Label(card_dist, text="Расстояние", font=font_subtitle, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"]).pack(anchor="w")
dist_row = tk.Frame(card_dist, bg=COLORS["surface_card"])
dist_row.pack(fill=tk.X, pady=(8, 0))
distance_entry = tk.Entry(dist_row, font=font_body, width=12, relief=tk.FLAT, bd=0, highlightthickness=1, highlightcolor=COLORS["primary"], highlightbackground=COLORS["divider"])
distance_entry.insert(0, "250")
distance_entry.pack(side=tk.LEFT, padx=(0, 10), ipady=8, ipadx=10)
set_distance_button = tk.Button(dist_row, text="Установить", font=font_body, fg=COLORS["on_primary"], bg=COLORS["primary"], activeforeground=COLORS["on_primary"], activebackground=COLORS["primary_dark"], relief=tk.FLAT, padx=16, pady=8, cursor="hand2", command=lambda: set_distance(app_state))
set_distance_button.pack(side=tk.LEFT)
set_distance_button.bind("<Enter>", lambda e: set_distance_button.config(bg=COLORS["primary_dark"]))
set_distance_button.bind("<Leave>", lambda e: set_distance_button.config(bg=COLORS["primary"]))
start_race_button = tk.Button(dist_row, text="Старт", font=font_body, fg=COLORS["on_primary"], bg=COLORS["secondary"], activeforeground=COLORS["on_primary"], activebackground=COLORS["secondary_dark"], relief=tk.FLAT, padx=16, pady=8, cursor="hand2", command=lambda: start_race(app_state))
start_race_button.pack(side=tk.LEFT, padx=(8, 0))
start_race_button.bind("<Enter>", lambda e: start_race_button.config(bg=COLORS["secondary_dark"]))
start_race_button.bind("<Leave>", lambda e: start_race_button.config(bg=COLORS["secondary"]))
reset_race_button = tk.Button(dist_row, text="Сброс", font=font_body, fg=COLORS["on_surface"], bg=COLORS["divider"], activeforeground=COLORS["on_surface"], activebackground=COLORS["on_surface_secondary"], relief=tk.FLAT, padx=14, pady=8, cursor="hand2", command=lambda: reset_race(app_state))
reset_race_button.pack(side=tk.LEFT, padx=(8, 0))
reset_race_button.bind("<Enter>", lambda e: reset_race_button.config(bg="#CCCCCC"))
reset_race_button.bind("<Leave>", lambda e: reset_race_button.config(bg=COLORS["divider"]))
settings_button = tk.Button(dist_row, text="Настройки", font=font_body, fg=COLORS["on_surface"], bg=COLORS["surface_card"], activeforeground=COLORS["on_surface"], activebackground=COLORS["divider"], relief=tk.FLAT, padx=14, pady=8, cursor="hand2", command=open_settings)
settings_button.pack(side=tk.RIGHT)
settings_button.bind("<Enter>", lambda e: settings_button.config(bg=COLORS["divider"]))
settings_button.bind("<Leave>", lambda e: settings_button.config(bg=COLORS["surface_card"]))

# Обратный отсчёт 3–2–1: один виджет над секундомерами (виден только при старте)
countdown_frame = tk.Frame(main, bg=COLORS["background"])
countdown_label = tk.Label(countdown_frame, text="", font=("Segoe UI", 56, "bold"), fg=COLORS["primary"], bg=COLORS["background"])
countdown_label.pack()
countdown_frame.pack(fill=tk.X, pady=(0, 8))  # всегда в разметке; пустой текст = минимум места

# Card: Riders
card_riders = tk.Frame(main, bg=COLORS["surface_card"], padx=20, pady=16)
card_riders.pack(fill=tk.X, pady=(0, 12))

tk.Label(card_riders, text="Гонщик 1", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"]).pack(anchor="w")
row1 = tk.Frame(card_riders, bg=COLORS["surface_card"])
row1.pack(fill=tk.X)
time_label1 = tk.Label(row1, text="0.00 сек", font=("Segoe UI", 14, "bold"), fg=COLORS["rider1"], bg=COLORS["surface_card"])
time_label1.pack(side=tk.LEFT)
max_speed_label1 = tk.Label(row1, text="0.00 км/ч макс.", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"])
max_speed_label1.pack(side=tk.LEFT, padx=(16, 0))
sensor_status_label1 = tk.Label(row1, text="Не подключён", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"])
sensor_status_label1.pack(side=tk.RIGHT)

tk.Frame(card_riders, bg=COLORS["surface_card"], height=12).pack()
tk.Label(card_riders, text="Гонщик 2", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"]).pack(anchor="w")
row2 = tk.Frame(card_riders, bg=COLORS["surface_card"])
row2.pack(fill=tk.X)
time_label2 = tk.Label(row2, text="0.00 сек", font=("Segoe UI", 14, "bold"), fg=COLORS["rider2"], bg=COLORS["surface_card"])
time_label2.pack(side=tk.LEFT)
max_speed_label2 = tk.Label(row2, text="0.00 км/ч макс.", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"])
max_speed_label2.pack(side=tk.LEFT, padx=(16, 0))
sensor_status_label2 = tk.Label(row2, text="Не подключён", font=font_caption, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"])
sensor_status_label2.pack(side=tk.RIGHT)

# Card: Live speed bars (масштабируется при изменении окна)
card_canvas = tk.Frame(main, bg=COLORS["surface_card"], padx=20, pady=16)
card_canvas.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
tk.Label(card_canvas, text="Скорость в реальном времени", font=font_subtitle, fg=COLORS["on_surface_secondary"], bg=COLORS["surface_card"]).pack(anchor="w", pady=(0, 12))
canvas = tk.Canvas(card_canvas, width=MIN_WIDTH - 40, height=220, bg=COLORS["surface_card"], highlightthickness=0)
canvas.pack(fill=tk.BOTH, expand=True)
canvas.bind("<Configure>", update_projection)  # перерисовка при изменении размера
root.after(100, update_projection)  # initial draw

# Поток, который непрерывно запрашивает обновление UI (каждые UI_REFRESH_MS мс кладёт в очередь)
threading.Thread(target=_ui_refresh_worker, args=(app_state,), daemon=True).start()
# Главный поток забирает из очереди и обновляет виджеты (тянет данные из app_state)
root.after(UI_REFRESH_MS, lambda: _ui_drain_and_update(app_state))

# Запуск BLE в отдельном потоке
threading.Thread(target=start_bleak_tasks, args=(app_state,), daemon=True).start()

# Запуск основного цикла
root.mainloop()
