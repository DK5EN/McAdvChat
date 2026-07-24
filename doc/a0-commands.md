# MeshCom BLE Command Reference

Complete reference for Bluetooth Low Energy (BLE) commands in MeshCom firmware.

**Firmware Version:** 4.35k
**Source:** `src/phone_commands.cpp` and `src/command_functions.cpp`

---

## BLE Protocol Overview

### Message Format
```
[Length 1B] [Message ID 1B] [Data...]
```

- **Length:** Total message length in bytes
- **Message ID:** Command type identifier (0x10, 0x20, 0xA0, etc.)
- **Data:** Command-specific payload

---

## BLE Message Types

All BLE commands are handled by `readPhoneCommand()` in `src/phone_commands.cpp:368-724`.

| Msg ID | Command | Data Format | Description |
|--------|---------|-------------|-------------|
| **0x10** | Hello Message | `0x20 0x30` | Initial handshake from phone app |
| **0x20** | Timestamp | 4B UNIX timestamp (UTC) | Synchronize device time |
| **0x50** | Callsign | 1B length + callsign string | Set station callsign |
| **0x55** | WiFi Settings | 1B SSID_len + SSID + 1B PWD_len + PWD | Configure WiFi credentials |
| **0x70** | Latitude | 4B float + 1B save_flag | Set latitude position |
| **0x80** | Longitude | 4B float + 1B save_flag | Set longitude position |
| **0x90** | Altitude | 4B int + 1B save_flag | Set altitude in meters |
| **0x95** | APRS Symbols | 1B primary + 1B secondary | Set APRS map symbols (/ or \) |
| **0xA0** | **Text Message** | **Text string** | **Send text message or command** |
| **0xF0** | Save & Reboot | (none) | Save settings to flash and restart |

### Position Save Flags (0x70, 0x80, 0x90)
- `0x0A` = Save to flash (one-time configuration)
- `0x0B` = Don't save (periodic GPS updates from phone)

---

## 0xA0 Text Message Command

### Format
```
[Total Length] [0xA0] [Message Text]
```

### Behavior
- Messages starting with `--` are treated as **system commands** (see below)
- Other messages are sent as **APRS text messages** to the mesh (prefixed with `:`)
- Maximum length: `MAX_MSG_LEN_PHONE` bytes
- Sets `hasMsgFromPhone = true` flag for main loop processing

### Examples

**Send text message to mesh:**
```
[0x0D] [0xA0] [H][e][l][l][o][ ][W][o][r][l][d]
```
Result: `:Hello World` sent to mesh network

**Send system command:**
```
[0x08] [0xA0] [--reboot]
```
Result: Node reboots

---

## Querying Device State (TYP Responses)

Several commands return JSON-formatted data with a `TYP` field identifying the response type. These responses use message ID `0x44` (data message) instead of `0x40` (text message).

### Response Format
```
[Length][0x44][JSON string]
```

### Query Commands

| Command | TYP | Description | Multi-Part | Response Fields |
|---------|-----|-------------|------------|-----------------|
| `--pos` | **G** | GPS/Position data | No | LAT, LON, ALT, SAT, SFIX, HDOP, RATE, NEXT, DIST, DIRn, DIRo, DATE |
| `--info` | **I** | General device info | No | FWVER, CALL, ID, HWID, MAXV, BLE, BATP, BATV, GCB0-5, CTRY, BOOST |
| `--nodeset` | **SN** | Node settings | No | GW, WS, WSPWD, DISP, BTN, MSH, GPS, TRACK, UTCOF, TXP, MQRG, MSF, MCR, MBW, GWNPOS, NOALL, BLED, GWS |
| `--aprsset` | **SA** | APRS settings | No | ATXT, SYMID, SYMCD, NAME |
| `--seset` | **SE** + **S1** | Sensor settings | **YES** (2 msgs) | **SE:** BME, BMP, BMXF, BMP3, 680, 811, 226, AHT, SS, LPS33, OW, OWPIN, OWF, USERPIN<br>**S1:** INA226, SHUNT, IMAX, SAMP, SHT, SHTF |
| `--wifiset` | **SW** + **S2** | WiFi/network settings | **YES** (2 msgs) | **SW:** SSID, IP, GW, AP, DNS, SUB<br>**S2:** OWNIP, OWNGW, OWNMS, OWNDNS, EUDP, EUDPIP, TXPOW |
| `--analogset` | **AN** | Analog input config | No | APN, AFC, AK, AFL, ACK, ADC, ADCRAW, ADCE1, ADCE2, ADCSL, ADCOF, ADCAT |
| `--tel` | **TM** | Telemetry data | No | PARM, UNIT, FORMAT, EQNS, VALES, PTIME |
| `--weather` / `--wx` | **W** | Weather/sensor data | No | TEMP, TOFFI, TOUT, TOFFO, HUM, PRES, QNH, ALT, GAS, CO2, VBUS, VSHUNT, VAMP, VPOW |
| `--io` | **IO** | GPIO/IO status | No | MCP23017, AxOUT, AxVAL, BxOUT, BxVAL |

