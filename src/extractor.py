"""Member 2 - File detection and extraction.

Turns one downloaded arXiv source file into the `.tex` and `.bib` files of a
version folder. arXiv's /src/ endpoint does not always send a .tar.gz, so the
real format is detected from the file's first bytes (its "magic number"):

    gzip + tar inside    -> "tar.gz"       extract the .tex/.bib members
    plain tar            -> "tar"          extract the .tex/.bib members
    gzip + one TeX file  -> "gzip-tex"     the whole submission is one .tex file
    plain TeX text       -> "plain-tex"    same, without compression
    PDF (maybe gzipped)  -> "pdf"          no LaTeX source: status pdf_only
    DOCX                 -> "docx"         no LaTeX source: status no_tex
    PostScript           -> "postscript"   no LaTeX source: status no_tex
    other zip            -> "zip"          extract the .tex/.bib members
    HTML page            -> error page from the server, not a source: failed

Rules followed:
  * only `.tex` and `.bib` files are written, so figures and every other
    binary never enter the corpus;
  * the files are copied byte for byte: no LaTeX command is changed, so
    `\\begin{figure}`, `\\includegraphics`, `\\input`, `\\cite`... stay as they are;
  * the relative paths inside the archive are kept (sub/intro.tex stays
    sub/intro.tex), so `\\input{sub/intro}` still resolves in Milestone 2;
  * extraction is safe: absolute paths, `..` components and links are never
    followed, so an archive cannot write outside its version folder.

Sizes are measured on one basis, the *uncompressed* size of the files:
`original_bytes` is the total size of every regular file in the source and
`kept_bytes` the total size of the kept .tex/.bib files, so
`1 - kept_bytes / original_bytes` is the share removed by dropping figures and
other files. The compressed download size is measured by downloader.py.
"""

from __future__ import annotations

import errno
import gzip
import re
import shutil
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from common import PDF_ONLY, SUCCESS

# A version whose source exists but holds no .tex/.bib file (DOCX, PostScript,
# HTML submission...). Like pdf_only it is final: downloading again won't help.
NO_TEX = "no_tex"

KEEP_EXTENSIONS = (".tex", ".bib")
# Only used for the statistics of the report (how much of a source is figures).
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg",
                    ".eps", ".ps", ".pdf", ".webp", ".pgm", ".ppm", ".pbm")

CHUNK = 1024 * 1024  # copy files 1 MB at a time, so RAM stays low on big sources
TEXT_SNIFF_BYTES = 64 * 1024
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# Disk or Google Drive quota full: never skip the file as if only its name were
# a problem, or the version would be saved as success with files missing.
DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


class IncompleteSourceError(Exception):
    """The file ends too early (interrupted download). Download it again."""


class CorruptSourceError(Exception):
    """The file is damaged or is not a source at all (for example an HTML
    error page). The version is marked failed and retried on the next run."""


@dataclass
class ExtractionResult:
    """What extract_source() found and wrote for one version.

    status          SUCCESS, PDF_ONLY or NO_TEX (errors raise instead)
    source_format   detected format, see the module docstring
    original_files  number of regular files in the source
    original_bytes  total uncompressed size of those files
    image_bytes     part of original_bytes that are figure files
    kept_files      relative paths of the written .tex/.bib files (POSIX style)
    kept_bytes      total size of the written files
    skipped         members that were not written, with the reason
                    (unsafe path, link, name the file system refused)
    """

    status: str
    source_format: str
    original_files: int = 0
    original_bytes: int = 0
    image_bytes: int = 0
    kept_files: list[str] = field(default_factory=list)
    kept_bytes: int = 0
    skipped: list[dict] = field(default_factory=list)

    @property
    def tex_files(self) -> int:
        return sum(1 for name in self.kept_files if name.lower().endswith(".tex"))

    @property
    def bib_files(self) -> int:
        return sum(1 for name in self.kept_files if name.lower().endswith(".bib"))


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def sniff_bytes(head: bytes) -> str:
    """Name the format of a file from its first bytes.

    Returns one of "gzip", "bzip2", "xz", "pdf", "postscript", "zip", "tar",
    "html", "text" or "unknown". `head` should hold at least 512 bytes (a tar
    header) when the file is that long.
    """
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"%!PS") or head.startswith(b"\xc5\xd0\xd3\xc6"):
        return "postscript"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if looks_like_tar(head):
        return "tar"
    stripped = head.lstrip().lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
        return "html"
    if head and b"\x00" not in head[:TEXT_SNIFF_BYTES]:
        return "text"
    return "unknown"


