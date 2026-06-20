#!/usr/bin/env python3
"""
QNAP TS-470 Pro LCD menu for TrueNAS.

Architecture:
  main.py         this -- the menu app (config, wiring, run loop)
  qnap_panel.py   Qnap/Display/Button hardware abstraction (event API)
  a125_lcd.py     LCD + ENTER/SELECT serial driver
  sio_init.py     Fintek F71869A Super I/O init + COPY button
  qnap_sio.py     ACPI power button watcher
  pages.py        SysInfo collector + config-driven page providers
  menu.conf       which pages to show and in what order

Front-panel controls (TS-470 Pro has four buttons):
  ENTER (UP)    previous page
  SELECT (DOWN) next page
  ENTER+SELECT  jump to first page
  COPY          overlay message (USB backup hook -- not implemented yet)
  POWER         click = "shut down?" prompt; then a double-click = graceful poweroff

Usage:
  ./main.py                     run the menu (verbose)
  ./main.py --quiet             run quietly
  ./main.py --config FILE       use a specific menu config
  ./main.py --dry-run           print shutdown instead of executing it
  ./main.py --diag              Super I/O button diagnostic (init + watch)
"""

import argparse
import os
import signal
import subprocess
import threading
import time

from qnap_panel import Qnap
import pages
import sio_init

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'menu.conf')

BACKLIGHT_TIMEOUT = 120       # seconds before the LCD backlight turns off
REFRESH_INTERVAL = 10         # seconds between page data refreshes
OVERLAY_SECONDS = 4.0         # how long a transient overlay stays up
POWER_PROMPT_SECONDS = 5.0    # how long the shutdown prompt stays armed
DOUBLE_CLICK_INTERVAL = 0.6   # max gap between the two confirming clicks
SHUTDOWN_GRACE = 0.8          # let the LCD message render before halting


def log(msg, verbose=True):
  if verbose:
    print(f'[main] {msg}', flush=True)


