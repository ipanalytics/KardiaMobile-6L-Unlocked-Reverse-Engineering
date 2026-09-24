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
  --auto                 wait for electrode contact, record a session, send the result
  --simulate SEC         run without hardware: synthetic stream through the same path
  --selftest             check packet parsing and lead derivation

Output files go to the KARDIA_DIR directory (CSV + PNG + PDF), metrics can be posted
to a webhook, the file goes to Telegram. Settings come from the KARDIA_ENV file.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SERVICE = "ac060001-328c-a28f-9846-5a8aa212661b"
CHAR_CMD = "ac060002-328c-a28f-9846-5a8aa212661b"
CHAR_ECG = "ac060003-328c-a28f-9846-5a8aa212661b"
SAMPLE_RATE = 300
PACKET_BYTES = 36
SAMPLES_PER_PACKET = 9
DATA_DIR = Path(os.environ.get("KARDIA_DIR", "kardia_data"))
ENV_FILE = Path(os.environ.get("KARDIA_ENV", "telegram.env"))
# Live session: treat the electrodes as untouched when a packet's spread is below this.
ACTIVITY_THRESHOLD = float(os.environ.get("KARDIA_ACTIVITY", "150"))
FLAT_STOP_SECONDS = float(os.environ.get("KARDIA_FLAT_STOP", "6"))
MIN_SESSION_SECONDS = float(os.environ.get("KARDIA_MIN_SESSION", "10"))
MAX_SESSION_SECONDS = float(os.environ.get("KARDIA_MAX_SESSION", "60"))


# ────────────────────────── protocol ──────────────────────────
def unlock_token(device_name: str) -> str:
    """Unlock token: 'K' + first 8 bytes of sha256('Triangle' + name), hex."""
    digest = hashlib.sha256(("Triangle" + device_name).encode()).digest()
    return "K" + digest[:8].hex()


def command_for_mode(device_name: str, mode: str = "M2") -> str:
    return f"{mode} {unlock_token(device_name)}"