### Multi-Part Responses

**IMPORTANT:** Some commands send multiple JSON responses automatically:

1. **`--seset`** sends **2 responses** in sequence:
   - First: TYP: **SE** (primary sensor settings)
   - Then: TYP: **S1** (extended sensor settings - INA226, shunt config, SHT21)

2. **`--wifiset`** sends **2 responses** in sequence:
   - First: TYP: **SW** (basic WiFi settings - SSID, IP, gateway, DNS)
   - Then: TYP: **S2** (advanced network - static IP config, external UDP, WiFi TX power)

**Note:** There is no **S3** register type in the firmware.

### Field Descriptions

#### TYP: G (GPS/Position)
- **LAT/LON:** Decimal degrees (negative for S/W)
- **ALT:** Altitude in meters
- **SAT:** Number of satellites in view
- **SFIX:** GPS fix status (0=no fix, 1=fix)
- **HDOP:** Horizontal dilution of precision
- **RATE:** Position beacon interval (seconds)
- **NEXT:** Seconds until next beacon
- **DIST:** Distance traveled since last beacon (meters)
- **DIRn:** Current direction (degrees)
- **DIRo:** Previous direction (degrees)
- **DATE:** Current date/time string

#### TYP: I (Device Information)
- **FWVER:** Firmware version (e.g., "4.35 k")
- **CALL:** Station callsign with SSID
- **ID:** Gateway ID
- **HWID:** Hardware ID (board type)
- **MAXV:** 100% battery voltage reference
- **BLE:** BLE message mode ("short" or "long")
- **BATP:** Battery percentage (0-100)
- **BATV:** Battery voltage (V)
- **GCB0-5:** Group code bytes
- **CTRY:** Country code (2-letter)
- **BOOST:** Boosted RX gain enabled (true/false)

#### TYP: SN (Node Settings)
- **GW:** Gateway mode enabled
- **WS:** Web server enabled
- **WSPWD:** Web server password
- **DISP:** Display off status
- **BTN:** Button check enabled
- **MSH:** Mesh networking enabled
- **GPS:** GPS chip enabled
- **TRACK:** SmartBeaconing enabled
- **UTCOF:** UTC offset (hours)
- **TXP:** TX power (dBm)
- **MQRG:** LoRa frequency (MHz)
- **MSF:** Spreading factor (7-12)
- **MCR:** Coding rate (5-8)
- **MBW:** Bandwidth (kHz)
- **GWNPOS:** Gateway without position
- **NOALL:** No message to all
- **BLED:** Board LED enabled
- **GWS:** Gateway server IP

