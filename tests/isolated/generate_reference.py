#!/usr/bin/env python3
"""Generate synthetic speech larger than 20 MiB with already installed local tools.

Example: --fixture-dir /var/tmp/psy-audio-fixture --output /var/tmp/psy-audio-fixture/reference.wav
No network, model download or participant recording is used. Generated files stay outside Git.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave


REFERENCE = Path(__file__).with_name('reference.txt')


def validate_destination(fixture_dir, output):
    """Require a named private scratch directory, a direct WAV child and no overwrite."""
    root = Path(fixture_dir).expanduser().resolve()
    bases = {Path('/tmp').resolve(), Path('/var/tmp').resolve()}
    system_temp = Path(tempfile.gettempdir()).resolve()
    # macOS per-user scratch lives here. An arbitrary inherited TMPDIR must
    # never turn a production application directory into an allowed base.
    if sys.platform == 'darwin' and system_temp.is_relative_to(Path('/private/var/folders')):
        bases.add(system_temp)
    if not any(root != base and root.is_relative_to(base) for base in bases):
        raise ValueError('The fixture directory must be a child of the operating system temp directory')
    if Path(fixture_dir).is_symlink():
        raise ValueError('The fixture directory must not be a symbolic link')
    target = Path(output).expanduser()
    if not target.is_absolute() or target.parent.resolve() != root or target.suffix.lower() != '.wav':
        raise ValueError('Output must be one absolute WAV path directly inside the declared fixture directory')
    if target.exists() or target.is_symlink():
        raise ValueError('Refusing to replace an existing fixture file')
    if root.exists():
        stat = root.stat()
        if not root.is_dir() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError('The fixture directory must be owned by the current user with mode 700')
    return root, root / target.name


def find_tools():
    ffmpeg = shutil.which('ffmpeg')
    speech = '/usr/bin/say' if sys.platform == 'darwin' and Path('/usr/bin/say').is_file() else shutil.which('espeak-ng')
    if not ffmpeg or not speech:
        raise RuntimeError('Already installed ffmpeg and macOS say or espeak-ng are required. Nothing was installed')
    return ffmpeg, speech


def generate(fixture_dir, output):
    root, target = validate_destination(fixture_dir, output)
    ffmpeg, speech = find_tools()
    phrase = REFERENCE.read_text(encoding='utf-8').strip()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Recheck after creating the directory, before any file-producing subprocess.
    root, target = validate_destination(root, target)
    with tempfile.TemporaryDirectory(prefix='speech-', dir=root) as temporary:
        work = Path(temporary)
        spoken = work / ('speech.aiff' if Path(speech).name == 'say' else 'speech.wav')
        if Path(speech).name == 'say':
            command = [speech, '-v', 'Samantha', '-o', str(spoken), phrase]
        else:
            command = [speech, '-v', 'en', '-s', '165', '-w', str(spoken), phrase]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        rendered = work / 'reference.wav'
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i', str(spoken), '-af', 'apad',
                        '-t', '40', '-ar', '192000', '-ac', '2', '-c:a', 'pcm_s16le', str(rendered)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        with wave.open(str(rendered), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) != (192000, 2, 2, 7680000):
                raise RuntimeError('Generated audio failed the duration and PCM format check')
        if rendered.stat().st_size <= 20 * 1024 ** 2:
            raise RuntimeError('Generated audio is not larger than 20 MiB')
        # O_EXCL prevents a concurrent file creation from being overwritten.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as destination, rendered.open('rb') as source:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return target.stat().st_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-dir', type=Path, required=True, help='private scratch directory in /tmp or /var/tmp')
    parser.add_argument('--output', type=Path, required=True, help='new WAV file directly inside --fixture-dir')
    args = parser.parse_args()
    try:
        size = generate(args.fixture_dir, args.output)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Never print subprocess output or environment data.
        parser.exit(1, 'Synthetic fixture generation failed: ' + type(error).__name__ + '\n')
    print(f'Synthetic fixture generated: {size} bytes, 40 seconds, 192000 Hz, stereo PCM16')


if __name__ == '__main__':
    main()
