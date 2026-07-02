#!/usr/bin/env python3
"""
Directly initialize the Fintek F71869A Super I/O on a QNAP TS-470 Pro so the
COPY button reads correctly via config-space GPIO. Replaces the QNAP
hal_daemon chroot dance entirely.

What the Super I/O init needs to do:
  1. Enable the Hardware-Monitor logical device (LDN 0x04, base 0x0A00).
  2. Enable the GPIO logical device (LDN 0x06).
  3. Read the COPY button's GPIO data-in line via config space (0x2E/0x2F).

Register map (Fintek f7188x GPIO banks):
  bank 1x  regbase 0xE0:  dir 0xE0, data_out 0xE1, data_in 0xE2, out_mode 0xE3

  USB_COPY_BUTTON = data_in 0xE2 bit 2   (bank 1x, GPIO12)

The COPY button is active-low: idle pin reads 1, pressed reads 0.

Run as root (needs /dev/port). Safe to run repeatedly.
"""

import argparse
import struct
import sys
import threading
import time

CONFIG_PORTS = (0x2E, 0x4E)
ENTER_KEY = 0x87
EXIT_KEY = 0xAA

REG_LDN = 0x07
REG_CHIPID_HI = 0x20
REG_CHIPID_LO = 0x21
REG_MANID_HI = 0x23
REG_MANID_LO = 0x24
REG_ACTIVATE = 0x30
REG_BASE_HI = 0x60
REG_BASE_LO = 0x61

FINTEK_MANID = 0x1934
CHIP_IDS = {
  0x0814: 'F71869', 0x1007: 'F71869A', 0x0541: 'F71882FG',
  0x0909: 'F71889', 0x1005: 'F71889A',
}

LD_HWM = 0x04
LD_GPIO = 0x06
HWM_BASE = 0x0A00

WIN_INDEX = 0xA05
WIN_DATA = 0xA06

# GPIO bank register bases reachable through the window.
BANK_1X = 0xE0   # dir/out/in/mode = 0xE0/0xE1/0xE2/0xE3
BANK_6X = 0x90   # dir/out/in/mode = 0x90/0x91/0x92/0x93

COPY_REG_IN = 0xE2
COPY_BIT = 1 << 2     # GPIO12


def log(msg):
  print(f'[sio_init] {msg}', flush=True)


class Port:
  def __init__(self):
    self._f = open('/dev/port', 'r+b', buffering=0)

  def outb(self, port, value):
    self._f.seek(port)
    self._f.write(struct.pack('B', value & 0xFF))

  def inb(self, port):
    self._f.seek(port)
    return struct.unpack('B', self._f.read(1))[0]

  def close(self):
    self._f.close()


def cfg_enter(p, cfg):
  p.outb(cfg, ENTER_KEY)
  p.outb(cfg, ENTER_KEY)


def cfg_exit(p, cfg):
  p.outb(cfg, EXIT_KEY)


def cfg_read(p, cfg, reg):
  p.outb(cfg, reg)
  return p.inb(cfg + 1)


def cfg_write(p, cfg, reg, value):
  p.outb(cfg, reg)
  p.outb(cfg + 1, value)


def cfg_select_ld(p, cfg, ldn):
  cfg_write(p, cfg, REG_LDN, ldn)


def win_read(p, reg):
  p.outb(WIN_INDEX, reg)
  return p.inb(WIN_DATA)


def win_write(p, reg, value):
  p.outb(WIN_INDEX, reg)
  p.outb(WIN_DATA, value & 0xFF)


def probe_config_port(p, cfg):
  cfg_enter(p, cfg)
  chip = (cfg_read(p, cfg, REG_CHIPID_HI) << 8) | cfg_read(p, cfg, REG_CHIPID_LO)
  manid = (cfg_read(p, cfg, REG_MANID_HI) << 8) | cfg_read(p, cfg, REG_MANID_LO)
  if chip in (0x0000, 0xFFFF):
    cfg_exit(p, cfg)
    return None
  return chip, manid


