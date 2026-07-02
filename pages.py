#!/usr/bin/env python3
"""
Menu pages for the QNAP LCD: a SysInfo collector plus a registry that turns
config tokens into rendered (line1, line2) pages.

Config file format (see menu.conf): one token per line, order = display order,
'#' (or blank) lines are ignored, so commenting a line disables that page.

Supported tokens:
  OS_VERSION            TrueNAS version
  HOSTNAME              system hostname + machine
  CPU_TEMP              CPU package temperature (lm-sensors / hwmon)
  FAN_SPEED             chassis fan RPM (Fintek hwmon)
  UPTIME                uptime
  LOAD                  load averages
  MEMORY                used / total RAM
  NET                   one page per active non-loopback interface
  NET_<iface>           a specific interface (e.g. NET_eth0)
  STORAGE_BOOT          the boot-pool
  STORAGE_ALL           one page per zpool
  STORAGE_<pool>        a specific pool (e.g. STORAGE_Tank-24TB)
"""

import glob
import json
import os
import platform
import subprocess
import time

import sio_init


def _run(cmd, timeout=5):
  try:
    return subprocess.check_output(
      cmd, shell=True, universal_newlines=True,
      stderr=subprocess.DEVNULL, timeout=timeout).strip()
  except Exception:  # noqa: BLE001
    return ''


