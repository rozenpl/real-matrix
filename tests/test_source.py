"""Offline tests: the Zabbix API is replaced by canned answers."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import zabbix_matrix_rain as zmr  # noqa: E402


class FakeClient:
    version = "7.0.0"

    def __init__(self, now):
        self.now = now
        self.triggers = []
        self.calls = []

    def connect(self):
        return self.version

    def call(self, method, params, auth=True):
        self.calls.append((method, params))
        fresh = str(int(self.now) - 30)
        if method == "host.get":
            return [{"hostid": "1", "name": "web-01"}, {"hostid": "2", "name": "db-01"},
                    {"hostid": "3", "name": "switch-01"}]
        if method == "item.get" and params["search"]["key_"] == "system.cpu.util":
            return [
                {"hostid": "1", "key_": "system.cpu.util", "lastvalue": "40", "lastclock": fresh},
                {"hostid": "1", "key_": "system.cpu.util[,user]", "lastvalue": "99",
                 "lastclock": fresh},
                # SNMP style, two processors: averaged
                {"hostid": "3", "key_": "system.cpu.util[hrProcessorLoad.1]", "lastvalue": "10",
                 "lastclock": fresh},
                {"hostid": "3", "key_": "system.cpu.util[hrProcessorLoad.2]", "lastvalue": "30",
                 "lastclock": fresh},
                # never collected: lastvalue 0 is not a real zero
                {"hostid": "2", "key_": "system.cpu.util", "lastvalue": "0", "lastclock": "0"},
            ]
        if method == "item.get" and params["search"]["key_"] == "net.if.":
            return [
                {"hostid": "1", "key_": 'net.if.in["eth0"]', "lastvalue": "1000",
                 "lastclock": fresh},
                {"hostid": "1", "key_": 'net.if.out["eth0"]', "lastvalue": "500",
                 "lastclock": fresh},
                {"hostid": "1", "key_": 'net.if.in["eth0",errors]', "lastvalue": "7",
                 "lastclock": fresh},
                # stale value is ignored
                {"hostid": "2", "key_": 'net.if.in["eth0"]', "lastvalue": "9",
                 "lastclock": str(int(self.now) - 100000)},
            ]
        if method == "trigger.get":
            return self.triggers
        raise AssertionError("unexpected call %s" % method)


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(time.time())
        self.source = zmr.ZabbixSource(self.client, [])

    def hosts(self, snap):
        return {h.name: h for h in snap.hosts}

    def test_metrics(self):
        hosts = self.hosts(self.source.fetch())
        self.assertEqual(hosts["web-01"].cpu, 40)
        self.assertEqual(hosts["switch-01"].cpu, 20)
        self.assertIsNone(hosts["db-01"].cpu)
        self.assertEqual(hosts["web-01"].net, 1500)
        self.assertIsNone(hosts["db-01"].net)

    def test_new_problems_only_after_first_poll(self):
        self.client.triggers = [{"triggerid": "10", "priority": "3", "description": "Disk full",
                                 "hosts": [{"hostid": "2"}]}]
        snap = self.source.fetch()
        self.assertEqual(snap.new_problems, [])
        db = self.hosts(snap)["db-01"]
        self.assertEqual((db.severity, db.problems), (3, 1))

        self.client.triggers.append({"triggerid": "11", "priority": "4",
                                     "description": "Link down", "hosts": [{"hostid": "1"}]})
        snap = self.source.fetch()
        self.assertEqual([(p.host, p.severity) for p in snap.new_problems], [("web-01", 4)])
        snap = self.source.fetch()
        self.assertEqual(snap.new_problems, [])

    def test_anonymize(self):
        self.client.triggers = [{"triggerid": "10", "priority": "3", "description": "x",
                                 "hosts": [{"hostid": "2"}]}]
        snap = zmr.anonymize(self.source.fetch())
        self.assertEqual([h.name for h in snap.hosts], ["host-01", "host-02", "host-03"])


class HelpersTest(unittest.TestCase):
    def test_literal_prefix(self):
        self.assertEqual(zmr.literal_prefix(zmr.DEFAULT_CPU_KEY), "system.cpu.util")
        self.assertEqual(zmr.literal_prefix(zmr.DEFAULT_NET_KEY), "net.if.")
        self.assertEqual(zmr.literal_prefix(r"^vm\.memory.*"), "vm.memory")
        self.assertEqual(zmr.literal_prefix(r"^cpus?"), "cpu")

    def test_demo_runs(self):
        demo = zmr.DemoSource(5, seed=1)
        for _ in range(50):
            snap = demo.fetch()
        self.assertEqual(len(snap.hosts), 5)
        self.assertTrue(all(0 <= h.cpu <= 100 for h in snap.hosts))

    def test_token_never_on_command_line(self):
        _, args = zmr.parse_args(["--demo"])
        self.assertFalse(hasattr(args, "token"))


if __name__ == "__main__":
    unittest.main()