def ld_state(p, cfg, ldn):
  cfg_select_ld(p, cfg, ldn)
  act = cfg_read(p, cfg, REG_ACTIVATE)
  base = (cfg_read(p, cfg, REG_BASE_HI) << 8) | cfg_read(p, cfg, REG_BASE_LO)
  return act, base


def ensure_ld_active(p, cfg, ldn, want_base=None, label=''):
  cfg_select_ld(p, cfg, ldn)
  act = cfg_read(p, cfg, REG_ACTIVATE)
  base = (cfg_read(p, cfg, REG_BASE_HI) << 8) | cfg_read(p, cfg, REG_BASE_LO)
  log(f'LDN 0x{ldn:02x} {label} before: activate=0x{act:02x} base=0x{base:04x}')

  if want_base is not None and base != want_base:
    cfg_write(p, cfg, REG_BASE_HI, (want_base >> 8) & 0xFF)
    cfg_write(p, cfg, REG_BASE_LO, want_base & 0xFF)
    log(f'  set base 0x{base:04x} -> 0x{want_base:04x}')

  if not (act & 0x01):
    cfg_write(p, cfg, REG_ACTIVATE, act | 0x01)
    log(f'  activated LDN 0x{ldn:02x} (0x30 |= 1)')


def dump_gpio_banks_window(p):
  for regbase, name in ((BANK_1X, 'bank1x'), (BANK_6X, 'bank6x')):
    d = win_read(p, regbase + 0)
    o = win_read(p, regbase + 1)
    i = win_read(p, regbase + 2)
    m = win_read(p, regbase + 3)
    log(f'[window] {name} regbase 0x{regbase:02x}: dir=0x{d:02x} out=0x{o:02x} '
        f'in=0x{i:02x} mode=0x{m:02x}')


def dump_gpio_banks_cfg(p, cfg):
  """Read GPIO banks via config port (0x2E/0x2F) with GPIO LD selected."""
  cfg_select_ld(p, cfg, LD_GPIO)
  for regbase, name in ((BANK_1X, 'bank1x'), (BANK_6X, 'bank6x')):
    d = cfg_read(p, cfg, regbase + 0)
    o = cfg_read(p, cfg, regbase + 1)
    i = cfg_read(p, cfg, regbase + 2)
    m = cfg_read(p, cfg, regbase + 3)
    log(f'[config] {name} regbase 0x{regbase:02x}: dir=0x{d:02x} out=0x{o:02x} '
        f'in=0x{i:02x} mode=0x{m:02x}')


def set_pin_input(p, regbase, bit, label):
  """Clear the direction bit (f7188x: 0 = input) for one GPIO pin."""
  dir_reg = regbase + 0
  cur = win_read(p, dir_reg)
  new = cur & ~bit
  if new != cur:
    win_write(p, dir_reg, new)
    log(f'{label}: dir 0x{dir_reg:02x} 0x{cur:02x} -> 0x{new:02x} (set input)')
  else:
    log(f'{label}: dir 0x{dir_reg:02x} already 0x{cur:02x} (bit clear = input)')


def read_copy_window(p):
  return win_read(p, COPY_REG_IN)


def read_copy_cfg(p, cfg):
  """Config-space read with GPIO LD assumed selected (f7188x driver method)."""
  return cfg_read(p, cfg, COPY_REG_IN)


