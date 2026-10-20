"""Audio helpers: decoding, resampling, envelopes and framing.

Everything works on ``float32`` mono numpy arrays in ``[-1.0, 1.0]`` unless the
function name says otherwise. Decoding has three independent backends so the
project works on a bare ``pip install``:

1. ``miniaudio``  - bundled C decoder, handles mp3/wav/flac/ogg (no ffmpeg)
2. ``soundfile``  - libsndfile, handles wav/flac/ogg/mp3
3. ``ffmpeg``     - last resort, needs the binary on ``PATH``
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from loguru import logger

FloatArray = np.ndarray
_SILENCE_DB = 20.0 * np.log10(np.finfo(np.float32).tiny)  # -inf guard


class AudioDecodeError(RuntimeError):
    """Raised when no decoder could make sense of the payload."""


@dataclass(slots=True)
class DecodedAudio:
    """Mono float32 audio."""

    samples: FloatArray
    sample_rate: int

    @property
    def duration(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return float(len(self.samples)) / float(self.sample_rate)

    def resampled(self, target_rate: int) -> DecodedAudio:
        if target_rate == self.sample_rate:
            return self
        return DecodedAudio(resample(self.samples, self.sample_rate, target_rate), target_rate)

    def pcm16(self) -> bytes:
        return to_pcm16(self.samples)


# --------------------------------------------------------------------------- #
#  Decoding
# --------------------------------------------------------------------------- #
def _decode_miniaudio(data: bytes) -> DecodedAudio:
    import miniaudio

    info = miniaudio.get_file_info(data)
    decoded = miniaudio.decode(
        data,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=info.nchannels,
        sample_rate=info.sample_rate,
    )
    raw = np.asarray(decoded.samples, dtype=np.int16)
    if info.nchannels > 1:
        raw = raw.reshape(-1, info.nchannels).mean(axis=1).astype(np.int16)
    return DecodedAudio(raw.astype(np.float32) / 32768.0, int(decoded.sample_rate))


def _decode_soundfile(data: bytes) -> DecodedAudio:
    import soundfile as sf

    samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1) if samples.shape[1] > 1 else samples[:, 0]
    return DecodedAudio(np.ascontiguousarray(mono, dtype=np.float32), int(rate))


def _decode_ffmpeg(data: bytes, fallback_rate: int) -> DecodedAudio:
    """Pipe the payload through ffmpeg into raw s16le mono."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioDecodeError("ffmpeg not found")

    with tempfile.TemporaryDirectory(prefix="vtuber-dec-") as tmp:
        src = Path(tmp) / "in.bin"
        src.write_bytes(data)
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(fallback_rate),
            "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0 or not proc.stdout:
            raise AudioDecodeError(proc.stderr.decode("utf-8", "ignore")[:400])
        pcm = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        return DecodedAudio(pcm, fallback_rate)


def decode_audio(data: bytes, fallback_rate: int = 24000) -> DecodedAudio:
    """Decode any supported container into mono float32.

    ``fallback_rate`` is only used by the ffmpeg backend (which must be told an
    output rate). The first two backends keep the source rate intact.
    """
    if not data:
        raise AudioDecodeError("empty payload")

    errors: list[str] = []
    for name, fn in (
        ("miniaudio", lambda: _decode_miniaudio(data)),
        ("soundfile", lambda: _decode_soundfile(data)),
        ("ffmpeg", lambda: _decode_ffmpeg(data, fallback_rate)),
    ):
        try:
            decoded = fn()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if decoded.samples.size == 0:
            errors.append(f"{name}: decoded 0 samples")
            continue
        logger.debug(
            "decoded {} bytes with {} -> {:.2f}s @ {}Hz",
            len(data), name, decoded.duration, decoded.sample_rate,
        )
        return decoded

    raise AudioDecodeError("all decoders failed -> " + " | ".join(errors))


# --------------------------------------------------------------------------- #
#  Conversion / resampling
# --------------------------------------------------------------------------- #
def resample(samples: FloatArray, src_rate: int, dst_rate: int) -> FloatArray:
    """Linear (band-limited-enough) resampling.

    Linear interpolation is fine for our use case because (a) the common path
    is a no-op (edge-tts emits 24 kHz, the client plays 24 kHz) and (b) the
    result only drives a mouth shape, never a critical listening experience.
    """
    if src_rate == dst_rate or samples.size == 0:
        return samples
    duration = samples.size / float(src_rate)
    out_len = max(1, round(duration * dst_rate))
    src_idx = np.linspace(0.0, samples.size - 1, num=out_len, dtype=np.float64)
    return np.interp(src_idx, np.arange(samples.size, dtype=np.float64), samples).astype(np.float32)


