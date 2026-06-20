#!/usr/bin/env python3
"""
High-level hardware abstraction for the QNAP TS-470 Pro front panel on TrueNAS.

Wraps the low-level drivers (a125_lcd, sio_init, qnap_sio) behind a single
object with a small, prototyping-friendly event API:

    qnap = Qnap(verbose=True)
    qnap.start()

    qnap.btnUp.onClick(lambda: print('up'))
    qnap.btnPower.onDoubleClick(lambda: shutdown())
    qnap.display.print(5, 1, 'hello')   # col 5 (6th char), row 1 (2nd line)

Buttons (TS-470 Pro front panel: POWER, COPY, ENTER, SELECT)
  btnUp, btnDown, btnUpDown   serial panel (/dev/ttyS1); ENTER=UP, SELECT=DOWN
  btnCopy                     Fintek F71869A Super I/O GPIO
  btnPower                    ACPI evdev (momentary; use onDoubleClick)

Each button exposes (chainable, accept a zero-arg callable):
  onClick, onDoubleClick, onPress, onRelease     (+ snake_case aliases)

Display (2x16):
  print(col, row, text)   place text at column/row (0-indexed), keep rest of row
  line(row, text)         overwrite a whole row
  show(line1, line2)      overwrite both rows
  clear()                 blank the screen
  backlight(on)           force backlight
  wake()                  backlight on + restart auto-off timer
  set_backlight_timeout(seconds)
"""

import threading
import time

from a125_lcd import A125LCD
from sio_init import SIOButtonPoller
from qnap_sio import PowerClickWatcher


def _log(msg):
  print(f'[qnap_panel] {msg}', flush=True)


class Button:
  """A single logical button with click / double-click / press / release events.

  Inputs arrive as momentary clicks via _click() (a press immediately followed
  by a release). If any double-click handler is registered, single clicks are
  delayed by `double_interval` to disambiguate; otherwise they fire instantly.
  """

  def __init__(self, name, double_interval=0.45):
    self.name = name
    self.double_interval = double_interval
    self._press_cbs = []
    self._release_cbs = []
    self._click_cbs = []
    self._double_cbs = []
    self._pending = None
    self._last_click = 0.0
    self._lock = threading.Lock()

  def onClick(self, cb):
    self._click_cbs.append(cb)
    return self

  def onDoubleClick(self, cb):
    self._double_cbs.append(cb)
    return self

  def onPress(self, cb):
    self._press_cbs.append(cb)
    return self

  def onRelease(self, cb):
    self._release_cbs.append(cb)
    return self

  # snake_case aliases
  on_click = onClick
  on_double_click = onDoubleClick
  on_press = onPress
  on_release = onRelease

  def _fire(self, cbs):
    for cb in list(cbs):
      try:
        cb()
      except Exception as exc:  # noqa: BLE001
        _log(f'{self.name} handler error: {exc}')

  def _click(self):
    """A complete momentary press+release (called by input sources)."""
    self._press()
    self._release()

  def _press(self):
    self._fire(self._press_cbs)

  def _release(self):
    self._fire(self._release_cbs)
    self._resolve_click()

  def _resolve_click(self):
    if not self._double_cbs:
      self._fire(self._click_cbs)
      return
    now = time.monotonic()
    with self._lock:
      if self._pending is not None and (now - self._last_click) <= self.double_interval:
        self._pending.cancel()
        self._pending = None
        self._last_click = 0.0
        self._fire(self._double_cbs)
      else:
        self._last_click = now
        self._pending = threading.Timer(self.double_interval, self._flush_single)
        self._pending.daemon = True
        self._pending.start()

  def _flush_single(self):
    with self._lock:
      self._pending = None
    self._fire(self._click_cbs)