class SysInfo:
  """Collects system metrics. Slow sources (zpool/ip) are cached with a TTL."""

  def __init__(self, slow_ttl=15.0):
    self.slow_ttl = slow_ttl
    self._cache = {}
    self._static = {}

  def _cached(self, key, ttl, producer):
    now = time.monotonic()
    hit = self._cache.get(key)
    if hit and (now - hit[0]) < ttl:
      return hit[1]
    value = producer()
    self._cache[key] = (now, value)
    return value

  # --- static-ish ---
  def os_version(self):
    if 'os' not in self._static:
      out = _run("cli -c 'system version'") or _run('cat /etc/version')
      self._static['os'] = out
    return self._static['os']

  def hostname(self):
    return platform.node() or 'truenas'

  def machine(self):
    return f'{platform.system()} {platform.machine()}'

  # --- cheap / live ---
  def uptime_str(self):
    try:
      with open('/proc/uptime', encoding='utf-8') as fh:
        secs = float(fh.readline().split()[0])
      days = int(secs // 86400)
      hours = int((secs % 86400) // 3600)
      mins = int((secs % 3600) // 60)
      return f'{days}d {hours}h {mins}m' if days else f'{hours}h {mins}m'
    except Exception:  # noqa: BLE001
      return 'unknown'

  def loadavg(self):
    try:
      return os.getloadavg()
    except Exception:  # noqa: BLE001
      return (0.0, 0.0, 0.0)

  def memory(self):
    """Return (used_gb, total_gb) or None."""
    try:
      info = {}
      with open('/proc/meminfo', encoding='utf-8') as fh:
        for ln in fh:
          parts = ln.split()
          if len(parts) >= 2:
            info[parts[0].rstrip(':')] = int(parts[1])  # kB
      total = info.get('MemTotal', 0)
      avail = info.get('MemAvailable', info.get('MemFree', 0))
      if not total:
        return None
      used = total - avail
      return (used / 1048576.0, total / 1048576.0)
    except Exception:  # noqa: BLE001
      return None

  def cpu_temp(self):
    """Hottest CPU-core/package temperature in C, or None."""
    best = None
    for hwmon in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
      name = ''
      try:
        with open(os.path.join(hwmon, 'name'), encoding='utf-8') as fh:
          name = fh.read().strip()
      except OSError:
        pass
      if name not in ('coretemp', 'k10temp', 'zenpower'):
        continue
      for inp in sorted(glob.glob(os.path.join(hwmon, 'temp*_input'))):
        try:
          with open(inp, encoding='utf-8') as fh:
            val = int(fh.read().strip()) / 1000.0
          best = val if best is None else max(best, val)
        except (OSError, ValueError):
          pass
    return best

  def fan_rpms(self):
    """List of chassis fan RPMs (>0).

    Tries the standard hwmon sysfs first (works if the f71882fg kernel module
    is loaded), then falls back to reading the Fintek HWM tachometers directly
    over /dev/port -- so fan speed works with no kernel module at all.
    """
    def produce():
      rpms = []
      for hwmon in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        for inp in sorted(glob.glob(os.path.join(hwmon, 'fan*_input'))):
          try:
            with open(inp, encoding='utf-8') as fh:
              val = int(fh.read().strip())
            if val > 0:
              rpms.append(val)
          except (OSError, ValueError):
            pass
      if not rpms:
        try:
          rpms = sio_init.read_fans()
        except Exception:  # noqa: BLE001
          rpms = []
      return rpms
    return self._cached('fans', min(self.slow_ttl, 5.0), produce)

  def interfaces(self):
    """List of (ifname, ipv4) for active non-loopback interfaces."""
    def produce():
      result = []
      out = _run('ip -json address show')
      if not out:
        return result
      try:
        for iface in json.loads(out):
          if iface.get('link_type') == 'loopback':
            continue
          name = iface.get('ifname', 'net')
          ip_val = '-'
          for addr in iface.get('addr_info', []):
            if addr.get('family') == 'inet':
              ip_val = addr.get('local', '-')
              break
          if ip_val == '-' and iface.get('operstate') != 'UP':
            continue
          result.append((name, ip_val))
      except Exception:  # noqa: BLE001
        pass
      return result
    return self._cached('ifaces', self.slow_ttl, produce)

  def pools(self):
    """List of dicts: {name, size, alloc, cap, health}."""
    def produce():
      result = []
      out = _run('zpool list -H -o name,size,alloc,cap,health')
      for ln in out.splitlines():
        f = ln.split('\t') if '\t' in ln else ln.split()
        if len(f) >= 5:
          result.append({'name': f[0], 'size': f[1], 'alloc': f[2],
                         'cap': f[3], 'health': f[4]})
      return result
    return self._cached('pools', self.slow_ttl, produce)

  def pool(self, name):
    for p in self.pools():
      if p['name'] == name:
        return p
    return None


# --- page providers: each returns a list of (line1, line2) tuples ---

def _fmt_pool(p):
  cap = p['cap'].rstrip('%')
  return (p['name'][:16], f"{p['alloc']}/{p['size']} {cap}%"[:16])


def page_os_version(si, _arg):
  v = si.os_version() or 'Unknown'
  if '-' in v:
    head, tail = v.rsplit('-', 1)
    return [(head[:16], tail[:16])]
  return [('TrueNAS', v[:16])]


def page_hostname(si, _arg):
  return [(si.hostname()[:16], si.machine()[:16])]


def page_cpu_temp(si, _arg):
  t = si.cpu_temp()
  return [('CPU Temp', f'{t:.0f} C' if t is not None else 'N/A')]


def page_fan_speed(si, _arg):
  rpms = si.fan_rpms()
  if not rpms:
    return [('Fan Speed', 'N/A')]
  if len(rpms) == 1:
    return [('Fan Speed', f'{rpms[0]} RPM')]
  return [('Fan Speed', ' '.join(str(r) for r in rpms)[:16])]


def page_uptime(si, _arg):
  return [('Uptime', si.uptime_str())]


def page_load(si, _arg):
  l = si.loadavg()
  return [('Load Average', f'{l[0]:.2f} {l[1]:.2f} {l[2]:.2f}'[:16])]


def page_memory(si, _arg):
  m = si.memory()
  if not m:
    return [('Memory', 'N/A')]
  used, total = m
  return [('Memory', f'{used:.1f}/{total:.1f}GB'[:16])]


def page_net(si, arg):
  ifaces = si.interfaces()
  if arg:  # specific interface
    ifaces = [(n, ip) for (n, ip) in ifaces if n == arg]
    if not ifaces:
      return [(arg[:16], 'down')]
  if not ifaces:
    return [('Network', 'no link')]
  return [(n[:16], ip[:16]) for (n, ip) in ifaces]


def page_storage(si, arg):
  if arg in (None, '', 'ALL'):
    pools = si.pools()
    return [_fmt_pool(p) for p in pools] or [('Storage', 'no pools')]
  name = 'boot-pool' if arg == 'BOOT' else arg
  p = si.pool(name)
  if not p:
    return [(name[:16], 'not found')]
  return [_fmt_pool(p)]


_REGISTRY = {
  'OS_VERSION': page_os_version,
  'HOSTNAME': page_hostname,
  'CPU_TEMP': page_cpu_temp,
  'FAN_SPEED': page_fan_speed,
  'UPTIME': page_uptime,
  'LOAD': page_load,
  'MEMORY': page_memory,
  'NET': page_net,
}


def _resolve(token):
  """Return (provider, arg) for a config token, or (None, None)."""
  if token in _REGISTRY:
    return _REGISTRY[token], None
  if token == 'STORAGE':
    return page_storage, 'ALL'
  for prefix, fn in (('NET_', page_net), ('STORAGE_', page_storage)):
    if token.startswith(prefix):
      return fn, token[len(prefix):]
  return None, None


def load_config(path):
  """Parse a menu config file into (settings, tokens).

  - Lines with '=' are settings:  KEY = VALUE   (KEY upper-cased)
  - Other non-blank, non-'#' lines are page tokens, in display order.
  Returns (None, None) if the file cannot be read.
  """
  settings = {}
  tokens = []
  try:
    with open(path, encoding='utf-8') as fh:
      for raw in fh:
        line = raw.strip()
        if not line or line.startswith('#'):
          continue
        if '=' in line:
          key, value = line.split('=', 1)
          settings[key.strip().upper()] = value.strip()
        else:
          tokens.append(line.split()[0])
  except OSError:
    return None, None
  return settings, tokens


DEFAULT_TOKENS = [
  'OS_VERSION', 'HOSTNAME', 'UPTIME', 'LOAD', 'MEMORY',
  'CPU_TEMP', 'FAN_SPEED', 'NET', 'STORAGE_BOOT', 'STORAGE_ALL',
]


def build_pages(tokens, si):
  """Resolve config tokens into a flat list of (line1, line2) pages."""
  pages = []
  for token in tokens:
    provider, arg = _resolve(token)
    if provider is None:
      pages.append(('Bad cfg token', token[:16]))
      continue
    try:
      pages.extend(provider(si, arg))
    except Exception as exc:  # noqa: BLE001
      pages.append((token[:16], f'err: {exc}'[:16]))
  return pages or [('No pages', 'check menu.conf')]