def decode_m2(payload: bytes) -> list[tuple[int, int]]:
    """ECG stream -> (channel1, channel2) int16 little-endian pairs.

    Taken from the device HCI capture: a notification arrives in 20-byte chunks
    (MTU 23), that is 5 pairs, not 9. So the length is not fixed here: cut by
    4 bytes and let the caller keep an incomplete tail in its buffer.
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
    # simple 5-18 Hz band-pass through FFT
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spec[(freqs < 5) | (freqs > 18)] = 0
    y = np.fft.irfft(spec, n=len(x))
    d = np.diff(y)
    energy = np.convolve(d ** 2, np.ones(int(0.12 * SAMPLE_RATE)) / (0.12 * SAMPLE_RATE), mode="same")
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
    # variability: SDNN over normal intervals, reference only
    sdnn = float(np.std(rr)) * 1000.0
    irregular = "yes" if sdnn > 200 else "no"
    metrics = {
        "heart_rate": round(bpm, 1),
        "hrv": round(sdnn, 1),
        "ecg_beats": len(peaks),
        "ecg_seconds": round(len(lead_ii) / SAMPLE_RATE, 1),
    }
    text = (f"heart rate about {bpm:.0f} bpm from {len(peaks)} complexes, "
            f"SDNN {sdnn:.0f} ms (marked irregularity: {irregular})")
    return metrics, text


# ────────────────────────── recording and output ──────────────────────────
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


def post_metrics(metrics: dict) -> str:
    if not metrics:
        return "no metrics to post"
    payload = {"source": os.environ.get("KARDIA_SOURCE", "kardia-ble"),
               "metrics": [{"metric": k, "value": v, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                           for k, v in metrics.items()]}
    url = os.environ.get("KARDIA_WEBHOOK_URL", "")
    if not url:
        return "no webhook URL set — metrics stay in the files"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=25).read().decode()[:200]
    except Exception as e:
        return f"webhook refused: {e}"


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"\'')
    return env


def send_telegram(path: Path, caption: str) -> str:
    if os.environ.get("KARDIA_NO_SEND") == "1":
        return "Telegram sending disabled (KARDIA_NO_SEND=1)"
    env = load_env()
    token, chat = env.get("TG_BOT_TOKEN", ""), env.get("TG_CHAT_ID", "")
    if not token or not chat:
        return "Telegram is not configured (no telegram.env) — file kept locally"
    boundary = "----kardia" + hashlib.md5(str(time.time()).encode()).hexdigest()[:12]

    def part(head: str, data: bytes) -> bytes:
        return (f"--{boundary}\r\n{head}\r\n\r\n").encode() + data + b"\r\n"

    field = "photo" if path.suffix.lower() == ".png" else "document"
    method = "sendPhoto" if field == "photo" else "sendDocument"
    body = b""
    body += part('Content-Disposition: form-data; name="chat_id"', str(chat).encode())
    body += part('Content-Disposition: form-data; name="caption"', caption.encode())
    body += part(f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"',
                 path.read_bytes())
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
        return "sent to Telegram" if r.get("ok") else f"Telegram answered: {str(r)[:120]}"
    except Exception as e:
        return f"Telegram refused: {e}"


def finish_session(channels: dict[str, list[float]], sim: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"sim_ecg_{stamp}" if sim else f"ecg_{stamp}"
    csv_path = DATA_DIR / f"{stem}.csv"
    png_path = DATA_DIR / f"{stem}.png"
    write_csv(channels, csv_path, source="simulate" if sim else "kardia-ble")
    img = write_plot(channels, png_path, DATA_DIR / f"{stem}.pdf")
    metrics, note = read_metrics(channels)
    seconds = len(channels["I"]) / SAMPLE_RATE
    caption = f"KardiaMobile 6L ECG, {seconds:.0f} s\n{note}"
    print(f"session {seconds:.0f} s -> {csv_path}")
    print("metrics:", metrics or "none")
    if sim:
        # Dry run without hardware: never send to the owner, or synthetic data would
        # arrive as if it were his ECG.
        print("simulation run: nothing sent to Telegram or the webhook")
        return
    print("webhook:", post_metrics(metrics))
    print(send_telegram(Path(img), caption))


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

    Without a bond the vendor characteristics answer ATT Error 5 and the link drops
    with "Not connected" — verified on a live device. The BlueZ agent lives in the
    bluetoothctl D-Bus session, so the process stays open until the recording ends.
    """
    if os.environ.get("KARDIA_NO_BOND") == "1":
        return None
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
    """Explicit Just Works pairing (without a bond the device rejects subscriptions).

    Pairing must happen before connecting: then BlueZ brings encryption up on connect
    and the vendor characteristics accept writes and subscriptions.
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


async def capture(seconds: float, auto: bool) -> dict[str, list[float]]:
    from bleak import BleakClient
    if auto:
        # The device only advertises while fingers bridge the electrodes, so scan in
        # cycles instead of once — otherwise the 8-second window is missed.
        deadline = time.monotonic() + float(os.environ.get("KARDIA_WAIT", "1800"))
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
    state = {"start": time.monotonic(), "active": False, "last_active": 0.0, "packets": 0}

    raw_path = DATA_DIR / f"raw_ecg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"
    raw_fp = None
    if os.environ.get("KARDIA_RAW", "1") == "1":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw_fp = raw_path.open("wb")
    buffer = bytearray()

    def on_ecg(_sender, data: bytearray) -> None:
        # Keep the raw bytes: the channel layout gets settled from evidence, not guesses.
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
        now = time.monotonic()
        if spread >= ACTIVITY_THRESHOLD:
            state["active"] = True
            state["last_active"] = now
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
        print(f"ECG stream on, recording {seconds:.0f} s" if not auto else "waiting for electrode contact...")
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


def simulate(seconds: float) -> dict[str, list[float]]:
    """Synthetic stream: the same decoding path as live Bluetooth."""
    channels = {k: [] for k in ("I", "II", "III", "aVR", "aVL", "aVF")}
    random.seed(4)
    total_packets = int(seconds * 100 / 3)
    for p in range(total_packets):
        payload = bytearray()
        for s in range(SAMPLES_PER_PACKET):
            idx = (p * SAMPLES_PER_PACKET + s) / SAMPLE_RATE
            phase = (idx * 1.2) % 1.0
            beat = math.exp(-((phase - 0.15) ** 2) / 0.0008) * 900 if phase < 0.4 else 0
            ch1 = int(beat * 0.7 + random.gauss(0, 12))
            ch2 = int(beat + random.gauss(0, 15))
            payload += ch1.to_bytes(2, "little", signed=True) + ch2.to_bytes(2, "little", signed=True)
        for ch1, ch2 in decode_m2(bytes(payload)):
            channels["I"].append(float(ch1))
            channels["II"].append(float(ch2))
            for lead, val in derive_leads(ch1, ch2).items():
                channels[lead].append(val)
    return channels


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
    ap.add_argument("--capture", type=float, default=0)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--simulate", type=float, default=0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--tool", default="", help="address or name to use if the wrong device is found")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.scan:
        for r in asyncio.run(scan(15.0)):
            print(f"{'*' if r['kardia'] else ' '} {r['name'] or '(no name)':28} {r['address']:20} RSSI {r['rssi']}")
        return 0
    if a.simulate:
        ch = simulate(a.simulate)
        sim = True
    elif a.capture or a.auto:
        ch = asyncio.run(capture(a.capture or MAX_SESSION_SECONDS, a.auto))
        sim = False
    else:
        ap.print_help()
        return 2
    if not ch or not ch["I"]:
        print("no recording")
        return 1
    finish_session(ch, sim)
    return 0


if __name__ == "__main__":
    sys.exit(main())