class MenuApp:
  def __init__(self, config_path, dry_run=False, verbose=True):
    self.config_path = config_path
    self.dry_run = dry_run
    self.verbose = verbose

    self.si = pages.SysInfo()
    self.tokens = pages.parse_config(config_path) or pages.DEFAULT_TOKENS
    self.pages = []
    self.index = 0

    self._overlay = False
    self._overlay_timer = None
    self._shutdown_requested = False
    self._lock = threading.Lock()

    # Power button: 1st click arms a prompt; a following double-click confirms.
    self._power_armed = False
    self._power_arm_timer = None
    self._power_last_click = 0.0

    self.qnap = Qnap(verbose=verbose)

  def log(self, msg):
    log(msg, self.verbose)

  # --- rendering ---
  def render(self):
    with self._lock:
      if self._overlay or not self.pages:
        return
      line1, line2 = self.pages[self.index]
      self.qnap.display.show(line1, line2)

  def refresh(self):
    self.pages = pages.build_pages(self.tokens, self.si)
    if self.index >= len(self.pages):
      self.index = max(0, len(self.pages) - 1)
    self.render()

  def show_overlay(self, line1, line2, seconds=OVERLAY_SECONDS):
    with self._lock:
      self._overlay = True
      self.qnap.display.show(line1, line2)
      if self._overlay_timer:
        self._overlay_timer.cancel()
      self._overlay_timer = threading.Timer(seconds, self.clear_overlay)
      self._overlay_timer.daemon = True
      self._overlay_timer.start()

  def clear_overlay(self):
    with self._lock:
      self._overlay = False
      if self._overlay_timer:
        self._overlay_timer.cancel()
        self._overlay_timer = None
    self.render()

  def _cancel_power_arm(self):
    if not self._power_armed:
      return
    self._power_armed = False
    self._power_last_click = 0.0
    if self._power_arm_timer:
      self._power_arm_timer.cancel()
      self._power_arm_timer = None

  # --- handlers ---
  def go_prev(self):
    self._cancel_power_arm()
    if self._overlay:
      self.clear_overlay()
    if self.pages:
      self.index = (self.index - 1) % len(self.pages)
      self.render()

  def go_next(self):
    self._cancel_power_arm()
    if self._overlay:
      self.clear_overlay()
    if self.pages:
      self.index = (self.index + 1) % len(self.pages)
      self.render()

  def go_home(self):
    self._cancel_power_arm()
    if self._overlay:
      self.clear_overlay()
    self.index = 0
    self.render()

  def on_copy(self):
    self._cancel_power_arm()
    self.log('COPY pressed')
    self.show_overlay('Copy Pressed', 'Not impl. yet')

  def on_power_click(self):
    """Momentary power press. 1st click arms a prompt; a *following* double-click
    (two more clicks within DOUBLE_CLICK_INTERVAL) confirms shutdown.

    This deliberately needs 3 presses total (arm + double-click) so a stray
    single or double tap can never power off the NAS by accident.
    """
    if self._shutdown_requested:
      return
    now = time.monotonic()

    if not self._power_armed:
      self._power_armed = True
      self._power_last_click = 0.0  # confirm must be two *subsequent* clicks
      self.show_overlay('Shut down NAS?', '2x power = yes', POWER_PROMPT_SECONDS)
      if self._power_arm_timer:
        self._power_arm_timer.cancel()
      self._power_arm_timer = threading.Timer(POWER_PROMPT_SECONDS, self._disarm_power)
      self._power_arm_timer.daemon = True
      self._power_arm_timer.start()
      self.log('power armed -- double-click power to confirm shutdown')
      return

    if self._power_last_click and (now - self._power_last_click) <= DOUBLE_CLICK_INTERVAL:
      self._do_shutdown()
      return
    self._power_last_click = now

  def _disarm_power(self):
    if self._power_armed and not self._shutdown_requested:
      self._power_armed = False
      self._power_last_click = 0.0
      self.log('power disarmed (timeout)')
      self.clear_overlay()

  def _do_shutdown(self):
    if self._shutdown_requested:
      return
    self._shutdown_requested = True
    if self._power_arm_timer:
      self._power_arm_timer.cancel()
      self._power_arm_timer = None
    self.log('POWER confirmed -> graceful shutdown')
    self.show_overlay('Shutting Down', 'Please wait...', 30.0)
    # The LCD is 1200 baud: make sure the full message is on the wire and
    # readable before the OS starts tearing down, otherwise it gets cut off.
    self.qnap.display.drain()
    time.sleep(SHUTDOWN_GRACE)
    if self.dry_run:
      self.log('[dry-run] would run: shutdown -h now')
      return
    try:
      subprocess.Popen(['shutdown', '-h', 'now'])
    except Exception as exc:  # noqa: BLE001
      self.log(f'shutdown failed: {exc}')

  # --- lifecycle ---
  def wire(self):
    q = self.qnap
    q.display.set_backlight_timeout(BACKLIGHT_TIMEOUT)
    q.onInput(q.display.wake)

    q.btnUp.onClick(self.go_prev)       # ENTER
    q.btnDown.onClick(self.go_next)     # SELECT
    q.btnUpDown.onClick(self.go_home)   # ENTER+SELECT
    q.btnCopy.onClick(self.on_copy)
    q.btnPower.onClick(self.on_power_click)

  def run(self):
    self.log('=== QNAP LCD menu starting ===')
    self.log(f'config={self.config_path} pages={len(self.tokens)} tokens')
    if os.geteuid() != 0:
      self.log('WARNING: not root (need /dev/port and /dev/ttyS1)')

    self.wire()
    status = self.qnap.start()
    self.log(f'hardware: lcd={status["lcd"]} sio={status["sio"]} power={status["power"]}')
    if not status['lcd']:
      self.log('FATAL: LCD serial unavailable')
      return 1

    self.qnap.display.wake()
    self.refresh()
    self.log('ready -- ENTER/SELECT: navigate | COPY: overlay | POWER: arm+2x = shutdown')

    try:
      while True:
        time.sleep(REFRESH_INTERVAL)
        self.refresh()
    except KeyboardInterrupt:
      pass
    finally:
      self.qnap.stop()
    return 0


def main():
  parser = argparse.ArgumentParser(description='QNAP TS-470 Pro LCD menu')
  parser.add_argument('--config', default=DEFAULT_CONFIG,
                      help=f'menu config file (default: {DEFAULT_CONFIG})')
  parser.add_argument('--quiet', action='store_true', help='disable debug logs')
  parser.add_argument('--dry-run', action='store_true',
                      help='print the shutdown command instead of running it')
  parser.add_argument('--diag', action='store_true',
                      help='Super I/O button diagnostic (init + watch)')
  parser.add_argument('--diag-seconds', type=float, default=30.0)
  args = parser.parse_args()

  if args.diag:
    return sio_init.run(diagnostic=True, watch_seconds=args.diag_seconds)

  app = MenuApp(args.config, dry_run=args.dry_run, verbose=not args.quiet)

  def _sigterm(_signum, _frame):
    app.log('SIGTERM -> shutting down menu')
    try:
      app.qnap.display.show('System Going', 'Down...')
    except Exception:  # noqa: BLE001
      pass
    raise KeyboardInterrupt

  signal.signal(signal.SIGTERM, _sigterm)
  return app.run()


if __name__ == '__main__':
  raise SystemExit(main())
