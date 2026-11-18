"""The health bar under the SSH shell: parsing /proc output and showing it."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.monitoring import RemoteMonitor, cpu_percent, parse_cpu_sample, parse_net_sample
from app.ssh_terminal import HealthBar, format_bytes, format_rate, format_uptime

_APP = QApplication.instance() or QApplication([])

# What STATS_COMMAND prints on an ordinary 4-core Ubuntu box.
SAMPLE = """@@cpu
cpu  10000 200 3000 80000 500 0 100 0 0 0
@@mem
MemTotal:        8000000 kB
MemAvailable:    6000000 kB
SwapTotal:       2000000 kB
SwapFree:        1500000 kB
@@uptime
1069210.55 4200000.00
@@load
0.52 0.40 0.35 2/312 48213
@@cores
4
@@disk
/dev/sda1  102400000 61440000 40960000  60% /
@@net
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
  eth0: 1000000 800 0 0 0 0 0 0 500000 600 0 0 0 0 0 0
@@users
2
"""

# Same host, one interval later: 1000 more jiffies, 400 of them busy.
LATER = SAMPLE.replace("cpu  10000 200 3000 80000 500 0 100 0 0 0",
                       "cpu  10300 200 3100 80600 500 0 100 0 0 0")
LATER = LATER.replace("eth0: 1000000 800 0 0 0 0 0 0 500000",
                      "eth0: 1512000 900 0 0 0 0 0 0 756000")


class ParsingTests(unittest.TestCase):
    def test_everything_is_read_from_one_command(self):
        stats = RemoteMonitor.parse_stats(SAMPLE)
        self.assertAlmostEqual(stats["ram"], 25.0)
        self.assertEqual(stats["ram_total"], 8000000 * 1024)
        self.assertEqual(stats["swap_used"], 500000 * 1024)
        self.assertAlmostEqual(stats["swap"], 25.0)
        self.assertAlmostEqual(stats["uptime"], 1069210.55)
        self.assertEqual((stats["load1"], stats["load5"], stats["load15"]), (0.52, 0.40, 0.35))
        self.assertEqual((stats["procs_running"], stats["procs_total"]), (2, 312))
        self.assertEqual(stats["cores"], 4)
        self.assertEqual(stats["disk"], 60.0)
        self.assertEqual(stats["disk_used"], 61440000 * 1024)
        self.assertEqual(stats["users"], 2)

    def test_rates_need_a_previous_sample(self):
        first = RemoteMonitor.parse_stats(SAMPLE, now=100.0)
        self.assertEqual(first["cpu"], 0.0)          # no earlier sample to compare
        self.assertNotIn("net_rx", first)

        second = RemoteMonitor.parse_stats(
            LATER, first["_cpu_sample"], first["_net_sample"], 100.0, now=105.0)
        self.assertAlmostEqual(second["cpu"], 40.0)
        self.assertAlmostEqual(second["net_rx"], 512000 / 5)
        self.assertAlmostEqual(second["net_tx"], 256000 / 5)

    def test_first_poll_uses_the_warm_up_sample(self):
        warm = "cpu  10000 200 3000 80000 500 0 100 0 0 0\n" + LATER
        stats = RemoteMonitor.parse_stats(warm)
        self.assertAlmostEqual(stats["cpu"], 40.0)

    def test_loopback_and_virtual_interfaces_are_not_traffic(self):
        lines = [
            "lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0",
            "docker0: 999 10 0 0 0 0 0 0 999 10 0 0 0 0 0 0",
            "eth0: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0",
            "wlan0: 10 1 0 0 0 0 0 0 20 1 0 0 0 0 0 0",
        ]
        self.assertEqual(parse_net_sample(lines), (110, 220))
        self.assertIsNone(parse_net_sample(["lo: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0"]))

    def test_a_missing_section_only_blanks_itself(self):
        # busybox without who, and no /proc/loadavg for some reason
        text = SAMPLE.replace("0.52 0.40 0.35 2/312 48213\n", "").replace("@@users\n2\n", "@@users\n")
        stats = RemoteMonitor.parse_stats(text)
        self.assertNotIn("load1", stats)
        self.assertNotIn("users", stats)
        self.assertEqual(stats["disk"], 60.0)
        self.assertAlmostEqual(stats["ram"], 25.0)

    def test_garbage_still_yields_the_three_basics(self):
        stats = RemoteMonitor.parse_stats("bash: /proc/stat: No such file\n")
        self.assertEqual(stats["cpu"], 0.0)
        self.assertEqual(stats["ram"], 0.0)
        self.assertEqual(stats["disk"], 0.0)

    def test_cpu_percent_counts_iowait_as_idle(self):
        a = parse_cpu_sample(["cpu 100 0 0 100 100 0 0 0"])
        b = parse_cpu_sample(["cpu 200 0 0 150 150 0 0 0"])
        self.assertAlmostEqual(cpu_percent(a, b), 50.0)
        self.assertIsNone(cpu_percent(a, a))
        self.assertIsNone(cpu_percent(None, b))


class FormattingTests(unittest.TestCase):
    def test_uptime_reads_like_uptime(self):
        self.assertEqual(format_uptime(1069210), "12d 9h")
        self.assertEqual(format_uptime(4 * 3600 + 12 * 60), "4h 12m")
        self.assertEqual(format_uptime(720), "12m")
        self.assertEqual(format_uptime(5), "0m")

    def test_bytes_and_rates(self):
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(8000000 * 1024), "7.6 GB")
        self.assertEqual(format_rate(0), "0B/s")
        self.assertEqual(format_rate(1536), "1.5K/s")
        self.assertEqual(format_rate(120 * 1024 * 1024), "120M/s")


class HealthBarTests(unittest.TestCase):
    def test_new_metrics_show_up_with_details_on_hover(self):
        bar = HealthBar()
        bar.update_stats({
            "cpu": 40.0, "ram": 25.0, "disk": 60.0,
            "ram_used": 2 * 1024 ** 3, "ram_total": 8 * 1024 ** 3,
            "swap": 25.0, "swap_used": 512 * 1024 ** 2, "swap_total": 2 * 1024 ** 3,
            "uptime": 1069210.0, "load1": 0.52, "load5": 0.40, "load15": 0.35,
            "cores": 4, "procs_running": 2, "procs_total": 312,
            "net_rx": 102400.0, "net_tx": 51200.0, "users": 2,
        })
        self.assertEqual(bar.cpu_val.text(), "40.0%")
        self.assertEqual(bar.load_val.text(), "0.52")
        self.assertEqual(bar.uptime_val.text(), "12d 9h")
        self.assertEqual(bar.net_val.text(), "↓100K/s ↑50K/s")
        self.assertEqual(bar.users_val.text(), "2")
        self.assertIn("Swap: 512.0 MB of 2.0 GB", bar.ram_val.toolTip())
        self.assertIn("0.40 (5m)", bar.load_val.toolTip())
        self.assertIn("2 of 312 processes", bar.load_val.toolTip())
        self.assertIn("12 days, 9 hours", bar.uptime_val.toolTip())
        self.assertEqual(bar.status_tag.text(), "OPTIMAL")

    def test_load_is_judged_per_core(self):
        bar = HealthBar()
        bar.update_stats({"cpu": 1.0, "ram": 1.0, "disk": 1.0, "load1": 3.8,
                          "load5": 3.0, "load15": 2.0, "cores": 4})
        self.assertIn("#f87171", bar.load_val.styleSheet())     # 95% of 4 cores: hot
        bar.update_stats({"cpu": 1.0, "ram": 1.0, "disk": 1.0, "load1": 3.8,
                          "load5": 3.0, "load15": 2.0, "cores": 16})
        self.assertIn("#e2e8f0", bar.load_val.styleSheet())     # same load, 16 cores: fine

    def test_old_style_stats_still_work(self):
        bar = HealthBar()
        bar.update_stats({"cpu": 95.0, "ram": 10.0, "disk": 0.0})
        self.assertEqual(bar.cpu_val.text(), "95.0%")
        self.assertEqual(bar.disk_val.text(), "0%")
        self.assertEqual(bar.uptime_val.text(), "—")
        self.assertEqual(bar.status_tag.text(), "HOT")

    def test_narrow_bar_drops_the_least_important_chips_first(self):
        bar = HealthBar()
        bar.update_stats({"cpu": 1.0, "ram": 1.0, "disk": 1.0, "uptime": 60.0,
                          "load1": 0.1, "load5": 0.1, "load15": 0.1,
                          "net_rx": 1.0, "net_tx": 1.0, "users": 1})
        bar.show()      # resize events are only delivered to a shown widget
        bar.resize(1400, 30)
        self.assertTrue(all(not w.isHidden() for ws in bar._chips.values() for w in ws))
        bar.resize(320, 30)
        self.assertTrue(bar.users_val.isHidden())
        self.assertTrue(bar.net_val.isHidden())
        self.assertFalse(bar.cpu_val.isHidden())
        self.assertFalse(bar.disk_val.isHidden())
        bar.resize(1400, 30)
        self.assertFalse(bar.users_val.isHidden())


if __name__ == "__main__":
    unittest.main()
