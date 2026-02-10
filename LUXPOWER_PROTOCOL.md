# LuxPower Inverter Protocol Documentation

Документація протоколу зв'язку з інверторами LuxPower через WiFi донгл.

## Загальна інформація

- **Порт**: 8000 (TCP)
- **Протокол**: Пропрієтарний, обгортка над Modbus
- **Джерела**: ant0nkr/luxpower-ha-integration, celsworth/lxp-bridge

## Конфігурація підключення

```python
INVERTER_IP = "YOUR_INVERTER_IP"          # IP адреса WiFi донгла в локальній мережі
DONGLE_SERIAL = "YOUR_DONGLE_SERIAL"      # 10 символів, серійний номер донгла
INVERTER_SERIAL = "YOUR_INVERTER_SERIAL"  # 10 символів, серійний номер інвертора
PORT = 8000
```

## Структура пакету запиту (38 байт)

```
Offset  Size  Field              Value           Description
------  ----  -----------------  --------------  --------------------------
0-1     2     Magic              0xA1 0x1A       Фіксований заголовок
2-3     2     Protocol           0x01 0x00       Версія протоколу (little-endian)
4-5     2     Frame Length       0x20 0x00       32 (little-endian)
6       1     Address            0x01            Фіксоване значення
7       1     TCP Function       0xC2            194 = TranslatedData
8-17    10    Dongle Serial      ASCII           Серійний номер донгла
18-19   2     Data Length        0x12 0x00       18 (little-endian)
20      1     Action             0x00            0 = запит до інвертора
21      1     Modbus Function    0x04            0x03=Hold, 0x04=Input
22-31   10    Inverter Serial    ASCII           Серійний номер інвертора
32-33   2     Start Register     little-endian   Початковий регістр
34-35   2     Register Count     little-endian   Кількість регістрів
36-37   2     CRC-16             little-endian   CRC над байтами 20-35
```

## TCP Function Codes

| Code | Hex  | Name           | Description                    |
|------|------|----------------|--------------------------------|
| 193  | 0xC1 | Heartbeat      | Keep-alive пакет               |
| 194  | 0xC2 | TranslatedData | Читання/запис регістрів        |
| 195  | 0xC3 | ReadParam      | Читання параметрів             |
| 196  | 0xC4 | WriteParam     | Запис параметрів               |

## Modbus Function Codes

| Code | Hex  | Name               | Description                |
|------|------|--------------------|----------------------------|
| 3    | 0x03 | Read Holding       | Читання налаштувань        |
| 4    | 0x04 | Read Input         | Читання поточних даних     |
| 6    | 0x06 | Write Single       | Запис одного регістру      |
| 16   | 0x10 | Write Multiple     | Запис кількох регістрів    |

## CRC-16/Modbus

```python
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
```

**Важливо**: CRC обчислюється над байтами 20-35 (data frame без CRC).

## Структура відповіді

```
Offset  Size  Field              Description
------  ----  -----------------  --------------------------
0-1     2     Magic              0xA1 0x1A
2-3     2     Protocol           Версія протоколу
4-5     2     Frame Length       Довжина фрейму
6       1     Address            1
7       1     TCP Function       0xC2
8-17    10    Dongle Serial      Серійний номер донгла
18-19   2     Data Length        Довжина даних
20      1     Action             1 = відповідь від інвертора
21      1     Modbus Function    Код функції
22-31   10    Inverter Serial    Серійний номер інвертора
32      1     Byte Count         Кількість байт даних
33-N    var   Register Data      Дані регістрів (BIG-ENDIAN!)
N-N+1   2     CRC-16             Контрольна сума
```

## Формат даних регістрів

**ВАЖЛИВО**: Дані в регістрах використовують **BIG-ENDIAN** (стандарт Modbus),
незважаючи на те, що заголовок пакету використовує little-endian.

```python
import struct
# Парсинг регістру
value = struct.unpack('>H', data[i:i+2])[0]  # Big-endian unsigned short
```

### Знакові значення (signed)

Деякі регістри (струм, потужність) можуть бути від'ємними:

```python
if value > 32767:
    value = value - 65536
```

### Спеціальні регістри

**Регістр 5 (SOC/SOH)** - два значення в одному регістрі:
```python
soc = register_value & 0xFF           # Low byte = SOC (%)
soh = (register_value >> 8) & 0xFF    # High byte = SOH (%)
```

## Карта INPUT регістрів (Function 0x04)

**Модель**: LuxPower (серійні номери в config.py)
**Дата калібрування**: 2026-02-08

### Основні параметри (0-10)

| Reg | Name             | Unit | Scale | Signed | Description            | Приклад |
|-----|------------------|------|-------|--------|------------------------|---------|
| 0   | status           | -    | 1     | No     | Стан (16=норма)        | 16      |
| 3   | dc_bus_voltage   | V    | 0.1   | No     | Напруга DC шини        | 400.5 V |
| 4   | battery_voltage  | V    | 0.1   | No     | Напруга батареї        | 51.0 V  |
| 5   | battery_soc      | %    | 1     | No     | Заряд батареї SOC      | 100%    |

