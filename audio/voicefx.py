#!/usr/bin/env python3
"""
VoiceFX - a fun real-time voice changer that shows up as a virtual microphone.

100 % local: audio only travels  mic -> PipeWire -> this script -> PipeWire -> "VoiceFX-Mic".
No network code anywhere. Dependencies (all in the Arch/Omarchy repos):
    sudo pacman -S --needed python-numpy tk libpulse
Run:  python voicefx.py   then pick "VoiceFX-Mic" as the microphone in Discord/OBS/browser.
Closing the window removes the virtual mic again.
"""
import atexit, shutil, signal, subprocess, sys, threading
import numpy as np
import tkinter as tk
from tkinter import ttk

SR, N = 48000, 1024                     # sample rate, block size (~21 ms)
SINK, MIC = "voicefx_sink", "voicefx_mic"


# ---------------------------------------------------------------- DSP building blocks
class Delay:
    """Feedback delay line (comb / allpass), vectorised in chunks <= delay length."""
    def __init__(s, sec):
        s.d = max(1, int(sec * SR)); s.xb = np.zeros(s.d); s.yb = np.zeros(s.d)

    def _run(s, x, f):
        out = np.empty_like(x)
        for i in range(0, len(x), s.d):
            c = x[i:i + s.d]; L = len(c)
            y = f(c, s.xb[:L], s.yb[:L])
            s.xb = np.concatenate((s.xb[L:], c)); s.yb = np.concatenate((s.yb[L:], y))
            out[i:i + L] = y
        return out

    def comb(s, x, g):    return s._run(x, lambda c, xd, yd: c + g * yd)
    def allpass(s, x, g): return s._run(x, lambda c, xd, yd: -g * c + xd + g * yd)


class ModDelay:
    """Delay line with a per-sample (modulated) delay time; several taps allowed."""
    def __init__(s, max_samples): s.h = np.zeros(int(max_samples) + 2)

    def __call__(s, x, *delays):
        buf = np.concatenate((s.h, x)); n = len(s.h) + np.arange(len(x))
        outs = []
        for d in delays:
            idx = n - d; i0 = idx.astype(int); f = idx - i0
            outs.append(buf[i0] * (1 - f) + buf[np.minimum(i0 + 1, len(buf) - 1)] * f)
        s.h = buf[-len(s.h):]
        return outs if len(outs) > 1 else outs[0]


class Pitch:
    """Real-time pitch shifter: two crossfaded taps sweeping through a delay line."""
    def __init__(s, ratio, win=0.04):
        s.r, s.w, s.ph = ratio, int(win * SR), 0.0
        s.md = ModDelay(s.w)

    def __call__(s, x):
        ph = (s.ph + (1 - s.r) / s.w * np.arange(1, len(x) + 1)) % 1.0
        s.ph = ph[-1]; p2 = (ph + 0.5) % 1.0
        a, b = s.md(x, ph * s.w, p2 * s.w)
        return a * np.sin(np.pi * ph) ** 2 + b * np.sin(np.pi * p2) ** 2


class FIR:
    def __init__(s, taps): s.h = taps; s.hist = np.zeros(len(taps) - 1)
    def __call__(s, x):
        buf = np.concatenate((s.hist, x)); s.hist = buf[-len(s.hist):]
        return np.convolve(buf, s.h, "valid")


def lowpass(fc, M=129):
    n = np.arange(M) - (M - 1) / 2
    h = np.sinc(2 * fc / SR * n) * np.hamming(M); return h / h.sum()

def bandpass(lo, hi): return lowpass(hi) - lowpass(lo)

def crush(x, bits, hold=1):
    q = 2 ** (bits - 1); y = np.round(x * q) / q
    return np.repeat(y[::hold], hold)[:len(x)] if hold > 1 else y

def reverb(size=1.0, fb=0.84):
    """Classic Schroeder reverb: 4 parallel combs + 2 allpasses."""
    combs = [Delay(t * size) for t in (0.0353, 0.0367, 0.0338, 0.0322)]
    aps = [Delay(0.0051), Delay(0.0126)]
    def run(x):
        y = sum(c.comb(x * (1 - fb), fb) for c in combs)
        for a in aps: y = a.allpass(y, 0.5)
        return y
    return run

def ms(v): return v * SR / 1000
def sine(f, t): return np.sin(2 * np.pi * f * t)


# ---------------------------------------------------------------- effects
# Each factory returns fn(x, t): x = block of samples, t = their time in seconds.

def fx_dry():       return lambda x, t: x

def fx_echo():
    d = Delay(0.35); return lambda x, t: 0.8 * d.comb(x, 0.55)

def fx_reverb():
    r = reverb(); return lambda x, t: 0.6 * x + 1.5 * r(x)

