"""Run one local decoder with kernel-enforced access to just this request's media."""
import ctypes
import os
from pathlib import Path
import sys

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
READ_ONLY = EXECUTE | READ_FILE | READ_DIR
libc = ctypes.CDLL(None, use_errno=True)


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('access', ctypes.c_uint64), ('fd', ctypes.c_int32)]


def syscall(number, *args):
    result = libc.syscall(number, *args)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def restrict(source, jobdir):
    # Ubuntu x86_64, Linux 6.8+. Fail closed if the kernel lacks Landlock v3.
    abi = syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError('Landlock version 3 is required')
    handled = (1 << 15) - 1   # Execute through truncate, including REFER.
    attrs = ctypes.c_uint64(handled)
    ruleset = syscall(444, ctypes.byref(attrs), ctypes.sizeof(attrs), 0)
    entries = [(Path(p), READ_ONLY) for p in (
        '/usr',sys.base_prefix,sys.prefix,os.environ['PSY_WHISPER_MODEL'],'/etc/ld.so.cache',
        '/etc/localtime','/proc/cpuinfo','/proc/meminfo','/proc/stat','/sys/devices/system/cpu')]
    entries += [(Path(__file__).with_name('transcribe.py'), READ_FILE),
                (Path('/dev/null'), READ_FILE | WRITE_FILE), (Path('/dev/urandom'), READ_FILE),
                (source, READ_FILE), (jobdir, handled)]
    try:
        for path, access in dict.fromkeys(entries):
            if not path.exists():
                continue
            if not path.is_dir():
                access &= EXECUTE | READ_FILE | WRITE_FILE
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access,fd)
                syscall(445,ruleset,1,ctypes.byref(rule),0)
            finally:
                os.close(fd)
        if libc.prctl(38,1,0,0,0) != 0:
            raise RuntimeError('Cannot enable no_new_privs')
        syscall(446,ruleset,0)
    finally:
        os.close(ruleset)


if __name__ == '__main__':
    # The job folder holds the resumable journal; the target sits in a fresh per-attempt folder below it.
    source, target, language, *state = sys.argv[1:]
    jobdir = Path(state[0]) if state else Path(target).parent
    if len(state) > 1 or not Path(target).resolve().is_relative_to(jobdir.resolve()):
        raise SystemExit('Output must stay inside the job folder')
    restrict(Path(source), jobdir)
    os.execv(sys.executable, [sys.executable,'-I',str(Path(__file__).with_name('transcribe.py')),source,
                             '--output',target,'--language',language])
