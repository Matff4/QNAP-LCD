#!/usr/bin/env python3
"""
Raw power-button evdev dumper for the QNAP TS-470 Pro.

Prints every KEY event from all "Power Button" input devices with timing, so we
can see whether the button reports a real hold (value=1 ... <hold> ... value=0)
or just a momentary pulse (value=1 then value=0 immediately).

Test: run it, then (a) quick-tap power, (b) hold power ~3s, (c) tap again.
Compare the gap between press (value=1) and release (value=0).

Run as root:  ./test-power.py
Ctrl+C to quit.
"""

import glob
import os
import select
import struct
import sys
import time

EV_KEY = 1
KEY_POWER = 116
EVENT_SIZE = 24  # struct input_event on 64-bit: 2*long + 2*H + i
EVENT_FMT = 'llHHi'


def device_name(path):
  base = os.path.basename(path)
  try:
    with open(f'/sys/class/input/{base}/device/name') as fh:
      return fh.read().strip()
  except OSError:
    return '?'


def find_power_devices():
  found = []
  for path in sorted(glob.glob('/dev/input/event*')):
    name = device_name(path)
    if 'power' in name.lower():
      found.append((path, name))
  return found


def main():
  if os.geteuid() != 0:
    print('ERROR: run as root', file=sys.stderr)
    return 1

  devices = find_power_devices()
  if not devices:
    print('No "Power Button" evdev devices found.')
    return 1

  fds = {}
  for path, name in devices:
    try:
      fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
      fds[fd] = (path, name)
      print(f'watching {path}  "{name}"', flush=True)
    except OSError as exc:
      print(f'cannot open {path}: {exc}', flush=True)

  if not fds:
    return 1

  print('\nNow: quick-tap power, then hold ~3s, then tap again. Ctrl+C to quit.\n',
        flush=True)

  press_mono = {}
  try:
    while True:
      readable, _, _ = select.select(list(fds), [], [], 1.0)
      for fd in readable:
        path, name = fds[fd]
        try:
          data = os.read(fd, EVENT_SIZE * 16)
        except OSError:
          continue
        for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
          sec, usec, etype, code, value = struct.unpack(
            EVENT_FMT, data[off:off + EVENT_SIZE])
          if etype != EV_KEY or code != KEY_POWER:
            continue
          now = time.monotonic()
          tag = f'{os.path.basename(path)}'
          if value == 1:
            press_mono[fd] = now
            print(f'[{tag}] PRESS   (value=1)', flush=True)
          elif value == 0:
            held = now - press_mono.get(fd, now)
            print(f'[{tag}] RELEASE (value=0) held={held:.2f}s', flush=True)
          elif value == 2:
            held = now - press_mono.get(fd, now)
            print(f'[{tag}] REPEAT  (value=2) held={held:.2f}s', flush=True)
  except KeyboardInterrupt:
    print('\nbye', flush=True)
  finally:
    for fd in fds:
      os.close(fd)
  return 0


if __name__ == '__main__':
  sys.exit(main())
