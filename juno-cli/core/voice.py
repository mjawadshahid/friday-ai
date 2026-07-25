"""Optional voice layer (loaded only when --voice is passed).

STT  - faster-whisper (local Whisper)  OR  Groq audio.transcriptions (cloud)
TTS  - pyttsx3 (offline, zero-setup)   OR  Piper (offline, more natural)

If a backend isn't installed, the functions raise a clear error and the
CLI falls back to text-only output (see main._render_reply).
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from config import settings


# ---------- STT ----------

def _record(seconds: int = 5, rate: int = 16000) -> str:
    """Record `seconds` of audio from the default mic to a temp wav file."""
    import sounddevice as sd  # type: ignore
    from scipy.io.wavfile import write  # type: ignore
    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="int16")
    sd.wait()
    path = tempfile.mktemp(suffix=".wav")
    write(path, rate, audio)
    return path


def _transcribe_local(path: str) -> str:
    """Transcribe with faster-whisper (model loaded once per process)."""
    from faster_whisper import WhisperModel  # type: ignore
    # tiny = small + fast, runs on CPU. swap to "base" / "small" for quality.
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(path)
    return " ".join(seg.text for seg in segments).strip()


def _transcribe_groq(path: str) -> str:
    """Transcribe with Groq's free Whisper endpoint (no local model)."""
    from openai import OpenAI
    client = OpenAI(api_key=settings.api_key, base_url="https://api.groq.com/openai/v1")
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=f,
        )
    return result.text.strip()


def listen(seconds: int = 5) -> str:
    """Record from the mic and transcribe. Uses Groq if available, else local."""
    path = _record(seconds)
    try:
        # Prefer cloud Whisper (no model download) when we have a Groq key.
        if os.getenv("GROQ_API_KEY") or "groq" in (settings.base_url or ""):
            try:
                return _transcribe_groq(path)
            except Exception:
                pass  # fall through to local
        return _transcribe_local(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------- TTS ----------

def speak(text: str) -> None:
    """Speak `text` out loud using pyttsx3 (offline, zero setup)."""
    import pyttsx3  # type: ignore
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()
