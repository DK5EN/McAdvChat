# McApp

McApp is a single page, webbrowser rendered, web application. It should run on every modern browser out there, but you never know. Settings get persisted in a sqlite database.

Rendering on the client, the Raspberry Pi is only a lightweight proxy database server between a MeshCom ESP32 IoT device that is connected via BLE and the web browser. It uses SSE and RestAPI for communication.

- Modern streaming to the browser, TypeScript code in the browser, Python in the backend
- On initial page load, latest messages get to your browser
- Infinite scrolling is enabled, so go back to 1972 if you're so inclined
- Try to install the app on your mobile phone homescreen by storing it as an icon on your home screen (works best on iPhone, Android phones have SSL requirement for this)
- Installs with an icon on your mobile phone

### Installation

Run this single command on a Raspberry Pi Zero 2 W for fresh install, update, or repair:

# Install latest stable release

```bash
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash
```

The script auto-detects its context and does the right thing:

- **Fresh install**: Prompts for configuration, installs everything
- **Update**: Checks versions, updates if newer available
- **Incomplete**: Resumes configuration prompts
- **Migration**: Upgrades from old install scripts
- **Dev mode** (`--dev`): Installs the latest pre-release instead of the latest stable release

A piped install is pinned: the bootstrap script resolves one release tag
(latest stable by default, latest pre-release under `--dev`, or the tag from
`--tag`) and fetches its libs, templates, and app from that same tag — the
command above run again later on the same tag reproduces the same box. See
[Pinning to a specific release](#pinning-to-a-specific-release) below and
[`doc/version-logic.md`](doc/version-logic.md) for the resolution rules.

#### Requirements

- Raspberry Pi Zero 2W (or any Pi with ARM Cortex-A53)
- Debian Bookworm (12) or Trixie (13) Light (Desktop OS not supported on Pi Zero)
- Python 3.11+ (provided by the OS — no manual install needed)
- 512MB RAM
- SD card (8GB+)
- Internet Network connectivity
- MeshCom Node in Bluetooth range, but also support UDP communication
- Supports mDNS name resultion
- Looks for Fritz!Box DNS server in your environment

| Debian              | Python | Firewall | Status         |
| ------------------- | ------ | -------- | -------------- |
| Trixie (13) Light   | 3.13   | nftables | Primary target |
| Bookworm (12) Light | 3.11   | iptables | Supported      |

The bootstrap script auto-detects the Debian version and uses the appropriate Python and firewall packages.

#### Flashing the SD Card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to flash the SD card:

1. Select **Raspberry Pi Zero 2 W** as device model

   ![Select Pi Zero 2W](doc/1_Flash-PiZero2W.png)

2. Under **Raspberry Pi OS (other)**...

   ![Select Other OS](doc/2_OtherOS.png)

3. ...choose **Raspberry Pi OS Lite (64-bit)** — Debian Trixie, no desktop environment

   ![Select Lite OS](doc/3_LightOS.png)

4. Select your SD card and flash

   ![Select SD Card](doc/4_SelectCard.png)

> **Note:** After selecting the SD card, the Imager prompts you to configure hostname, user, WiFi, and SSH. Set these before writing — this avoids manual setup after first boot.

#### Configuration

During first install, you'll be prompted for:

| Setting      | Example             | Description                          |
| ------------ | ------------------- | ------------------------------------ |
| Callsign     | `DX9XX-99`          | Your ham radio callsign with SSID    |
| User info    | `My Node \| Munich` | Text returned by `!userinfo` command |
| Node address | `mcapp.local`       | MeshCom node hostname or IP          |
| Latitude     | `48.2082`           | Your station latitude                |
| Longitude    | `16.3738`           | Your station longitude               |
| City         | `Munich`            | Your city (for weather reports)      |

Configuration is stored in `/etc/mcapp/config.json`.

#### Command-Line Options

```bash
sudo ./mcapp.sh --check       # Dry-run, show what would change
sudo ./mcapp.sh --force       # Force reinstall everything
sudo ./mcapp.sh --fix         # Repair broken installation
sudo ./mcapp.sh --reconfigure # Re-prompt for configuration
sudo ./mcapp.sh --dev         # Install latest development pre-release
sudo ./mcapp.sh --tag v1.6.13 # Install a specific release tag (pins libs+templates+app too)
sudo ./mcapp.sh --ref development # Force just the bootstrap tree ref, independent of the app version
sudo ./mcapp.sh --quiet       # Minimal output (for cron)
```

#### Service Management

```bash
# McApp
sudo systemctl status mcapp       # Check status
sudo journalctl -u mcapp -f       # View logs
sudo systemctl restart mcapp      # Restart service
sudo systemctl stop mcapp         # Stop service

# BLE Remote Service (on Pi with Bluetooth hardware)
sudo systemctl status mcapp-ble   # Check status
sudo journalctl -u mcapp-ble -f   # View logs
sudo systemctl restart mcapp-ble  # Restart service
sudo systemctl stop mcapp-ble     # Stop service
```

#### Access Points

After installation, all client traffic goes through port 80 (lighttpd):

| Service      | URL                                | Notes                           |
| ------------ | ---------------------------------- | ------------------------------- |
| Web UI       | `http://<hostname>.local/webapp/`  | Vue.js SPA                      |
| SSE Stream   | `http://<hostname>.local/events`   | Proxied to FastAPI :2981        |
| REST API     | `http://<hostname>.local/api/send` | Proxied to FastAPI :2981        |
| Health Check | `http://<hostname>.local/health`   | Proxied to FastAPI :2981        |
| BLE Service  | `http://<pi-hostname>.local:8081`  | Only for distributed BLE setups |

No high ports are exposed to clients. lighttpd reverse-proxies `/events`, `/api/`, and `/health` to the FastAPI backend on `127.0.0.1:2981`.

#### Firewall Ports

| Port     | Protocol | Direction      | Purpose                                   |
| -------- | -------- | -------------- | ----------------------------------------- |
| 22/tcp   | SSH      | inbound        | Remote administration (rate limited)      |
| 80/tcp   | HTTP     | inbound        | lighttpd (webapp + API proxy)             |
| 1799/udp | UDP      | bidirectional  | MeshCom node communication                |
| 2985/tcp | HTTP     | inbound        | Update runner SSE stream (OTA updates)    |
| 5353/udp | mDNS     | inbound        | `.local` hostname resolution              |
| 2981/tcp | HTTP     | localhost only | FastAPI SSE/REST (not exposed externally) |

#### TLS Remote Access (Optional)

To access McApp securely over the internet with HTTPS, run the standalone TLS setup script after confirming your local setup works:

```bash
sudo ./scripts/ssl-tunnel-setup.sh
```

This installs **Caddy** as a TLS reverse proxy with automated Let's Encrypt certificates (DNS-01 challenge) and built-in DDNS updates. Supported DNS providers:

| Provider          | Type            | Port Forwarding |
| ----------------- | --------------- | --------------- |
| DuckDNS           | Free DDNS       | 443 required    |
| Cloudflare        | Own domain      | 443 required    |
| deSEC.io          | Free DDNS       | 443 required    |
| Cloudflare Tunnel | Outbound tunnel | None needed     |

After setup, your McApp is available at `https://your-hostname/webapp/`.

```bash
sudo ./scripts/ssl-tunnel-setup.sh --status   # Check TLS status
sudo ./scripts/ssl-tunnel-setup.sh --remove   # Revert to plain HTTP
```

For more details see:

- [`doc/ssl-tunnel.md`](doc/ssl-tunnel.md) — setup guide (German)
- [`doc/tls-architecture.md`](doc/tls-architecture.md) — architecture diagrams
- [`doc/tls-maintenance-SOP.md`](doc/tls-maintenance-SOP.md) — maintenance procedures
- [`doc/2026-02-10_1200-tls-remote-access-ADR.md`](doc/2026-02-10_1200-tls-remote-access-ADR.md) — design rationale

#### Automatic Updates

Set up a cron job for automatic updates:

```bash
# /etc/cron.d/mcapp-update
0 4 * * * root curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | bash --quiet 2>&1 | logger -t mcapp-update
```

#### Frontend-Triggered Updates (OTA)

McApp supports over-the-air updates directly from the webapp UI. The system uses a slot-based architecture with 3 independent deployment slots, automatic database snapshots, and health-check-based rollback.

**How it works:**

1. The webapp checks GitHub for new releases via `GET /api/update/check`
2. The user triggers an update from the Settings page
3. A standalone update runner starts on **port 2985** and streams real-time progress as SSE events
4. The bootstrap script deploys into a new slot while the current version keeps running
5. After deployment, health checks verify the new version works correctly
6. On failure, the system automatically rolls back to the previous slot (restoring config and database)

The update runner is launched via a systemd `.path` trigger — no elevated permissions are needed from the webapp. Slot metadata (version, deployment time, active status) is persisted in `~/mcapp-slots/meta/` and available via `GET /api/update/slots`.

```bash
# Check update runner logs
sudo journalctl -u mcapp-update --no-pager -l

# View slot metadata on Pi
ls -la ~/mcapp-slots/current  # Shows active slot symlink
cat ~/mcapp-slots/meta/slot-*.json | jq .
```

#### Non-stable Development Release

Install latest development pre-release

```bash
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/development/bootstrap/mcapp.sh | sudo bash -s -- --dev
```

#### Pinning to a specific release

```bash
# Install (or reinstall/repair onto) a specific release tag — libs,
# templates, and app all come from that tag, not from branch tip
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash -s -- --tag v1.6.13

# Force just the bootstrap tree (libs + templates) to a branch or tag,
# independently of the app version — for developing bootstrap changes
# without cutting a release, or as a one-line field rollback to pre-pinning
# branch-tip behavior
curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/main/bootstrap/mcapp.sh | sudo bash -s -- --ref development
```

`--tag` only pins a release that already ships every function the fetched
script's `main()` calls — an old tag whose libs predate a newer feature
(e.g. one cut before Caddy/system-epoch support existed) aborts cleanly
naming the missing functions instead of installing a mismatched pair. See
[`doc/version-logic.md`](doc/version-logic.md) for the full resolution
rules, the GitHub-API-unreachable fallback, and that skew guard.

# Architecture

```mermaid
graph TD
    Browser["🌐 Browser<br/>(Vue.js SPA)"]

    Browser -- "WSS" --> MeshComServer["☁️ MeshCom<br/>Internet Server"]

    Browser -- "HTTP :80" --> Lighttpd["🔀 lighttpd<br/>(static files + proxy)"]

    Browser -- "SSE :2985" --> UpdateRunner["🔄 Update Runner<br/>(OTA deploy)"]

    Lighttpd -- "/events, /api/" --> McApp["⚙️ McApp<br/>FastAPI :2981"]

    McApp -- "trigger file" --> UpdateRunner

    McApp -- "HTTP / SSE<br/>:8081" --> BLEProxy["📡 BLE Proxy<br/>(FastAPI)"]

    BLEProxy -- "Bluetooth<br/>GATT" --> MeshComNode["📻 MeshCom<br/>Node"]

    McApp -- "UDP :1799<br/>⇄ bidirectional" --> MeshComNode
```

| Chat                      | Map                    |
| ------------------------- | ---------------------- |
| ![Chat](doc/chat.png)     | ![Map](doc/map.png)    |
| **MHeard**                | **Weather**            |
| ![MHeard](doc/mheard.png) | ![Weather](doc/wx.png) |

# McApp Specification

The MeshCom McApp project consists of three components:

- **Frontend** - attractive, responsive, and multi-device capable
  - Must be super fast and responsive
  - Must have dark mode auto switching
  - Should behave like a native app
  - Should follow current web application programming standards
  - Should be installable as a Progressive Web App (PWA) in the browser
  - Should be addable to the phone's home screen as a PWA
  - Should run on laptops, iPads, and phones alike
  - Must dynamically adapt to the screen size (responsive design)
  - Must be regularly updated with security fixes, as libraries may contain vulnerabilities

  - **Connectivity**
    - Must be able to connect to the MeshCom node via BLE and UDP
    - Must filter out characters that do not conform to the APRS + UTF-8 protocol
    - Should be fault-tolerant with characters that can be filtered without issues
    - Messages that cannot be easily filtered and contain illegal binary data are hard-rejected, as they originate from malfunctioning E22 nodes. There is no point displaying them since they contain garbage data.
    - Must reload messages from the MeshCom node on every browser refresh

  - **Chat View**
    - Must be able to filter groups and users
    - Spam and illegal messages are assigned to group 9999, as they disrupt the normal message view
    - Must receive the time signal and issue a watchdog message when no time signal packet is received from Vienna (MeshCom node must be connected to the internet for this to work)
    - Must have a search field so that any callsign or group chat can be filtered
    - The target should dynamically adapt to the selected group or callsign
    - The send button is triggered by pressing Enter (newlines are not allowed in APRS)
    - Messages first enter a send queue so the MeshCom node is not overwhelmed. Current send delay is 12 seconds, which is sometimes still too fast for reliable RF transmission.
    - Optional: dynamic message delay, which can only be implemented with Bluetooth access since we don't get feedback from the MeshCom node

  - **Map View**
    - Requires APRS graphics
    - Must have a searchable map of nodes
    - Map must support satellite view and dark mode
    - Clicking on a node shows more information
    - Displays temperature, humidity, and air pressure sensor data from telemetry beacons

  - **MHeard View**
    - Shows all heard stations with RSSI and SNR signal quality
    - Displays the relay path for each station
    - Clicking a station highlights its path on the map

  - **FT - File Transfer**
    - Was implemented, but then removed as the bandwidth is too small

  - **Setup Page**
    - Must be able to filter groups and PN users
    - Must automatically detect WebSocket connection parameters
    - Must be able to delete users and groups (in the browser, not on the server)
    - No "SAVE Settings" button - must save input when leaving the page

- **Server Backend**
  - Runs on a Raspberry Pi Zero 2W, which is particularly power-efficient and more than sufficient for our purposes
  - Receives all messages from the MeshCom node via UDP (`--extudpip 192...` and `--extudp on` must be set)
  - Preferably connects via Bluetooth protocol for more stable transmission with more data and more capabilities like RSSI and SNR info
  - Implements a keep-alive over Bluetooth and automatically reconnects if the connection is lost
  - Automatically sets the timezone on the MeshCom node, accounting for daylight saving time
  - Must perform UTF-8 and APRS protocol checks, as there are regularly illegal characters that cause crashes
  - Can automatically update scripts and website via the bootstrap script
  - Generates mheard RSSI and SNR statistics when BLE is connected

- **Use Cases:**
  - Chat
    - With delivery confirmation "double checkmark" (for personal chats and group chats)
    - Look and feel following current chat apps for intuitive usage
    - "Single checkmark" for messages that successfully arrived on the MeshCom server, when web connection is active
  - Map
    - Display all received POS reports on a map with various display options
  - Configuration page: the config page must follow current design guidelines

  - Optional: connect multiple nodes via UDP
    - Multiple nodes can point to the Raspberry Pi as their target. This ensures that if one node misses something, we get the message from the other node. Two is good, three is better. Best to implement "antenna diversity" by distributing nodes across the space.

# Background Information

- Messages must conform to the APRS protocol
  - APRS messages are designed to be ASCII-compatible, typically 7-bit printable ASCII (decimal 33-126)
  - Control characters (like null \x00, bell \x07, or newline \x0A) and extended 8-bit values (128-255) are not safe
  - Characters outside this range may cause message corruption
  - Allowed: A-Z, a-z, 0-9, common punctuation
  - Not allowed: _binary_data_, _emoji_, _extended_Unicode_

- MeshCom uses UTF-8, with the peculiarity that during UDP transmission the JSON is double-stringified

- MeshCom can transmit unsafe characters, especially when an E22 node is operated with an unclean power supply
  - The raw byte stream can be toxic and should urgently go through multiple sanitizing steps

- Compression on just a few bytes unfortunately only adds overhead without any real savings
  - One would have to build a custom dictionary for the ham radio jargon in the DACH region to increase entropy
  - Cutting off one bit to effectively implement Base91 would in turn require all 8 bits to be usable on the LoRa link

- Encoding binary data with Base64 works and transmission works as well

- Reed-Solomon (RS) is operational and would offer significant advantages over the very simple Hamming coding in LoRa for transmitting binary data and receiving error-prone packets. However, access to raw packets that have an invalid CRC is missing.

- RS operates on fixed-size blocks, so we can pack long messages into short chunks that are transmitted as bursts
  - This would bring enormous advantages since messages >70 characters can hardly be transmitted successfully because of too many collisions in the 433MHz Band

- RS assumes that individual bits flip during transmission. However, the MeshCom node already handles this with Hamming, though far less robustly and fault-tolerantly
  - MeshCom discards LoRa packets with bit errors. Therefore RS cannot help us recover the packet here

- With interleaving, the loss of one or two chunks during multi-chunk transmission can be compensated
  - The overhead is immense, making retransmission requests significantly more effective

- RS can be Base64 encoded and can then handle the loss of entire chunks. But many of the major advantages are throttled by the LoRa protocol

### Technical Details on the Preliminary Considerations, Which Unfortunately Did Not Pan Out

- Channel model: publicly shared medium, definitely with hidden-node problem since everything is repeated up to 4 hops, high packet error probability with increasing payload size
- Payload packet size: maximum 149 bytes; restriction to UTF-8-safe, APRS-compatible, JSON-compatible characters
- Chunking: messages are segmented into ~10-byte payload chunks
- Compression: real-time compatible lossless compression (e.g., deflate)
- Error correction: FEC with redundancy factor r = 1.2 - i.e., 20% additional data (Reed-Solomon)
- Packet structure: [Message Header ID|Payload incl. FEC]
- Retransmissions: optional request for individual packets upon detecting gaps in reception

## 2) Statistical/Technical Foundation, a Brief Look at the Scientific Side

Channel Modeling (Packet Loss Rate as a Function of Payload)

Assuming the error probability Pe(l) increases exponentially with the payload length l:
Pe(l) = 1 - e^(-lambda * l)

With typical lambda approximately 0.01, for example:

- 10 bytes: ~10% error probability
- 50 bytes: ~39%
- 100 bytes: ~63%
- 149 bytes: ~77%

This empirical model allows us to determine the optimal chunk size: a compromise between efficiency (overhead down) and success rate (packet loss down).

FEC Method:

Established methods such as Reed-Solomon (for block-based transmission) are used.

- Reed-Solomon is more robust than Hamming code in LoRa
- Can correct multiple errors per block
- Can process both distributed and burst errors
- Better secures lossy channels like LoRa or UDP
- Even more robust with interleaving (equivalent to a 90-degree rotation of the transmission matrix)

The goal is to generate n packets from k original packets, so that the message is reconstructable as long as at least k packets are received:

    r=n/k, e.g. r=1.2 (for 20% overhead)

Expected Success Rate:

With p as the success probability per packet and k as the minimum number:

    P_success = sum {i=k}^{n} (n/i) * p^i (1-p)^(n-i)

This allows targeted optimization of n, k, and r.

## 3) State of the Art (Similar Systems: DVB, DAB, LoRa)

Comparable Systems

- DVB-S2: Uses LDPC + BCH for FEC, with very high redundancy levels in poor channels
- DAB+ (Digital Audio Broadcast): Reed-Solomon at the application level, time diversity
- LoRa: Adaptive Data Rate, small packets, strong FEC with Hamming/FEC(4,5)

Lessons Learned

- FEC + interleaving + fragmentation are central pillars
- Adaptive coding based on channel conditions improves efficiency (not tested)
- Selective Acknowledgements (SACK) are essential for high reliability in real-time reassembly

## 4) MeshCom

It is important to emphasize that we build on the existing MeshCom protocol, which in turn uses LoRa with the APRS protocol as its foundation. Communication with the current channel parameters is possible down to approximately SNR -17dB. Nevertheless, transmissions are regularly lost since delivery is not guaranteed, and as shown above, longer messages have a higher susceptibility to complete loss. One can also observe that some LoRa frames are retransmitted; the author has no further details on this.

## 5) Verdict, Discussion, and Open Points

Strengths

- Clean concept with exact boundaries for MeshCom spec payload (149 bytes, APRS/JSON-safe)
- Real-time capable, robust, and adaptive - for high user satisfaction
- Practical, realistic assumptions (error rates, broadcast model)
- Scientifically grounded, based on known models and approaches, no pseudo-science

Weaknesses

- The calculation was made without access to raw MeshCom LoRa frames

What's Still Missing / What Needs to Be Defined to Deepen the Idea

- Chunk size tuning algorithm - optimal based on channel quality - for optimal channel throughput
- Loss model / packet scheduling - retry strategy and timeouts?
  - How can channel quality be measured in MeshCom?
- Buffer strategy on reception - how long to wait for missing packets? When does patience run out?
- Collision avoidance with simultaneous senders?
  - Hidden node is a real problem with max hop count 4 and leads to massive collisions
  - How could a token or time-slot model be implemented?
  - Does a time-slot model even make sense when we have many LoRa devices that are completely unaware?
  - Would likely need to be implemented as a change to the MeshCom firmware ("won't happen, says Kurt OE1KBC")
- Security - already handled at the LoRa MeshCom node (Hamming). If RS is used, everything is secured there

Optional Extensions

- Adaptive redundancy: increase FEC share with high packet loss
- Streaming preview: display "User is typing" + live fragment display. This would definitely be the coolest feature.
- UI feedback: green = received, yellow = expected, red = lost. Definitely needs to be included.

References:

- Private exchange with developers in the Telegram chat (cannot be made public)
- https://icssw.org/grundlegende-spezifikationen/
- https://en.wikipedia.org/wiki/Raptor_code
- https://en.wikipedia.org/wiki/Reed-Solomon_error_correction
- https://en.wikipedia.org/wiki/JSON
- https://en.wikipedia.org/wiki/Chunking_(computing)
- https://en.wikipedia.org/wiki/Streaming_protocol
- https://en.wikipedia.org/wiki/Forward_error_correction
- https://en.wikipedia.org/wiki/Channel_(communications)
- https://en.wikipedia.org/wiki/Data_transmission
- https://files.tapr.org/software_library/aprs/aprsspec/spec/aprs100/APRSProt.pdf
  > for allowed APRS character definition
