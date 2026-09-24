# KardiaMobile 6L — protocol reference

Byte-level notes recovered from an Android HCI snoop capture and confirmed against a live device.

## 1. GATT layout

| Handle | UUID | Properties | Notes |
| --- | --- | --- | --- |
| — | `ac060001-328c-a28f-9846-5a8aa212661b` | primary service | vendor service, 128-bit |
| — | `ac060002-328c-a28f-9846-5a8aa212661b` | write, indicate | control channel |
| — | `ac060003-328c-a28f-9846-5a8aa212661b` | notify | ECG sample stream |

Standard services also present (Device Information, Battery, Generic Access) and used for the metadata below, but they are not needed to get the ECG.

## 2. Session sequence

```
connect (LE, address type as advertised)
  -> bond: Just Works pairing, encryption ON before any GATT write
write  ac060002  "M2 Kfeaa7255122d948f"      (20 bytes ASCII, write request)
write  ac060002 CCCD  02 00                   (indications)
write  ac060003 CCCD  01 00                   (notifications)
  <- indication on ac060002: 01
  <- notification stream on ac060003: int16 LE pairs, 300 Hz/channel
disconnect
```

Deviations observed in the capture: the app writes the command **after** the link is encrypted, and it subscribes to both CCCDs before reading anything else. Skipping the bond is the single most common reason the sequence fails (`0x0E`).

## 3. The unlock command

20 bytes, ASCII:

```
4d 32 20 4b 66 65 61 61 37 32 35 35 31 32 32 64 39 34 38 66
 M  2     K  f  e  a  a  7  2  5  5  1  2  2  d  9  4  8  f
```

Layout:

| Bytes | Field |
| --- | --- |
| 0 | `'M'` |
| 1 | mode digit, `'2'` in every capture (see below) |
| 2 | space |
| 3 | `'K'` |
| 4–19 | 16 hex characters = first 8 bytes of `sha256("Triangle" + name)` |

```
token = sha256(b"Triangle" + advertised_name.encode()).hexdigest()[:16]
command = f"M{mode} K{token}".encode("ascii")
```

`Triangle` is a fixed string, not a per-device value. The advertised name is the BLE name the device prints while advertising, e.g. `Kardia6L XXXX` — the suffix is the last four hex digits of its address.

The mode digit was `2` in all captures of a 6L recording session. Modes observed in vendor tooling differ for firmware/self-test paths; this project uses `2` and does not claim the others.

## 4. The ECG stream

- Payload: consecutive **int16 little-endian** values, alternating channel 1 / channel 2.
- Rate: **~300 pairs per second** (measured: 2001 notifications, 18009 pairs, 60 s → 300.15 Hz).
- Framing: the device packs **9 pairs = 36 bytes** when the negotiated MTU allows it, and **5 pairs = 20 bytes** at MTU 23. The frame is trimmed to fit the MTU, so the length is a transport detail, not a protocol constant.
- Incomplete tail: after concatenating notifications, a remainder shorter than 4 bytes is dropped.

Baseline: the raw codes sit around a slowly moving offset (thousands of counts away from zero) and go to saturation (`±26009 ≈ 0x6599`) while the electrodes are open. Remove the offset before doing anything else:

```
y[i] = x[i] - median(x[i-w : i+w])
```

Heart-rate estimate used here: band-pass 5–18 Hz, square, moving-average envelope, peak detection with a refractory window, then median RR → BPM. On a well-contacted 30-second recording this is stable; on a saturated or noisy one it is not, and the honest output is "unusable", not a number.

## 5. Device metadata

Read from the standard Device Information / Battery services during a session:

| Field | Example | Where |
| --- | --- | --- |
| serial number | 13 digits | `0x2A25` |
| firmware revision | `3.0.1` | `0x2A26` |
| hardware/model | `19SC03.04` | `0x2A27` / `0x2A24` |
| battery level | 85–91 % | `0x2A19` |

These are informational; nothing in the ECG path depends on them.

## 6. Error codes seen

| Symptom | Meaning | Cause |
| --- | --- | --- |
| `GATT Protocol Error: Unlikely Error (0x0E)` on subscribe | link not encrypted | missing Just Works bond |
| `ATT Error: Insufficient Authentication (0x05)` on read | same | same |
| no advertisement at all | device asleep | electrodes not bridged, or another connection holds it |
| `Trusted: yes, Paired: no, Bonded: no` | pairing torn down | `bluetoothctl` batch `quit` racing `pair` |
