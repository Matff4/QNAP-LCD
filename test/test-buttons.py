#!/usr/bin/env python3
"""
Interactive test for every QNAP TS-470 Pro front-panel control, exercised
through the high-level Qnap panel API (qnap_panel.py).

  ENTER / SELECT / both -> serial buttons (/dev/ttyS1); ENTER=UP, SELECT=DOWN
  COPY                  -> Fintek F71869A Super I/O GPIO
  POWER                 -> ACPI evdev (single click + double-click)

Run as root from anywhere:  ./test/test-buttons.py   (Ctrl+C to quit)
"""

import os
import sys
import time

# Allow running from ./test/ by importing the project modules one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qnap_panel import Qnap  # noqa: E402


def stamp():
  return time.strftime('%H:%M:%S')


class ButtonTester:
  def __init__(self):
    self.qnap = Qnap(verbose=True)
    self.seen = {k: False for k in
                 ('UP', 'DOWN', 'UP+DOWN', 'COPY', 'POWER')}

  def event(self, name, detail=''):
    print(f'[{stamp()}] {name}{("  " + detail) if detail else ""}', flush=True)
    if name in self.seen and not self.seen[name]:
      self.seen[name] = True
      if all(self.seen.values()):
        print(f'[{stamp()}] ALL BUTTONS OK -- every control responded', flush=True)

  def show(self, text):
    try:
      self.qnap.display.line(1, text)
    except Exception:  # noqa: BLE001
      pass

  def wire(self):
    q = self.qnap
    q.display.set_backlight_timeout(0)  # keep backlight on during the test
    q.onInput(q.display.wake)

    q.btnUp.onClick(lambda: (self.event('UP', 'click'), self.show('UP')))
    q.btnDown.onClick(lambda: (self.event('DOWN', 'click'), self.show('DOWN')))
    q.btnUpDown.onClick(lambda: (self.event('UP+DOWN', 'combo'), self.show('UP+DOWN')))
    q.btnCopy.onClick(lambda: (self.event('COPY', 'click'), self.show('COPY')))
    q.btnPower.onClick(lambda: (self.event('POWER', 'single click'), self.show('POWER 1x')))
    q.btnPower.onDoubleClick(
      lambda: (self.event('POWER', 'DOUBLE-CLICK -> would shut down'),
               self.show('POWER 2x->off')))

  def run(self):
    print('=== QNAP TS-470 Pro button test (panel API) ===', flush=True)
    print('Press ENTER(UP), SELECT(DOWN), both, COPY, POWER (single + double-click).',
          flush=True)
    print('Ctrl+C to quit.\n', flush=True)

    self.wire()
    status = self.qnap.start()
    print(f'hardware: lcd={status["lcd"]} sio={status["sio"]} '
          f'power={status["power"]}\n', flush=True)
    if status['lcd']:
      self.qnap.display.show('Button test:', 'Press a button')

    try:
      while True:
        time.sleep(1)
    except KeyboardInterrupt:
      print('\nstopping...', flush=True)
    finally:
      self.qnap.stop()


def main():
  if os.geteuid() != 0:
    print('ERROR: run as root (needs /dev/port and /dev/ttyS1).', file=sys.stderr)
    return 1
  ButtonTester().run()
  return 0


if __name__ == '__main__':
  sys.exit(main())