def fx_megatron():
    p, tube = Pitch(0.72), Delay(0.008)
    def f(x, t):
        y = p(x) * (0.4 + 0.6 * sine(70, t))            # pitch down + ring-mod buzz
        y = tube.comb(0.5 * np.tanh(3 * y), 0.6)         # distortion + metallic resonance
        return crush(y, 8)
    return f

def fx_tiny():
    p = Pitch(1.9, 0.025); return lambda x, t: p(x)

def fx_alien():
    md = ModDelay(ms(12))
    return lambda x, t: md(x, ms(6 + 5 * sine(7, t))) * (0.7 + 0.3 * sine(300, t))

def fx_telephone():
    bp = FIR(bandpass(300, 3400)); return lambda x, t: np.tanh(2 * bp(x))

def fx_techno(bpm=128):
    beat = 60 / bpm; echo = Delay(beat * 0.75)       # dotted-eighth echo, synced
    def f(x, t):
        gate = 0.5 * (1 + np.tanh(6 * sine(4 / beat, t)))   # 16th-note trance gate
        v = echo.comb(x * gate, 0.45)
        tb = t % beat                                        # four-on-the-floor kick
        kick = np.sin(2 * np.pi * (50 * tb + 100 * (1 - np.exp(-30 * tb)) / 30)) * np.exp(-8 * tb)
        th = (t + beat / 2) % beat                           # off-beat hi-hat
        hat = np.diff(np.random.uniform(-1, 1, len(t)) * np.exp(-60 * th), prepend=0)
        return v + 0.35 * kick + 0.08 * hat
    return f

def fx_demon():
    p1, p2, r = Pitch(0.6), Pitch(0.5, 0.05), reverb(0.7, 0.75)
    def f(x, t):
        y = 0.6 * np.tanh(2.5 * (p1(x) + 0.7 * p2(x)))
        return y + 0.6 * r(y)
    return f

def fx_choir():                                      # you + octave down + third + fifth
    ps = [Pitch(r) for r in (0.5, 1.26, 1.5)]
    return lambda x, t: 0.5 * x + 0.35 * sum(p(x) for p in ps)

def fx_flanger():
    md = ModDelay(ms(12))
    return lambda x, t: 0.6 * (x + md(x, ms(1 + 4 * (1 + sine(0.25, t)))))

def fx_underwater():
    lp, md, r = FIR(lowpass(500)), ModDelay(ms(30)), reverb(0.5, 0.7)
    def f(x, t):
        y = 1.5 * md(lp(x), ms(12 + 8 * sine(1.3, t)))
        return y + 0.5 * r(y)
    return f

def fx_walkie():
    bp = FIR(bandpass(600, 2800))
    return lambda x, t: 0.5 * np.tanh(6 * bp(x)) + 0.015 * np.random.randn(len(x))

def fx_8bit():      return lambda x, t: crush(np.tanh(2 * x), 4, 6)

class fx_glitch:                                     # randomly loops the last few blocks
    def __init__(s): s.hist = np.zeros(4 * N); s.seg = None; s.pos = s.left = 0
    def __call__(s, x, t):
        s.hist = np.concatenate((s.hist[len(x):], x))
        if not s.left and np.random.rand() < 0.07:
            k = np.random.choice([1, 2, 4])
            s.seg, s.pos, s.left = s.hist[-k * N:].copy(), 0, np.random.randint(2, 8)
        if not s.left: return x
        out = s.seg[s.pos:s.pos + len(x)]
        s.pos = (s.pos + len(x)) % len(s.seg); s.left -= 1
        return out

def fx_drunk():
    md, e = ModDelay(ms(60)), Delay(0.18)
    return lambda x, t: 0.8 * e.comb(md(x, ms(30 + 12 * sine(0.4, t) + 6 * sine(0.93, t))), 0.3)

def fx_stadium():
    r, e = reverb(1.6, 0.88), Delay(0.45)
    return lambda x, t: 0.8 * e.comb(0.6 * x + 1.3 * r(x), 0.35)

EFFECTS = {
    "Dry": fx_dry, "Echo": fx_echo, "Reverb": fx_reverb, "Megatron": fx_megatron,
    "Tiny": fx_tiny, "Alien": fx_alien, "Telephone": fx_telephone, "Techno": fx_techno,
    "Demon": fx_demon, "Choir": fx_choir, "Flanger": fx_flanger, "Underwater": fx_underwater,
    "Walkie-Talkie": fx_walkie, "8-bit": fx_8bit, "Glitch": fx_glitch, "Drunk": fx_drunk,
    "Stadium": fx_stadium,
}


# ---------------------------------------------------------------- audio engine
FMT = ["--client-name=VoiceFX", "--format=float32le", f"--rate={SR}", "--channels=1",
       "--latency-msec=20",
       # Pin our streams: PipeWire must never move them when you change default devices.
       "--property=node.dont-move=true", "--property=node.dont-reconnect=true"]

