"""Retrieve bounded public texts and locate quotations independently of the model."""
import http.client
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import tempfile
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

BYTE_LIMIT = 8 * 1024 * 1024
TEXT_LIMIT = 4 * 1024 * 1024
CACHE_LIMIT = 12 * 1024 * 1024  # Aggregate retained Python strings per research job.
MAX_BLOCKS = 30_000

# Fixed public search terms; matched text from a participant never enters this mapping.
PUBLIC_SUBJECTS = (
    (r"винникот|winnicott", "D. W. Winnicott"),
    (r"мелани\s+кляйн|melanie\s+klein", "Melanie Klein"),
    (r"лакан|lacan", "Jacques Lacan"),
    (r"лапланш|laplanche", "Jean Laplanche"),
    (r"понталис|понтали|pontalis", "Jean-Bertrand Pontalis"),
    (r"фрейд|freud", "Sigmund Freud"),
    (r"французск\w*\s+психоанализ|french psychoanalysis", "French psychoanalysis"),
    (r"переходн\w+\s+объект|transitional objects?", "Transitional Objects and Transitional Phenomena"),
    (r"объектн\w+\s+отношени|object relations", "object relations"),
    (r"перенос\w*\s+и\s+контрперенос|transference and countertransference", "transference and countertransference"),
    (r"толковани\w*\s+сновидени|interpretation of dreams", "The Interpretation of Dreams"),
    (r"по ту сторону принципа удовольствия|beyond the pleasure principle", "Beyond the Pleasure Principle"),
    (r"стади\w+\s+зеркал|mirror stage", "mirror stage"),
    (r"форклюзи|foreclosure", "foreclosure"),
    (r"вытеснен|refoulement|verdr[äa]ngung|repression", "repression"),
)


def public_topic(question):
    """Only an explicitly released line or a known public identifier may leave the chat."""
    if not isinstance(question, str):
        return ""
    for line in question.strip().splitlines()[:1]:
        match = re.fullmatch(r"\s*(?:публичная тема|public topic)\s*:\s*(.{3,240})\s*", line, re.I)
        if match:
            topic = match.group(1).strip()
            if not re.search(r"[@:/\\]|\d[\d() .-]{4,}\d|[^\S ]", topic):
                return topic
            return ""
    terms = [canonical for pattern, canonical in PUBLIC_SUBJECTS if re.search(pattern, question, re.I)]
    if (re.search(r"фрейд|freud", question, re.I)
            and re.search(r"вытеснен|refoulement|verdr[äa]ngung|repression", question, re.I)):
        topic = "Freud Repression 1915"
        if re.search(r"франц|лакан|лапланш|понталис|french|fran[cç]|lacan|laplanche|pontalis", question, re.I):
            topic += " and French psychoanalysis"
        retained = [name for name in terms if name not in ("Sigmund Freud", "repression", "French psychoanalysis")]
        return ", ".join([topic, *retained])
    match = re.search(r"\bRFC[ -]?(\d{1,5})\b", question, re.I)
    if match:
        return "RFC " + match.group(1)
    return ", ".join(terms)


class PublicConnection(http.client.HTTPSConnection):
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not infos or any(not ipaddress.ip_address(item[4][0]).is_global or
                            ipaddress.ip_address(item[4][0]).is_multicast for item in infos):
            raise ValueError("non-public peer")
        error = None
        for family, kind, proto, _, address in infos[:4]:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                sock.close()
                error = exc
        raise error or OSError("no available peer")


def retrieve(url):
    deadline = time.monotonic() + 16
    for _ in range(4):
        p = urlsplit(url)
        if p.scheme != "https" or not p.hostname or p.username is not None or p.password is not None or p.port not in (None,443):
            raise ValueError("invalid public source")
        conn = PublicConnection(p.hostname, timeout=3, context=ssl.create_default_context())
        try:
            conn.request("GET", (p.path or "/") + ("?" + p.query if p.query else ""),
                         headers={"Accept-Encoding":"identity", "User-Agent":"PSY-SourceReader/1"})
            resp = conn.getresponse()
            if resp.status in (301,302,303,307,308) and resp.getheader("Location"):
                url = urljoin(url, resp.getheader("Location"))
                continue
            if resp.status != 200 or resp.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("unavailable source")
            kind = resp.headers.get_content_type()
            if kind not in ("text/html","text/plain","application/xhtml+xml","application/pdf","application/octet-stream"):
                raise ValueError("unsupported source")
            size = resp.getheader("Content-Length")
            if size and int(size) > BYTE_LIMIT:
                raise ValueError("source too large")
            data = bytearray()
            while len(data) <= BYTE_LIMIT and time.monotonic() < deadline:
                block = resp.read1(min(65536, BYTE_LIMIT - len(data) + 1))
                if not block:
                    return kind, resp.headers.get_content_charset() or "utf-8", bytes(data)
                data.extend(block)
            raise ValueError("source retrieval budget")
        finally:
            conn.close()
    raise ValueError("too many redirects")


