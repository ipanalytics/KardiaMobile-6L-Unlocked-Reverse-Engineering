#!/usr/bin/env python3
"""Offline decoder for the KardiaMobile 6L ECG stream.

No Bluetooth here: this takes the raw bytes that came off the vendor
characteristic (a .bin dump or hex text) and turns them into channels,
CSV and a stable heart-rate estimate.

The stream is little-endian int16 pairs, ~300 pairs per second, cut into
frames by the MTU. Frame length is therefore not a constant: the decoder
concatenates everything first and only then cuts it into 4-byte pairs.

Usage:
    python3 kardia_decode.py stream.bin                -> stream.csv
    python3 kardia_decode.py stream.bin -o out.csv
    python3 kardia_decode.py --hex "a9f7adf9"          -> prints pairs
    python3 kardia_decode.py --command "Kardia6L ABCD" -> prints the unlock command
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import statistics as st
import sys

SAMPLE_RATE = 300  # Hz per channel, measured from the capture
TOKEN_SALT = "Triangle"


def unlock_token(name: str) -> str:
    """First 8 bytes of sha256("Triangle" + advertised name), as hex."""
    return hashlib.sha256((TOKEN_SALT + name).encode()).hexdigest()[:16]


def build_command(name: str, mode: int = 2) -> bytes:
    """The 20-byte ASCII unlock command written to the control characteristic."""
    return f"M{mode} K{unlock_token(name)}".encode("ascii")


def decode_stream(data: bytes) -> tuple[list[int], list[int]]:
    """Cut a raw byte stream into the two channels.

    Pairs are int16 little-endian, interleaved. A tail shorter than one
    pair is incomplete and is dropped.
    """
    ch1: list[int] = []
    ch2: list[int] = []
    for i in range(0, len(data) - 3, 4):
        ch1.append(int.from_bytes(data[i : i + 2], "little", signed=True))
        ch2.append(int.from_bytes(data[i + 2 : i + 4], "little", signed=True))
    return ch1, ch2


def remove_baseline(values: list[int], window: int = 150) -> list[float]:
    """Subtract a moving median, so the trace sits on zero."""
    if not values:
        return []
    half = max(1, window // 2)
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(values[i] - st.median(values[lo:hi]))
    return out


def heart_rate(values: list[int], sample_rate: int = SAMPLE_RATE) -> float:
    """Band-pass 5-18 Hz, envelope, refractory peak picking, median RR."""
    if len(values) < sample_rate:
        return 0.0
    clean = remove_baseline(values)
    # crude 5-18 Hz band-pass through a moving average difference
    win_lo = max(1, sample_rate // 18)
    win_hi = max(2, sample_rate // 5)
    smooth = _moving_average(clean, win_lo)
    slower = _moving_average(clean, win_hi)
    band = [a - b for a, b in zip(smooth, slower)]
    energy = _moving_average([v * v for v in band], sample_rate // 10) or [0.0]
    threshold = st.mean(energy) + 2.0 * st.pstdev(energy)
    peaks: list[int] = []
    refractory = int(0.25 * sample_rate)
    i = 1
    while i < len(energy) - 1:
        if energy[i] > threshold and energy[i] >= energy[i - 1] and energy[i] > energy[i + 1]:
            if not peaks or i - peaks[-1] > refractory:
                peaks.append(i)
                i += refractory
                continue
        i += 1
    if len(peaks) < 2:
        return 0.0
    rr = [(peaks[i + 1] - peaks[i]) / sample_rate for i in range(len(peaks) - 1)]
    median_rr = st.median(rr)
    return 60.0 / median_rr if median_rr > 0 else 0.0


def _moving_average(values: list[float], window: int) -> list[float]:
    window = max(1, window)
    out: list[float] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= window:
            running -= values[i - window]
        out.append(running / min(i + 1, window))
    return out


def write_csv(path: str, ch1: list[int], ch2: list[int], sample_rate: int = SAMPLE_RATE) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["seconds", "ch1", "ch2"])
        for i, (a, b) in enumerate(zip(ch1, ch2)):
            w.writerow([f"{i / sample_rate:.6f}", a, b])


def main() -> int:
    ap = argparse.ArgumentParser(description="KardiaMobile 6L offline stream decoder")
    ap.add_argument("stream", nargs="?", help="raw .bin dump of the notify characteristic")
    ap.add_argument("-o", "--out", help="CSV output path (default: stream.csv)")
    ap.add_argument("--hex", help="decode a hex string instead of a file")
    ap.add_argument("--command", metavar="NAME", help="print the unlock command for a BLE name")
    args = ap.parse_args()

    if args.command:
        print(build_command(args.command))
        return 0

    if args.hex:
        data = bytes.fromhex(args.hex.replace(" ", ""))
        ch1, ch2 = decode_stream(data)
        print(f"pairs: {len(ch1)}")
        print("ch1:", ch1[:12])
        print("ch2:", ch2[:12])
        return 0

    if not args.stream:
        ap.print_help()
        return 2

    with open(args.stream, "rb") as f:
        data = f.read()
    ch1, ch2 = decode_stream(data)
    out = args.out or "stream.csv"
    write_csv(out, ch1, ch2)
    seconds = len(ch1) / SAMPLE_RATE
    print(f"bytes: {len(data)} | pairs: {len(ch1)} | {seconds:.1f} s @ {SAMPLE_RATE} Hz")
    print(f"ch1 range: {min(ch1)}..{max(ch1)}    ch2 range: {min(ch2)}..{max(ch2)}")
    bpm = heart_rate(ch1)
    print(f"heart rate (ch1): {bpm:.1f} bpm" if bpm else "heart rate (ch1): unusable (saturated or too short)")
    print(f"csv: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
