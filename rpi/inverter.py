"""
LuxPower Inverter Communication Module
Based on ant0nkr/luxpower-ha-integration protocol
"""

import socket
import struct
import logging
from typing import Dict, Optional, List
from dataclasses import dataclass
import time

logger = logging.getLogger(__name__)

# =============================================================================
# КОНСТАНТИ ПРОТОКОЛУ
# =============================================================================

MAGIC_PREFIX = bytes([0xA1, 0x1A])
PROTOCOL_VERSION = 1
DEFAULT_PORT = 8000

TCP_FUNC_HEARTBEAT = 193
TCP_FUNC_TRANSLATED = 194
TCP_FUNC_READ_PARAM = 195
TCP_FUNC_WRITE_PARAM = 196

MODBUS_READ_HOLD = 0x03
MODBUS_READ_INPUT = 0x04


def compute_crc16_modbus(data: bytes) -> int:
    """CRC-16/Modbus (polynomial 0xA001)"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


@dataclass
class InverterStatus:
    """Структура даних інвертора"""
    timestamp: float
    connected: bool = False

    # Стан
    status: int = 0
    status_text: str = ""

    # Батарея
    battery_soc: int = 0
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    battery_power: float = 0.0

    # Мережа
    grid_available: bool = False
    grid_voltage: float = 0.0
    grid_frequency: float = 0.0

    # Вихід
    output_voltage: float = 0.0
    output_frequency: float = 0.0
    load_power: int = 0

    # Температури
    inverter_temp: int = 0
    radiator_temp: float = 0.0

    # DC Bus
    dc_bus_voltage: float = 0.0

    def to_dict(self) -> Dict:
        """Конвертація в словник для JSON"""
        return {
            "timestamp": self.timestamp,
            "connected": self.connected,
            "status": self.status,
            "status_text": self.status_text,
            "battery": {
                "soc": self.battery_soc,
                "voltage": self.battery_voltage,
                "current": self.battery_current,
                "power": self.battery_power
            },
            "grid": {
                "available": self.grid_available,
                "voltage": self.grid_voltage,
                "frequency": self.grid_frequency
            },
            "output": {
                "voltage": self.output_voltage,
                "frequency": self.output_frequency,
                "load_power": self.load_power
            },
            "temperature": {
                "inverter": self.inverter_temp,
                "radiator": self.radiator_temp
            },
            "dc_bus_voltage": self.dc_bus_voltage
        }


class LuxPowerInverter:
    """Клас для роботи з інвертором LuxPower"""

    def __init__(self, host: str, port: int = 8000,
                 dongle_serial: str = "", inverter_serial: str = "",
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.dongle_serial = dongle_serial.encode('ascii')
        self.inverter_serial = inverter_serial.encode('ascii')
        self.timeout = timeout
        self.sock = None
        self._last_status: Optional[InverterStatus] = None

    def connect(self) -> bool:
        """Підключення до інвертора"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.host, self.port))
            logger.debug(f"Connected to {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.sock = None
            return False

    def disconnect(self):
        """Відключення"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def is_connected(self) -> bool:
        """Перевірка з'єднання"""
        return self.sock is not None

    def _build_read_packet(self, start_register: int, count: int,
                           function: int = MODBUS_READ_INPUT) -> bytes:
        """Побудова пакету для читання регістрів"""
        buf = bytearray()

        # TCP Header
        buf += MAGIC_PREFIX
        buf += PROTOCOL_VERSION.to_bytes(2, 'little')
        buf += (32).to_bytes(2, 'little')
        buf += (1).to_bytes(1, 'little')
        buf += TCP_FUNC_TRANSLATED.to_bytes(1, 'little')
        buf += self.dongle_serial.ljust(10, b'\x00')[:10]

        # Data frame
        buf += (18).to_bytes(2, 'little')
        buf += (0).to_bytes(1, 'little')
        buf += function.to_bytes(1, 'little')
        buf += self.inverter_serial.ljust(10, b'\x00')[:10]
        buf += start_register.to_bytes(2, 'little')
        buf += count.to_bytes(2, 'little')

        # CRC
        crc = compute_crc16_modbus(bytes(buf[20:36]))
        buf += crc.to_bytes(2, 'little')

        return bytes(buf)

    def _send_receive(self, packet: bytes) -> Optional[bytes]:
        """Відправка та отримання"""
        if not self.sock:
            return None

        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(packet)
            response = self.sock.recv(1024)
            return response if response else None
        except socket.timeout:
            logger.warning("Socket timeout")
            return None
        except Exception as e:
            logger.error(f"Send/receive error: {e}")
            return None

    def _parse_registers(self, response: bytes) -> Optional[List[int]]:
        """Парсинг відповіді з регістрами"""
        if not response or len(response) < 38:
            return None

        if response[0:2] != MAGIC_PREFIX:
            return None

        byte_count = response[34]
        data_start = 35
        data_end = 35 + byte_count

        if data_end > len(response) - 2:
            data_end = len(response) - 2

        register_data = response[data_start:data_end]

        registers = []
        for i in range(0, len(register_data), 2):
            if i + 2 <= len(register_data):
                value = struct.unpack('<H', register_data[i:i+2])[0]
                registers.append(value)

        return registers

    def read_registers(self, start: int, count: int) -> Optional[List[int]]:
        """Читання регістрів"""
        packet = self._build_read_packet(start, count)
        response = self._send_receive(packet)

        if response:
            return self._parse_registers(response)
        return None

    def get_status(self, grid_threshold: float = 180.0) -> InverterStatus:
        """Отримання повного статусу інвертора"""
        status = InverterStatus(timestamp=time.time())

        # Підключення якщо потрібно
        if not self.is_connected():
            if not self.connect():
                return status

        status.connected = True

        # Читаємо регістри 0-50
        regs = self.read_registers(0, 50)

        if not regs or len(regs) < 40:
            status.connected = False
            self.disconnect()
            return status

        try:
            # Статус
            status.status = regs[0]
            status.status_text = "Нормальна робота" if regs[0] == 16 else f"Код {regs[0]}"

            # DC Bus
            status.dc_bus_voltage = round(regs[3] * 0.1, 1)

            # Батарея
            status.battery_voltage = round(regs[4] * 0.1, 1)
            status.battery_soc = regs[5]

            # Температура інвертора
            status.inverter_temp = regs[11]

            # Мережа
            status.grid_voltage = round(regs[12] * 0.1, 1)
            status.grid_frequency = round(regs[15] * 0.01, 2)
            status.grid_available = status.grid_voltage >= grid_threshold

            # Навантаження
            status.load_power = regs[18] * 10

            # Вихід
            status.output_voltage = round(regs[20] * 0.1, 1)
            status.output_frequency = round(regs[23] * 0.01, 2)

            # Температура радіатора
            status.radiator_temp = round(regs[27] * 0.1, 1)

            # Струм батареї (signed)
            bat_current = regs[37]
            if bat_current > 32767:
                bat_current = bat_current - 65536
            status.battery_current = round(bat_current * 0.1, 1)

            # Потужність батареї
            status.battery_power = round(status.battery_voltage * status.battery_current, 0)

            self._last_status = status

        except Exception as e:
            logger.error(f"Error parsing registers: {e}")
            status.connected = False

        return status

    def get_last_status(self) -> Optional[InverterStatus]:
        """Останній збережений статус"""
        return self._last_status

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


# Тестування
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    inv = LuxPowerInverter(
        host="YOUR_INVERTER_IP",
        dongle_serial="YOUR_DONGLE_SERIAL",
        inverter_serial="YOUR_INVERTER_SERIAL"
    )

    status = inv.get_status()
    print(f"Connected: {status.connected}")
    print(f"Grid: {status.grid_voltage}V, {'ON' if status.grid_available else 'OFF'}")
    print(f"Battery: {status.battery_soc}%, {status.battery_voltage}V")
    print(f"Load: {status.load_power}W")

    inv.disconnect()