def do_init(p, diagnostic=False):
  found_cfg = None
  for cfg in CONFIG_PORTS:
    info = probe_config_port(p, cfg)
    if info is None:
      log(f'config port 0x{cfg:02x}: no Super I/O')
      continue
    chip, manid = info
    name = CHIP_IDS.get(chip, f'unknown(0x{chip:04x})')
    fk = ' [Fintek]' if manid == FINTEK_MANID else ''
    log(f'config port 0x{cfg:02x}: chip=0x{chip:04x} ({name}) '
        f'manid=0x{manid:04x}{fk}')
    found_cfg = cfg

    if diagnostic:
      for ldn in (0x00, 0x04, 0x05, 0x06, 0x07, 0x0A):
        act, base = ld_state(p, cfg, ldn)
        log(f'  LDN 0x{ldn:02x}: activate=0x{act:02x} base=0x{base:04x}')

    ensure_ld_active(p, cfg, LD_HWM, want_base=HWM_BASE, label='(HWM)')
    ensure_ld_active(p, cfg, LD_GPIO, want_base=HWM_BASE, label='(GPIO)')

    # Compare both access methods so we know which one reads the button.
    log('--- GPIO banks via 0xA05/0xA06 window ---')
    cfg_exit(p, cfg)
    dump_gpio_banks_window(p)

    log('--- GPIO banks via config port (0x2E/0x2F, GPIO LD) ---')
    cfg_enter(p, cfg)
    dump_gpio_banks_cfg(p, cfg)
    cfg_exit(p, cfg)

    log('--- fan tachometers (HWM window) ---')
    read_fans(verbose=True)
    break

  if found_cfg is None:
    log('no Fintek Super I/O found at 0x2E/0x4E')
    return None
  return found_cfg


# All F71869A GPIO banks (f7188x): regbase -> data_in is regbase+2.
ALL_BANKS = (
  (0xF0, 'GPIO0x'),
  (0xE0, 'GPIO1x'),
  (0xD0, 'GPIO2x'),
  (0xC0, 'GPIO3x'),
  (0xB0, 'GPIO4x'),
  (0xA0, 'GPIO5x'),
  (0x90, 'GPIO6x'),
)


def scan_all_gpio(p, cfg, seconds):
  """Watch every GPIO bank's data-in for changing bits — find a button's pin."""
  log(f'--- scanning ALL GPIO inputs {seconds}s ---')
  log('Hold/press the POWER button (and others) to see which bit tracks it.')

  def read_all():
    cfg_enter(p, cfg)
    cfg_select_ld(p, cfg, LD_GPIO)
    vals = {}
    for regbase, name in ALL_BANKS:
      vals[regbase] = cfg_read(p, cfg, regbase + 2)
    cfg_exit(p, cfg)
    return vals

  base = read_all()
  for regbase, name in ALL_BANKS:
    log(f'idle {name} in(0x{regbase + 2:02x})=0x{base[regbase]:02x}')

  last = dict(base)
  end = time.monotonic() + seconds
  hits = {}
  while time.monotonic() < end:
    cur = read_all()
    for regbase, name in ALL_BANKS:
      diff = cur[regbase] ^ last[regbase]
      if diff:
        for bit in range(8):
          if diff & (1 << bit):
            state = 'low(pressed)' if not (cur[regbase] & (1 << bit)) else 'high'
            log(f'{name} reg0x{regbase + 2:02x} bit{bit} -> {state} '
                f'(0x{last[regbase]:02x}->0x{cur[regbase]:02x})')
            hits.setdefault((name, regbase + 2, bit), 0)
            hits[(name, regbase + 2, bit)] += 1
        last[regbase] = cur[regbase]
    time.sleep(0.02)

  log('--- scan summary (bits that changed) ---')
  if not hits:
    log('NO GPIO bit changed. The pressed button is NOT on a Super I/O GPIO.')
    log('(Power button is likely ACPI-only / momentary — no level to read.)')
  else:
    for (name, reg, bit), n in sorted(hits.items(), key=lambda x: -x[1]):
      log(f'  {name} reg0x{reg:02x} bit{bit}: {n} transitions')


