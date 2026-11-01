import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from hms.host import (HostPlatform, SimulatedHost, SpeakerUnavailable,
                      DiskError, FileMissing)


class SimulatedHostTests(unittest.TestCase):
    def test_time_moves_only_when_told(self):
        h = SimulatedHost(start=100.0)
        self.assertEqual(h.now(), 100.0)
        h.advance(1.5)
        self.assertEqual(h.now(), 101.5)
        self.assertEqual(h.monotonic(), 1.5)

    def test_tone_is_recorded_and_repeat_is_harmless(self):
        h = SimulatedHost(start=0.0)
        h.set_tone(True)
        h.set_tone(True)
        h.advance(2)
        h.set_tone(False)
        self.assertEqual(h.tone_log, [(0.0, True), (2.0, False)])
        self.assertFalse(h.tone_is_on())

    def test_broken_speaker_raises(self):
        h = SimulatedHost()
        h.speaker_broken = True
        with self.assertRaises(SpeakerUnavailable):
            h.set_tone(True)
        self.assertFalse(h.tone_is_on())

    def test_files(self):
        h = SimulatedHost()
        h.write_file("a", b"x")
        self.assertTrue(h.exists("a"))
        self.assertEqual(h.read_file("a"), b"x")
        h.copy_file("a", "b")
        self.assertEqual(h.read_file("b"), b"x")
        with self.assertRaises(FileMissing):
            h.read_file("nope")
        with self.assertRaises(FileMissing):
            h.copy_file("nope", "c")

    def test_disk_full_changes_nothing(self):
        h = SimulatedHost()
        h.write_file("a", b"old")
        h.disk_full = True
        with self.assertRaises(DiskError):
            h.write_file("a", b"new")
        with self.assertRaises(DiskError):
            h.copy_file("a", "b")
        self.assertEqual(h.read_file("a"), b"old")
        self.assertFalse(h.exists("b"))


class RealHostTests(unittest.TestCase):
    def test_clock(self):
        h = HostPlatform()
        a = h.monotonic()
        b = h.monotonic()
        self.assertGreaterEqual(b, a)
        self.assertLess(abs(h.now() - __import__("time").time()), 1.0)

    def test_files_round_trip_and_no_temp_left(self):
        h = HostPlatform()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            h.write_file(p, b"one")
            h.write_file(p, b"two")
            self.assertEqual(h.read_file(p), b"two")
            self.assertFalse(os.path.exists(p + ".tmp"))
            q = os.path.join(d, "backup.json")
            h.copy_file(p, q)
            self.assertEqual(h.read_file(q), b"two")
            with self.assertRaises(FileMissing):
                h.read_file(os.path.join(d, "missing"))
            with self.assertRaises(FileMissing):
                h.copy_file(os.path.join(d, "missing"), q)

    def test_unwritable_path_raises_disk_error(self):
        h = HostPlatform()
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "a_file")
            with open(blocker, "wb") as f:
                f.write(b"x")
            with self.assertRaises(DiskError):      # a folder cannot be made inside a file
                h.write_file(os.path.join(blocker, "sub", "f"), b"x")

    def test_tone_state(self):
        h = HostPlatform()
        h._beeper = staticmethod(lambda: (lambda: None))   # silent for the test
        h.set_tone(True)
        self.assertTrue(h.tone_is_on())
        h.set_tone(False)
        self.assertFalse(h.tone_is_on())


if __name__ == "__main__":
    unittest.main()
