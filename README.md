# QNAP TS-470 Pro Front Panel for TrueNAS

Bring the **LCD, navigation buttons, COPY button, and power button** of a QNAP
TS-470 Pro back to life after flashing it with **TrueNAS** — using nothing but
standalone Python.

No QTS firmware. No `hal_daemon`. No vendor chroot. This project talks
**directly to the hardware**: it pokes the Fintek F71869A Super I/O chip over
`/dev/port` to read the COPY button, reads the navigation buttons straight off
the panel's serial bus, and watches the power button via ACPI — then drives the
2×16 character LCD with a small, friendly menu.

---

## Highlights

- **100% standalone.** Reimplements what QNAP's `hal_daemon` did, without
  extracting or chrooting any QTS binaries. Pure Python standard library.
- **Bare-metal Super I/O access.** Reads the COPY button by entering the
  Fintek F71869A config space (ports `0x2E`/`0x2F`) and reading the GPIO
  data-in register — the same thing the kernel driver does, done in userspace.
- **Config-driven menu.** A plain-text `menu.conf` decides which pages show and
  in what order. Comment a line to hide a page. No code changes needed.
- **Clean hardware abstraction.** A small `Qnap` object exposes every control
  with an event API: `qnap.btnPower.onDoubleClick(...)`,
  `qnap.display.print(col, row, text)`, etc.
- **Safe shutdown.** The power button arms a confirmation prompt and only powers
  off on a deliberate double-click — an accidental tap can never halt the NAS.

---

## The front panel

The TS-470 Pro has a 2×16 character LCD and **four** front buttons:

| Button   | What it does in the menu                                   | How it's read |
|----------|------------------------------------------------------------|---------------|
| `ENTER`  | previous page                                              | serial panel bus |
| `SELECT` | next page                                                  | serial panel bus |
| `COPY`   | shows an overlay (USB-backup hook, see roadmap)            | Super I/O GPIO |
| `POWER`  | click → "Shut down?" prompt; double-click → graceful halt  | ACPI evdev |

> `ENTER` + `SELECT` together jumps back to the first page.

---

## How it talks to the hardware

Everything is read from a different bus, and none of it needs QNAP's software:

| Control          | Bus / device              | Mechanism |
|------------------|---------------------------|-----------|
| LCD + ENTER/SELECT | Serial `/dev/ttyS1` @ 1200 8N1 | QNAP A125 protocol — button frames `0x53 0x05 0x00 <mask>` |
| COPY button      | Fintek F71869A Super I/O via `/dev/port` | Config-space GPIO: data-in register `0xE2`, bit 2, active-low |
| POWER button     | ACPI `/dev/input/event*` ("Power Button") | Momentary `KEY_POWER` press events |

### Why the Super I/O dance?

On a stock TS-470 Pro, QNAP's `hal_daemon` initializes the Fintek Super I/O at
boot so the front-panel GPIOs are readable. On TrueNAS that daemon doesn't
exist, so the COPY button reads as "stuck". Instead of dragging the QTS
userland into a chroot, this project does the initialization itself:

1. Enter Super I/O config mode (`0x87 0x87` to port `0x2E`).
2. Activate the Hardware-Monitor logical device (LDN `0x04`) and the GPIO
   logical device (LDN `0x06`).
3. Read the COPY GPIO line from config space on every poll, re-selecting the
   GPIO logical device each time so the kernel's `f71882fg` hwmon driver can't
   leave us pointed at the wrong device.

It's idempotent and safe to run on every boot.

> **Heads up:** the register map here (`0xE2` bit 2 for COPY, base `0x0A00`,
> etc.) is specific to the **TS-470 Pro / F71869A**. Other QNAP models use
> different chips and pins.

---

## Architecture

```
main.py         the menu app: load config, wire buttons, run loop
qnap_panel.py   Qnap / Display / Button — the hardware abstraction + event API
a125_lcd.py     A125 LCD driver + ENTER/SELECT serial reader (/dev/ttyS1)
sio_init.py     Fintek F71869A Super I/O init + COPY button (config-space GPIO)
qnap_sio.py     ACPI power-button watcher (evdev)
pages.py        SysInfo collector + config-driven page providers
menu.conf       which pages to show, and in what order
test/           hardware test + diagnostic scripts
```

---

## Requirements

- A **QNAP TS-470 Pro** running **TrueNAS**.
- **Python 3** (standard library only — no `pip install` needed).
- **root** (direct access to `/dev/port`, `/dev/ttyS1`, and `/dev/input/*`).

---

## Install

```bash
git clone https://github.com/Matff4/QNAP-LCD.git
cd QNAP-LCD

sudo ./main.py
```

Press `ENTER` / `SELECT` to flip through pages. `Ctrl+C` to quit.

---

## Configuration

`menu.conf` is a plain list of page tokens — **order is display order**, and any
line starting with `#` is disabled:

```conf
OS_VERSION
HOSTNAME
UPTIME
LOAD
MEMORY
# CPU_TEMP
# FAN_SPEED
NET
STORAGE_BOOT
STORAGE_ALL
```

