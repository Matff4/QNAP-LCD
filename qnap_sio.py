#!/usr/bin/env python3
"""
Power-button handling for the QNAP TS-470 Pro on TrueNAS.

The front power button is wired to ACPI and shows up as Linux evdev "Power
Button" node(s) under /dev/input/event*. It is *momentary*: each physical press
emits a single KEY_POWER value=1 then value=0 — the hold duration is never
reported. So we treat every press as a click and let callers build their own
gesture logic (e.g. double-click = shutdown) on top.

The COPY button lives on the Fintek F71869A Super I/O and is handled by
sio_init.py instead.
"""

import glob
import os
import select
import struct
import threading
import time

POLL_INTERVAL = 0.05

EV_KEY = 1
KEY_POWER = 116


def log(msg):
  print(f'[qnap_sio] {msg}', flush=True)


def _input_device_name(event_path):
  base = os.path.basename(event_path)
  sysfs = f'/sys/class/input/{base}/device/name'
  try:
    with open(sysfs, 'r', encoding='utf-8', errors='replace') as handle:
      return handle.read().strip()
  except OSError:
    return 'unknown'


def scan_input_devices(verbose=False):
  devices = []
  for event_path in sorted(glob.glob('/dev/input/event*')):
    name = _input_device_name(event_path)
    devices.append({'path': event_path, 'name': name})
    if verbose:
      log(f'input device: {event_path} name="{name}"')
  return devices


class PowerClickWatcher:
  """Watch ACPI power-button evdev as momentary clicks.

  Each physical press emits a single KEY_POWER value=1 then value=0 (hold
  duration is NOT reported), so we emit one click per press edge and let the
  caller implement double-click logic. Listens on all "Power Button" nodes
  (TS-470 Pro exposes event4 + event5).
  """

  def __init__(self, on_click=None, bounce_seconds=0.08, verbose=False):
    self.on_click = on_click
    self.bounce_seconds = bounce_seconds
    self.verbose = verbose
    self._thread = None
    self._stop = threading.Event()
    self._fds = {}
    self._last_click = 0.0

  def _power_paths(self):
    return [e['path'] for e in scan_input_devices(verbose=False)
            if 'power' in e['name'].lower()]

  def start(self):
    paths = self._power_paths()
    if not paths:
      if self.verbose:
        log('PowerClickWatcher: no power input device found')
      return False
    for path in paths:
      try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self._fds[fd] = path
        if self.verbose:
          log(f'PowerClickWatcher: watching {path}')
      except OSError as exc:
        log(f'PowerClickWatcher: cannot open {path}: {exc}')
    if not self._fds:
      return False
    self._stop.clear()
    self._thread = threading.Thread(target=self._run, name='power-click', daemon=True)
    self._thread.start()
    return True

  def _emit_click(self):
    now = time.monotonic()
    if now - self._last_click < self.bounce_seconds:
      return  # contact bounce
    self._last_click = now
    if self.verbose:
      log('power click')
    if self.on_click:
      self.on_click()

  def _run(self):
    while not self._stop.is_set():
      if not self._fds:
        break
      readable, _, _ = select.select(list(self._fds), [], [], POLL_INTERVAL)
      for fd in readable:
        try:
          data = os.read(fd, 24 * 16)
        except OSError:
          continue
        for off in range(0, len(data) - 24 + 1, 24):
          _sec, _usec, ev_type, code, value = struct.unpack(
            'llHHi', data[off:off + 24])
          if ev_type == EV_KEY and code == KEY_POWER and value == 1:
            self._emit_click()

  def stop(self):
    self._stop.set()
    if self._thread:
      self._thread.join(timeout=2.0)
    for fd in list(self._fds):
      try:
        os.close(fd)
      except OSError:
        pass
    self._fds.clear()


def main():
  watcher = PowerClickWatcher(
    on_click=lambda: log('CLICK'), verbose=True)
  if not watcher.start():
    return 1
  log('watching power button — press it (Ctrl+C to quit)')
  try:
    while True:
      time.sleep(1)
  except KeyboardInterrupt:
    pass
  finally:
    watcher.stop()
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