class Paragraphs(HTMLParser):
    boundaries = {"p","h1","h2","h3","h4","li","div","section","article","blockquote","br","tr","td","pre"}
    hidden = {"script","style","template","noscript","head","svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.parts = []
        self.depth = 0

    def flush(self):
        value = "".join(self.parts).strip()
        if value:
            if len(self.lines) >= MAX_BLOCKS:
                raise ValueError("source paragraph budget")
            self.lines.append(value)
        self.parts.clear()

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.depth += 1
        if tag in self.boundaries and not self.depth:
            self.flush()

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.depth = max(0, self.depth - 1)
        if tag in self.boundaries and not self.depth:
            self.flush()

    def handle_data(self, value):
        if not self.depth:
            self.parts.append(value)


def passages(url):
    kind, charset, data = retrieve(url)
    if data.startswith(b"%PDF-"):
        # The child drops file and network access before starting the native decoder.
        with tempfile.TemporaryFile() as out:
            subprocess.run(["/usr/bin/python3", "-I", str(Path(__file__).resolve()), "--pdf"],
                                  input=data, stdout=out, stderr=subprocess.DEVNULL, timeout=32,
                                  env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"}, check=True)
            if out.tell() >= TEXT_LIMIT:
                raise ValueError("PDF extraction budget")
            out.seek(0)
            return "PDF", out.read().decode("utf-8").split("\f")
    if kind not in ("text/html","application/xhtml+xml","text/plain") or len(data) > TEXT_LIMIT:
        raise ValueError("unreadable text")
    body = data.decode(charset, errors="replace")
    if kind == "text/plain":
        return "текст", [x for x in re.split(r"\n\s*\n", body) if x.strip()]
    parser = Paragraphs()
    parser.feed(body)
    parser.flush()
    return "HTML", parser.lines


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).replace("\u00ad", "").split()).casefold()


