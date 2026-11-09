"""릴스 배경음 (2026-11-09): 코드로 만든 곡의 길이·크기·초안별 변화."""

import wave

import numpy as np

from newsbot.design import music


def test_music_length_level_and_no_clipping():
    audio = music.make_music(12.5, "https://a.kr/1")
    assert audio.shape == (int(12.5 * music.RATE), 2)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    assert 0.08 < rms < 0.14 and float(np.max(np.abs(audio))) <= music.PEAK_LIMIT + 1e-6
    assert abs(audio[-10:]).max() < 0.01, "끝은 부드럽게 사라진다"
    assert not np.allclose(audio[:, 0], audio[:, 1]), "좌우가 조금 달라 넓게 들린다"


def test_same_seed_same_song_and_seeds_vary():
    assert np.array_equal(music.make_music(3, "x"), music.make_music(3, "x"))
    picks = [music.choose(f"seed-{n}") for n in range(40)]
    assert len({key for key, _ in picks}) > 2 and len({tuple(p) for _, p in picks}) > 2


def test_write_wav(tmp_path):
    path = music.write_wav(music.make_music(1.0, "x"), tmp_path / "m.wav")
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (2, 2, 48000, 48000)