def watch(p, cfg, seconds):
  log(f'--- watching {seconds}s: press the COPY button ---')
  log('(reading both window and config-space methods)')

  cfg_enter(p, cfg)
  cfg_select_ld(p, cfg, LD_GPIO)

  cw0 = read_copy_window(p)
  cc0 = read_copy_cfg(p, cfg)
  log(f'idle window: COPY=0x{cw0:02x}')
  log(f'idle config: COPY=0x{cc0:02x}')

  last = (cw0, cc0)
  win_changes = 0
  cfg_changes = 0
  end = time.monotonic() + seconds
  while time.monotonic() < end:
    cw = read_copy_window(p)
    cc = read_copy_cfg(p, cfg)
    cur = (cw, cc)
    if cur != last:
      if cw != last[0]:
        win_changes += 1
      if cc != last[1]:
        cfg_changes += 1
      log(f'CHANGE window[COPY=0x{cw:02x}] config[COPY=0x{cc:02x}]')
      last = cur
    time.sleep(0.03)

  cfg_exit(p, cfg)
  log(f'watch done: window changes={win_changes} config changes={cfg_changes}')
  if win_changes == 0 and cfg_changes == 0:
    log('No changes on either method — pin function (mux) likely not GPIO.')
  elif cfg_changes > 0:
    log('CONFIG-SPACE method works! Use 0x2E/0x2F + GPIO LD to read COPY.')
  elif win_changes > 0:
    log('WINDOW method works! Use 0xA05/0xA06 to read COPY.')


def init_superio(verbose=True):
  """Activate the Fintek HWM + GPIO logical devices. Returns config port or None.

  Idempotent and safe to call at every boot. Leaves config mode locked.
  """
  p = Port()
  try:
    for cfg in CONFIG_PORTS:
      info = probe_config_port(p, cfg)
      if info is None:
        continue
      chip, manid = info
      if verbose:
        name = CHIP_IDS.get(chip, f'0x{chip:04x}')
        log(f'F71869A init: chip={name} manid=0x{manid:04x} at port 0x{cfg:02x}')
      ensure_ld_active(p, cfg, LD_HWM, want_base=HWM_BASE,
                       label='(HWM)' if verbose else '')
      ensure_ld_active(p, cfg, LD_GPIO, want_base=HWM_BASE,
                       label='(GPIO)' if verbose else '')
      cfg_exit(p, cfg)
      return cfg
    if verbose:
      log('init_superio: no Fintek Super I/O found at 0x2E/0x4E')
    return None
  finally:
    p.close()


# Fintek HWM fan tachometers (f71882fg layout): fan N value at 0xA0 + 16*N,
# 16-bit big-endian, read via the HWM window. RPM = 1500000 / count.
FAN_REG_BASE = 0xA0
FAN_REG_STRIDE = 0x10
MAX_FANS = 4
FAN_CLOCK = 1500000


def read_fans(verbose=False):
  """Read fan tachometers directly from the Fintek HWM block via /dev/port.

  Returns a list of plausible RPM values. Requires the HWM logical device to be
  active with base 0x0A00 (done by init_superio()/SIOButtonPoller.init()).
  Reads only the runtime HWM window (0xA05/0xA06), never config space, so it
  does not interfere with the COPY-button poller.
  """
  try:
    p = Port()
  except OSError as exc:
    if verbose:
      log(f'read_fans: cannot open /dev/port: {exc}')
    return []
  try:
    rpms = []
    for nr in range(MAX_FANS):
      reg = FAN_REG_BASE + FAN_REG_STRIDE * nr
      count = (win_read(p, reg) << 8) | win_read(p, reg + 1)
      rpm = FAN_CLOCK // count if count else 0
      if verbose:
        log(f'fan{nr}: reg 0x{reg:02x} count={count} -> '
            f'{rpm if count else 0} rpm')
      if count and count != 0xFFFF and 100 <= rpm <= 12000:
        rpms.append(rpm)
    if not rpms:
      return []
    # TS-470 Pro: unused tach channels often read ~366 RPM; the chassis fan is
    # the highest plausible reading (typically fan1 / reg 0xB0).
    plausible = [r for r in rpms if r >= 500]
    return [max(plausible if plausible else rpms)]
  finally:
    p.close()


