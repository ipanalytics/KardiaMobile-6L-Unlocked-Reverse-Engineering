# How the protocol was recovered

The method, so that the same route works on the next closed device.

## 1. Make the vendor's own client do the work, and record it

There is no need to guess when the official app already speaks the protocol. Android's **HCI snoop log** writes every Bluetooth exchange to a file: connections, ATT requests and responses, L2CAP payloads, values included.

Enable developer options → *Enable Bluetooth HCI snoop log*, then reboot Bluetooth (the toggle only takes effect for new traffic), then let the app do a full session — here: connect, unlock, one 30-second recording, drain.

Pull the capture: `/sdcard/btsnoop_hci.log`, or `.cfa` on newer builds. Copy it to a machine where you can parse it.

## 2. Parse by layer, not by eye

`btsnoop` is a pcap-style container. Walk it as HCI → ACL/L2CAP → ATT, keeping the order:

- `LE Extended Create Connection` / `Connection Complete` — where the link is, and whether it is encrypted (`Encryption Change`);
- `ATT_READ_BY_GROUP_TYPE_REQ/RSP` — service discovery, which gives you the vendor UUIDs and the handles;
- `ATT_WRITE_REQ/CMD` — what the client sends, with the value;
- `ATT_HANDLE_VALUE_NTF/IND` — the stream, with lengths and frequency.

Two numbers kill most hypotheses on contact: **the length of the notification payloads** and **how many of them arrive per second**.

## 3. Read the command, then explain it

The interesting write was 20 printable bytes. Eight of them were hex-looking, which immediately suggested a digest. Testing the obvious inputs against `sha256`:

```
sha256("Triangle" + name)[:8]  ->  the exact suffix from the capture
```

A derived token means the "unlock" is reproducible from public information. That is the finding worth repeating first: an undocumented handshake is often not a secret, just an unadvertised function of data the device publishes anyway.

## 4. Measure the stream, do not assume it

Getting the samples out of the capture is mechanical; getting the *right* structure is not. What settled it:

- take all notification payloads, concatenate, and look at the byte count: `2001 × 36 = 72036` bytes, divisible by 4, pairing into `18009` samples — and `18009 / 60 s ≈ 300 Hz`;
- compare with another capture where the payloads were 20 bytes: same data, coarser framing — proving the frame length is an MTU artefact;
- plot the decoded pairs: channel 1 carries the signal, channel 2 the same signal with a different baseline.

## 5. Replay it on the live device

A capture proves what the app does, not that your client can do it. Replay the exact sequence — bond, unlock, subscribe, stream — and check the numbers that must match:

- the device answers the command with `01`;
- the notification rate lands at ~300/s;
- battery, serial and firmware read back from the standard services.

Only when those three line up is the protocol actually reproduced.

## 6. Keep the capture

The `.cfa` is the project's ground truth. When the client later misbehaves, the fastest check is always the same: open the capture, find the step in question, compare byte for byte. In this project the capture has already overturned two confident conclusions — "the device needs no bond" and "the frame is 20 bytes".