def start_recorder(mic):
    return subprocess.Popen(["parec", f"--device={mic}", *FMT], stdout=subprocess.PIPE)

class Engine:
    def __init__(s, mic):
        s.want, s.mix, s.gain, s.level = "Dry", 1.0, 1.0, 0.0
        s.mic, s.rec = mic, start_recorder(mic)

    def set_mic(s, mic):                       # hot-swap the real microphone
        if mic == s.mic: return
        old, s.mic = s.rec, mic
        s.rec = start_recorder(mic); old.terminate()

    def loop(s, play):
        name, fx, n0 = None, None, 0
        try:
            while True:
                rec = s.rec
                raw = rec.stdout.read(N * 4)
                if len(raw) < N * 4:
                    if rec is not s.rec: continue        # mic was switched, keep going
                    print("recorder stopped", file=sys.stderr); break
                x = np.frombuffer(raw, np.float32).astype(np.float64)
                if s.want != name: name = s.want; fx = EFFECTS[name]()
                t = (n0 + np.arange(N)) / SR; n0 += N
                try: wet = fx(x, t)
                except Exception as e: print("effect error:", e, file=sys.stderr); wet = x
                y = np.tanh(s.gain * ((1 - s.mix) * x + s.mix * wet))   # soft limiter
                s.level = float(np.sqrt(np.mean(y ** 2)))
                play.stdin.write(y.astype(np.float32).tobytes()); play.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass


# ---------------------------------------------------------------- PipeWire plumbing
def pactl(*a, check=True):
    return subprocess.run(["pactl", *a], capture_output=True, text=True, check=check).stdout.strip()

def remove_virtual_mic():
    lines = pactl("list", "short", "modules", check=False).splitlines()
    for line in reversed(lines):
        if SINK in line: pactl("unload-module", line.split()[0], check=False)

def real_mics():
    """All capture devices except monitors and our own virtual mic."""
    names = [l.split("\t")[1] for l in pactl("list", "short", "sources").splitlines() if "\t" in l]
    return [n for n in names if not n.endswith(".monitor") and SINK not in n and MIC not in n]

def main():
    for tool in ("pactl", "parec", "pacat"):
        if not shutil.which(tool): sys.exit(f"'{tool}' not found: sudo pacman -S libpulse")
    remove_virtual_mic()                                     # leftovers from a crash
    mics = real_mics()
    if not mics: sys.exit("No real microphone found.")
    default = pactl("get-default-source", check=False)
    mic = default if default in mics else mics[0]           # never record ourselves

    pactl("load-module", "module-null-sink", f"sink_name={SINK}",
          "sink_properties=device.description=VoiceFX-Output")
    pactl("load-module", "module-remap-source", f"master={SINK}.monitor",
          f"source_name={MIC}", "source_properties=device.description=VoiceFX-Mic")
    atexit.register(remove_virtual_mic)

    play = subprocess.Popen(["pacat", "--playback", f"--device={SINK}", *FMT], stdin=subprocess.PIPE)
    eng = Engine(mic)
    atexit.register(lambda: [p.terminate() for p in (eng.rec, play)])
    threading.Thread(target=eng.loop, args=(play,), daemon=True).start()
    gui(eng, mics)

def gui(eng, mics):
    root = tk.Tk(); root.title("VoiceFX")
    signal.signal(signal.SIGINT, lambda *a: root.quit())
    tk.Label(root, text="Real microphone:").pack(pady=(8, 0))
    pick = ttk.Combobox(root, values=mics, state="readonly", width=60)
    pick.set(eng.mic); pick.pack(padx=10)
    pick.bind("<<ComboboxSelected>>", lambda e: eng.set_mic(pick.get()))
    tk.Label(root, text='In your apps (or as system default input) choose  "VoiceFX-Mic"').pack(pady=6)

    grid = tk.Frame(root); grid.pack(padx=8)
    cur = tk.StringVar(value="Dry")
    for i, name in enumerate(EFFECTS):
        tk.Radiobutton(grid, text=name, value=name, variable=cur, indicatoron=False,
                       width=13, pady=6, command=lambda: setattr(eng, "want", cur.get())
                       ).grid(row=i // 4, column=i % 4, padx=2, pady=2)

    for label, start, hi, setter in (("Intensity %", 100, 100, lambda v: setattr(eng, "mix", v / 100)),
                                     ("Output gain %", 100, 300, lambda v: setattr(eng, "gain", v / 100))):
        s = tk.Scale(root, label=label, from_=0, to=hi, orient="horizontal", length=420,
                     command=lambda v, f=setter: f(float(v)))
        s.set(start); s.pack(padx=10)

    meter = ttk.Progressbar(root, length=420, maximum=1.0); meter.pack(pady=(4, 10))
    def tick():
        meter["value"] = min(1.0, eng.level * 3); root.after(50, tick)
    tick()
    root.mainloop()

if __name__ == "__main__":
    main()
