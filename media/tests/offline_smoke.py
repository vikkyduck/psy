"""Run the deployed Linux decoder boundary on two seconds of generated silence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

MEDIA = Path(__file__).resolve().parent.parent


def verify_model(directory):
    for entry in (MEDIA / 'model.sha256').read_text().splitlines():
        expected, name = entry.split()
        with (directory / name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise RuntimeError('Unexpected model file digest: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, default=Path('/opt/psy-asr-model'))
    args = parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid() == 0:
        parser.error('Use a non-root Linux user with Landlock ABI >= 3')
    model = args.model.resolve(strict=True)
    verify_model(model)
    with tempfile.TemporaryDirectory(prefix='psy-offline-') as temp:
        base = Path(temp)
        source = base / 'silence.wav'
        job = base / 'job'
        job.mkdir(mode=0o700)
        attempt = job / 'attempt-smoke'
        attempt.mkdir(mode=0o700)
        output = attempt / 'result.txt'
        with wave.open(str(source), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(2 * 16000 * 2))
        env = dict(PATH='/usr/bin:/bin', HOME=str(job), HERMES_HOME=str(job),
                   TMPDIR=str(attempt), PSY_WHISPER_MODEL=str(model), PSY_ASR_STANDALONE='1',
                   PSY_ASR_INPUT=str(base), PSY_ASR_OUTPUT=str(attempt),
                   PSY_ASR_PROGRESS_FILE=str(attempt / 'progress.json'), HF_HUB_OFFLINE='1',
                   OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        run = subprocess.run([sys.executable, '-I', str(MEDIA/'asr_sandbox.py'),
                              str(source), str(output), 'auto', str(job)], cwd=job, env=env,
                             capture_output=True, text=True, timeout=300)
        if run.returncode:
            raise RuntimeError(run.stderr[-1500:])
        result = json.loads(run.stdout.splitlines()[-1])
        if (result.get('complete') is not True or result.get('speech_detected') is not False
                or abs(result['duration'] - 2.0) > .05 or result['source_bytes'] != source.stat().st_size
                or 'речь' not in output.read_text() or result['chunks'] != 1):
            raise RuntimeError('Invalid result for generated silence')
        print('Offline decoder passed: 2 seconds, 1 checkpoint, no speech, UTF-8 transcript')


if __name__ == '__main__':
    main()