class EvidenceReader:
    """One short-lived cache per research job, bounded by the memory it retains.

    Parallel stages share it. The lock guards only the cache: each page is fetched once,
    without the lock, so one slow site never holds up checks against other pages."""
    def __init__(self):
        self.pages = {}
        self.fetching = set()
        self.cache_bytes = 0
        self.lock = threading.Lock()
        self.ready = threading.Condition(self.lock)

    def _page(self, base):
        with self.ready:
            while base in self.fetching:
                self.ready.wait()
            if base in self.pages:
                return self.pages[base]
            self.fetching.add(base)
            budget = CACHE_LIMIT - self.cache_bytes
        page, size, done = None, 0, False
        try:
            try:
                kind, blocks = passages(base)
                if len(blocks) > MAX_BLOCKS:
                    raise ValueError("source paragraph budget")
                normalized = []
                for block in blocks:
                    value = norm(block)
                    size += sys.getsizeof(value) + 16
                    if size > budget:
                        raise ValueError("source cache budget")
                    normalized.append(value)
                page = kind, normalized
            except (OSError, ValueError, LookupError, http.client.HTTPException, subprocess.SubprocessError):
                page, size = None, 0
            done = True
        finally:
            with self.ready:
                self.fetching.discard(base)
                if done:
                    # Another page may have been cached meanwhile. Keep the aggregate bound.
                    if page is not None and self.cache_bytes + size > CACHE_LIMIT:
                        page, size = None, 0
                    self.pages[base] = page
                    self.cache_bytes += size
                self.ready.notify_all()
        return page

    def locate(self, url, support, location, author, work):
        quote = norm(support)
        if not 12 <= len(quote) <= 600 or len(quote.split()) < 3:
            return None
        base = url.partition("#")[0]
        source = self._page(base)
        if not source:
            return None
        kind, blocks = source
        # Require independent evidence that the retrieved document identifies the work
        # and the attributed author. Initials, catalog dates and URL slugs are insufficient.
        title_words = re.findall(r"[^\W_]+", norm(work))
        title = " ".join(title_words)
        surnames = [word for word in re.findall(r"[^\W_]+", norm(author)) if len(word) >= 3]
        intro = ""
        for block in blocks:
            intro += " " + block[:30000-len(intro)]
            if len(intro) >= 30000:
                break
        title_intro = " ".join(re.findall(r"[^\W_]+", intro))
        if len(title) < 4 or not surnames or not re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", title_intro) or not re.search(
                r"(?<!\w)" + re.escape(surnames[-1]) + r"(?!\w)", intro):
            return None
        requested = re.search(r"(?:^|[&#])page=(\d+)\b", url.partition("#")[2], re.I) if kind == "PDF" else None
        if kind == "PDF" and not requested:
            requested = re.fullmatch(r"\s*(?:PDF\s*)?(?:стр\.?|с\.|p\.|page)\s*(\d+)(?:\s*PDF)?\s*", location, re.I)
        for i, block in enumerate(blocks, 1):
            if requested and int(requested.group(1)) != i:
                continue
            if quote in block:
                locator = f"PDF, страница {i}" if kind == "PDF" else f"{kind}, абзац {i}"
                start = block.index(quote)
                return locator, block[max(0,start-180):start+len(quote)+180]
        return None


def decode_pdf():
    # PSY runs on Linux x86_64. Fail closed if Landlock or libseccomp is unavailable.
    import ctypes
    import resource
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("unsupported PDF sandbox")
    for key, limit in ((resource.RLIMIT_CPU,20),(resource.RLIMIT_AS,256<<20),(resource.RLIMIT_FSIZE,TEXT_LIMIT)):
        resource.setrlimit(key,(limit,limit))
    libc = ctypes.CDLL(None, use_errno=True)
    sec = ctypes.CDLL("libseccomp.so.2")
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7fff0000)
    if not ctx or libc.prctl(38,1,0,0,0) != 0:
        raise OSError("sandbox init")
    for name in (b"socket", b"socketpair", b"connect", b"ptrace", b"process_vm_readv", b"process_vm_writev", b"kill", b"tgkill", b"pidfd_open", b"pidfd_getfd", b"io_uring_setup", b"clone", b"clone3", b"fork", b"vfork"):
        nr = sec.seccomp_syscall_resolve_name(name)
        if nr >= 0 and sec.seccomp_rule_add(ctx,0x50001,nr,0) != 0:
            raise OSError("sandbox filter")
    if sec.seccomp_load(ctx) != 0 or libc.syscall(444,0,0,1) < 3:
        raise OSError("sandbox unsupported")
    access = ctypes.c_uint64((1 << 15) - 1)
    fd = libc.syscall(444,ctypes.byref(access),8,0)
    if fd < 0:
        raise OSError("sandbox ruleset")
    import struct
    prefix = "/opt/psy-source-reader/usr"
    for path in (prefix,"/usr","/etc/ld.so.cache","/etc/fonts","/var/cache/fontconfig"):
        if not os.path.exists(path):
            continue
        rights = 13 if os.path.isdir(path) else 4
        target = os.open(path,os.O_PATH)
        if libc.syscall(445,fd,1,struct.pack("=Qi",rights,target),0) < 0:
            raise OSError("sandbox path")
        os.close(target)
    if libc.syscall(446,fd,0) < 0:
        raise OSError("sandbox activate")
    os.close(fd)
    executable = "/usr/bin/pdftotext"
    if os.path.isfile(prefix + "/bin/pdftotext"):
        executable = prefix + "/bin/pdftotext"
        os.environ["LD_LIBRARY_PATH"] = prefix + "/lib/x86_64-linux-gnu"
    os.execv(executable,["pdftotext","-enc","UTF-8","-","-"])


if __name__ == "__main__" and sys.argv[1:] == ["--pdf"]:
    decode_pdf()