def looks_like_tar(head: bytes) -> bool:
    """True when `head` starts with a valid tar header.

    Modern tars carry the "ustar" magic at offset 257; very old ones do not,
    so the header checksum is checked as a fallback.
    """
    if len(head) < 512:
        return False
    if head[257:262] == b"ustar":
        return True
    try:
        tarfile.TarInfo.frombuf(head[:512], "utf-8", "surrogateescape")
        return True
    except tarfile.HeaderError:
        return False


def gzip_original_name(path: Path) -> str | None:
    """Return the file name stored in a gzip header (FNAME field), if any.

    arXiv keeps the original name of a single-file submission there, for
    example "main.tex". Returns None when the header has no name.
    """
    with open(path, "rb") as f:
        header = f.read(10)
        if len(header) < 10 or header[:2] != b"\x1f\x8b":
            return None
        flags = header[3]
        if flags & 0x04:  # FEXTRA: skip the extra field
            extra_len = int.from_bytes(f.read(2), "little")
            f.read(extra_len)
        if not flags & 0x08:  # no FNAME
            return None
        name = bytearray()
        while True:
            byte = f.read(1)
            if not byte or byte == b"\x00":
                break
            name += byte
    return name.decode("latin-1") or None


def _read_head(path: Path, compressed: bool, size: int = TEXT_SNIFF_BYTES) -> bytes:
    """First `size` bytes of the file, decompressed when it is gzip."""
    try:
        if compressed:
            with gzip.open(path, "rb") as f:
                return f.read(size)
        with open(path, "rb") as f:
            return f.read(size)
    except EOFError as exc:
        raise IncompleteSourceError(f"gzip stream ends too early: {exc}") from exc
    except (gzip.BadGzipFile, zlib.error) as exc:
        raise CorruptSourceError(f"invalid gzip data: {exc}") from exc


# ---------------------------------------------------------------------------
# Safe paths
# ---------------------------------------------------------------------------

