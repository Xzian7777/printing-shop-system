# USB RFID reader

The ESP32 sends physical card taps over USB. `tools/rfid_bridge.py` persists them in a local SQLite queue and sends them to Flask. Reader credentials stay on the PC. Customer assignment and points calculations happen on the server.

## Wiring

Disconnect USB before changing connections. Use the labels printed on the boards to verify power connections.

| RC522 | ESP32 GPIO |
| --- | --- |
| SDA / SS | 5 |
| SCK | 18 |
| MOSI | 23 |
| MISO | 19 |
| RST | 0 |
| 3.3V | 3V3 |
| GND | GND |

IRQ is unused. Supply the RC522 with 3.3V. The firmware follows the latest wiring supplied for this reader, with MOSI GPIO23 and RST GPIO0. This configuration uses the ESP32 and RC522.

## Build and upload

Install the ESP32 Arduino board package and MFRC522 library. Compile `kcz_rfid_reader/kcz_rfid_reader.ino` for `esp32:esp32:esp32`, then upload to the detected serial port. Serial output uses 115200 baud. `PING` returns a JSON hello containing the board's reader ID. A startup `RC522 not detected` error requires checking reader power and wiring before enrollment.

## Pair the bridge

1. Start Flask and sign in as an administrator.
2. Open the RFID/Loyalty page and pair the reader using its ID. Copy the private key shown once.
3. In a terminal opened in the web project, run the following command. Paste the key only at the private terminal prompt.

```powershell
.\venv\Scripts\python.exe tools/rfid_bridge.py --setup --reader-id 982D2B0D5020 --port COM4
```

4. Start the bridge in that terminal.

```powershell
.\venv\Scripts\python.exe tools/rfid_bridge.py
```

The default backend is `http://127.0.0.1:5000`. Setup accepts `--server` for a hosted HTTPS backend. Configuration and durable queued events are stored in the ignored `rfid-local` directory. Keep the reader key private. Rotating it in admin requires repeating bridge setup with the replacement key.

## Enroll and test

1. Select the registered reader in admin and choose Capture card.
2. Wait for the bridge to report capture mode, then scan a card. Capture expires after five minutes and awards zero points.
3. Select an existing customer account and enroll the captured card. Replacing an existing physical card requires the replacement checkbox.
4. Remove the card for at least 600 milliseconds, then tap it again. A new eligible tap adds one point. Keeping the card on the reader produces one event.
5. Verify the available balance and history in web and mobile. Remove and tap again to verify a second event.
6. Temporarily stop Flask while the bridge stays running. Scan again, restart Flask, and verify that the queued event credits once. Reconnect USB and restart the bridge to check retry protection.

The existing paid-completed-purchase point remains enabled. Manual card identification awards zero points. Card enrollment preserves legacy RFID records for review.

## Queue behavior

The ESP32 keeps up to 32 unacknowledged scans in nonvolatile storage. The bridge acknowledges a scan after committing it to SQLite. Server retries use the same event ID and payload. The server transaction creates one immutable earn event and updates the canonical loyalty balance together.

The bridge caches card-binding versions. Unassigned cards and scans with a changed binding are recorded without credit. Enroll cards while online before testing offline credit. Keep the USB bridge running during a Flask outage so each scan can retain its current capture and binding context. A full board queue requires reconnecting the bridge and tapping again after removing the card.

The PC, bridge, and Flask service must be running for immediate balance updates. Physical card-detection, enrollment, and live balance tests must pass before operational use.
