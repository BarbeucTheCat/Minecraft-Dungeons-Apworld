import importlib.util
import tempfile
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "client" / "crash_report.py"
_spec = importlib.util.spec_from_file_location("crash_report_under_test", _PATH)
crash_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crash_report)

LOG = """
==== CRASH REPORT #1  build=Oct 16 2026 07:24:10  pid=4242  uptime=812s ====
exception 0xC0000005 ACCESS_VIOLATION (first-chance: harmless if the game keeps running)
at: Dungeons.exe+0x1244d10
thread: 1234 (game/hook thread)  ** inside OUR protected game call - caught by our __try, the game continues **
stack (2 frames):
  #00 Dungeons.exe+0x1244d10
  #01 dungeons_bridge.dll+0x2010
breadcrumbs (last 2, oldest first):
  +30ms tid=1234 CALL UnlockMerchantForAllLocalPlayers a=0x1f2 b=0x3a0
  +1ms tid=1234 CALLFAULT UnlockMerchantForAllLocalPlayers a=0xc0000005 b=0x1244d10
==== END ====
"""

MAP = """ Preferred load address is 0000000180000000

  Address         Publics by Value              Rva+Base               Lib:Object

 0001:00002000       ?UnlockThing@Merchants@@YAXXZ 0000000180002000 f   dungeons_bridge.obj
"""


class TestCrashReport(unittest.TestCase):
    def test_own_call_is_named_as_culprit(self):
        (report,) = crash_report.parse_reports(LOG)
        self.assertTrue(report["own_call"])
        text = "\n".join(crash_report.verdict(report))
        self.assertIn("UnlockMerchantForAllLocalPlayers", text)
        self.assertIn("CULPRIT", text)

    def test_map_resolves_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.map"
            path.write_text(MAP)
            self.assertEqual("Merchants::UnlockThing+0x10", crash_report.MapFile(path).resolve(0x2010))

    def test_feature_check(self):
        self.assertTrue(crash_report.check_features(",".join(crash_report.REQUIRED_FEATURES))[0])
        self.assertFalse(crash_report.check_features("merchants")[0])

    def test_build_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            dll = Path(tmp) / "x.dll"
            dll.write_bytes(b"MZ\0\0DBBUILD:Oct 16 2026 07:24:10\0tail")
            self.assertEqual("Oct 16 2026 07:24:10", crash_report.read_dll_build(dll))


HANG = """
==== HANG REPORT sample 1  build=Oct 16 2026 07:24:10  pid=4242  idle=2600ms  foreground=1 ====
game thread 1234 stuck at: ntdll.dll+0x9f1c4
stack (2 frames):
  #00 ntdll.dll+0x9f1c4
  #01 Dungeons.exe+0x1244d10
our state: OUR game call 'UnlockMerchantForAllLocalPlayers' has been running for 2300 ms
threads (top frame of each):
  tid=1234 "GameThread" ntdll.dll+0x9f1c4
breadcrumbs (last 1, oldest first):
  +2300ms tid=1234 CALL UnlockMerchantForAllLocalPlayers a=0x1f2 b=0x3a0
==== END ====
"""


class TestHangReport(unittest.TestCase):
    def test_freeze_inside_our_call_is_the_culprit(self):
        (hang,) = crash_report.parse_hangs(HANG)
        self.assertEqual(2600, hang["idle_ms"])
        self.assertIn("CULPRIT", "\n".join(crash_report.hang_verdict(hang)))

    def test_crash_report_does_not_swallow_the_next_block(self):
        (report,) = crash_report.parse_reports(LOG + HANG)
        self.assertEqual(2, len(report["crumbs"]))
