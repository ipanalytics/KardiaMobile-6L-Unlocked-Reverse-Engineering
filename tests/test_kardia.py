#!/usr/bin/env python3
"""Offline tests for the KardiaMobile 6L decoder. No Bluetooth involved."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import kardia_decode as k  # noqa: E402


class TestUnlockToken(unittest.TestCase):
    def test_token_is_derived_from_the_advertised_name(self):
        # public example name, so no real device identifiers live in the repo
        self.assertEqual(k.unlock_token("Kardia6L ABCD"), "feaa7255122d948f")

    def test_token_is_eight_bytes_of_hex(self):
        token = k.unlock_token("Kardia6L ABCD")
        self.assertEqual(len(token), 16)
        int(token, 16)  # must parse as hex

    def test_command_is_twenty_ascii_bytes(self):
        cmd = k.build_command("Kardia6L ABCD")
        self.assertEqual(len(cmd), 20)
        self.assertTrue(cmd.startswith(b"M2 K"))
        cmd.decode("ascii")  # must be plain ASCII

    def test_token_changes_with_the_name(self):
        self.assertNotEqual(k.unlock_token("Kardia6L ABCD"), k.unlock_token("Kardia6L ABCE"))


class TestStreamDecoding(unittest.TestCase):
    def test_full_frame_is_nine_pairs(self):
        data = bytes.fromhex("a9f7" * 9 + "adf9" * 9)
        ch1, ch2 = k.decode_stream(data)
        self.assertEqual(len(ch1), 9)
        self.assertEqual(len(ch2), 9)

    def test_short_frame_is_five_pairs(self):
        data = bytes.fromhex("a9f7adf9b5f98df7e5f999f7b9f99df7c5f9a1f7")
        ch1, ch2 = k.decode_stream(data)
        self.assertEqual((len(ch1), len(ch2)), (5, 5))

    def test_frames_are_concatenated_not_assumed(self):
        # two 20-byte frames must decode as ten pairs, not as one 5-pair frame
        data = bytes.fromhex("a9f7adf9b5f98df7e5f999f7b9f99df7c5f9a1f7" * 2)
        ch1, _ = k.decode_stream(data)
        self.assertEqual(len(ch1), 10)

    def test_incomplete_tail_is_dropped(self):
        data = bytes.fromhex("a9f7adf9" + "aa")
        ch1, _ = k.decode_stream(data)
        self.assertEqual(len(ch1), 1)

    def test_values_are_signed_little_endian(self):
        ch1, ch2 = k.decode_stream(bytes.fromhex("99659965"))
        self.assertEqual(ch1, [26009])  # 0x6599 = ADC saturation
        self.assertEqual(ch2, [26009])

    def test_saturation_is_visible_in_the_range(self):
        ch1, _ = k.decode_stream(bytes.fromhex("9965" * 4))
        self.assertEqual(max(ch1), 26009)


class TestBaselineAndRate(unittest.TestCase):
    def test_constant_signal_has_no_rate(self):
        self.assertEqual(k.heart_rate([1000] * 600, sample_rate=300), 0.0)

    def test_synthetic_rhythm_gives_a_rate(self):
        # 300 Hz, 60 bpm: one impulse every 300 samples
        samples = []
        for i in range(3000):
            samples.append(2000 if i % 300 == 0 else 0)
        bpm = k.heart_rate(samples, sample_rate=300)
        self.assertGreater(bpm, 40)
        self.assertLess(bpm, 90)

    def test_baseline_removal_centres_a_ramp(self):
        ramp = [i for i in range(400)]
        centred = k.remove_baseline(ramp, window=40)
        self.assertLess(abs(sum(centred) / len(centred)), 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