def safe_relative_path(name: str) -> PurePosixPath | None:
    """Clean an archive member name into a relative path, or None if unsafe.

    'paper/./sec/intro.tex' -> PurePosixPath('paper/sec/intro.tex')
    '/etc/passwd', '../x.tex', 'C:/x.tex', '' -> None
    """
    name = name.replace("\\", "/")
    if name.startswith("/") or WINDOWS_DRIVE.match(name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return PurePosixPath(*parts)


def _target_inside(dest: Path, rel: PurePosixPath) -> Path | None:
    """`dest / rel` if it really stays inside `dest` (second line of defence
    after safe_relative_path), otherwise None."""
    target = dest.joinpath(*rel.parts)
    try:
        target.resolve().relative_to(dest.resolve())
    except ValueError:
        return None
    return target


def is_kept(name: str) -> bool:
    """True for the file types the corpus keeps (.tex and .bib, any case)."""
    return name.lower().endswith(KEEP_EXTENSIONS)


def _is_image(name: str) -> bool:
    return name.lower().endswith(IMAGE_EXTENSIONS)


def _write_member(source, target: Path, expected_size: int | None) -> int:
    """Copy an open file object to `target` in chunks and return the number of
    bytes written. A short copy means the archive was cut off."""
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(target, "wb") as out:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    if expected_size is not None and written != expected_size:
        raise IncompleteSourceError(
            f"{target.name}: wrote {written} of {expected_size} bytes")
    return written


# ---------------------------------------------------------------------------
# Extraction per format
# ---------------------------------------------------------------------------

def _extract_tar(path: Path, dest: Path, result: ExtractionResult) -> None:
    """Write the .tex/.bib members of a (possibly compressed) tar into `dest`.

    Members are read one by one from the stream (`r|*`), so even a large
    archive is never loaded into memory. Directories are recreated only
    when they contain a kept file.
    """
    try:
        with tarfile.open(path, mode="r|*") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if member.isfile():
                    result.original_files += 1
                    result.original_bytes += member.size
                    if _is_image(member.name):
                        result.image_bytes += member.size
                if not is_kept(member.name):
                    continue
                if not member.isfile():  # symlink, hard link, device...
                    result.skipped.append({"name": member.name, "reason": "not a regular file"})
                    continue
                rel = safe_relative_path(member.name)
                target = _target_inside(dest, rel) if rel is not None else None
                if target is None:
                    result.skipped.append({"name": member.name, "reason": "unsafe path"})
                    continue
                source = tar.extractfile(member)
                try:
                    _write_member(source, target, member.size)
                except OSError as exc:  # e.g. a name Windows does not allow
                    if exc.errno in DISK_FULL:
                        raise  # stop the crawl: every following version would fail too
                    result.skipped.append({"name": member.name, "reason": f"write failed: {exc}"})
    except (EOFError, zlib.error) as exc:
        raise IncompleteSourceError(f"archive ends too early: {exc}") from exc
    except tarfile.ReadError as exc:
        if "unexpected end" in str(exc).lower():
            raise IncompleteSourceError(f"archive ends too early: {exc}") from exc
        raise CorruptSourceError(f"unreadable tar archive: {exc}") from exc


def _extract_zip(path: Path, dest: Path, result: ExtractionResult) -> None:
    """Handle a zip: a Word document is reported as docx, any other zip has its
    .tex/.bib members extracted with the same safety rules as a tar."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = {info.filename for info in infos}
            result.original_files = len(infos)
            result.original_bytes = sum(info.file_size for info in infos)
            result.image_bytes = sum(info.file_size for info in infos if _is_image(info.filename))
            if "word/document.xml" in names:
                result.source_format = "docx"
                return
            result.source_format = "zip"
            for info in infos:
                if not is_kept(info.filename):
                    continue
                rel = safe_relative_path(info.filename)
                target = _target_inside(dest, rel) if rel is not None else None
                if target is None:
                    result.skipped.append({"name": info.filename, "reason": "unsafe path"})
                    continue
                with archive.open(info) as source:
                    try:
                        _write_member(source, target, info.file_size)
                    except OSError as exc:
                        if exc.errno in DISK_FULL:
                            raise
                        result.skipped.append({"name": info.filename,
                                               "reason": f"write failed: {exc}"})
    except zipfile.BadZipFile as exc:
        raise CorruptSourceError(f"unreadable zip archive: {exc}") from exc


def _single_file_name(original: str | None, fallback_name: str) -> str:
    """Name for a single-file submission: the name arXiv stored in the gzip
    header when it is a usable .tex name, otherwise `fallback_name`."""
    if original:
        rel = safe_relative_path(original)
        if rel is not None and rel.name.lower().endswith(".tex"):
            return rel.name
    return fallback_name


def _extract_single(path: Path, dest: Path, result: ExtractionResult, *,
                    compressed: bool, fallback_name: str) -> None:
    """Copy a single-file TeX submission (gzipped or not) into `dest`."""
    original = gzip_original_name(path) if compressed else None
    name = _single_file_name(original, fallback_name)
    target = dest / name
    try:
        opener = gzip.open(path, "rb") if compressed else open(path, "rb")
        with opener as source:
            written = _write_member(source, target, None)
    except EOFError as exc:
        raise IncompleteSourceError(f"gzip stream ends too early: {exc}") from exc
    except (gzip.BadGzipFile, zlib.error) as exc:
        raise CorruptSourceError(f"invalid gzip data: {exc}") from exc
    result.original_files, result.original_bytes = 1, written


def _uncompressed_size(path: Path, *, compressed: bool) -> int:
    """Size of the file's content, decompressing a gzip on the fly."""
    if not compressed:
        return path.stat().st_size
    total = 0
    try:
        with gzip.open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                total += len(chunk)
    except EOFError as exc:
        raise IncompleteSourceError(f"gzip stream ends too early: {exc}") from exc
    except (gzip.BadGzipFile, zlib.error) as exc:
        raise CorruptSourceError(f"invalid gzip data: {exc}") from exc
    return total


def _kept_on_disk(dest: Path) -> tuple[list[str], int]:
    """List the files really written under `dest` and their total size."""
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    return ([p.relative_to(dest).as_posix() for p in files],
            sum(p.stat().st_size for p in files))


def extract_source(path: Path, dest: Path, *, fallback_name: str = "main.tex") -> ExtractionResult:
    """Detect the format of the downloaded file `path` and write its .tex/.bib
    files into the (empty, existing) folder `dest`.

    `fallback_name` names a single-file submission whose original name is
    unknown, e.g. "2604-05535v1.tex".

    Returns an ExtractionResult whose status is
      SUCCESS   at least one .tex/.bib file was written,
      PDF_ONLY  the source is a PDF, nothing is written,
      NO_TEX    the source has no .tex/.bib (DOCX, PostScript, HTML
                submission...), nothing is written.
    Raises IncompleteSourceError when the file is cut off (download again)
    and CorruptSourceError when it is damaged or not a source at all.
    """
    path, dest = Path(path), Path(dest)
    head = _read_head(path, compressed=False)
    outer = sniff_bytes(head)
    result = ExtractionResult(status=SUCCESS, source_format=outer)

    if outer == "gzip":
        inner = sniff_bytes(_read_head(path, compressed=True))
        if inner == "tar":
            result.source_format = "tar.gz"
            _extract_tar(path, dest, result)
        elif inner == "pdf":
            result.source_format = "pdf"
        elif inner == "postscript":
            result.source_format = "postscript"
        elif inner == "text":
            result.source_format = "gzip-tex"
            _extract_single(path, dest, result, compressed=True, fallback_name=fallback_name)
        elif inner == "html":
            raise CorruptSourceError("gzip file holds an HTML page, not a source")
        else:
            result.source_format = f"gzip-{inner}"
    elif outer in ("tar", "bzip2", "xz"):
        result.source_format = "tar" if outer == "tar" else f"tar.{outer}"
        _extract_tar(path, dest, result)
    elif outer == "zip":
        _extract_zip(path, dest, result)
    elif outer == "text":
        result.source_format = "plain-tex"
        _extract_single(path, dest, result, compressed=False, fallback_name=fallback_name)
    elif outer == "html":
        raise CorruptSourceError("the server returned an HTML page instead of a source")
    elif outer not in ("pdf", "postscript"):
        raise CorruptSourceError(f"unrecognised file format (first bytes {head[:8]!r})")

    single_non_tex = (result.source_format in ("pdf", "postscript")
                      or (result.source_format.startswith("gzip-")
                          and result.source_format != "gzip-tex"))
    if single_non_tex:
        # Measured uncompressed, on the same basis as every other source.
        result.original_files = 1
        result.original_bytes = _uncompressed_size(path, compressed=outer == "gzip")
    if result.source_format == "pdf":
        result.status = PDF_ONLY
        return result

    result.kept_files, result.kept_bytes = _kept_on_disk(dest)
    if not result.kept_files:
        result.status = NO_TEX
    return result


def clear_folder(folder: Path) -> None:
    """Delete `folder` and everything in it, if it exists (used for leftovers
    of an interrupted run; never called on a finished version)."""
    if folder.exists():
        shutil.rmtree(folder)


__all__ = ["NO_TEX", "KEEP_EXTENSIONS", "ExtractionResult", "IncompleteSourceError",
           "CorruptSourceError", "extract_source", "sniff_bytes", "safe_relative_path",
           "clear_folder"]
