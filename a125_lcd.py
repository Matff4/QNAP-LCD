#!/usr/bin/env python3
"""
QNAP A125 front-panel LCD driver (2x16, /dev/ttyS1 @ 1200 baud).

Button frames from panel: 0x53 0x05 0x00 {mask}
  mask 0x01 = UP, 0x02 = DOWN, 0x03 = both, 0x00 = released
"""

import fcntl
import os
import sys
import termios
import threading

if not sys.platform.startswith('linux'):
  raise RuntimeError('a125_lcd.py requires Linux (termios serial)')

_CMD_BACKLIGHT = bytes([0x4D, 0x5E])
_CMD_CLEAR = bytes([0x4D, 0x0D])
_CMD_RESET = bytes([0x4D, 0xFF])
_CMD_WRITE = 0x4D
_CMD_WRITE_SUB = 0x0C

_PREAMBLE = (0x53, 0x83)
_EVT_SWITCH = 0x05

# Total frame length (including preamble + command byte) for each panel->host
# command. Used to consume exactly one frame at a time and stay in sync.
_FRAME_LEN = {
  0x01: 4,   # Report_ID:        preamble cmd hi lo
  0x05: 4,   # Switch_Status:    preamble cmd 0x00 mask
  0x08: 4,   # Protocol_Version: preamble cmd hi lo
  0xAA: 2,   # Reset_OK:         preamble cmd
  0xFA: 2,   # Ack:              preamble cmd
  0xFB: 3,   # Nack:             preamble cmd code
}


def _log(msg):
  print(f'[a125_lcd] {msg}', flush=True)


class A125LCD:
  def __init__(self, port='/dev/ttyS1', speed=1200, handler=None, verbose=False):
    self.port = port
    self.speed = speed
    self.handler = handler
    self.verbose = verbose
    self.columns = 16
    self.lines = 2
    self._fd = None
    self._reader = None
    self._rx_buf = bytearray()

    self._open_serial()

    if handler and self._fd is not None:
      self._reader = threading.Thread(target=self._serial_reader, name='a125-reader', daemon=True)
      self._reader.start()
      if verbose:
        _log(f'reader thread started on {port} @ {speed}')

  def _open_serial(self):
    if not os.path.exists(self.port):
      _log(f'ERROR: serial device missing: {self.port}')
      return

    try:
      fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
      _log(f'ERROR: cannot open {self.port}: {exc}')
      return

    # Refuse to open if another instance already holds the port. Two readers on
    # the same serial line split the byte stream and corrupt every frame.
    try:
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
      _log(f'ERROR: {self.port} is already in use by another process '
           '(refusing to open -- is main.py already running?)')
      os.close(fd)
      return

    try:
      attrs = termios.tcgetattr(fd)
      speed_const = getattr(termios, f'B{self.speed}', None)
      if speed_const is None:
        raise ValueError(f'unsupported baud rate: {self.speed}')

      attrs[0] = 0
      attrs[1] = 0
      attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
      attrs[3] = 0
      attrs[4] = speed_const
      attrs[5] = speed_const
      attrs[6][termios.VMIN] = 1
      attrs[6][termios.VTIME] = 0
      termios.tcsetattr(fd, termios.TCSANOW, attrs)
      termios.tcflush(fd, termios.TCIOFLUSH)

      flags = fcntl.fcntl(fd, fcntl.F_GETFL)
      fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

      self._fd = fd
      if self.verbose:
        _log(f'opened {self.port} @ {self.speed} baud')
    except OSError as exc:
      _log(f'ERROR: termios setup failed on {self.port}: {exc}')
      os.close(fd)

  def _dispatch(self, cmd, payload):
    if not self.handler:
      return

    if cmd == 0x01 and len(payload) == 2:
      self.handler('Report_ID', payload[0] * 256 + payload[1])
    elif cmd == _EVT_SWITCH and len(payload) == 2:
      # QNAP sends 0x00 in high byte, button mask in low byte.
      buttons = payload[1] if payload[0] == 0x00 else (payload[0] * 256 + payload[1])
      if self.verbose:
        _log(f'Switch_Status mask=0x{buttons:02x}')
      self.handler('Switch_Status', buttons)
    elif cmd == 0x08 and len(payload) == 2:
      self.handler('Protocol_Version', payload[0] * 256 + payload[1])
    elif cmd == 0xAA:
      self.handler('Reset_OK', True)
    elif cmd == 0xFA:
      self.handler('Ack', None)
    elif cmd == 0xFB and len(payload) >= 1:
      self.handler('Nack', payload[0])

  def _consume_frames(self):
    """Strict, self-synchronizing parser.

    Aligns to a preamble byte, then consumes exactly one frame whose length is
    determined by the command byte. Unknown commands or stray bytes are dropped
    one at a time until the stream re-aligns -- this prevents the cascading
    desync that a greedy "find a switch anywhere" scan caused.
    """
    buf = self._rx_buf

    while buf:
      # Align: buf[0] must be a preamble byte.
      if buf[0] not in _PREAMBLE:
        if self.verbose:
          _log(f'resync: drop 0x{buf[0]:02x}')
        del buf[0]
        continue

      if len(buf) < 2:
        break  # need the command byte

      cmd = buf[1]
      need = _FRAME_LEN.get(cmd)
      if need is None:
        # Preamble followed by an unknown command -> spurious preamble byte.
        if self.verbose:
          _log(f'resync: drop preamble 0x{buf[0]:02x} (bad cmd 0x{cmd:02x})')
        del buf[0]
        continue

      if len(buf) < need:
        break  # wait for the rest of this frame

      payload = bytes(buf[2:need])
      self._dispatch(cmd, payload)
      del buf[:need]

  def _serial_reader(self):
    while True:
      try:
        chunk = os.read(self._fd, 64)
      except OSError:
        continue
      if not chunk:
        continue
      self._rx_buf.extend(chunk)
      self._consume_frames()

  def _write(self, data):
    if self._fd is None:
      return
    os.write(self._fd, data)

  def drain(self):
    """Block until all queued bytes have actually been transmitted.

    At 1200 baud os.write() only buffers; callers that must guarantee the
    frame reached the panel (e.g. a final message before shutdown) use this.
    """
    if self._fd is None:
      return
    try:
      termios.tcdrain(self._fd)
    except OSError:
      pass

  def backlight(self, on=True):
    self._write(_CMD_BACKLIGHT + (bytes([0x01]) if on else bytes([0x00])))

  def clear(self):
    self._write(_CMD_CLEAR)

  def reset(self):
    self._write(_CMD_RESET)

  def write(self, line, msg):
    if isinstance(msg, list):
      self.write(1, msg[0] if len(msg) >= 1 else '')
      self.write(2, msg[1] if len(msg) >= 2 else '')
      return

    text = str(msg)[: self.columns]
    row = 0x00 if (line % 2) else 0x01
    self._write(bytes([_CMD_WRITE, _CMD_WRITE_SUB, row, len(text)]) + text.encode('utf-8', errors='replace'))

  def close(self):
    if self._fd is not None:
      os.close(self._fd)
      self._fd = None
