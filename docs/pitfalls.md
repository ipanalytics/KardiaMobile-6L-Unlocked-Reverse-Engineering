# Pitfalls

What actually bites when you try to pull an ECG off a KardiaMobile 6L. Every item here cost a session.

## The bond is not optional, and BlueZ will not do it for you

BLE clients happily connect to the device and then fail on the first subscription. The link comes up, `ServicesResolved` fires, the characteristic is there — and the CCCD write is answered with `Unlikely Error (0x0E)` or `Insufficient Authentication (0x05)`. Nothing in the client's error message says "you are not encrypted".

Do the pairing explicitly, before connecting:

```
bluetoothctl --agent NoInputNoOutput pair <ADDR>   # then let it finish
bluetoothctl trust <ADDR>
```

`NoInputNoOutput` is what selects Just Works. There is no PIN and no passkey prompt; the device has no display and no keypad.

## A pairing that "succeeded" and still fails

Driving `bluetoothctl` from a script with a batch of commands and a trailing `quit` tears the pairing down mid-flight. The host reports success, and `info` on the device shows:

```
Trusted: yes
Paired: no
Bonded: no
```

That is a half-bond: the device will let you connect and will then refuse every write. Give `pair` time to complete (seconds, not milliseconds) before you quit the agent.

## One connection only

The 6L accepts a single central. If the phone app is connected — or still holds the link in the background — your adapter sees nothing or gets dropped. Turn the phone's Bluetooth off while the Linux client is working. This is also why a "signal strength" complaint is usually not radio noise but the wrong link: the device was never talking to you.

## The advertisement is triggered by touch

No button, no LED, no wake command. The device starts advertising while the two top electrodes are bridged and stops when the contact is released. A scan that runs for 30 seconds and sees nothing usually means the fingers came off.

Consequence for automation: a passive "wait for it to appear" loop only works if someone is holding the device. If you want it unattended, that is the wrong use of this device.

## Dry fingers, saturated front end

With dry skin the input goes straight into saturation for the first seconds: the raw codes sit at `±26009` (`0x6599`), the maximum of the ADC. A recording can be 60 seconds long, look perfectly fine at a glance, and still be unusable — the first seconds are not a signal at all, and any algorithm that computes a mean over them gets nonsense. Wet the finger pads, hold firmly, and check the raw range before trusting a number.

## MTU decides the frame size

The same device emits 20-byte frames (5 pairs) at MTU 23 and 36-byte frames (9 pairs) when the MTU is raised. Code that assumes one of them works with the stock phone app and breaks in your client — or the reverse. Concatenate the notification payloads and cut the resulting byte stream into 4-byte pairs. Never index by frame.

## Don't trust the vendor app's copy

The app uploads the recording and hands back a multi-page PDF. Whatever you get there is a presentation of the data, not the data. If you are reading the raw stream yourself, take it from the stream.

## Testing without the device

To test the decoder and the plotting without holding the device, feed a recorded dump
through `tools/kardia_decode.py` — it takes the raw notification bytes and never touches
Bluetooth.
