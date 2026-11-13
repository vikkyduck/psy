"""Measure actual journal appends and atomic publications on synthetic minute windows."""
from collections import Counter
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


DECODER = Path(__file__).resolve().parents[1] / 'transcribe.py'


class FileWrites:
    """Count application payload bytes before replacement and after durable journal appends."""

    def __init__(self):
        self.bytes = Counter()
        self.replacements = Counter()
        self.journal = None
        self.journal_size = 0
        self.committed_appends = 0
        self.replace = os.replace
        self.sync = os.fsync

    def replace_file(self, source, target):
        target = Path(target)
        info = Path(source).stat()
        self.replace(source, target)
        self.replacements[target.name] += 1
        self.bytes[target.name] += info.st_size
        if target.name == 'segments.jsonl':
            self.journal = (info.st_dev, info.st_ino)
            self.journal_size = info.st_size

    def sync_file(self, fd):
        self.sync(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == self.journal:
            if info.st_size < self.journal_size:
                raise AssertionError('The journal shrank during a fresh transcription')
            if info.st_size > self.journal_size:
                self.committed_appends += 1
            self.bytes['segments.jsonl'] += info.st_size - self.journal_size
            self.journal_size = info.st_size


def minute_line(number):
    # Unique 100-byte lines, the same size as the initial write-amplification workload.
    return f'minute-{number:04d}|' + 'x' * 88


def measure(minutes, decoder=DECODER):
    writes = FileWrites()
    with tempfile.TemporaryDirectory() as temp, ExitStack() as patches:
        root = Path(temp)
        src = root / 'in' / 'test.wav'
        out = root / 'out' / 'result.txt'
        model_dir = root / 'model'
        for directory in (src.parent, out.parent, model_dir):
            directory.mkdir()
        src.write_bytes(b'dummy')
        env = {
            'PSY_ASR_STANDALONE': '1', 'PSY_ASR_INPUT': str(src.parent),
            'PSY_ASR_OUTPUT': str(out.parent), 'PSY_WHISPER_MODEL': str(model_dir),
            'HERMES_HOME': temp, 'PSY_ASR_PROGRESS_FILE': str(root / 'progress.json'),
        }
        patches.enter_context(patch.dict(os.environ, env))
        patches.callback(os.umask, os.umask(0o077))
        fake = ModuleType('faster_whisper')
        fake.WhisperModel = Mock()
        patches.enter_context(patch.dict(sys.modules, faster_whisper=fake))
        main = runpy.run_path(str(decoder))['main']

        def window(model, source, cursor, duration, language, previous):
            return cursor + 60, [minute_line(int(cursor // 60))], []

        patches.enter_context(patch.dict(main.__globals__, {
            'audio_duration': lambda unused: minutes * 60, 'transcribe_window': window,
        }))
        patches.enter_context(patch.object(sys, 'argv', ['asr', str(src), '--output', str(out)]))
        patches.enter_context(patch.object(os, 'replace', writes.replace_file))
        patches.enter_context(patch.object(os, 'fsync', writes.sync_file))
        with redirect_stdout(io.StringIO()) as response:
            main()
        result = json.loads(response.getvalue())
        archive = Path(result['saved_transcript'])
        expected = ''.join(minute_line(i) + '\n' for i in range(minutes)).encode()
        if not result['complete'] or result['chunks'] != minutes:
            raise AssertionError('Incomplete decoder result')
        if archive.read_bytes() != expected or out.read_bytes() != expected:
            raise AssertionError('Saved transcript or caller export lost ordered text')
        journal = archive.with_name('segments.jsonl')
        if journal.exists():
            if writes.bytes[journal.name] != journal.stat().st_size:
                raise AssertionError('Journal size and observed writes diverged')
            markers = re.findall(rb'minute-\d{4}\|', journal.read_bytes())
            if markers != [f'minute-{i:04d}|'.encode() for i in range(minutes)]:
                raise AssertionError('Journal does not contain exactly one copy of each segment')
        return writes


class TranscriptWriteVolume(unittest.TestCase):
    def test_doubling_duration_keeps_writes_linear_and_exports_once(self):
        totals = []
        for minutes in (120, 240):
            with self.subTest(minutes=minutes):
                writes = measure(minutes)
                self.assertGreater(writes.bytes['segments.jsonl'], 0)
                self.assertEqual(writes.committed_appends, minutes)
                for name in ('transcript.txt', 'result.txt'):
                    self.assertEqual(writes.replacements[name], 1)
                    self.assertEqual(writes.bytes[name], minutes * 101)
                self.assertEqual(writes.replacements['progress.json'], minutes)
                totals.append(sum(writes.bytes.values()))
        if len(totals) == 2:
            self.assertLess(totals[1], totals[0] * 2.1)
