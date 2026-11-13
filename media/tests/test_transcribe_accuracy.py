"""Offline ASR checks. Pass a synthetic reference transcript to also score its two known phrases."""
import argparse
import collections
import importlib.util
import os
from pathlib import Path
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent/'transcribe.py'
spec = importlib.util.spec_from_file_location('transcribe_under_test', SOURCE)
decoder = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {'PSY_ASR_STANDALONE': '1'}):
    spec.loader.exec_module(decoder)

LINE = re.compile(r'^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.+)$')
PHRASES = (
    (57.5, 65.0, {
        'Москва': r'\bмоскв\w*', '47': r'\b(?:47|сорок\s+семь)\b', 'Казань': r'\bказан\w*'}),
    (117.5, 124.0, {
        'green': r'\bgreen\b', 'train': r'\btrain\b', 'London': r'\blondon\b', '9': r'\b(?:nine|9)\b'}),
)


def score_reference(transcript):
    lines = []
    for line in transcript.splitlines():
        match = LINE.fullmatch(line)
        if not match:
            raise ValueError('Expected timestamped reference transcript')
        hour, minute, second, text = match.groups()
        lines.append((int(hour) * 3600 + int(minute) * 60 + int(second), text.casefold()))
    timed_results = []
    for expected, end, terms in PHRASES:
        related = [(time, text) for time, text in lines if expected - 2.0 <= time <= end]
        words = ' '.join(text for _, text in related)
        # Count one occurrence per control term to catch a repeated boundary phrase.
        found = [name for name, pattern in terms.items() if len(re.findall(pattern, words)) == 1]
        actual = related[0][0] if related else None
        timed_results.append((found, len(terms), actual, expected))
    outside = sum(not any(expected - 2 <= time <= end for expected, end, _ in PHRASES) for time, _ in lines)
    return timed_results, outside


class WindowTests(unittest.TestCase):
    def test_overlapping_boundary_repeat_needs_text_and_time(self):
        previous = [['one', 58.9, 59.7], ['two', 59.7, 60.2]]
        repeated = [(' one', 59.0, 59.8, 1), (' two', 59.8, 60.1, 1)]
        next_words = [(' two', 60.4, 60.8, 1)]
        self.assertEqual(decoder.drop_boundary_echo(previous, repeated + next_words), next_words)
        self.assertEqual(decoder.drop_boundary_echo(previous, next_words), next_words)

    def test_continuous_speech_at_minute_belongs_to_one_block(self):
        spoken = [(58.8, 59.3, ' before'), (59.6, 60.5, ' boundary'), (60.7, 61.2, ' after')]
        sampled_at = []
        class Audio:
            def __init__(self, begin, end):
                self.begin = begin
                self.size = round((end - begin) * decoder.SAMPLE_RATE)
            def __len__(self):
                return self.size
            def __getitem__(self, selection):
                return self.begin + selection.start / decoder.SAMPLE_RATE
        def transcribe(offset, **options):
            sampled_at.append(offset)
            words = [SimpleNamespace(word=word, start=start-offset, end=end-offset)
                     for start, end, word in spoken if start >= offset]
            return [SimpleNamespace(words=words, no_speech_prob=0, avg_logprob=0)], None
        model = SimpleNamespace(transcribe=transcribe)
        with patch.object(decoder, 'read_window', side_effect=lambda _src, a, b: Audio(a, b)):
            with patch.object(decoder, 'speech_ranges', side_effect=lambda a: [{'start': 0, 'end': len(a)}]):
                committed, first, tail = decoder.transcribe_window(model, None, 0, 125, None, [])
                _, second, _ = decoder.transcribe_window(model, None, committed, 125, None, tail)
        self.assertEqual(committed, 60)
        self.assertEqual(sampled_at, [0, 56])
        counts = collections.Counter(re.findall(r'[a-z]+', ' '.join(first + second)))
        self.assertEqual(counts, {'before': 1, 'boundary': 1, 'after': 1})

    def test_silence_skips_decoder(self):
        def unexpected(*args, **kwargs):
            self.fail('Decoder invoked for silence')
        model = SimpleNamespace(transcribe=unexpected)
        with patch.object(decoder, 'read_window', return_value=[]):
            with patch.object(decoder, 'speech_ranges', return_value=[]):
                end, lines, tail = decoder.transcribe_window(model, None, 60, 65, None, [])
        self.assertEqual((end, lines, tail), (65, [], []))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transcript', type=Path)
    options = parser.parse_args()
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(WindowTests)
    passed = unittest.TextTestRunner().run(tests).wasSuccessful()
    if options.transcript:
        scores, outside = score_reference(options.transcript.read_text())
        for found, total, actual, expected in scores:
            print(f'{len(found)}/{total} controls, expected {expected}s, first timestamp {actual}s')
            passed &= len(found) == total and actual is not None and abs(actual - expected) <= 2
        print(f'{outside} lines outside test speech')
        passed &= outside == 0
    sys.exit(not passed)
