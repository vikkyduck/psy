"""Run the real transcript writer across process death using three synthetic audio windows."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch


DECODER = Path(__file__).resolve().parents[1] / "transcribe.py"
DURATION = 143.5
WINDOWS = (0.0, 63.125, 123.125, DURATION)
LINES = (["[00:00:01] Первая строка.", "[00:01:02] Граница окна."],
         [], ["[00:02:11] Финал."])
TEXT = ("\n".join(line for window in LINES for line in window) + "\n").encode()
NO_SPEECH = "[Разборчивая речь в записи не обнаружена.]\n".encode()


def tail_after(window):
    end = WINDOWS[window + 1]
    return [[f"boundary{window}", end - 1.0, end]] if LINES[window] else []


def source_identity(root):
    src = root / "input.audio"
    stat = src.stat()
    # This is the identity created by the shipped v3 audio decoder.
    return [str(src), stat.st_size, stat.st_mtime_ns, "ru", str(root / "model"), "vad-overlap-v3"]


def private_folder(root):
    tag = hashlib.sha256(json.dumps(source_identity(root)).encode()).hexdigest()[:24]
    return root / "media-vault" / "transcripts" / tag


def write_legacy(root, count):
    folder = private_folder(root)
    folder.mkdir(parents=True, mode=0o700)
    state = {"source": source_identity(root), "duration": DURATION, "next_start": WINDOWS[count],
             "complete": count == len(LINES), "word_tail": tail_after(count - 1), "updated_at": 12345,
             "chunks": [{"start": WINDOWS[i], "lines": LINES[i]} for i in range(count)]}
    checkpoint = folder / "checkpoint.json"
    checkpoint.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    checkpoint.chmod(0o600)
    (folder / "transcript.txt").write_bytes(b"stale private transcript\n")
    (root / "result.txt").write_bytes(b"stale export\n")


def worker(root, mode):
    os.environ.update(HOME=str(root), HERMES_HOME=str(root), PSY_ASR_STANDALONE="1",
                      PSY_ASR_INPUT=str(root), PSY_ASR_OUTPUT=str(root),
                      PSY_WHISPER_MODEL=str(root / "model"), PSY_ASR_PROGRESS_FILE=str(root / "progress.json"))
    spec = importlib.util.spec_from_file_location("transcript_under_test", DECODER)
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)

    def window(_model, _src, start, duration, _language, previous):
        index = WINDOWS.index(start)
        with (root / "calls").open("a", encoding="ascii") as log:
            log.write(str(index) + "\n")
        assert duration == DURATION
        expected = [] if not index or mode == "silence" else tail_after(index - 1)
        assert previous == expected, (index, previous, expected)
        if mode == "kill-in-window" and index == 1:
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "silence":
            return WINDOWS[index + 1], [], []
        return WINDOWS[index + 1], list(LINES[index]), tail_after(index)

    def whisper(*args, **kwargs):
        if mode == "completed-only":
            raise AssertionError("Already-completed transcript loaded the decoder")
        return object()

    original_replace = os.replace

    def replace(src, dst):
        if mode == "kill-before-export" and Path(dst) == root / "result.txt":
            os.kill(os.getpid(), signal.SIGKILL)
        if mode == "kill-before-progress" and Path(dst) == root / "progress.json":
            if json.loads(Path(src).read_bytes())["processed_seconds"] == WINDOWS[1]:
                os.kill(os.getpid(), signal.SIGKILL)
        if mode == "kill-at-migration" and Path(dst) == private_folder(root) / "segments.jsonl":
            os.kill(os.getpid(), signal.SIGKILL)
        original_replace(src, dst)

    stub = types.ModuleType("faster_whisper")
    stub.WhisperModel = whisper
    argv = [str(DECODER), str(root / "input.audio"), "--output", str(root / "result.txt")]
    with patch.dict(sys.modules, {"faster_whisper": stub}), patch.object(sys, "argv", argv), \
            patch.object(decoder, "audio_duration", return_value=DURATION), \
            patch.object(decoder, "transcribe_window", side_effect=window), patch.object(os, "replace", replace):
        decoder.main()


class TranscriptRecovery(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "model").mkdir()
        (self.root / "input.audio").write_bytes(b"synthetic audio")

    def child(self, mode="normal", *, killed=False, failed=False):
        result = subprocess.run([sys.executable, "-I", "-B", __file__, "--worker", str(self.root), mode],
                                capture_output=True, text=True, timeout=15)
        if failed:
            self.assertNotEqual(result.returncode, 0)
            return
        self.assertEqual(result.returncode, -signal.SIGKILL if killed else 0, result.stderr)
        if not killed:
            return json.loads(result.stdout)

    def calls(self):
        log = self.root / "calls"
        return [int(n) for n in log.read_text().splitlines()] if log.exists() else []

    def check_output(self, meta, *, silent=False):
        expected = NO_SPEECH if silent else TEXT
        for key, target in (("path", self.root / "result.txt"),
                            ("saved_transcript", private_folder(self.root) / "transcript.txt")):
            self.assertEqual(Path(meta[key]), target)
            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue(meta["complete"])
        self.assertEqual(meta["duration"], DURATION)
        self.assertEqual(meta["chunks"], len(LINES))
        self.assertEqual(meta["speech_detected"], not silent)

    def test_interrupted_window_restarts_at_recorded_audio_position_with_its_word_tail(self):
        self.child("kill-in-window", killed=True)
        self.assertEqual(self.calls(), [0, 1])
        self.check_output(self.child())
        # The interrupted window may run again. Previously committed audio must not.
        self.assertEqual(self.calls(), [0, 1, 1, 2])

    def test_all_committed_but_export_interrupted(self):
        self.child("kill-before-export", killed=True)
        self.assertFalse((self.root / "result.txt").exists())
        self.check_output(self.child("completed-only"))
        self.assertEqual(self.calls(), [0, 1, 2])

    def test_committed_window_survives_missing_progress_and_torn_next_append(self):
        self.child("kill-before-progress", killed=True)
        self.assertEqual(self.calls(), [0])
        log = private_folder(self.root) / "segments.jsonl"
        committed = log.read_bytes()
        self.assertTrue(committed.endswith(b"\n"))
        # A torn write can end midway through a UTF-8 character.
        with log.open("ab") as stream:
            stream.write(b'{"start":63.125,"end":123.125,"lines":["\xd0')
        self.check_output(self.child())
        self.assertEqual(self.calls(), [0, 1, 2])
        self.assertTrue(log.read_bytes().startswith(committed))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_corruption_in_a_committed_record_is_never_published(self):
        self.child("kill-before-progress", killed=True)
        log = private_folder(self.root) / "segments.jsonl"
        with log.open("ab") as stream:
            stream.write(b'{"start":63.125,}\n')
        damaged = log.read_bytes()
        self.child(failed=True)
        self.assertFalse((self.root / "result.txt").exists())
        self.assertEqual(self.calls(), [0])
        self.assertEqual(log.read_bytes(), damaged)

    def test_legacy_partial_resumes_without_redecoding_saved_lines(self):
        write_legacy(self.root, 1)
        self.check_output(self.child())
        self.assertEqual(self.calls(), [1, 2])
        self.check_output(self.child("completed-only"))
        self.assertEqual(self.calls(), [1, 2])

    def test_completed_legacy_checkpoint_repairs_stale_text_and_export(self):
        write_legacy(self.root, len(LINES))
        self.check_output(self.child("completed-only"))
        self.assertEqual(self.calls(), [])

    def test_interrupted_migration_leaves_legacy_checkpoint_available_for_retry(self):
        write_legacy(self.root, 1)
        checkpoint = private_folder(self.root) / "checkpoint.json"
        saved = checkpoint.read_bytes()
        self.child("kill-at-migration", killed=True)
        self.assertEqual(saved, checkpoint.read_bytes())
        self.check_output(self.child())
        self.assertEqual(self.calls(), [1, 2])

    def test_silent_windows_are_counted_and_rendered_once(self):
        self.check_output(self.child("silence"), silent=True)
        self.check_output(self.child("completed-only"), silent=True)
        self.assertEqual(self.calls(), [0, 1, 2])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
