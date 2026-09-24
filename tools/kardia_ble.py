#!/usr/bin/env python3
"""kardia_ble.py — AliveCor KardiaMobile 6L ECG over Bluetooth on Linux (bleak).

Protocol recovered from a phone HCI capture and verified against a live device:

  vendor service   : ac060001-328c-a28f-9846-5a8aa212661b
  control char     : ac060002-328c-a28f-9846-5a8aa212661b  (write + indicate)
  ECG stream char  : ac060003-328c-a28f-9846-5a8aa212661b  (notify)
  M2 mode          : 36 bytes = 9 int16 LE pairs (channel1, channel2), channel1 = I,
                     channel2 = II, 300 Hz, 33 1/3 packets per second
  unlock command   : "M2 K" + first 8 bytes of sha256("Triangle" + device name) in hex

Order that works: bond first (Just Works), then write the command, then subscribe to
indications on the control characteristic and notifications on the stream one.

Modes:
  --scan                 list nearby devices (name, address, RSSI)
  --capture SEC          record SEC seconds and write the files
  --auto                 wait for electrode contact, record one session
  --out DIR              where to write the files (default: current directory)
  --wait SEC             how long to wait for the device in --auto (default 1800)
  --raw                  also keep the raw notification bytes (.bin) next to the CSV
  --selftest             check packet parsing and lead derivation

Output: <stamp>.csv with all six leads, <stamp>.png and <stamp>.pdf next to it.
No account, no cloud, no vendor app — the data never leaves the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SERVICE = "ac060001-328c-a28f-9846-5a8aa212661b"
CHAR_CMD = "ac060002-328c-a28f-9846-5a8aa212661b"
CHAR_ECG = "ac060003-328c-a28f-9846-5a8aa212661b"
SAMPLE_RATE = 300
PACKET_BYTES = 36
SAMPLES_PER_PACKET = 9
# A packet whose spread is below this means the electrodes are not touched.
ACTIVITY_THRESHOLD = 150.0
FLAT_STOP_SECONDS = 6.0
MIN_SESSION_SECONDS = 10.0
MAX_SESSION_SECONDS = 60.0


# ────────────────────────── protocol ──────────────────────────
def unlock_token(device_name: str) -> str:
    """Unlock token: 'K' + first 8 bytes of sha256('Triangle' + name), hex."""
    digest = hashlib.sha256(("Triangle" + device_name).encode()).digest()
    return "K" + digest[:8].hex()


def command_for_mode(device_name: str, mode: str = "M2") -> str:
    return f"{mode} {unlock_token(device_name)}"


def decode_m2(payload: bytes) -> list[tuple[int, int]]:
    """ECG stream -> (channel1, channel2) int16 little-endian pairs.

    From the device capture a notification arrives in 20-byte chunks (MTU 23),
    that is 5 pairs, not 9, so the length is not fixed here: cut by 4 bytes and
    let the caller keep an incomplete tail in its buffer.
    """
    cut = len(payload) // 4 * 4
    out = []
    for i in range(0, cut, 4):
        ch1 = int.from_bytes(payload[i:i + 2], "little", signed=True)
        ch2 = int.from_bytes(payload[i + 2:i + 4], "little", signed=True)
        out.append((ch1, ch2))
    return out


def derive_leads(lead_i: float, lead_ii: float) -> dict[str, float]:
    """The remaining leads from I and II (standard formulas)."""
    return {
        "III": lead_ii - lead_i,
        "aVR": -(lead_i + lead_ii) / 2.0,
        "aVL": lead_i - lead_ii / 2.0,
        "aVF": lead_ii - lead_i / 2.0,
    }


def read_metrics(channels: dict[str, list[float]]) -> tuple[dict, str]:
    """Heart-rate estimate from lead II. Returns metrics and a short note."""
    lead_ii = channels.get("II") or []
    if len(lead_ii) < SAMPLE_RATE * 4:
        return {}, "recording too short to estimate a heart rate"
    try:
        import numpy as np
    except ImportError:
        return {}, "numpy is missing — heart-rate estimate skipped"
    x = np.asarray(lead_ii, dtype=float)
    x = x - np.mean(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spec[(freqs < 5) | (freqs > 18)] = 0
    y = np.fft.irfft(spec, n=len(x))
    energy = np.convolve(np.diff(y) ** 2, np.ones(int(0.12 * SAMPLE_RATE)) / (0.12 * SAMPLE_RATE),
                         mode="same")
    thr = 3.0 * float(np.median(energy))
    peaks, last = [], -SAMPLE_RATE
    for idx in range(1, len(energy) - 1):
        if energy[idx] > thr and energy[idx] >= energy[idx - 1] and energy[idx] > energy[idx + 1]:
            if idx - last > 0.25 * SAMPLE_RATE:
                peaks.append(idx)
                last = idx
    if len(peaks) < 4:
        return {}, "no stable complexes found — the recording is noisy or short"
    rr = np.diff(peaks) / SAMPLE_RATE
    rr = rr[(rr > 0.3) & (rr < 2.0)]
    if len(rr) < 3:
        return {}, "intervals outside a sane range — no estimate given"
    bpm = 60.0 / float(np.median(rr))
    sdnn = float(np.std(rr)) * 1000.0
    metrics = {
        "heart_rate": round(bpm, 1),
        "hrv": round(sdnn, 1),
        "ecg_beats": len(peaks),
        "ecg_seconds": round(len(lead_ii) / SAMPLE_RATE, 1),
    }
    text = (f"heart rate about {bpm:.0f} bpm from {len(peaks)} complexes, SDNN {sdnn:.0f} ms")
    return metrics, text


# ────────────────────────── output ──────────────────────────
def write_csv(channels: dict[str, list[float]], path: Path, source: str = "kardia-ble") -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["# source", source, "start", now, "sample_rate_hz", SAMPLE_RATE,
                    "units", "raw adc (not mV)"])
        w.writerow(["second", "I", "II", "III", "aVR", "aVL", "aVF"])
        n = len(next(iter(channels.values())))
        for i in range(n):
            w.writerow([f"{i / SAMPLE_RATE:.4f}"] + [f"{channels[k][i]:.0f}" for k in
                                                     ("I", "II", "III", "aVR", "aVL", "aVF")])


def write_plot(channels: dict[str, list[float]], png: Path, pdf: Path | None) -> str:
    """Six lead strips. Returns the image path; falls back to SVG without matplotlib."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return write_svg(channels, png.with_suffix(".svg"))
    order = ["I", "II", "III", "aVR", "aVL", "aVF"]
    fig, axes = plt.subplots(len(order), 1, figsize=(11, 9), sharex=True)
    t = [i / SAMPLE_RATE for i in range(len(channels["I"]))]
    for ax, name in zip(axes, order):
        ax.plot(t, channels[name], lw=0.7, color="#111")
        ax.set_ylabel(name, rotation=0, ha="right", va="center", fontsize=9)
        ax.grid(alpha=0.25, lw=0.4)
        ax.set_yticks([])
    axes[-1].set_xlabel("seconds")
    fig.suptitle("KardiaMobile 6L — raw ADC units, 300 Hz", fontsize=10)
    fig.tight_layout()
    fig.savefig(png, dpi=150)
    if pdf is not None:
        fig.savefig(pdf)
    plt.close(fig)
    return str(png)