class SIOButtonPoller:
  """Poll the COPY button via Fintek config-space GPIO reads.

  Each poll re-enters config mode and reselects the GPIO logical device, so a
  concurrent Super I/O user (kernel f71882fg hwmon driver) cannot leave us
  reading the wrong logical device.
  """

  def __init__(self, on_copy=None, interval=0.03, verbose=False):
    self.on_copy = on_copy
    self.interval = interval
    self.verbose = verbose
    self._port = None
    self._cfg = None
    self._thread = None
    self._stop = threading.Event()
    self._copy_pressed = False

  def init(self):
    """Open /dev/port and activate HWM+GPIO LDs. Returns True on success."""
    try:
      self._port = Port()
    except OSError as exc:
      log(f'SIOButtonPoller: cannot open /dev/port: {exc}')
      return False
    for cfg in CONFIG_PORTS:
      info = probe_config_port(self._port, cfg)
      if info is None:
        continue
      chip, manid = info
      if self.verbose:
        log(f'SIO buttons: F71869A chip=0x{chip:04x} at 0x{cfg:02x}')
      ensure_ld_active(self._port, cfg, LD_HWM, want_base=HWM_BASE,
                       label='(HWM)' if self.verbose else '')
      ensure_ld_active(self._port, cfg, LD_GPIO, want_base=HWM_BASE,
                       label='(GPIO)' if self.verbose else '')
      cfg_exit(self._port, cfg)
      self._cfg = cfg
      return True
    log('SIOButtonPoller: no Fintek Super I/O found')
    self._port.close()
    self._port = None
    return False

  def _read_once(self):
    cfg = self._cfg
    cfg_enter(self._port, cfg)
    cfg_select_ld(self._port, cfg, LD_GPIO)
    copy_raw = cfg_read(self._port, cfg, COPY_REG_IN)
    cfg_exit(self._port, cfg)
    return copy_raw

  def _run(self):
    while not self._stop.is_set():
      try:
        copy_raw = self._read_once()
      except OSError as exc:
        if self.verbose:
          log(f'SIO read error: {exc}')
        self._stop.wait(self.interval)
        continue

      copy_now = not (copy_raw & COPY_BIT)
      if copy_now != self._copy_pressed:
        self._copy_pressed = copy_now
        if self.verbose:
          log(f'COPY {"pressed" if copy_now else "released"} (raw=0x{copy_raw:02x})')
        if copy_now and self.on_copy:
          self.on_copy()

      self._stop.wait(self.interval)

  def start(self):
    if self._cfg is None:
      return False
    self._stop.clear()
    self._thread = threading.Thread(target=self._run, name='sio-buttons', daemon=True)
    self._thread.start()
    return True

  def stop(self):
    self._stop.set()
    if self._thread:
      self._thread.join(timeout=2.0)
    if self._port:
      self._port.close()
      self._port = None


def run(diagnostic=False, watch_seconds=0, scan_seconds=0):
  try:
    p = Port()
  except OSError as exc:
    log(f'cannot open /dev/port (run as root?): {exc}')
    return 1
  try:
    cfg = do_init(p, diagnostic=diagnostic)
    if cfg is None:
      return 1
    if scan_seconds > 0:
      scan_all_gpio(p, cfg, scan_seconds)
    if watch_seconds > 0:
      watch(p, cfg, watch_seconds)
    return 0
  finally:
    p.close()


def main():
  ap = argparse.ArgumentParser(description='Fintek F71869A Super I/O init')
  ap.add_argument('--diag', action='store_true',
                  help='dump logical-device + GPIO bank registers')
  ap.add_argument('--watch', type=float, default=0, metavar='SECS',
                  help='after init, poll the COPY button live for SECS seconds')
  ap.add_argument('--scan', type=float, default=0, metavar='SECS',
                  help='scan ALL GPIO banks for changing bits (find power pin)')
  ap.add_argument('--fans', action='store_true',
                  help='read and print fan tachometers, then exit')
  args = ap.parse_args()
  if args.fans:
    init_superio(verbose=False)
    read_fans(verbose=True)
    return 0
  return run(diagnostic=args.diag, watch_seconds=args.watch,
             scan_seconds=args.scan)


if __name__ == '__main__':
  sys.exit(main())