class Display:
  """2x16 character LCD with a software framebuffer for column-addressed text."""

  def __init__(self, lcd, cols=16, rows=2):
    self._lcd = lcd
    self.cols = cols
    self.rows = rows
    self._buf = [[' '] * cols for _ in range(rows)]
    self._bl_timeout = None
    self._bl_timer = None
    self._bl_on = False
    self._lock = threading.Lock()

  @property
  def ok(self):
    return getattr(self._lcd, '_fd', None) is not None

  def print(self, col, row, text):
    """Write `text` starting at (col, row); leave the rest of the row intact."""
    if not (0 <= row < self.rows):
      return
    text = str(text)
    with self._lock:
      line = self._buf[row]
      for i, ch in enumerate(text):
        c = col + i
        if 0 <= c < self.cols:
          line[c] = ch
      self._flush_row(row)

  def line(self, row, text):
    """Overwrite an entire row (blank-padded)."""
    if not (0 <= row < self.rows):
      return
    text = str(text)[: self.cols]
    with self._lock:
      self._buf[row] = list(text.ljust(self.cols))
      self._flush_row(row)

  def show(self, line1='', line2=''):
    self.line(0, line1)
    self.line(1, line2)

  def clear(self):
    with self._lock:
      self._buf = [[' '] * self.cols for _ in range(self.rows)]
      try:
        self._lcd.clear()
      except Exception:  # noqa: BLE001
        pass

  def _flush_row(self, row):
    text = ''.join(self._buf[row])
    try:
      # A125 write(): line 1 = top row, line 2 = bottom row.
      self._lcd.write(row + 1, text)
    except Exception as exc:  # noqa: BLE001
      _log(f'flush row {row} failed: {exc}')

  def drain(self):
    """Block until queued bytes have been transmitted (slow 1200-baud line)."""
    with self._lock:
      fn = getattr(self._lcd, 'drain', None)
      if callable(fn):
        try:
          fn()
        except Exception:  # noqa: BLE001
          pass

  def backlight(self, on=True):
    self._bl_on = on
    with self._lock:
      try:
        self._lcd.backlight(on)
      except Exception:  # noqa: BLE001
        pass

  def set_backlight_timeout(self, seconds):
    self._bl_timeout = seconds if seconds and seconds > 0 else None

  def wake(self):
    """Turn the backlight on and (re)start the auto-off timer if configured."""
    if not self._bl_on:
      self.backlight(True)
    if self._bl_timer:
      self._bl_timer.cancel()
      self._bl_timer = None
    if self._bl_timeout:
      self._bl_timer = threading.Timer(self._bl_timeout, lambda: self.backlight(False))
      self._bl_timer.daemon = True
      self._bl_timer.start()


class Qnap:
  """The whole front panel: display + all buttons, wired to the HW drivers."""

  def __init__(self, lcd_port='/dev/ttyS1', lcd_speed=1200,
               power_double_interval=0.45, verbose=False):
    self.verbose = verbose
    self._input_hooks = []
    self._serial_mask = 0

    self.btnUp = Button('UP')
    self.btnDown = Button('DOWN')
    self.btnUpDown = Button('UP+DOWN')
    self.btnCopy = Button('COPY')
    self.btnPower = Button('POWER', double_interval=power_double_interval)

    self._lcd = A125LCD(lcd_port, lcd_speed, handler=self._on_serial, verbose=verbose)
    self.display = Display(self._lcd)

    self._sio = SIOButtonPoller(
      on_copy=lambda: self._dispatch(self.btnCopy),
      verbose=verbose,
    )
    self._power = PowerClickWatcher(
      on_click=lambda: self._dispatch(self.btnPower), verbose=verbose)

  @property
  def lcd_ok(self):
    return self.display.ok

  def onInput(self, cb):
    """Register a callback fired on ANY button activity (e.g. backlight wake)."""
    self._input_hooks.append(cb)
    return self

  on_input = onInput

  def _fire_input(self):
    for cb in list(self._input_hooks):
      try:
        cb()
      except Exception as exc:  # noqa: BLE001
        _log(f'input hook error: {exc}')

  def _dispatch(self, button):
    self._fire_input()
    button._click()

  def _on_serial(self, command, data):
    if command != 'Switch_Status':
      return
    mask = data & 0x03
    prev = self._serial_mask
    self._serial_mask = mask
    if mask == prev:
      return
    if mask == 0x00:
      return  # release edge; clicks fire on the press edge below
    self._fire_input()
    if mask == 0x03 and prev != 0x03:
      self.btnUpDown._click()
    elif mask == 0x01 and prev == 0x00:
      self.btnUp._click()
    elif mask == 0x02 and prev == 0x00:
      self.btnDown._click()

  def start(self):
    """Start input watchers and reset the LCD. Returns a status dict."""
    sio_ok = False
    try:
      sio_ok = self._sio.init() and self._sio.start()
    except Exception as exc:  # noqa: BLE001
      _log(f'SIO start failed: {exc}')
    if not sio_ok:
      _log('WARNING: COPY button (Super I/O) unavailable')

    power_ok = False
    try:
      power_ok = self._power.start()
    except Exception as exc:  # noqa: BLE001
      _log(f'power start failed: {exc}')
    if not power_ok:
      _log('WARNING: power button (evdev) unavailable')

    if self.lcd_ok:
      try:
        self._lcd.reset()
      except Exception:  # noqa: BLE001
        pass

    return {'lcd': self.lcd_ok, 'sio': sio_ok, 'power': power_ok}

  def stop(self):
    for closer in (self._power.stop, self._sio.stop):
      try:
        closer()
      except Exception:  # noqa: BLE001
        pass
    try:
      self.display.clear()
      self._lcd.close()
    except Exception:  # noqa: BLE001
      pass