#### TYP: SA (APRS Settings)
- **ATXT:** APRS comment text
- **SYMID:** APRS symbol table ID (`/` or `\`)
- **SYMCD:** APRS symbol code (character)
- **NAME:** Operator name

#### TYP: SE (Sensor Settings - Part 1)
- **BME:** BME280 sensor enabled
- **BMP:** BMP280 sensor enabled
- **BMXF:** BMx sensor found status
- **BMP3:** BMP390 sensor enabled
- **BMP3F:** BMP390 found status
- **680:** BME680 sensor enabled
- **680F:** BME680 found status
- **811:** CCS811 sensor enabled
- **811F:** CCS811 found status
- **226:** INA226 current sensor enabled
- **226F:** INA226 found status
- **AHT:** AHT20 temp/humidity sensor enabled
- **AHTF:** AHT20 found status
- **SS:** SoftSerial enabled
- **LPS33:** LPS33 pressure sensor enabled (RAK4630 only)
- **OW:** OneWire (DS18x20) enabled
- **OWPIN:** OneWire GPIO pin
- **OWF:** OneWire sensor found count
- **USERPIN:** User button GPIO pin

#### TYP: S1 (Sensor Settings - Part 2)
Automatically sent after SE response.
- **INA226:** INA226 sensor found
- **SHUNT:** Shunt resistor value (Ω)
- **IMAX:** Maximum current (A)
- **SAMP:** Current sample count
- **SHT:** SHT21 sensor enabled
- **SHTF:** SHT21 found status

#### TYP: SW (WiFi Settings - Part 1)
- **SSID:** WiFi SSID (or AP name if in AP mode)
- **IP:** Current IP address
- **GW:** Gateway IP address
- **AP:** Access Point mode enabled
- **DNS:** DNS server IP
- **SUB:** Subnet mask

#### TYP: S2 (WiFi Settings - Part 2)
Automatically sent after SW response.
- **OWNIP:** Static IP address (user-configured)
- **OWNGW:** Static gateway IP (user-configured)
- **OWNMS:** Static subnet mask (user-configured)
- **OWNDNS:** Static DNS server IP (user-configured)
- **EUDP:** External UDP enabled
- **EUDPIP:** External UDP server IP
- **TXPOW:** WiFi TX power (dBm)

#### TYP: AN (Analog Settings)
- **APN:** Analog input GPIO pin
- **AFC:** Analog scaling factor
- **AK:** Analog filter alpha (smoothing)
- **AFL:** Analog filter enabled
- **ACK:** Analog check/monitoring enabled
- **ADC:** Current analog value (processed)
- **ADCRAW:** Raw ADC reading
- **ADCE1:** ADC exponential filter 1
- **ADCE2:** ADC exponential filter 2
- **ADCSL:** ADC calibration slope
- **ADCOF:** ADC calibration offset
- **ADCAT:** ADC attenuation setting (ESP32)

#### TYP: TM (Telemetry Data)
- **PARM:** Parameter names (comma-separated)
- **UNIT:** Units for each parameter (comma-separated)
- **FORMAT:** Decimal places for each value (comma-separated)
- **EQNS:** Calibration equations (a,b,c coefficients)
- **VALES:** Internal sensor value mappings
- **PTIME:** Measurement interval (minutes)

#### TYP: W (Weather/Sensor Data)
- **TEMP:** Temperature (indoor sensor)
- **TOFFI:** Indoor temperature offset
- **TOUT:** Temperature (outdoor sensor)
- **TOFFO:** Outdoor temperature offset
- **HUM:** Humidity (%)
- **PRES:** Atmospheric pressure (hPa)
- **QNH:** Pressure at sea level (QNH)
- **ALT:** Barometric altitude (m)
- **GAS:** Gas resistance (BME680)
- **CO2:** CO2 level (ppm)
- **VBUS:** Bus voltage (V)
- **VSHUNT:** Shunt voltage (mV)
- **VAMP:** Current (mA)
- **VPOW:** Power (mW)

#### TYP: IO (GPIO/IO Status)
- **MCP23017:** MCP23017 I/O expander detected
- **AxOUT:** Port A output pin configuration (binary string)
- **AxVAL:** Port A pin values (binary string)
- **BxOUT:** Port B output pin configuration (binary string)
- **BxVAL:** Port B pin values (binary string)

### Query Examples

**Query GPS Position:**
```
Send:    [0x05][0xA0][--pos]
Receive: [Length][0x44][{"TYP":"G","LAT":48.208176,"LON":16.373819,"ALT":171,"SAT":12,"SFIX":1,"HDOP":0.8,"RATE":300,"NEXT":45,"DIST":0,"DIRn":0,"DIRo":0,"DATE":"2025-02-14 15:30:00"}]
```

**Query Device Info:**
```
Send:    [0x06][0xA0][--info]
Receive: [Length][0x44][{"TYP":"I","FWVER":"4.35 k","CALL":"OE1ABC-7","ID":"ABC123","HWID":10,"MAXV":4.2,"BLE":"long","BATP":85,"BATV":4.05,"GCB0":1,"GCB1":2,"GCB2":0,"GCB3":0,"GCB4":0,"GCB5":0,"CTRY":"AT","BOOST":false}]
```

**Query Node Settings:**
```
Send:    [0x09][0xA0][--nodeset]
Receive: [Length][0x44][{"TYP":"SN","GW":true,"WS":true,"WSPWD":"admin","DISP":false,"BTN":true,"MSH":true,"GPS":true,"TRACK":true,"UTCOF":1.0,"TXP":20,"MQRG":433.175,"MSF":11,"MCR":6,"MBW":250,"GWNPOS":false,"NOALL":false,"BLED":true,"GWS":"192.168.1.100"}]
```

**Query APRS Settings:**
```
Send:    [0x09][0xA0][--aprsset]
Receive: [Length][0x44][{"TYP":"SA","ATXT":"MeshCom Node","SYMID":"/","SYMCD":"[","NAME":"Martin"}]
```

**Query Sensor Settings (Multi-Part):**
```
Send:    [0x07][0xA0][--seset]
Receive: [Length][0x44][{"TYP":"SE","BME":true,"BMP":false,"BMXF":1,"BMP3":false,"BMP3F":0,"680":false,"680F":0,"811":false,"811F":0,"226":false,"226F":0,"AHT":true,"AHTF":1,"SS":false,"LPS33":false,"OW":true,"OWPIN":23,"OWF":2,"USERPIN":38}]
         [Length][0x44][{"TYP":"S1","INA226":false,"SHUNT":0.1,"IMAX":3.0,"SAMP":10,"SHT":false,"SHTF":0}]
```

**Query WiFi Settings (Multi-Part):**
```
Send:    [0x09][0xA0][--wifiset]
Receive: [Length][0x44][{"TYP":"SW","SSID":"MyWiFi","IP":"192.168.1.50","GW":"192.168.1.1","AP":false,"DNS":"192.168.1.1","SUB":"255.255.255.0"}]
         [Length][0x44][{"TYP":"S2","OWNIP":"192.168.1.50","OWNGW":"192.168.1.1","OWNMS":"255.255.255.0","OWNDNS":"8.8.8.8","EUDP":true,"EUDPIP":"10.0.0.1","TXPOW":20}]
```

**Query Analog Settings:**
```
Send:    [0x0B][0xA0][--analogset]
Receive: [Length][0x44][{"TYP":"AN","APN":35,"AFC":2.0,"AK":0.1,"AFL":true,"ACK":true,"ADC":12.5,"ADCRAW":2048,"ADCE1":12.4,"ADCE2":12.6,"ADCSL":1.0,"ADCOF":0.0,"ADCAT":3}]
```

**Query Telemetry Data:**
```
Send:    [0x05][0xA0][--tel]
Receive: [Length][0x44][{"TYP":"TM","PARM":"VOLT,TEMP,HUM,,","UNIT":"V,C,%,,","FORMAT":"2,1,1,0,0","EQNS":"0,1,0,0,1,0,0,1,0,0,1,0,0,1,0","VALES":",,temp,press,","PTIME":5}]
```

**Query Weather Data:**
```
Send:    [0x09][0xA0][--weather]
Receive: [Length][0x44][{"TYP":"W","TEMP":22.5,"TOFFI":0.0,"TOUT":18.3,"TOFFO":-0.5,"HUM":45,"PRES":1013.25,"QNH":1020.0,"ALT":171,"GAS":150000,"CO2":420,"VBUS":12.5,"VSHUNT":0.5,"VAMP":250,"VPOW":3125}]
```

**Query GPIO/IO Status:**
```
Send:    [0x04][0xA0][--io]
Receive: [Length][0x44][{"TYP":"IO","MCP23017":true,"AxOUT":"10101010","AxVAL":"11001100","BxOUT":"01010101","BxVAL":"00110011"}]
```

### Configuration Sync Sequence

Phone apps typically query all registers on initial connection:

```bash
# Basic device info
--info        # → I (device info)
--nodeset     # → SN (node settings)
--aprsset     # → SA (APRS config)
--pos         # → G (GPS/position)

# Detailed settings
--seset       # → SE + S1 (sensors - 2 messages)
--wifiset     # → SW + S2 (WiFi - 2 messages)
--analogset   # → AN (analog config)
--tel         # → TM (telemetry)

# Real-time data
--weather     # → W (sensor readings)
--io          # → IO (GPIO status)
```

### Important Notes

- **BLE Only:** JSON responses are only sent via BLE, not serial console
- **Message ID 0x44:** All TYP responses use data message flag `0x44` (not `0x40`)
- **Real-time Data:** Commands return current/live values from the device
- **Multi-Part Handling:** `--seset` and `--wifiset` send 2 responses each - your code must handle both
- **No S3 Type:** Only S1 and S2 exist as multi-part continuations

### Source Code References

| Register | Function/Location | Line |
|----------|-------------------|------|
| **G** | `sendGpsJson()` | 4437 |
| **I** | Inline (within `bInfo` flag) | 4067-4109 |
| **SN** | `sendNodeSetting()` | 4480 |
| **SA** | `sendAPRSset()` | 4593 |
| **SE + S1** | Inline (within `bSensSetting` flag) | 4272-4339 |
| **SW + S2** | Inline (within `bWifiSetting` flag) | 4342-4402 |
| **AN** | `sendAnalogSetting()` | 4555 |
| **TM** | Inline (within `bTelemetry` flag) | 3849-3883 |
| **W** | Inline (within `bWeather` flag) | 3886-3943 |
| **IO** | Inline (within `bIO` flag) | 3995-4026 |

All references are in `src/command_functions.cpp`

---

## System Commands (via 0xA0)

All commands below are sent as text via the 0xA0 message type.
Commands are case-sensitive and must start with `--`.

---

### **System & Info Commands**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--help` | - | Display all available commands on serial console |
| `--info` | - | Query device info via JSON (TYP:I) - see Querying Device State section |
| `--reboot` | - | Reboot the node immediately |
| `--save` | - | Save current settings to flash memory |
| `--ota-update` | - | Start OTA firmware update process |

**Example:**
```
--info
--reboot
```

---

### **Position & GPS Commands**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--pos` | - | Query GPS/position data via JSON (TYP:G) - see Querying Device State section |
| `--sendpos` | - | Send position beacon to mesh immediately |
| `--sendhey` | - | Send APRS "hey" message (@ type) |
| `--sendtele` | - | Send telemetry data packet |
| `--sendtrack` | - | Send tracking beacon |
| `--setlat` | `44.12345` | Set latitude in decimal degrees |
| `--setlon` | `016.12345` | Set longitude in decimal degrees |
| `--setalt` | `9999` | Set altitude in meters |
| `--gps` | `on` / `off` | Enable or disable GPS chip |
| `--gps reset` | - | Factory reset GPS module |
| `--track` | `on` / `off` | Enable/disable SmartBeaconing |
| `--posshot` | - | Trigger one-shot position update |
| `--postime` | `seconds` | Set position beacon interval |

**Examples:**
```
--setlat 48.208176
--setlon 16.373819
--setalt 171
--sendpos
--track on
```

---

### **Radio/LoRa Configuration**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--lora` | - | Display current LoRa settings |
| `--txpower` | `-5` to `30` | Set TX power in dBm |
| `--txfreq` | `433.175` | Set TX frequency in MHz |
| `--txbw` | `125` / `250` / `500` | Set TX bandwidth in kHz |
| `--txsf` | `7` to `12` | Set spreading factor |
| `--txcr` | `5` to `8` | Set coding rate (4/5, 4/6, 4/7, 4/8) |
| `--setctry` | `0` to `99` | Set country preset (RX/TX parameters) |
| `--setboostedgain` | `on` / `off` | Enable/disable boosted RX gain (SX126x only) |

**Examples:**
```
--txpower 20
--txfreq 433.175
--txbw 250
--txsf 11
--lora
```

---

### **Spectrum Analysis**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--spectrum` | - | Run spectral scan with current settings |
| `--specstart` | `430.0` | Set spectrum start frequency (MHz) |
| `--specend` | `436.0` | Set spectrum end frequency (MHz) |
| `--specstep` | `0.1` | Set spectrum step size (MHz) |
| `--specsamples` | `500` to `2048` | Set number of samples per step |

**Example:**
```
--specstart 433.0
--specend 434.0
--specstep 0.05
--specsamples 1024
--spectrum
```

---

### **Network & Communication**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--setcall` | `OE0XXX-1` | Set callsign with SSID |
| `--setname` | `name` / `none` | Set operator first name |
| `--setudpcall` | `CALL-1` | Set UDP gateway callsign |
| `--mesh` | `on` / `off` | Enable/disable mesh networking |
| `--gateway` | `on` / `off` | Enable/disable gateway mode |
| `--gateway` | `pos` / `nopos` | Gateway with/without position forwarding |
| `--gateway srv` | `192.168.1.100` | Set gateway server IP address |
| `--extudp` | `on` / `off` | Enable/disable external UDP |
| `--extudpip` | `255.255.255.255` / `none` | Set external UDP server IP |
| `--mheard` | - | Show list of heard stations |
| `--mh` | - | Alias for `--mheard` |
| `--path` | - | Show message path information |
| `--hey` | - | Alias for `--path` |

**Examples:**
```
--setcall OE1ABC-7
--setname Martin
--mesh on
--gateway on
--mheard
```

---

### **WiFi Configuration**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--setssid` | `MyWiFi` / `none` | Set WiFi SSID |
| `--setpwd` | `password` / `none` | Set WiFi password |
| `--wifiap` | `on` / `off` | Enable/disable WiFi AP mode |
| `--setownip` | `192.168.1.100` | Set static IP address |
| `--setowngw` | `192.168.1.1` | Set gateway IP address |
| `--setownms` | `mask:255.255.255.0` | Set subnet mask |
| `--setowndns` | `8.8.8.8` | Set DNS server IP |
| `--sethamnet` | - | Apply HamNet default settings |
| `--setinet` | - | Apply Internet default settings |
| `--wifitxpower` | `20` | Set WiFi TX power in dBm |
| `--wifiset` | - | Show WiFi configuration |

**Examples:**
```
--setssid MyHomeWiFi
--setpwd MySecurePassword123
--setownip 192.168.1.50
--setowngw 192.168.1.1
--setownms mask:255.255.255.0
```

---

### **Web Server**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--webserver` | `on` / `off` | Enable/disable HTTP web server |
| `--webpwd` | `password` / `none` | Set web interface password |
| `--webtimer` | `0` | Set web server timeout (0 = always on) |

**Examples:**
```
--webserver on
--webpwd admin123
```

---

### **APRS Settings**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--symid` | `/` or `\` | Set APRS symbol table (primary / or alternate \) |
| `--symcd` | `[` | Set APRS symbol code (e.g., `[` = human) |
| `--atxt` | `MeshCom Node` / `none` | Set APRS comment text |
| `--setgrc` | `1;2;3;` | Set APRS group codes (semicolon-separated) |
| `--nomsgall` | `on` / `off` | Show/hide broadcast messages (*) on display |
| `--aprsset` | - | Show APRS configuration |

**APRS Symbol Examples:**
- `/[` = Human/Person
- `/k` = Truck
- `\n` = Red Cross
- `/>` = Car

**Examples:**
```
--symid /
--symcd [
--atxt MeshCom LoRa Node
--setgrc 1;5;7;
```

---

### **Sensor Configuration**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--weather` | - | Show temperature/humidity/pressure readings |
| `--wx` | - | Alias for `--weather` |
| `--bmp` | `on` | Enable BMP280 pressure sensor |
| `--bme` | `on` | Enable BME280 temp/hum/pressure sensor |
| `--680` | `on` | Enable BME680 gas sensor |
| `--811` | `on` | Enable CCS811 CO2/TVOC sensor |
| `--390` | `on` / `off` | Enable/disable BME390 sensor |
| `--aht20` | `on` / `off` | Enable/disable AHT20 temp/humidity sensor |
| `--sht21` | `on` / `off` | Enable/disable SHT21 temp/humidity sensor |
| `--lps33` | `on` / `off` | Enable/disable LPS33 pressure sensor (RAK4630 only) |
| `--bmx` | `BME` / `BMP` / `680` `off` | Disable BMx sensors |
| `--onewire` | `on` / `off` | Enable/disable DS18x20 OneWire temperature |
| `--onewire gpio` | `23` | Set OneWire GPIO pin number |
| `--setpress` | `1013.25` | Set pressure calibration (hPa) |
| `--tempoff in` | `2.5` | Set indoor temperature offset (°C) |
| `--tempoff out` | `-1.0` | Set outdoor temperature offset (°C) |
| `--showi2c` | - | Scan and display I2C devices |

**Examples:**
```
--bme on
--onewire on
--onewire gpio 23
--weather
--showi2c
```

---

### **Telemetry/APRS Data**

APRS telemetry allows sending custom sensor data in standardized format.

| Command | Parameters | Description |
|---------|------------|-------------|
| `--parm` | `VOLT,TEMP,HUM,,` | Set parameter names (5 values, comma-separated) |
| `--unit` | `V,C,%,,` | Set units for each parameter |
| `--format` | `2,1,1,0,0` | Set decimal places for each value |
| `--eqns` | `0,1,0, 0,1,0, ...` | Set calibration equations (a,b,c coefficients) |
| `--values` | `press,hum,temp,onewire,co2` | Map internal sensors to telemetry channels |
| `--ptime` | `10` | Set measurement interval in minutes |
| `--tel` | - | Show current telemetry configuration |

**APRS Telemetry Format:**
- Each channel: `value = a*raw^2 + b*raw + c`
- Default equation: `0,1,0` (linear, no offset)

**Examples:**
```
--parm BATT,TEMP,HUM,PRESS,
--unit V,C,%,hPa,
--format 2,1,1,1,0
--values ,,temp,press,
--ptime 5
```

---

### **Hardware & GPIO**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--button` | `on` / `off` | Enable/disable user button |
| `--button gpio` | `38` | Set button GPIO pin number |
| `--analog gpio` | `35` | Set analog input GPIO pin |
| `--analog factor` | `2.0` | Set analog scaling factor |
| `--analog alpha` | `0.1` | Set analog filter smoothing (0.0-1.0) |
| `--analog slope` | `1.0` | Set analog calibration slope |
| `--analog offset` | `0.0` | Set analog calibration offset |
| `--analog atten` | `0` to `3` | Set ADC attenuation (ESP32: 0=0dB, 3=11dB) |
| `--analog filter` | `on` / `off` | Enable/disable analog value filtering |
| `--analog check` | `on` / `off` | Enable/disable analog monitoring |
| `--board led` | `on` / `off` | Enable/disable onboard LED |
| `--io` | - | Show GPIO pin status |
| `--setio` | `pin,mode,value` | Configure GPIO (mode: 0=input, 1=output) |
| `--setio clear` | - | Clear all GPIO configurations |
| `--setout` | `pin,value` | Set output pin value (0 or 1) |
| `--analogset` | - | Show analog configuration |

**Examples:**
```
--analog gpio 35
--analog factor 2.0
--analog filter on
--button gpio 38
--setout 12,1
--io
```

---

### **Power & Battery**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--volt` | - | Show current battery voltage |
| `--proz` | - | Show battery percentage |
| `--maxv` | `4.2` | Set 100% battery voltage reference |
| `--batt factor` | `2.0` | Set battery voltage divider factor |
| `--shunt` | `0.1` | Set current shunt resistance (Ω) |
| `--imax` | `3.0` | Set maximum current (A) |
| `--isamp` | `10` | Set current measurement sample count |
| `--ina226` | `on` / `off` | Enable/disable INA226 current sensor |

**Examples:**
```
--volt
--maxv 4.2
--batt factor 2.0
--ina226 on
```

---

### **Display & UI**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--display` | `on` / `off` | Enable/disable display |
| `--t5` | `on` / `off` | Enable/disable T5 e-paper display |
| `--all` | - | Show all messages on display |
| `--msg` | - | Show message information |

**Examples:**
```
--display on
--all
```

---

### **Bluetooth**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--btcode` | `123456` | Set BLE pairing PIN code (6 digits) |
| `--bleshort` | - | Use short BLE message format |
| `--blelong` | - | Use long BLE message format |

**Examples:**
```
--btcode 987654
--blelong
```

---

### **Debug & Logging**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--debug` | `on` / `off` | Enable/disable general debug output |
| `--bledebug` | `on` / `off` | Enable/disable BLE debug messages |
| `--loradebug` | `on` / `off` | Enable/disable LoRa radio debug |
| `--gpsdebug` | `on` / `off` | Enable/disable GPS debug output |
| `--wxdebug` | `on` / `off` | Enable/disable weather sensor debug |
| `--softserdebug` | `on` / `off` | Enable/disable SoftSerial debug |
| `--softserread` | `on` / `off` | Show/hide SoftSerial received messages |

**Examples:**
```
--loradebug on
--gpsdebug on
--debug off
```

---

### **Serial & SoftSerial**

SoftSerial provides a second serial port for external sensors.

| Command | Parameters | Description |
|---------|------------|-------------|
| `--softser` | `on` / `off` | Enable/disable SoftSerial |
| `--softser send` | - | Send test data via SoftSerial |
| `--softser app` | - | Enable application mode |
| `--softser test` | - | Run SoftSerial test |
| `--softser baud` | `9600` | Set SoftSerial baud rate |
| `--softser rxpin` | `16` | Set SoftSerial RX GPIO pin |
| `--softser txpin` | `17` | Set SoftSerial TX GPIO pin |
| `--softser fixpegel` | `100.5` | Set fixed water level value |
| `--softser fixpegel2` | `50.0` | Set second fixed water level |
| `--softser fixtemp` | `20.5` | Set fixed temperature value |
| `--softser xml` | - | Enable XML parsing mode |
| `--seset` | - | Show serial settings |

**Examples:**
```
--softser on
--softser baud 9600
--softser rxpin 16
--softser txpin 17
```

---

### **Time & Date**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--utcoff` | `+2.0` / `-5.5` | Set UTC offset in hours (supports half hours) |
| `--settime` | `2025.02.14 15:30:00` | Set date and time manually |
| `--setrtc` | `value` | Set RTC register value |

**Examples:**
```
--utcoff +1.0
--settime 2025.02.14 12:00:00
```

---

### **Advanced Settings**

| Command | Parameters | Description |
|---------|------------|-------------|
| `--setinfo` | `on` / `off` | Include system info in transmitted messages |
| `--setcont` | `on` / `off` | Enable/disable continuous transmission mode |
| `--setretx` | `on` / `off` | Enable/disable automatic retransmission |
| `--shortpath` | `on` / `off` | Use shortened path format in messages |
| `--compress` | `value` | Set message compression level |
| `--passwd` | `password` | Set system password |
| `--regex` | - | Show regex pattern configuration |
| `--nodeset` | - | Show node configuration summary |
| `--conffin` | - | Mark configuration as finished |

**Examples:**
```
--setinfo on
--shortpath on
--nodeset
```

---

## Complete BLE Command Example

### Python Example (using `bleak` library)

```python
import asyncio
from bleak import BleakClient, BleakScanner

MESHCOM_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
TX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # Write to device


async def send_command(device_address, command):
    """Send a command to MeshCom device via BLE"""
    async with BleakClient(device_address) as client:
        # Prepare 0xA0 message
        cmd_bytes = command.encode("utf-8")
        length = len(cmd_bytes) + 2
        message = bytes([length, 0xA0]) + cmd_bytes

        # Send via BLE
        await client.write_gatt_char(TX_CHAR_UUID, message)
        print(f"Sent: {command}")


# Usage
asyncio.run(send_command("AA:BB:CC:DD:EE:FF", "--info"))
asyncio.run(send_command("AA:BB:CC:DD:EE:FF", "--sendpos"))
```

### Arduino/ESP32 Example

```cpp
#include <BLEDevice.h>

void sendBLECommand(BLEClient* pClient, const char* command) {
    BLERemoteCharacteristic* pTxChar = /* get TX characteristic */;

    uint8_t length = strlen(command) + 2;
    uint8_t message[length];
    message[0] = length;
    message[1] = 0xA0;
    memcpy(message + 2, command, strlen(command));

    pTxChar->writeValue(message, length);
}

// Usage
sendBLECommand(pClient, "--info");
sendBLECommand(pClient, "--sendpos");
```

---

## Message Flow

```
Phone App
   ↓
[Length][0xA0][--command]
   ↓
BLE UART Service (NimBLE)
   ↓
readPhoneCommand() → phone_commands.cpp:368
   ↓
commandProcessor() → command_functions.cpp
   ↓
Execute command & set flags
   ↓
Main loop processes flags
   ↓
Response sent back via BLE (if applicable)
```

---

## Important Notes

1. **Command Parsing:** Commands starting with `--` are treated as system commands; others are sent as APRS text messages to the mesh network.

2. **Case Sensitivity:** All commands are case-sensitive and must use lowercase `--`.

3. **BLE MTU Limit:** Maximum BLE packet size is 247 bytes (MTU). Messages exceeding this will be truncated.

4. **Settings Persistence:** Most configuration commands require `--save` or `0xF0` message to persist to flash, otherwise settings are lost on reboot.

5. **Hello Handshake:** The phone app must send `0x10` hello message before other commands will be processed.

6. **Timestamp Sync:** Send `0x20` with UNIX timestamp to synchronize device clock (especially important for devices without GPS or RTC battery).

---

## Source Code References

- **BLE Protocol Handler:** `src/phone_commands.cpp:368-724`
- **Command Parser:** `src/command_functions.cpp:228-3842`
- **Command Check Function:** `commandCheck()` - string comparison helper
- **BLE Service:** NimBLE UART service (UUID: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E)

---

## Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-02-14 | 1.0 | Initial documentation based on firmware 4.35k |
| 2026-02-14 | 1.1 | Added "Querying Device State (TYP Responses)" section with JSON query commands |
| 2026-02-14 | 1.2 | Complete register mapping: all 12 TYP registers (G, I, SN, SA, SE, S1, SW, S2, AN, TM, W, IO) with detailed field descriptions, multi-part response handling, and source code references |

---

**End of Document**