def to_pcm16(samples: FloatArray) -> bytes:
    """float32 [-1,1] -> little endian signed 16 bit PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def from_pcm16(data: bytes) -> FloatArray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def encode_wav(samples: FloatArray, sample_rate: int) -> bytes:
    """Minimal mono 16 bit WAV writer (for multipart ASR uploads).

    Hand rolled so that ``soundfile`` stays optional.
    """
    pcm = to_pcm16(samples)
    data_size = len(pcm)
    header = bytearray()
    header += b"RIFF"
    header += (36 + data_size).to_bytes(4, "little")
    header += b"WAVEfmt "
    header += (16).to_bytes(4, "little")          # PCM chunk size
    header += (1).to_bytes(2, "little")           # format = PCM
    header += (1).to_bytes(2, "little")           # channels
    header += int(sample_rate).to_bytes(4, "little")
    header += (int(sample_rate) * 2).to_bytes(4, "little")  # byte rate
    header += (2).to_bytes(2, "little")           # block align
    header += (16).to_bytes(2, "little")          # bits per sample
    header += b"data"
    header += data_size.to_bytes(4, "little")
    return bytes(header) + pcm


def iter_chunks(samples: FloatArray, chunk_size: int) -> Iterator[FloatArray]:
    """Split an array into consecutive chunks of ``chunk_size`` samples."""
    chunk_size = max(1, int(chunk_size))
    for start in range(0, len(samples), chunk_size):
        yield samples[start : start + chunk_size]


# --------------------------------------------------------------------------- #
#  Analysis
# --------------------------------------------------------------------------- #
def rms_envelope(samples: FloatArray, sample_rate: int, slice_ms: int = 20) -> list[float]:
    """Normalised RMS envelope, one value per ``slice_ms``.

    Equivalent to Open-LLM-VTuber's ``volumes`` array: the client multiplies the
    mouth opening with it so that loud syllables open the mouth wider.
    """
    if samples.size == 0 or sample_rate <= 0:
        return []

    slice_len = max(1, int(sample_rate * slice_ms / 1000.0))
    frames = len(samples) // slice_len
    if frames == 0:
        values = np.array([float(np.sqrt(np.mean(np.square(samples)) + 1e-12))])
    else:
        trimmed = samples[: frames * slice_len].reshape(frames, slice_len)
        values = np.sqrt(np.mean(np.square(trimmed), axis=1) + 1e-12)

    peak = float(values.max())
    if peak <= 1e-5:
        return [0.0] * len(values)
    return [round(float(v) / peak, 3) for v in values]


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def peak_normalise(samples: FloatArray, target_db: float = -1.0, max_gain: float = 4.0) -> FloatArray:
    """Make sure the voice is audible without clipping the WebAudio graph."""
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-4:
        return samples
    gain = min(max_gain, db_to_gain(target_db) / peak)
    return (samples * gain).astype(np.float32)


def trim_silence(
    samples: FloatArray,
    sample_rate: int,
    threshold_db: float = -45.0,
    pad_ms: float = 15.0,
) -> tuple[FloatArray, float]:
    """Trim leading/trailing silence.

    TTS engines (and edge-tts especially) pad the start with 50-300 ms of
    silence, which shows up as a visible "mouth closed before the audio"
    delay. Returns the trimmed samples plus the number of seconds that were
    removed from the front, so the viseme timeline can be shifted.
    """
    if samples.size == 0:
        return samples, 0.0

    threshold = db_to_gain(threshold_db)
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples, 0.0

    pad = int(sample_rate * pad_ms / 1000.0)
    start = max(0, int(loud[0]) - pad)
    stop = min(len(samples), int(loud[-1]) + pad + 1)
    if start == 0 and stop == len(samples):
        return samples, 0.0

    logger.trace("trimmed {:.3f}s of silence", start / sample_rate)
    return samples[start:stop], start / float(sample_rate)


def estimate_duration(text: str, chars_per_second: float = 14.5, minimum: float = 0.45) -> float:
    """Rough speaking time, used when the engine gives us no timings."""
    return max(minimum, len(text.strip()) / max(1.0, chars_per_second))


__all__ = [
    "AudioDecodeError",
    "DecodedAudio",
    "db_to_gain",
    "decode_audio",
    "encode_wav",
    "estimate_duration",
    "from_pcm16",
    "iter_chunks",
    "peak_normalise",
    "resample",
    "rms_envelope",
    "to_pcm16",
    "trim_silence",
]
