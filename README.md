# Universal Remote for the GitHub Universe 2025 Badge

A full MonaOS remote-control application for the GitHub Universe 2025
hackable badge. It transmits and learns consumer infrared commands, manages
multiple saved devices, discovers nearby Wi-Fi/BLE radios, and can back up its
profiles to an optional SQLite companion service.

## Features

- Multi-screen 160x120 UI with scrollable menus and consistent controls.
- Saved-device list, active-device selection, rename, details, and confirmed
  deletion.
- Selected long options and status messages scroll as a marquee instead of
  hiding the important text behind an ellipsis.
- Common Samsung TV preset with 35 controls, including navigation, playback,
  digits, input, menu, and channel controls.
- Tunable Samsung Power burst: x1, x2 (default), or x3 complete Samsung32
  frames scheduled 110,000 microseconds start-to-start. This is designed to
  improve marginal reception without continuously repeating toggle commands.
- Targeted Samsung Power repair restores only the canonical `E0E040BF` Power
  command on a selected remote while preserving every other learned key.
- Blank IR remotes with a two-press learning flow. The receiver distinguishes
  full, repeated, malformed, fragmented, edge-glitched, and mismatched frames
  before saving.
- Live IR diagnostics with activity, capture, timeout, discard, RX/TX, carrier,
  and repeat-timing status.
- Non-blocking Wi-Fi access-point and BLE-advertiser discovery, strongest-signal
  deduplication, one/many/all selection, and one-transaction saving. The badge
  can retain 32 devices, including a complete 24-result scan.
- Crash-safe schema-v4 profile storage with compact parsed presets, validation, atomic replacement,
  backup recovery, migration, multi-device metadata, and bounded resource use.
- Low-memory saves stream profiles to flash, stop the IR receiver outside Learn/Listen,
  and store decoded Samsung commands without duplicating raw pulse arrays.
- Optional file-based SQLite companion with profile backup/restore and a
  dependency-free local HTTP API.
- Standalone **Badge Settings** app with nearby Wi-Fi selection plus nested
  editors for GitHub identity/token, Weather location, WLED IPv4, and the IR
  companion URL. Sensitive values remain masked and targeted saves preserve
  every unrelated `secrets.py` setting.
- Built-in WLED discovery and control: select a verified controller from the
  local network, toggle power, apply color presets or custom RGB values, browse
  the controller's real effect list, and adjust brightness.

## Install on the badge

1. Double-press RESET to mount the `BADGER` drive.
2. Copy the contents of `app` to `BADGER:/apps/universal_ir`.
3. Eject `BADGER`, then press RESET once if it does not reboot itself.
4. Press HOME once to open the launcher and select **Universal IR**.

The factory firmware aliases are used directly:

- IR receiver: `board.IR_RX` / GPIO21
- IR transmitter: `board.IR_TX` / GPIO20

## Optional scrollable launcher patch

`extras/menu/__init__.py` is a modified version of the official 2025 badge
launcher. It makes all discovered app pages reachable when more than six apps
are installed. It is not required by Universal IR itself.

Use this replacement only with the matching `badger/home` launcher version,
and back up the badge's original `/system/apps/menu/__init__.py` first. On the
mounted `BADGER` volume, the runtime path is normally represented by
`BADGER:/apps/menu/__init__.py`. The upstream file and license are recorded in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Badge Settings app

The `badge_settings` directory is a separate app; it does not overwrite the
factory `wifi` status app. Install it by copying the whole directory to
`BADGER:/apps/badge_settings`. If you use the optional launcher replacement,
also copy the updated `extras/menu/__init__.py` to
`BADGER:/apps/menu/__init__.py` so the launcher displays **Badge Settings**.

The app has five categories with their own submenus:

- **Wi-Fi**: status, scan/change network, connect saved, disconnect, and forget.
- **GitHub**: update the user ID, replace the masked API token, or clear either.
- **Weather**: set a city/location string or return to automatic IP location.
- **WLED**: scan/select a controller, edit or clear its IPv4 address, toggle
  power, choose preset/custom RGB colors, load supported effects, and adjust
  brightness.
- **IR Companion**: set or clear the trusted-LAN companion-service URL.

Wi-Fi scans retain only the strongest 16 bounded results. WPA/WPA2 networks are
supported; WEP, WPA3, enterprise Wi-Fi, captive portals, and 5 GHz-only access
points are not offered as connectable choices. Credentials are written only
after the badge receives a valid DHCP address, so a wrong password never
replaces the previous saved network.

Text fields use a five-row carousel keyboard:

- UP/DOWN changes the character group.
- A/C moves left or right through that group.
- B inserts the selected character or runs an action such as LEFT, RIGHT,
  BACKSPACE, DELETE, CLEAR, DONE, or CANCEL.

Passwords and tokens are never rendered as plaintext or included in errors.
The app atomically updates only supported assignments in `/secrets.py`, checks
the resulting Python syntax, preserves comments and unknown settings, and keeps
a recoverable backup. Press HOME after saving to reset the badge so existing
apps reload the new values.

`/secrets.py` is still plaintext on the USB-accessible badge filesystem; this
app improves safe editing and recovery, not credential-at-rest security.

