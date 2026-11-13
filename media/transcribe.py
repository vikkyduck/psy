"""Sequential local Whisper transcription with durable, resumable private output."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from itertools import groupby
import json
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

standalone = os.environ.get('PSY_ASR_STANDALONE') == '1'
home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
if not standalone:
    sys.path.insert(0, str(home / 'hermes-agent'))
    import hermes_bootstrap

FORMATS = 'wav,mp3,mov,matroska,webm,ogg,flac,aac,asf,aiff,caf,mpeg'
MAX_SECONDS = 24 * 3600
SAMPLE_RATE = 16000
CHUNK_SECONDS = 60
LEFT_CONTEXT = 4
LOOKAHEAD = 8
DECODER_VERSION = 'vad-overlap-v3'


def audio_duration(src):
    inputs = ['-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMATS, '-i', str(src)]
    info = json.loads(subprocess.check_output([
        find_program('ffprobe'), '-v', 'error', *inputs, '-select_streams', 'a:0',
        '-show_entries', 'stream=duration:format=duration', '-of', 'json'], timeout=60))
    streams = info.get('streams', [])
    if not streams:
        raise ValueError('No audio stream')
    for item in (streams[0], info.get('format', {})):
        try:
            duration = float(item.get('duration', ''))
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    # Streaming WebM may have no header duration. Count decoded audio on a null sink.
    run = subprocess.run([find_program('ffmpeg'), '-nostdin', '-v', 'error', *inputs,
                          '-map', '0:a:0', '-t', str(MAX_SECONDS+1), '-progress', 'pipe:1',
                          '-f', 'null', '-'], capture_output=True, check=True, timeout=1800)
    times = [int(line.split('=',1)[1]) for line in run.stdout.decode().splitlines()
             if line.startswith('out_time_us=') and line.split('=',1)[1].isdigit()]
    if not times or max(times) <= 0:
        raise ValueError('Invalid audio duration')
    return max(times)/1_000_000


def find_program(name):
    resolved = shutil.which(name)
    if resolved:
        return resolved
    # Hermes installs ffmpeg as a private runtime, which is not always in systemd's PATH.
    candidates = sorted((home / 'tools').glob('ffmpeg-*/bin/' + name))
    if candidates:
        return str(candidates[-1])
    raise RuntimeError(name + ' is not installed')


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_journal_directory(path):
    # Persist each new directory entry, including the job folder itself, before
    # acknowledging any segment. The parent may already exist after a retry.
    if not path.is_dir():
        create_journal_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        sync_directory(path.parent)


@contextmanager
def atomic_output(path):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as out:
            yield out
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_text(path, text):
    with atomic_output(path) as out:
        out.write(text.encode('utf-8'))


def write_record(out, record):
    data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    out.write((data + '\n').encode('utf-8'))


def valid_position(value):
    return type(value) in (float, int) and math.isfinite(value)


def validate_segment(segment, cursor, duration):
    start, end = segment['start'], segment['end']
    if not (valid_position(start) and start == cursor and valid_position(end) and start < end <= duration):
        raise ValueError('Invalid segment positions')
    lines, tail = segment['lines'], segment['word_tail']
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError('Invalid segment text')
    if not isinstance(tail, list) or len(tail) > 12 or not all(
            isinstance(word, list) and len(word) == 3 and isinstance(word[0], str)
            and valid_position(word[1]) and valid_position(word[2]) for word in tail):
        raise ValueError('Invalid decoder overlap')


def prepare_journal(path, checkpoint, src, fingerprint):
    """Publish a complete journal before retiring the previous checkpoint format."""
    if path.exists():
        return
    legacy = json.loads(checkpoint.read_bytes()) if checkpoint.exists() else None
    if legacy is not None and legacy['source'] != fingerprint:
        raise ValueError('Checkpoint belongs to a different recording')
    duration = legacy['duration'] if legacy is not None else audio_duration(src)
    if not valid_position(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('Invalid or unsupported recording duration')

    with atomic_output(path) as out:
        write_record(out, {'version': 1, 'source': fingerprint, 'duration': duration})
        if legacy is not None:
            cursor = 0
            chunks = legacy['chunks']
            for i, chunk in enumerate(chunks):
                last = i + 1 == len(chunks)
                end = legacy['next_start'] if last else chunks[i + 1]['start']
                segment = {'start': chunk['start'], 'end': end, 'lines': chunk['lines'],
                           'word_tail': legacy.get('word_tail', []) if last else []}
                validate_segment(segment, cursor, duration)
                write_record(out, segment)
                cursor = end
            if cursor != legacy['next_start'] or (cursor == duration) != legacy['complete']:
                raise ValueError('Inconsistent legacy checkpoint')


def resume_journal(journal, fingerprint):
    header = json.loads(journal.readline())
    if header.get('version') != 1 or header.get('source') != fingerprint:
        raise ValueError('Unexpected segment journal')
    duration = header['duration']
    if not valid_position(duration) or not 0 < duration <= MAX_SECONDS:
        raise ValueError('Invalid journal duration')
    cursor, tail, count = 0, [], 0
    while True:
        offset = journal.tell()
        raw = journal.readline()
        if not raw:
            break
        if not raw.endswith(b'\n'):
            # A crash can leave only a prefix of the last append, including half a UTF-8 character.
            journal.seek(offset)
            journal.truncate()
            journal.flush()
            os.fsync(journal.fileno())
            break
        segment = json.loads(raw)
        validate_segment(segment, cursor, duration)
        cursor, tail = segment['end'], segment['word_tail']
        count += 1
    return duration, cursor, tail, count


def export_journal(journal, transcript, requested):
    """Build the final timestamped TXT one segment at a time, including after a crash."""
    journal.seek(0)
    journal.readline()
    speech = False
    with atomic_output(transcript) as out:
        for raw in journal:
            for line in json.loads(raw)['lines']:
                out.write((line + '\n').encode('utf-8'))
                speech = True
        if not speech:
            out.write('[Разборчивая речь в записи не обнаружена.]\n'.encode('utf-8'))
    if requested != transcript:
        with transcript.open('rb') as source, atomic_output(requested) as target:
            shutil.copyfileobj(source, target)
    return speech


def read_window(src, begin, end):
    import numpy as np
    result = subprocess.run([
        find_program('ffmpeg'), '-nostdin', '-v', 'error', '-threads', '1',
        '-ss', f'{begin:.6f}', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', FORMATS, '-i', str(src), '-t', f'{end - begin:.6f}',
        '-map', '0:a:0', '-vn', '-ar', str(SAMPLE_RATE), '-ac', '1', '-f', 'f32le', 'pipe:1',
    ], check=True, timeout=180, stdout=subprocess.PIPE)
    return np.frombuffer(result.stdout, dtype='<f4')


def speech_ranges(audio):
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    if not len(audio) or not np.any(audio):
        return []
    vad = VadOptions(threshold=0.35, min_speech_duration_ms=120,
                     min_silence_duration_ms=900, speech_pad_ms=350)
    return get_speech_timestamps(audio, vad)


def normalized(word):
    return ''.join(letter for letter in word.casefold() if letter.isalnum())


def drop_boundary_echo(previous, words):
    """Remove only an acoustically overlapping, matching suffix from the previous window."""
    for size in range(min(len(previous), len(words), 12), 0, -1):
        matched = list(zip(previous[-size:], words[:size]))
        if all(old[0] and old[0] == normalized(new[0]) and
               min(old[2], new[2]) > max(old[1], new[1])
               for old, new in matched):
            return words[size:]
    return words


def transcribe_window(model, src, cursor, duration, language, previous):
    begin = max(0.0, cursor - LEFT_CONTEXT)
    nominal_end = min(cursor + CHUNK_SECONDS, duration)
    window_end = min(duration, nominal_end + LOOKAHEAD)
    audio = read_window(src, begin, window_end)
    ranges = speech_ranges(audio)
    # Finish a short utterance that crosses the minute boundary. For continuous speech,
    # both calls hear the overlap and midpoint ownership assigns each word to one output.
    commit = nominal_end
    for index, item in enumerate(ranges):
        speech_start = begin + item['start'] / SAMPLE_RATE
        speech_end = begin + item['end'] / SAMPLE_RATE
        if (speech_start < nominal_end < speech_end
                and (window_end == duration or item['end'] < len(audio))):
            next_voice = (begin + ranges[index + 1]['start'] / SAMPLE_RATE
                          if index + 1 < len(ranges) else window_end)
            # VAD padding can shift by a few frames when the next window starts. Commit
            # within the intervening silence so the end of this phrase is not decoded again.
            commit = min(window_end, speech_end + min(0.6, (next_voice - speech_end) / 2))
            break

    # Nearby phrases of the same conversational turn can share a Whisper context. Keeping
    # each merge below 28 s bounds the extra silence decoded on pause-heavy recordings.
    utterances = []
    for item in ranges:
        if (utterances and item['start'] - utterances[-1][1] < 2 * SAMPLE_RATE
                and item['end'] - utterances[-1][0] <= 28 * SAMPLE_RATE):
            utterances[-1] = (utterances[-1][0], item['end'])
        else:
            utterances.append((item['start'], item['end']))

    words = []
    segment_id = 0
    for first, last in utterances:
        offset = begin + first / SAMPLE_RATE
        if offset >= commit or begin + last / SAMPLE_RATE <= cursor:
            continue
        # Detect the language on actual speech from this utterance. Supplying long silence
        # to Whisper here corrupts both its language decision and its first timestamp.
        segments, _ = model.transcribe(
            audio[first:last], language=language, beam_size=5, temperature=0,
            word_timestamps=True, condition_on_previous_text=True, vad_filter=False,
            no_repeat_ngram_size=8,
        )
        for segment in segments:
            segment_id += 1
            if segment.no_speech_prob > 0.6 and segment.avg_logprob < -1:
                continue
            candidates = [(w.word, w.start, w.end) for w in (segment.words or [])]
            if not candidates and segment.text.strip():
                candidates = [(segment.text, segment.start, segment.end)]
            for text, start, end in candidates:
                start = max(0.0, offset + float(start))
                end = min(duration, max(start, offset + float(end)))
                midpoint = (start + end) / 2
                if cursor <= midpoint < commit and text.strip():
                    words.append((text, start, end, segment_id))
    words = drop_boundary_echo(previous, words)
    lines = []
    for _, group in groupby(words, key=lambda word: word[3]):
        phrase = list(group)
        second = int(phrase[0][1])
        text = ''.join(word[0] for word in phrase).strip()
        lines.append(f'[{second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}] {text}')
    tail = (previous + [[normalized(w), a, b] for w, a, b, _ in words])[-12:]
    return commit, lines, [word for word in tail if word[2] >= commit - LEFT_CONTEXT]


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument('input')
    p.add_argument('--output', required=True)
    p.add_argument('--language', default='ru')
    args = p.parse_args()
    src = Path(args.input).expanduser().resolve(strict=True)
    requested = Path(args.output).expanduser().resolve()
    if not src.is_file() or src == requested:
        raise ValueError('Input must be a file and output must not overwrite it')
    if standalone:
        if (src.parent != Path(os.environ['PSY_ASR_INPUT']).resolve(strict=True)
                or requested.parent != Path(os.environ['PSY_ASR_OUTPUT']).resolve(strict=True)):
            raise ValueError('ASR source or destination outside configured storage')
        model_path = Path(os.environ['PSY_WHISPER_MODEL'])
        if not model_path.is_absolute() or not model_path.is_dir():
            raise ValueError('Local Whisper model missing')
    else:
        from agent.file_safety import get_read_block_error
        if get_read_block_error(str(src)):
            raise ValueError('Protected source is not audio input')
        model_path = os.environ.get('PSY_WHISPER_MODEL', '/opt/psy-asr-model')
    language = None if args.language in ('auto', '') else args.language
    fingerprint = [str(src), src.stat().st_size, src.stat().st_mtime_ns, language, str(model_path), DECODER_VERSION]
    job_id = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()[:24]
    folder = home / 'media-vault' / 'transcripts' / job_id
    create_journal_directory(folder)
    checkpoint = folder / 'checkpoint.json'
    journal_path = folder / 'segments.jsonl'
    transcript = folder / 'transcript.txt'
    lockdir = home / 'cache' / 'scratch'
    lockdir.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = lockdir / 'audio-transcription.lock'
    progress = os.environ.get('PSY_ASR_PROGRESS_FILE') if standalone else None
    if requested in (checkpoint, journal_path, lock_path) or (progress and requested == Path(progress).resolve()):
        raise ValueError('Output conflicts with internal decoder state')

    def report_progress(cursor, duration):
        if progress:
            atomic_text(Path(progress), json.dumps({'processed_seconds': cursor, 'total_seconds': duration}))

    # Serialize retries so that a single writer owns the append and its cursor.
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prepare_journal(journal_path, checkpoint, src, fingerprint)
        with journal_path.open('r+b') as journal:
            duration, start, tail, count = resume_journal(journal, fingerprint)
            if checkpoint.exists():
                checkpoint.unlink()
                sync_directory(folder)
            if start:
                report_progress(start, duration)
            if start < duration:
                from faster_whisper import WhisperModel
                model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                                     cpu_threads=1, num_workers=1, local_files_only=True)
                while start < duration:
                    end, lines, tail = transcribe_window(model, src, start, duration, language, tail)
                    segment = {'start': start, 'end': end, 'lines': lines, 'word_tail': tail}
                    validate_segment(segment, start, duration)
                    write_record(journal, segment)
                    journal.flush()
                    os.fsync(journal.fileno())
                    start = end
                    count += 1
                    report_progress(start, duration)
            speech = export_journal(journal, transcript, requested)
        print(json.dumps({'complete': True, 'speech_detected': speech, 'path': str(requested),
                          'saved_transcript': str(transcript), 'duration': duration,
                          'source_bytes': src.stat().st_size, 'chunks': count}, ensure_ascii=False))


if __name__ == '__main__':
    main()
