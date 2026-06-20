#!/usr/bin/env python3
"""
Raw byte dumper for the A125 panel serial line (/dev/ttyS1 @ 1200 8N1).

Prints incoming bytes in hex exactly as they arrive, with no framing/parsing.
Use it to see the panel's *true* output -- e.g. to confirm whether the stream
is clean `53 05 00 MM` button frames or corrupted (which usually means another
process is reading the same port at the same time).

IMPORTANT: make sure nothing else (main.py / test-buttons.py) is running,
or you'll only see half the bytes.

Run as root:  ./test/serial-dump.py        (Ctrl+C to quit)
"""

import os
import sys
import termios
import time

PORT = '/dev/ttyS1'
SPEED = termios.B1200


def main():
  if os.geteuid() != 0:
    print('ERROR: run as root', file=sys.stderr)
    return 1

  try:
    fd = os.open(PORT, os.O_RDONLY | os.O_NOCTTY)
  except OSError as exc:
    print(f'cannot open {PORT}: {exc}', file=sys.stderr)
    return 1

  attrs = termios.tcgetattr(fd)
  attrs[0] = 0
  attrs[1] = 0
  attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
  attrs[3] = 0
  attrs[4] = SPEED
  attrs[5] = SPEED
  attrs[6][termios.VMIN] = 1
  attrs[6][termios.VTIME] = 0
  termios.tcsetattr(fd, termios.TCSANOW, attrs)
  termios.tcflush(fd, termios.TCIOFLUSH)

  print(f'dumping {PORT} -- press panel buttons. Ctrl+C to quit.\n', flush=True)
  col = 0
  last = time.monotonic()
  try:
    while True:
      data = os.read(fd, 64)
      if not data:
        continue
      now = time.monotonic()
      if now - last > 0.5 and col:  # newline after a quiet gap
        print(flush=True)
        col = 0
      last = now
      for b in data:
        print(f'{b:02x} ', end='', flush=True)
        col += 1
        if col % 16 == 0:
          print(flush=True)
  except KeyboardInterrupt:
    print('\nbye', flush=True)
  finally:
    os.close(fd)
  return 0


if __name__ == '__main__':
  sys.exit(main())