### Available tokens

| Token              | Page |
|--------------------|------|
| `OS_VERSION`       | TrueNAS version |
| `HOSTNAME`         | hostname + OS/arch |
| `UPTIME`           | system uptime |
| `LOAD`             | load averages (1/5/15 min) |
| `MEMORY`           | used / total RAM |
| `CPU_TEMP`         | CPU package temperature (lm-sensors / hwmon) |
| `FAN_SPEED`        | chassis fan RPM (Fintek hwmon) |
| `NET`              | one page per active interface |
| `NET_<iface>`      | a specific interface, e.g. `NET_eth0` |
| `STORAGE_BOOT`     | the `boot-pool` |
| `STORAGE_ALL`      | one page per zpool |
| `STORAGE_<pool>`   | a specific pool, e.g. `STORAGE_Tank-24TB` |

Use a different file with `--config /path/to/menu.conf`.

---

## Usage

```bash
sudo ./main.py                 # run the menu (verbose)
sudo ./main.py --quiet         # run quietly (good for services)
sudo ./main.py --config FILE   # use a specific menu config
sudo ./main.py --dry-run       # arm/confirm power flow without actually halting
sudo ./main.py --diag          # Super I/O button diagnostic (init + live watch)
```

---

## Run at boot

Thanks to an exclusive lock on the serial port, a second instance will refuse to
start rather than corrupt the panel stream — so auto-start is safe.

**systemd unit** (`/etc/systemd/system/qnap-lcd.service`):

```ini
[Unit]
Description=QNAP TS-470 Pro LCD menu
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/qnap-lcd/main.py --quiet
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now qnap-lcd.service
```

> On TrueNAS you can alternatively register `main.py --quiet` as a
> **Post-Init** command in the UI. Don't also launch it by hand — one instance
> only.

---

## Developer API

The menu is just one consumer of `qnap_panel.py`. The `Qnap` object makes it
trivial to prototype your own behavior:

```python
from qnap_panel import Qnap

qnap = Qnap(verbose=True)
qnap.start()

# Buttons: onClick / onDoubleClick / onPress / onRelease (chainable)
qnap.btnUp.onClick(lambda: print("ENTER"))
qnap.btnDown.onClick(lambda: print("SELECT"))
qnap.btnCopy.onClick(lambda: print("COPY"))
qnap.btnPower.onDoubleClick(lambda: print("power double-click"))

# Any input wakes the backlight
qnap.display.set_backlight_timeout(120)
qnap.onInput(qnap.display.wake)

# Display: 2x16, addressable by column/row (0-indexed)
qnap.display.show("Hello", "World")
qnap.display.print(5, 1, "here")   # col 5 (6th char), row 1 (2nd line)
```

Single-clicks are only delayed for buttons that *also* have a double-click
handler, so navigation stays instant.

---

## Tests & diagnostics

```bash
sudo ./test/test-buttons.py    # exercise every control via the panel API
sudo ./test/test-power.py      # raw power-button evdev dump
sudo ./test/serial-dump.py     # raw hex dump of the panel serial line
sudo ./main.py --diag          # Super I/O init + live COPY watch
```

A clean panel stream looks like this in `serial-dump.py`:

```
53 05 00 02   <- SELECT pressed
53 05 00 00   <- released
53 05 00 01   <- ENTER pressed
53 05 00 00   <- released
```

---

## Troubleshooting

- **Garbled buttons / `resync: drop` spam / every-other-byte corruption.**
  Two processes are reading `/dev/ttyS1` at once (e.g. a boot service *and* a
  manual run). Stop the duplicate: `sudo lsof /dev/ttyS1`, then
  `sudo pkill -f main.py`. The port lock now prevents this, but a pre-existing
  second reader will still split the stream.
- **COPY button does nothing.** Run `sudo ./main.py --diag` and press COPY — you
  should see the config-space value change. If not, the Super I/O didn't
  initialize (check you're root and `/dev/port` is accessible).
- **`CPU_TEMP` / `FAN_SPEED` show `N/A`.** The sensors are auto-discovered from
  `/sys/class/hwmon`. Make sure the relevant kernel module is loaded
  (`coretemp` for CPU, `f71882fg` for the Fintek fan/temps).

---

## Roadmap

- COPY button → trigger a USB backup (rsync to a plugged-in drive) with progress
  on the LCD, mirroring the original QNAP behavior.
- Per-disk SMART temperatures.
- Pool-health alerts (flash backlight / dedicated page on degraded/resilvering).
- Idle clock / screensaver and optional auto-cycling pages.

---

## Disclaimer

This project accesses hardware registers directly through `/dev/port`. It was
built and tested specifically on a **QNAP TS-470 Pro running TrueNAS**. Running
it on other hardware, or with an incorrect register map, could behave
unexpectedly. Use at your own risk — no warranty.

Not affiliated with or endorsed by QNAP or iXsystems.