### Мережа та вихід (11-30)

| Reg | Name             | Unit | Scale | Signed | Description            | Приклад  |
|-----|------------------|------|-------|--------|------------------------|----------|
| 11  | inverter_temp    | °C   | 1     | No     | Температура інвертора  | 23 °C    |
| 12  | grid_voltage     | V    | 0.1   | No     | Напруга мережі         | 228.1 V  |
| 15  | grid_frequency   | Hz   | 0.01  | No     | Частота мережі         | 50.00 Hz |
| 18  | load_power       | W    | 10    | No     | Потужність навантаж.   | 1100 W   |
| 20  | output_voltage   | V    | 0.1   | No     | Напруга виходу         | 228.9 V  |
| 23  | output_frequency | Hz   | 0.01  | No     | Частота виходу         | 49.99 Hz |
| 27  | radiator_temp    | °C   | 0.1   | No     | Температура радіатора  | 35.3 °C  |

### Батарея (37-39)

| Reg | Name             | Unit | Scale | Signed | Description            | Приклад |
|-----|------------------|------|-------|--------|------------------------|---------|
| 37  | battery_current  | A    | 0.1   | Yes    | Струм батареї          | 7.4 A   |
| 38  | dc_bus_voltage_2 | V    | 0.1   | No     | DC шина (дубль)        | 400.4 V |
| 39  | unknown_39       | V    | 0.1   | No     | Невідомий              | 360.6 V |

### Лічильники енергії (40-50)

| Reg | Name             | Unit | Scale | Description            | Приклад |
|-----|------------------|------|-------|------------------------|---------|
| 40  | energy_counter_1 | Wh   | 1     | Лічильник 1            | 21362   |
| 42  | energy_counter_2 | Wh   | 1     | Лічильник 2            | 22704   |
| 46  | energy_counter_3 | Wh   | 1     | Лічильник 3            | 28021   |
| 48  | energy_counter_4 | Wh   | 1     | Лічильник 4            | 9498    |

### Невідомі регістри (потребують ідентифікації)

| Reg | Raw Value | ÷10    | ÷100   | Можливе значення       |
|-----|-----------|--------|--------|------------------------|
| 14  | 2049      | 204.9  | 20.49  | Напруга? Потужність?   |
| 17  | 37        | 3.7    | 0.37   | Струм? Температура?    |
| 21  | 5136      | 513.6  | 51.36  | Напруга батареї × 10?  |
| 22  | 4170      | 417.0  | 41.70  | DC bus voltage?        |
| 32  | 9         | -      | -      | Прапорець/статус?      |
| 33  | 2         | -      | -      | Прапорець/статус?      |
| 34  | 3         | -      | -      | Прапорець/статус?      |

## Приклад коду

### Підключення та читання

```python
from lux_power_read_data import LuxPowerInverter

inverter = LuxPowerInverter(
    host="YOUR_INVERTER_IP",
    port=8000,
    dongle_serial="YOUR_DONGLE_SERIAL",
    inverter_serial="YOUR_INVERTER_SERIAL"
)

if inverter.connect():
    data = inverter.get_status()
    print(f"SOC: {data['battery_soc']}%")
    print(f"Battery: {data['battery_voltage']}V")
    inverter.disconnect()
```

### Побудова пакету вручну

```python
import struct

def build_packet(dongle_serial, inverter_serial, start_reg, count):
    buf = bytearray()

    # Header
    buf += bytes([0xA1, 0x1A])              # Magic
    buf += (1).to_bytes(2, 'little')        # Protocol
    buf += (32).to_bytes(2, 'little')       # Frame length
    buf += bytes([0x01])                    # Address
    buf += bytes([0xC2])                    # TCP Function
    buf += dongle_serial.encode()[:10].ljust(10, b'\x00')

    # Data frame
    buf += (18).to_bytes(2, 'little')       # Data length
    buf += bytes([0x00])                    # Action
    buf += bytes([0x04])                    # Modbus Read Input
    buf += inverter_serial.encode()[:10].ljust(10, b'\x00')
    buf += start_reg.to_bytes(2, 'little')
    buf += count.to_bytes(2, 'little')

    # CRC
    crc = compute_crc16_modbus(bytes(buf[20:36]))
    buf += crc.to_bytes(2, 'little')

    return bytes(buf)
```

## Примітки

1. **Серійні номери** мають бути рівно 10 символів
2. **Timeout** рекомендується 10 секунд
3. **Затримка** між запитами ~300-500ms
4. **Розмір блоку** регістрів: 125 (стандарт) або 40 (старі прошивки)
5. **Порт 80** також відкритий - веб-інтерфейс донгла

## Джерела

- https://github.com/ant0nkr/luxpower-ha-integration
- https://github.com/celsworth/lxp-bridge
- https://www.dth.net/solar/luxpower/modbus/EG4-18KPV-12LV-Modbus-Protocol.pdf

## Історія змін

- 2026-02-08: Початкова версія документації
