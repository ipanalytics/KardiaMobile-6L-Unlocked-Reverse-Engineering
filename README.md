# KardiaMobile 6L Unlocked — Reverse Engineering Project

_Russian version: [README.ru.md](README.ru.md)_

**Reverse engineering the AliveCor KardiaMobile 6L ECG: the BLE handshake, the byte-level stream layout and a direct Linux client that records the electrocardiogram with no vendor app, no account and no cloud.**

The KardiaMobile 6L only hands over an ECG to its own phone app, over a BLE link with a vendor service and an undocumented unlock command. That annoyed me: the device sits on my desk, the server sits next to it, and my own heart data still has to make a detour through someone else's account and come back as a five-page PDF.

So I took the protocol apart. Now the ECG is recorded by my own code on Linux — over the air, without the app, without accounts, without the cloud.

Everything below is not theory: the bytes came out of a real Bluetooth HCI capture taken on the phone, were replayed on a live device, and the sample rate, the framing and the unlock token all match down to the last byte.

* * *

## The protocol in short

The 6L exposes a single **vendor service**, and everything happens on two characteristics inside it:

| UUID | Role | Properties |
| --- | --- | --- |
| `ac060001-328c-a28f-9846-5a8aa212661b` | service | — |
| `ac060002-328c-a28f-9846-5a8aa212661b` | control | write, indicate |
| `ac060003-328c-a28f-9846-5a8aa212661b` | ECG stream | notify |

The order is exactly what the phone app does:

1. **Bond first.** The device refuses to work on an unencrypted link — see below.
2. Write the unlock command to `ac060002` — 20 ASCII bytes, `M2 K<8-byte token>`.
3. Subscribe to indications on `ac060002` (`02 00` written to its CCCD).
4. Subscribe to notifications on `ac060003` (`01 00` written to its CCCD).
5. The device answers `01` and starts streaming samples on `ac060003`.

### The unlock token is derived, not exchanged

The command is plain ASCII. Only the last eight bytes look random:

```
M2 Kfeaa7255122d948f
```

That suffix is not a secret handed out by the server — it is computed from what the device already advertises:

```
token = sha256("Triangle" + <advertised BLE name>)[:8]   # hex, 8 bytes
```

With the advertised name `Kardia6L XXXX` this reproduces the captured command byte for byte. There is no account, no server round-trip and no per-device secret: the "unlock" is a handshake, not a credential.

### The stream is 300 Hz, two channels, cut by MTU

Samples arrive as **little-endian int16 pairs**, interleaved:

```
<ch1 int16 LE> <ch2 int16 LE> <ch1 int16 LE> <ch2 int16 LE> ...
```

- Two channels, ~300 samples per second per channel.
- A full record is **9 pairs (36 bytes)** when the MTU is raised, and **5 pairs (20 bytes)** on a stock 23-byte MTU — the device trims the frame to fit.
- Consequence: a decoder must **buffer the byte stream and cut it into pairs**, not assume a fixed frame size. A tail shorter than 4 bytes is incomplete and is dropped.

The raw values are unsigned ADC codes around a moving baseline; the ECG is the difference, so the first thing to do is remove the baseline drift, not to trust the absolute numbers.

* * *

## Why this was not trivial

**1. No bond, no data.** On an unencrypted link the device answers the subscribe with `GATT Protocol Error: Unlikely Error (0x0E)` (and `ATT Error: Insufficient Authentication (0x05)` on reads). BlueZ does not start pairing on its own: the link comes up and stays unencrypted, silently. The fix is an explicit Just Works pairing *before* connecting — no PIN, no key, but the bond itself is mandatory.

**2. The bond is easy to half-do.** Driving `bluetoothctl` in batch mode and sending `quit` straight after `pair` tears the pairing down mid-flight: the device ends up `Trusted: yes, Paired: no, Bonded: no`, which looks like success from the host and fails on every write. Give the pairing a moment to complete.

**3. The frame size lies.** Reading the capture, the same data shows up as 20-byte frames on one session and 36-byte frames on another. Anchoring on a fixed length produces a decoder that works on the phone's link (MTU 23) and breaks on a raised MTU.

**4. Advertising is triggered by touch.** The 6L has no button and no LED. It powers up and advertises only while the two top electrodes are bridged — that is, while you actually hold the device — and the advertisement stops the moment the contact is released. It also only takes **one** connection at a time: if the phone app is holding the device, Linux sees nothing useful.

* * *

## Reproducing it

You need Linux with a Bluetooth adapter, BlueZ and Python 3 (`bleak`). Put two fingers on the two top electrodes, bridge them with both hands, and hold still — that is what powers the device and keeps the link alive.

The client in `tools/` does the whole sequence: bond, unlock, subscribe, stream, and decode into a CSV plus a plotted report.

```
python3 tools/kardia_ble.py --scan          # find the device: name + address type
python3 tools/kardia_ble.py --auto          # bond, unlock, record 60 s, write ECG files
```

`--simulate` generates a synthetic ECG for plumbing tests. It is deliberately marked in the output and in the filenames so it can never be mistaken for a real measurement.

Every session produces:

- `ecg_<timestamp>.csv` — two channels, one row per sample, 300 Hz;
- `ecg_<timestamp>.png` — the trace as a rhythm strip;
- `ecg_<timestamp>.pdf` — the printable report.

* * *

## How it was cracked

The protocol was not guessed and not brute-forced — it was read out of the traffic:

1. Enabled the **HCI snoop log** on Android, which records every Bluetooth exchange to a `.cfa` file.
2. Started the vendor app, took a recording, and pulled the capture.
3. Parsed the capture as L2CAP/ATT: connections, service discovery, every write and every notification, in order.
4. Found the unlock command as a 20-byte ASCII write and recovered its suffix.
5. Recognised the stream, measured its framing and its rate (2001 notifications → 18009 pairs → 300 Hz).
6. Confirmed the derived token by hashing the advertised name — it reproduced the captured bytes exactly.
7. Replayed the sequence against the live device: bond, unlock, subscribe, 60 seconds of real ECG.

* * *

## Status

- Protocol cracked and verified against a live device — **done**.
- Direct BLE client on Linux, real ECG landing as CSV/PNG/PDF — **working**.
- Quality metrics (QRS detection, heart rate, HRV) — **working, being hardened**: dry fingers and a loose grip drive the front end into saturation in the first seconds, so a recording can be real and still unusable for measurements.
- Sixth lead (LL/RA) — **not decoded**: the 6L computes the augmented leads in the app from the two raw channels; the raw stream carries two, and this project does not claim six.

* * *

## Disclaimer

This project is about my own device and my own data. Nothing was "hacked" in the sense of defeating a protection: it uses the Bluetooth standard and the characteristics the device itself exposes. The device was not opened and its firmware was not modified. If you reproduce this, do it on your own hardware — this is a consumer ECG, not a diagnostic device, and nothing here is medical advice.
