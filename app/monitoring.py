"""
RPMote Monitoring Engine — Background SSH polling for server health.
"""

import threading
import time
import re
from PyQt6.QtCore import QObject, pyqtSignal


# One round trip per poll.  Every section is fenced by a marker line so a
# missing file or command on an unusual host only blanks its own section
# instead of shifting every line after it.  Everything reads /proc directly:
# it is present on any Linux (including Synology and busybox routers) and
# costs nothing, unlike spawning top for a single number.
STATS_COMMAND = (
    "echo @@cpu; head -1 /proc/stat 2>/dev/null; "
    "echo @@mem; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null; "
    "echo @@uptime; cat /proc/uptime 2>/dev/null; "
    "echo @@load; cat /proc/loadavg 2>/dev/null; "
    "echo @@cores; nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null; "
    "echo @@disk; df -P / 2>/dev/null | tail -1; "
    "echo @@net; cat /proc/net/dev 2>/dev/null; "
    "echo @@users; who 2>/dev/null | wc -l"
)


def parse_sections(text):
    """Split the command output into {marker: [lines]}."""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("@@"):
            current = line[2:]
            sections[current] = []
        elif current is not None and line:
            sections[current].append(line)
    return sections


def parse_cpu_sample(lines):
    """Return (busy_jiffies, total_jiffies) from a /proc/stat 'cpu' line."""
    for line in lines:
        fields = line.split()
        if fields and fields[0] == "cpu" and len(fields) >= 5:
            values = [int(v) for v in fields[1:] if v.isdigit()]
            idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
            return sum(values) - idle, sum(values)
    return None


def cpu_percent(previous, current):
    """Busy percentage between two /proc/stat samples."""
    if not previous or not current:
        return None
    busy = current[0] - previous[0]
    total = current[1] - previous[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * busy / total))


def parse_net_sample(lines):
    """Sum of received and transmitted bytes over every real interface."""
    rx = tx = 0
    found = False
    for line in lines:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
        found = True
    return (rx, tx) if found else None