### WLED discovery and controls

The badge and WLED controller must already be connected to the same local
network. **Scan & select** listens for native WLED announcements and performs a
bounded, incremental scan of only the badge's local subnet. Each candidate is
verified through WLED's `/json/info` endpoint before it can replace the saved
`WLED_IP`. A full scan usually takes about 30–40 seconds; manual IPv4 entry remains
available for networks that block device-to-device traffic or discovery.

Color, power, brightness, and effect changes take effect immediately and do not
require HOME. Effect names and IDs are loaded from the selected controller's
`/json/eff` endpoint, so reserved/unsupported modes are excluded and the list
matches that controller's firmware. The implementation uses the official
[WLED JSON API](https://kno.wled.ge/interfaces/json-api/).

## Controls

Controls are shown in the footer on every screen:

- UP/DOWN: move through the current list.
- B: open, choose, toggle, or send the highlighted command.
- C: screen-specific action, such as Learn or Actions.
- A: back or cancel.
- HOME on the back: return to the MonaOS launcher.

To power a Samsung TV, highlight **Power** in the remote screen and press the
front **B** button. RESET and HOME are system buttons, not TV Power buttons.
If reception is still inconsistent, open **My devices → C Actions → Power
strength** and cycle x1/x2/x3. Start with x2; x3 is the strongest option. If
an older learned profile sends a different action from its Power slot, choose
**Repair Power code** in the same Actions menu; the confirmation changes only
Power and leaves every other key untouched.

To learn a key, highlight the slot and press C. Aim the original remote at the
badge receiver, press and release the key, then press the same key a second
time when prompted. Many Samsung Smart/One remotes send most keys over
Bluetooth and use infrared only for Power; test Power first. Bluetooth-only
button presses cannot be captured by an IR learner.

## Nearby discovery boundary

IR and 315/433 MHz sub-GHz devices cannot advertise themselves, so they cannot
be enumerated by a software scan. The badge also has no sub-GHz transceiver;
that requires external hardware such as a CC1101.

The Nearby screen performs two real radio scans supported by the badge:

- Wi-Fi finds access points, not every client connected to the LAN.
- BLE finds advertising devices.

A discovered radio is deliberately labelled as not yet controllable. Presence
alone does not reveal a device's pairing credentials or control protocol. IR
devices are added from a preset or by learning their original remote.

## Storage and SQLite

MonaOS MicroPython does not include `sqlite3`. The badge therefore keeps its
working profile in a small atomic store at
`/storage/universal_ir/profiles.json`; this lets the remote work offline and
boot without another computer.

For a real SQLite file, run the included CPython companion on a laptop or
Raspberry Pi:

```powershell
python -m companion --db .\badge-ir.sqlite3 serve
```

The safe default listens only on `127.0.0.1`. To let the badge reach it over a
trusted LAN, bind deliberately and allow remote access:

```powershell
python -m companion --db .\badge-ir.sqlite3 serve `
  --host 0.0.0.0 --allow-remote
```

Add the laptop/Raspberry Pi LAN address to the badge's root `secrets.py`:

```python
IR_COMPANION_URL = "http://192.168.1.50:8765"
```

Then use **SQLite backup** in the app to back up or restore all devices and
commands. A restore requires a separate confirmation because it replaces the
badge profile. Do not expose the unauthenticated companion port to the public
internet; keep it on a trusted LAN or behind an authenticated gateway.

The companion CLI can also import schema-v3 backups and export schema-v4 JSON:

```powershell
python -m companion --db .\badge-ir.sqlite3 import-profile .\profile.json
python -m companion --db .\badge-ir.sqlite3 export-profile .\profile.json
```

See [companion/API.md](companion/API.md) for the HTTP endpoints.

## Verification

Run all desktop-safe tests with:

```powershell
python -m unittest discover -s tests -v
```

The suite covers codecs, learning, discovery, diagnostics, repeat scheduling,
navigation, transactional badge storage, SQLite persistence, HTTP security
boundaries, companion sync, Wi-Fi state transitions, secret redaction, and
atomic settings rollback. The official badge simulator is used for UI
navigation and rendering; final PIO waveform and radio checks require the
physical badge.

## Attribution

The PIO pulse reader/sender is derived from the MIT-licensed IR beacon code in
[`badger/home`](https://github.com/badger/home/tree/main/ir-beacon), copyright
Christopher Parrott for Pimoroni Ltd.

The optional launcher replacement is derived from the MIT-licensed
[`badger/home` menu](https://github.com/badger/home/blob/4a3bf0395f79ae386a8d952f7da54281a2f00299/badge/apps/menu/__init__.py),
copyright Pimoroni & GitHub.

The Badge Settings icon is the MIT-licensed Wi-Fi icon from
[`badger/home`](https://github.com/badger/home/blob/99f555209256dfe0e91fd62a699445942cb40838/badge/apps/wifi/icon.png),
copyright Pimoroni & GitHub.

The Samsung TV address/command mapping is cross-checked against the
CC0-licensed Samsung collection in
[`Flipper-IRDB`](https://github.com/Lucaslhm/Flipper-IRDB/tree/main/TVs/Samsung).