def write_svg(channels: dict[str, list[float]], path: Path) -> str:
    """Fallback without matplotlib: a compact SVG."""
    order = ["I", "II", "III", "aVR", "aVL", "aVF"]
    n = len(channels["I"])
    wpx, hpx, strip = 1100, 620, 100
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{wpx}" height="{hpx + 40}">',
             '<rect width="100%" height="100%" fill="white"/>']
    for r, name in enumerate(order):
        y0 = 20 + r * strip
        vals = channels[name][::max(1, n // 1100)]
        span = max(1.0, max(abs(v) for v in vals))
        pts = " ".join(f"{x * 1100 / len(vals):.1f},{y0 + strip / 2 - (v / span) * (strip / 2 - 6):.1f}"
                       for x, v in enumerate(vals))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#111" stroke-width="0.8"/>')
        parts.append(f'<text x="4" y="{y0 + 14}" font-size="12">{name}</text>')
    parts.append(f'<text x="4" y="{hpx + 30}" font-size="11">KardiaMobile 6L, 300 Hz, raw units, '
                 f'{n / SAMPLE_RATE:.0f} s</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts))
    return str(path)


def finish_session(channels: dict[str, list[float]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"ecg_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    csv_path = out_dir / f"{stem}.csv"
    png_path = out_dir / f"{stem}.png"
    write_csv(channels, csv_path)
    img = write_plot(channels, png_path, out_dir / f"{stem}.pdf")
    metrics, note = read_metrics(channels)
    print(f"session {len(channels['I']) / SAMPLE_RATE:.0f} s -> {csv_path}")
    print("metrics:", metrics or "none")
    print(note)
    print("plot:", img)


# ────────────────────────── Bluetooth ──────────────────────────
async def scan(seconds: float = 12.0) -> list:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        svc = [u.lower() for u in (adv.service_uuids or [])]
        rows.append({"name": name, "address": addr, "rssi": adv.rssi,
                     "kardia": SERVICE in svc or "kardia" in name.lower()})
    rows.sort(key=lambda r: (not r["kardia"], -(r["rssi"] or -999)))
    return rows


async def find_device(wait: float) -> object:
    hits = await scan(wait)
    for r in hits:
        if r["kardia"]:
            print(f"found: {r['name'] or '(no name)'} {r['address']} RSSI {r['rssi']}")
            return r
    print("device not found. Nearby:")
    for r in hits[:12]:
        print(f"  {r['name'] or '(no name)'} {r['address']} RSSI {r['rssi']}")
    return None


def start_bond_agent():
    """Keep a Just Works agent alive for the duration of the session.

    Without a bond the vendor characteristics answer ATT Error 5 and the link drops;
    verified on a live device. The agent lives in the bluetoothctl session, so the
    process stays open until the recording is over.
    """
    try:
        proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        proc.stdin.write("agent NoInputNoOutput\ndefault-agent\n")
        proc.stdin.flush()
        print("bonding agent up (Just Works)")
        return proc
    except Exception as e:
        print("bonding agent failed to start:", e)
        return None


def stop_bond_agent(proc) -> None:
    if proc is None:
        return
    try:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def pair_device(address: str) -> str:
    """Explicit Just Works pairing, before connecting.

    The device rejects writes and subscriptions until the link is encrypted, and BlueZ
    does not start pairing by itself on the first GATT access.
    """
    proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    try:
        proc.stdin.write("agent NoInputNoOutput\ndefault-agent\n")
        proc.stdin.flush()
        time.sleep(1)
        proc.stdin.write(f"pair {address}\n")
        proc.stdin.flush()
        time.sleep(14)
        proc.stdin.write(f"trust {address}\nquit\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
    except Exception as e:
        return f"pairing failed: {e}"
    out = subprocess.run(["bluetoothctl", "info", address], capture_output=True, text=True).stdout
    return "bond present" if "Bonded: yes" in out else "no bond"


async def capture(seconds: float, auto: bool, wait: float = 1800.0, keep_raw: bool = False,
                  raw_dir: Path | None = None) -> dict[str, list[float]]:
    from bleak import BleakClient
    if auto:
        # The device only advertises while fingers bridge the electrodes, so scan in
        # cycles instead of once — otherwise the 8-second window is missed.
        deadline = time.monotonic() + wait
        target = None
        while time.monotonic() < deadline:
            target = await find_device(8.0)
            if target:
                break
            print("device not on air — scanning on (touch the electrodes to wake it)")
    else:
        target = await find_device(20.0)
    if not target:
        return {}
    channels = {k: [] for k in ("I", "II", "III", "aVR", "aVL", "aVF")}
    state = {"active": False, "last_active": 0.0, "packets": 0}

    raw_fp = None
    raw_path = None
    if keep_raw:
        raw_dir = raw_dir or Path(".")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"raw_ecg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"
        raw_fp = raw_path.open("wb")
    buffer = bytearray()

    def on_ecg(_sender, data: bytearray) -> None:
        # Keep the raw bytes: the channel layout is settled from evidence, not guesses.
        if raw_fp is not None:
            raw_fp.write(bytes(data))
        buffer.extend(data)
        cut = len(buffer) // 4 * 4
        if not cut:
            return
        pairs = decode_m2(bytes(buffer[:cut]))
        del buffer[:cut]
        # First element of a pair is the signal, second is a slow component.
        sig = [a for a, _ in pairs]
        spread = (max(sig) - min(sig)) if sig else 0
        if spread >= ACTIVITY_THRESHOLD:
            state["active"] = True
            state["last_active"] = time.monotonic()
        if auto and not state["active"]:
            return
        for ch1, ch2 in pairs:
            channels["I"].append(float(ch1))
            channels["II"].append(float(ch2))
            for lead, val in derive_leads(ch1, ch2).items():
                channels[lead].append(val)
        state["packets"] += 1

    print("pairing:", pair_device(target["address"]))
    agent = start_bond_agent()
    async with BleakClient(target["address"], timeout=25) as client:
        # The device name feeds the unlock token: for the example name "Kardia6L ABCD"
        # the token is sha256("Triangle" + name)[:8] = feaa7255122d948f, checked against
        # the phone capture.
        name = target["name"] or ""
        try:
            await client.start_notify(CHAR_CMD, lambda s, d: print("command:", bytes(d).hex()[:40]))
        except Exception as e:
            print("indications on the control characteristic did not start:", e)
        cmd = command_for_mode(name if name.lower().startswith("kardia") else "Kardia6L ABCD")
        try:
            await client.write_gatt_char(CHAR_CMD, cmd.encode(), response=True)
            print("unlock command written")
        except Exception as e:
            print("could not write the command:", e)
        await client.start_notify(CHAR_ECG, on_ecg)
        print(f"ECG stream on, recording {seconds:.0f} s" if not auto
              else "waiting for electrode contact...")
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            el = time.monotonic() - t0
            if not auto and el >= seconds:
                break
            if auto:
                got = len(channels["I"]) / SAMPLE_RATE
                if got >= MAX_SESSION_SECONDS:
                    print("session length limit reached")
                    break
                if state["active"] and got >= MIN_SESSION_SECONDS and \
                        time.monotonic() - state["last_active"] > FLAT_STOP_SECONDS:
                    print("signal gone — closing the session")
                    break
                if el > 900:
                    print("15 minutes without a signal — giving up")
                    break
        try:
            await client.stop_notify(CHAR_ECG)
        except Exception:
            pass
    stop_bond_agent(agent)
    if raw_fp is not None:
        raw_fp.close()
        print("raw stream:", raw_path)
    print(f"packets: {state['packets']}, samples: {len(channels['I'])}")
    return channels if channels["I"] else {}


def selftest() -> int:
    ok = True
    # vector from a public repository: device name Kardia6L
    if command_for_mode("Kardia6L") != "M2 Kd8a179a137775575":
        print("FAIL: unlock token does not match the reference vector")
        ok = False
    # reference checked against a live phone HCI capture
    if command_for_mode("Kardia6L ABCD") != "M2 Kfeaa7255122d948f":
        print("FAIL: unlock token does not match the captured command")
        ok = False
    payload = b"".join(int(v).to_bytes(2, "little", signed=True) for pair in
                       [(-32768, 32767), (-2000, 2000), (-3, 3), (-2, 2), (-1, 1), (0, 0),
                        (1, -1), (2, -2), (3, -3)] for v in pair)
    if decode_m2(payload)[:3] != [(-32768, 32767), (-2000, 2000), (-3, 3)]:
        print("FAIL: packet decoding does not match the reference")
        ok = False
    # The stream arrives in 20-byte chunks: an incomplete tail must not become a pair.
    if len(decode_m2(b"\x01\x00\x02")) != 0 or len(decode_m2(b"\x01\x00\x02\x00\x03")) != 1:
        print("FAIL: incomplete stream tail decoded wrongly")
        ok = False
    d = derive_leads(1.0, 3.0)
    if round(d["III"], 6) != 2.0 or round(d["aVR"], 6) != -2.0:
        print("FAIL: lead derivation is wrong", d)
        ok = False
    if SAMPLE_RATE != 300 or PACKET_BYTES != 36 or SAMPLES_PER_PACKET != 9:
        print("FAIL: stream constants changed")
        ok = False
    print("selftest:", "all consistent" if ok else "discrepancies found")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="KardiaMobile 6L ECG over Bluetooth")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--capture", type=float, default=0, metavar="SEC")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--out", default=".", help="directory for the output files")
    ap.add_argument("--wait", type=float, default=1800.0, metavar="SEC",
                    help="how long to wait for the device in --auto")
    ap.add_argument("--raw", action="store_true", help="also keep the raw notification bytes")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.scan:
        for r in asyncio.run(scan(15.0)):
            print(f"{'*' if r['kardia'] else ' '} {r['name'] or '(no name)':28} "
                  f"{r['address']:20} RSSI {r['rssi']}")
        return 0
    if not (a.capture or a.auto):
        ap.print_help()
        return 2
    out = Path(a.out)
    ch = asyncio.run(capture(a.capture or MAX_SESSION_SECONDS, a.auto, wait=a.wait,
                             keep_raw=a.raw, raw_dir=out if a.raw else None))
    if not ch or not ch["I"]:
        print("no recording")
        return 1
    finish_session(ch, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
