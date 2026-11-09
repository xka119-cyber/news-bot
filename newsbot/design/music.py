"""릴스 배경음: 코드로 직접 만드는 잔잔한 곡 (2026-11-09 사용자 선택 '잔잔한 배경음 자동').

남의 음원을 쓰지 않으므로 저작권 문제가 없고 비용도 없다. 부드러운 화음(패드) + 통통 튀는 분산화음(플럭)
+ 낮은 베이스로 된 짧은 곡을 영상 길이에 맞춰 만든다. 초안마다 조(key)와 진행을 바꿔 매번 같은 소리가 되지 않게 한다.
무음 릴스는 넘기는 손이 빠르고 덜 퍼진다는 판단(11-09 인사이트: 3초 안에 넘김 90~99%)에서 넣었다.
"""

import hashlib
import wave
from pathlib import Path

import numpy as np

RATE = 48000
BPM = 96
# 조: 으뜸음 MIDI 번호 (C4=60). 너무 낮거나 높지 않은 것만
KEYS = [60, 62, 65, 67, 57]
# 진행: 장조 음계 도수(0=I). 각 화음은 [근음, 3음, 5음]을 음계에서 쌓는다
PROGRESSIONS = [
    [0, 4, 5, 3],   # I V vi IV
    [0, 5, 3, 4],   # I vi IV V
    [3, 4, 0, 5],   # IV V I vi
    [0, 3, 5, 4],   # I IV vi V
]
MAJOR = [0, 2, 4, 5, 7, 9, 11]
TARGET_RMS = 0.11    # 배경음 크기(약 -19 dBFS). 목소리가 없으니 들릴 만큼, 거슬리지 않을 만큼
PEAK_LIMIT = 0.7


def _freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _chord(key: int, degree: int) -> list[int]:
    notes = []
    for step in (0, 2, 4):
        index = degree + step
        notes.append(key + MAJOR[index % 7] + 12 * (index // 7))
    return notes


def _tone(freq: float, n: int, harmonics=(1.0, 0.25, 0.08), detune: float = 0.0) -> np.ndarray:
    t = np.arange(n) / RATE
    out = np.zeros(n)
    for k, amp in enumerate(harmonics, start=1):
        out += amp * np.sin(2 * np.pi * freq * k * (1 + detune) * t)
    return out


def _envelope(n: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n)
    a, r = min(int(attack * RATE), n // 2), min(int(release * RATE), n // 2)
    if a:
        env[:a] = np.linspace(0, 1, a)
    if r:
        env[-r:] *= np.linspace(1, 0, r)
    return env


def _pluck(freq: float, seconds: float) -> np.ndarray:
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    decay = np.exp(-t / 0.28)
    attack = np.minimum(1, t / 0.004)
    return _tone(freq, n, harmonics=(1.0, 0.35, 0.12, 0.05)) * decay * attack


def _add(buffer: np.ndarray, start: int, signal: np.ndarray) -> None:
    """buffer[start:]에 signal을 더한다. 끝을 넘는 부분은 버린다."""
    end = min(len(buffer), start + len(signal))
    if end > start:
        buffer[start:end] += signal[: end - start]


def choose(seed: str) -> tuple[int, list[int]]:
    """초안 id 같은 문자열로 조와 진행을 고른다(같은 초안이면 늘 같은 곡)."""
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return KEYS[h % len(KEYS)], PROGRESSIONS[(h // 7) % len(PROGRESSIONS)]


def make_music(seconds: float, seed: str = "") -> np.ndarray:
    """seconds 길이의 스테레오 곡을 (샘플 수, 2) float 배열(-1~1)로 돌려준다."""
    key, progression = choose(seed)
    total = int(round(seconds * RATE))
    beat = 60 / BPM
    bar = beat * 4
    left = np.zeros(total)
    right = np.zeros(total)

    chord_index = 0
    start = 0.0
    while start < seconds:
        chord = _chord(key, progression[chord_index % len(progression)])
        s = int(start * RATE)
        # 패드: 한 마디 내내 부드럽게. 좌우를 살짝 다르게 어긋나게 해 넓게 들리게
        pad_n = int((bar + 0.4) * RATE)
        env = _envelope(pad_n, 0.35, 0.6)
        for note in chord:
            f = _freq(note - 12)
            _add(left, s, 0.16 * _tone(f, pad_n, detune=-0.0015) * env)
            _add(right, s, 0.16 * _tone(f, pad_n, detune=0.0015) * env)
        # 베이스: 마디 첫 박과 셋째 박
        for b in (0, 2):
            bs = s + int(b * beat * RATE)
            bass = _pluck(_freq(chord[0] - 24), beat * 2) * 0.55
            _add(left, bs, bass)
            _add(right, bs, bass)
        # 플럭: 8분음표 분산화음(위로 올라갔다 내려옴), 좌우로 번갈아
        pattern = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[1] + 24,
                   chord[2] + 12, chord[1] + 12, chord[0] + 12, chord[2]]
        for k, note in enumerate(pattern):
            ps = s + int(k * beat / 2 * RATE)
            p = _pluck(_freq(note), beat * 1.5) * (0.30 if k % 2 == 0 else 0.22)
            pan = 0.35 if k % 2 == 0 else -0.35
            _add(left, ps, p * (1 - pan) / 1.35)
            _add(right, ps, p * (1 + pan) / 1.35)
        start += bar
        chord_index += 1

    audio = np.stack([left, right], axis=1)
    audio *= _envelope(total, 0.25, 0.9)[:, None]   # 시작은 바로, 끝은 부드럽게 사라지게
    rms = float(np.sqrt(np.mean(audio ** 2))) or 1.0
    audio *= TARGET_RMS / rms
    peak = float(np.max(np.abs(audio))) or 1.0
    if peak > PEAK_LIMIT:
        audio *= PEAK_LIMIT / peak
    return audio


def write_wav(audio: np.ndarray, path: Path) -> Path:
    """16비트 48kHz 스테레오 WAV로 저장한다(ffmpeg가 AAC로 바꿔 영상에 넣는다)."""
    data = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())
    return path
