from __future__ import annotations

import math
import struct

import pytest

from custom_components.tacktab.voice_realtime_providers import RealtimeProviderError, StatefulResampler


def pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def samples(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 2}h", data))


def convert_chunks(source: list[int], sizes: list[int]) -> list[int]:
    resampler = StatefulResampler()
    output: list[int] = []
    offset = 0
    for size in sizes:
        output.extend(samples(resampler.convert(pcm(source[offset : offset + size]))))
        offset += size
    assert offset == len(source)
    output.extend(samples(resampler.flush()))
    return output


def test_phase_grid_interpolates_the_exact_ramp_and_holds_final_endpoint() -> None:
    resampler = StatefulResampler()
    assert samples(resampler.convert(pcm([0, 600, 1200, 1800]))) == [0, 400, 800, 1200, 1600]
    assert samples(resampler.flush()) == [1800]


def test_irregular_chunks_are_bit_identical_to_one_chunk() -> None:
    source = [((index * 683) % 60_000) - 30_000 for index in range(37)]
    assert convert_chunks(source, [37]) == convert_chunks(source, [1, 4, 2, 9, 3, 8, 10])


def test_tone_duration_and_shape_are_preserved_across_chunks() -> None:
    source = [round(20_000 * math.sin(2 * math.pi * 440 * index / 16_000)) for index in range(1601)]
    output = convert_chunks(source, [17, 1, 511, 73, 999])
    assert len(output) == (3 * len(source)) // 2
    assert max(abs(value) for value in output) >= 19_000
    assert output == convert_chunks(source, [len(source)])


def test_odd_and_single_frame_turns_do_not_lose_audio() -> None:
    assert convert_chunks([0, 600, 1200], [3]) == [0, 400, 800, 1200]
    assert convert_chunks([42], [1]) == [42]


def test_flush_is_idempotent_after_the_held_tail() -> None:
    resampler = StatefulResampler()
    resampler.convert(pcm([0, 600]))
    assert samples(resampler.flush()) == [600]
    assert resampler.flush() == b""


def test_rejects_partial_pcm_frame() -> None:
    with pytest.raises(RealtimeProviderError):
        StatefulResampler().convert(b"\x00")
