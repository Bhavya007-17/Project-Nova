"""Text to a 16 kHz mono WAV with Windows' own SAPI voice (System.Speech, in every Windows; no new dependency).

For the session replay (`nova eval --sessions --replay N`): the speech goes into a file, never the speakers.
"""

from __future__ import annotations

import subprocess
import wave
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import escape

SAMPLE_RATE = 16_000

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$voice.SetOutputToWaveFile($env:NOVA_TTS_WAV, $format)
$voice.SpeakSsml([System.IO.File]::ReadAllText($env:NOVA_TTS_SSML))
$voice.Dispose()
"""


def ssml(lines: Sequence[str], *, pause_ms: int) -> str:
    """One SSML document: each line, then ``pause_ms`` of silence (he pauses after every piece of speech)."""
    body = "".join(f"<s>{escape(line)}</s><break time=\"{pause_ms}ms\"/>\n" for line in lines)
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">\n'
            f"{body}</speak>\n")


def speak(lines: Sequence[str], wav: Path, *, pause_ms: int = 900, timeout: float = 120.0) -> float:
    """Write ``lines`` as speech to ``wav`` (16 kHz, 16-bit, mono). Returns its length in seconds."""
    import os

    wav = Path(wav)
    wav.parent.mkdir(parents=True, exist_ok=True)
    source = wav.with_suffix(".ssml")
    source.write_text(ssml(lines, pause_ms=pause_ms), encoding="utf-8")
    env = {**os.environ, "NOVA_TTS_WAV": str(wav.resolve()), "NOVA_TTS_SSML": str(source.resolve())}
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT], env=env,
                          capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0 or not wav.exists():
        raise RuntimeError((done.stderr or done.stdout or "no output").strip()[-400:])
    with wave.open(str(wav), "rb") as f:
        if f.getframerate() != SAMPLE_RATE or f.getnchannels() != 1:
            raise RuntimeError(f"SAPI wrote {f.getframerate()} Hz × {f.getnchannels()} channels, not 16 kHz mono")
        return f.getnframes() / SAMPLE_RATE