class RemoteMonitor(QObject):
    """Monitors remote server health via a background SSH channel."""
    # Always present: cpu, ram, disk (percent, 0.0 when unknown).
    # Present when the host reports them: ram_used/ram_total, swap/swap_used/
    # swap_total, disk_used/disk_total (bytes), uptime (seconds), load1/5/15,
    # cores, procs_running/procs_total, net_rx/net_tx (bytes per second), users.
    stats_updated = pyqtSignal(dict)

    def __init__(self, client, interval=5):
        super().__init__()
        self.client = client
        self.interval = interval
        self.running = False
        self._thread = None
        # Event lets the worker wake up immediately when stop() is called
        # rather than sleeping out the full interval on a dying connection.
        self._wake = threading.Event()
        # Rates need two samples: the previous poll's counters and when we
        # took them.
        self._last_cpu = None
        self._last_net = None
        self._last_time = None

    def start(self):
        if self.running: return
        self.running = True
        self._wake.clear()
        self._last_cpu = self._last_net = self._last_time = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the worker and disconnect all signals so a late emit can't
        fire on a C++-deleted receiver (a common idle-crash pattern)."""
        self.running = False
        self._wake.set()
        try:
            self.stats_updated.disconnect()
        except (TypeError, RuntimeError):
            pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def _run(self):
        while self.running:
            transport = None
            try:
                transport = self.client.get_transport() if self.client else None
            except Exception:
                transport = None

            if not transport or not transport.is_active():
                if self._wake.wait(self.interval):
                    break
                continue

            if not self.running:
                break

            try:
                stats = self._get_stats()
                if stats and self.running:
                    self.stats_updated.emit(stats)
            except Exception as e:
                print(f"Monitoring error: {e}")

            if self._wake.wait(self.interval):
                break

    def _get_stats(self):
        """Execute one light command to fetch health data."""
        cmd = STATS_COMMAND
        if self._last_cpu is None:
            # CPU is a rate, so the very first poll takes a second sample
            # half a second later instead of showing a dash until next time.
            cmd = "head -1 /proc/stat 2>/dev/null; sleep 0.5; " + cmd

        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=5)
        text = stdout.read().decode("utf-8", "replace")
        if not text.strip():
            return None

        now = time.monotonic()
        stats = self.parse_stats(text, self._last_cpu, self._last_net, self._last_time, now)
        self._last_cpu = stats.pop("_cpu_sample", None)
        self._last_net = stats.pop("_net_sample", None)
        self._last_time = now
        return stats

    @staticmethod
    def parse_stats(text, last_cpu=None, last_net=None, last_time=None, now=None):
        """Turn the command output into a stats dict.

        Pure so the parsing can be tested without a server.  The raw CPU and
        network counters are returned under private keys for the next call.
        """
        stats = {"cpu": 0.0, "ram": 0.0, "disk": 0.0}
        head, _, body = text.partition("@@cpu")
        sections = parse_sections("@@cpu" + body)

        # CPU: prefer the previous poll's sample (a longer, steadier window);
        # on the first poll use the warm-up sample that preceded the markers.
        current = parse_cpu_sample(sections.get("cpu", []))
        previous = last_cpu or parse_cpu_sample(head.splitlines())
        pct = cpu_percent(previous, current)
        if pct is not None:
            stats["cpu"] = pct
        stats["_cpu_sample"] = current

        # Memory and swap
        mem = {}
        for line in sections.get("mem", []):
            m = re.match(r"(\w+):\s+(\d+)", line)
            if m:
                mem[m.group(1)] = int(m.group(2)) * 1024
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable")
        if total > 0 and avail is not None:
            stats["ram"] = 100.0 * (1.0 - avail / total)
            stats["ram_used"] = total - avail
            stats["ram_total"] = total
        swap_total = mem.get("SwapTotal", 0)
        if "SwapTotal" in mem:
            swap_used = swap_total - mem.get("SwapFree", 0)
            stats["swap_total"] = swap_total
            stats["swap_used"] = swap_used
            stats["swap"] = 100.0 * swap_used / swap_total if swap_total > 0 else 0.0

        # Uptime
        for line in sections.get("uptime", []):
            m = re.match(r"([\d.]+)", line)
            if m:
                stats["uptime"] = float(m.group(1))
                break

        # Load average and process counts
        for line in sections.get("load", []):
            fields = line.split()
            if len(fields) >= 3:
                try:
                    stats["load1"], stats["load5"], stats["load15"] = (
                        float(fields[0]), float(fields[1]), float(fields[2]))
                except ValueError:
                    break
                if len(fields) >= 4 and "/" in fields[3]:
                    running, _, total_procs = fields[3].partition("/")
                    if running.isdigit() and total_procs.isdigit():
                        stats["procs_running"] = int(running)
                        stats["procs_total"] = int(total_procs)
                break

        for line in sections.get("cores", []):
            if line.isdigit() and int(line) > 0:
                stats["cores"] = int(line)
                break

        # Disk: df -P prints 1024-byte blocks: fs total used avail cap% mount
        for line in sections.get("disk", []):
            fields = line.split()
            if len(fields) >= 5 and fields[4].endswith("%"):
                try:
                    stats["disk"] = float(fields[4].rstrip("%"))
                    stats["disk_total"] = int(fields[1]) * 1024
                    stats["disk_used"] = int(fields[2]) * 1024
                except ValueError:
                    pass
                break

        # Network throughput since the previous poll
        net = parse_net_sample(sections.get("net", []))
        stats["_net_sample"] = net
        if net and last_net and last_time is not None and now is not None:
            elapsed = now - last_time
            if elapsed > 0:
                stats["net_rx"] = max(0, net[0] - last_net[0]) / elapsed
                stats["net_tx"] = max(0, net[1] - last_net[1]) / elapsed

        for line in sections.get("users", []):
            if line.isdigit():
                stats["users"] = int(line)
                break

        return stats
