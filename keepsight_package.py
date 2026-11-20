#!/usr/bin/env python3
"""Read and verify Keepsight video packages.

One file, Python 3.9 or later, standard library only. It shares no code with the service
that makes packages; it follows SPEC.md in this folder.

    python3 keepsight_package.py verify package.zip
    python3 keepsight_package.py info package.zip
    python3 keepsight_package.py transcript package.zip [--vtt [--raw]]
    python3 keepsight_package.py notes package.zip
    python3 keepsight_package.py extract package.zip DESTINATION

A ZIP or an unpacked folder can be given wherever a package is asked for. The ZIP is parsed
here, strictly (SPEC.md 2.3), not by zipfile. `extract` verifies first, writes into a new
folder only, and checks every file it writes against what verification read. Text from a
package is printed with control and invisible characters shown as escapes; --json output is
ASCII.

Exit codes: 0 verified, 1 the package failed verification, 2 it could not be read as a
Keepsight package at all (not a ZIP, unknown layout, unsafe archive, bad arguments) or does
not hold what was asked for (transcript --vtt without a transcript.vtt), 130 interrupted.

Verifying shows that the files are the ones the package lists and that they agree with each
other. It does not show who made the package or that what it says about the video is true.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
import zlib
from pathlib import Path

__version__ = "0.4.0"

# Upload exports carry export.json with this format version; link packages name theirs in
# manifest.json. A reader refuses versions it does not know rather than guess.
UPLOAD_VERSIONS = {"2.0"}
LINK_VERSIONS = {"youtube-evidence-gemini-url-v1", "youtube-evidence-gemini-url-v2"}

# Limits a reader applies before trusting anything in the archive. Keepsight's own largest
# package (an upload with its original video) is under 512 MiB.
MAX_MEMBERS = 10_000
MAX_TOTAL_BYTES = 768 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 200_000  # lines in one JSONL file
MAX_CONTAINERS = 500_000  # objects and arrays in one JSON or JSONL file
MAX_PROBLEMS = 100  # listed; the rest are counted
MAX_NAME = 255
MAX_DEPTH = 16
CHUNK = 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
NAME_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
DEVICE = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])")
# Left by operating systems when a folder is browsed or re-zipped; ignored in folders only.
OS_LITTER = {".DS_Store", "Thumbs.db", "desktop.ini"}

STORED, DEFLATED = 0, 8
UTF8_FLAG = 0x800
END = struct.Struct("<IHHHHIIH")  # end of central directory record, 22 bytes
CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")  # central directory entry, 46 bytes
LOCAL = struct.Struct("<IHHHHHIIIHH")  # local file header, 30 bytes
FIXED_TIME, FIXED_DATE = 0, (0 << 9) | (1 << 5) | 1  # 1980-01-01 00:00:00 in MS-DOS form


class Unreadable(Exception):
    """The input is not a Keepsight package this reader can read (exit code 2)."""


# --------------------------------------------------------------------------- containers


def safe_name(name: str) -> bool:
    """A member name that means the same thing on every system and stays inside the
    package: ASCII letters, digits, '.', '_' and '-', parts joined by '/', no part empty,
    starting with '.' or '-' or ending with '.', no name Windows keeps for a device, at most
    16 parts and 255 characters."""
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
        return False
    parts = name.split("/")
    return len(parts) <= MAX_DEPTH and all(
        NAME_PART.fullmatch(part)
        and not part.endswith(".")
        and not DEVICE.fullmatch(part.split(".", 1)[0])
        for part in parts
    )


class Member:
    __slots__ = ("method", "crc", "packed", "size", "start")

    def __init__(self, method: int, crc: int, packed: int, size: int, start: int):
        self.method, self.crc, self.packed, self.size, self.start = method, crc, packed, size, start


class Container:
    """The files of a package, from a ZIP or a folder, read with limits.

    A ZIP is parsed here, not by zipfile: it must be exactly its members, one after another
    from the first byte, then the central directory and the end record, with every local
    header agreeing with the central directory. Any two ZIP tools then see the same files.

    A ZIP is read through one open file, so a Container (and a Package) must not be shared
    between threads; open one per thread. Close it when done, or use it in a with block."""

    def __init__(self, source: Path):
        self.source = source
        self.notes: list[str] = []
        self._members: dict[str, Member] = {}
        self._sizes: dict[str, int] = {}
        self._seen: dict[str, str] = {}  # SHA-256 of each member the first time it was read
        self._handle = None  # the ZIP, opened once: every read is from the file checked
        self._zip = False
        if source.is_dir():
            self._open_folder(source)
        elif source.is_file():
            self._open_zip(source)
        elif source.exists():
            raise Unreadable(f"{source}: not a file or a folder")
        else:
            raise Unreadable(f"{source}: no such file or folder")
        try:
            self._check_names()
        except BaseException:
            self.close()
            raise

    def _check_names(self) -> None:
        folded: dict[str, str] = {}
        for name in self._sizes:
            key = name.lower()
            if key in folded:
                raise Unreadable(f"two members differ only in case: {folded[key]!r}, {name!r}")
            folded[key] = name
        for name in self._sizes:
            cut = name.find("/")
            while cut != -1:
                if name[:cut].lower() in folded:
                    raise Unreadable(f"{name[:cut]!r} is both a file and a folder")
                cut = name.find("/", cut + 1)

    def _open_zip(self, source: Path) -> None:
        try:
            handle = open(source, "rb")  # noqa: SIM115 - kept open; see close()
        except OSError as error:
            raise Unreadable(f"{source}: not readable ({error})") from None
        try:
            self._parse_zip(handle, os.fstat(handle.fileno()).st_size)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._zip = True

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _parse_zip(self, handle, size: int) -> None:
        if size < END.size:
            raise Unreadable("not a ZIP")
        handle.seek(size - END.size)
        signature, disk, first_disk, here, entries, length, offset, comment = END.unpack(
            handle.read(END.size)
        )
        if signature != 0x06054B50:
            raise Unreadable(
                "not a plain ZIP: the file does not end with the ZIP end record "
                "(not a ZIP, or one with a comment, trailing bytes or ZIP64)"
            )
        if comment:
            raise Unreadable("the ZIP end record has a comment")
        if disk or first_disk or here != entries:
            raise Unreadable("a ZIP split over several disks")
        if entries == 0:
            raise Unreadable("an empty ZIP")
        if entries > MAX_MEMBERS:
            raise Unreadable(f"more than {MAX_MEMBERS} members")
        if offset + length != size - END.size:
            raise Unreadable("bytes between the central directory and the end record")
        if length > entries * (CENTRAL.size + MAX_NAME):
            raise Unreadable("the central directory is larger than its entries")
        handle.seek(offset)
        directory = handle.read(length)
        listed = []
        at = total = 0
        for _ in range(entries):
            if at + CENTRAL.size > len(directory):
                raise Unreadable("the central directory is cut short")
            (signature, _made_by, needs, flags, method, time, date, crc, packed, unpacked,
             name_length, extra_length, comment_length, disk, internal, external,
             start) = CENTRAL.unpack_from(directory, at)  # fmt: skip
            raw = directory[at + CENTRAL.size : at + CENTRAL.size + name_length]
            at += CENTRAL.size + name_length + extra_length + comment_length
            if signature != 0x02014B50 or len(raw) != name_length or at > len(directory):
                raise Unreadable("the central directory is damaged")
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                raise Unreadable(f"member name {raw!r} is not plain ASCII") from None
            if name.endswith("/") or external & 0x10:
                raise Unreadable(f"directory entry {name!r}: packages hold files only")
            if not safe_name(name):
                raise Unreadable(f"unsafe member name {name!r}")
            if name in self._sizes:
                raise Unreadable(f"member {name!r} appears twice")
            if extra_length or comment_length:
                raise Unreadable(f"member {name!r} carries extra fields or a comment")
            if flags & 0x1:
                raise Unreadable(f"member {name!r} is encrypted")
            if flags & ~UTF8_FLAG:
                raise Unreadable(f"member {name!r} uses ZIP features a package never needs")
            if method not in (STORED, DEFLATED):
                raise Unreadable(f"member {name!r} uses an unsupported compression method")
            if method == STORED and packed != unpacked:
                raise Unreadable(f"member {name!r} is stored with two different sizes")
            # Judged whatever system the ZIP says made it: unzip honours a link mode from
            # any of them. A plain file, readable and writable at most; no exec, setuid,
            # setgid or sticky bit, no MS-DOS attribute bits, no text flag.
            mode = external >> 16
            if (
                stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or mode & 0o7111
                or external & 0xFFFF
                or internal
            ):
                raise Unreadable(f"member {name!r} is not marked as a plain file")
            if needs > 20 or (time, date) != (FIXED_TIME, FIXED_DATE):
                raise Unreadable(f"member {name!r}: version or timestamp a package never has")
            if disk:
                raise Unreadable("a ZIP split over several disks")
            total += unpacked
            if total > MAX_TOTAL_BYTES:
                raise Unreadable(f"members add up to more than {MAX_TOTAL_BYTES} bytes")
            self._sizes[name] = unpacked
            listed.append((start, name, raw, needs, flags, method, crc, packed, unpacked))
        if at != len(directory):
            raise Unreadable("the central directory holds more than its entries")
        expected = 0
        for start, name, raw, *fields in sorted(listed):
            if start != expected:
                raise Unreadable(
                    f"{name}: the ZIP has bytes before or between its members, "
                    "or members that overlap"
                )
            handle.seek(start)
            header = handle.read(LOCAL.size + len(raw))
            if len(header) != LOCAL.size + len(raw):
                raise Unreadable("the ZIP is cut short")
            signature, *local, local_name, local_extra = LOCAL.unpack_from(header)
            local_time, local_date = local.pop(3), local.pop(3)
            if (
                signature != 0x04034B50
                or local != fields
                or (local_time, local_date) != (FIXED_TIME, FIXED_DATE)
                or (local_name, local_extra) != (len(raw), 0)
                or header[LOCAL.size :] != raw
            ):
                raise Unreadable(f"member {name!r}: its local header disagrees with the directory")
            method, crc, packed, unpacked = fields[2:]
            data = start + len(header)
            self._members[name] = Member(method, crc, packed, unpacked, data)
            expected = data + packed
        if expected != offset:
            raise Unreadable("bytes between the last member and the central directory")

    def _open_folder(self, source: Path) -> None:
        total = 0

        def refuse(error):
            raise Unreadable(f"{error.filename}: not readable ({error.strerror})")

        for dirpath, dirnames, filenames in os.walk(source, onerror=refuse):
            here = Path(dirpath)
            for directory in list(dirnames):
                if (here / directory).is_symlink():
                    raise Unreadable(f"{here / directory}: symbolic links are not followed")
                if directory == "__MACOSX":
                    dirnames.remove(directory)
                    self.notes.append("ignored folder __MACOSX (operating system litter)")
            for filename in filenames:
                path = here / filename
                name = path.relative_to(source).as_posix()
                if filename in OS_LITTER:
                    self.notes.append(f"ignored {name} (operating system litter)")
                    continue
                if path.is_symlink() or not path.is_file():
                    raise Unreadable(f"{name!r}: not a regular file")
                if not safe_name(name):
                    raise Unreadable(f"unsafe file name {name!r}")
                size = path.stat().st_size
                total += size
                if len(self._sizes) >= MAX_MEMBERS or total > MAX_TOTAL_BYTES:
                    raise Unreadable("folder is larger than any package")
                self._sizes[name] = size
        if not self._sizes:
            raise Unreadable(f"{source}: empty folder")

    def names(self) -> set[str]:
        return set(self._sizes)

    def size(self, name: str) -> int:
        return self._sizes[name]

    def _chunks(self, name: str, limit: int):
        if name not in self._sizes:
            raise KeyError(name)
        if self._sizes[name] > limit:
            raise Unreadable(f"{name} is larger than {limit} bytes")
        if self._zip and self._handle is None:
            raise ValueError("the package has been closed")
        try:
            if self._zip:
                yield from self._zip_chunks(name)
            else:
                yield from self._file_chunks(name, limit)
        except zlib.error as error:
            raise Unreadable(f"{name}: damaged in the ZIP ({error})") from None
        except OSError as error:
            raise Unreadable(f"{name}: not readable ({error})") from None

    def _zip_chunks(self, name: str):
        member = self._members[name]
        produced = crc = 0
        handle = self._handle
        handle.seek(member.start)
        for piece in _unpack(handle, member, name):
            produced += len(piece)
            if produced > member.size:
                raise Unreadable(f"{name}: holds more than its declared size")
            crc = zlib.crc32(piece, crc)
            yield piece
        if produced != member.size or crc != member.crc:
            raise Unreadable(f"{name}: damaged in the ZIP (size or CRC-32 differs)")

    def _file_chunks(self, name: str, limit: int):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.source.joinpath(*name.split("/")), flags)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise Unreadable(f"{name}: not a regular file")
            read = 0
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    return
                read += len(chunk)
                if read > limit:
                    raise Unreadable(f"{name} is larger than {limit} bytes")
                yield chunk

    def digest(self, name: str) -> tuple[str, int]:
        """SHA-256 and length of the bytes actually stored, streaming. For a ZIP this also
        checks the member's size and CRC-32."""
        sha = hashlib.sha256()
        length = 0
        for chunk in self._chunks(name, MAX_TOTAL_BYTES):
            sha.update(chunk)
            length += len(chunk)
        return self._remember(name, sha.hexdigest()), length

    def read(self, name: str, limit: int = MAX_JSON_BYTES) -> bytes:
        data = b"".join(self._chunks(name, limit))
        self._remember(name, hashlib.sha256(data).hexdigest())
        return data

    def _remember(self, name: str, sha: str) -> str:
        # The first read is the one verification judged; any later read must be the same
        # bytes, or the package changed under the reader.
        first = self._seen.setdefault(name, sha)
        if first != sha:
            raise Unreadable(f"{name} changed after it was verified")
        return sha

    def seen(self, name: str) -> str | None:
        """The SHA-256 of the member the first time it was read, if it was."""
        return self._seen.get(name)


def _unpack(handle, member: Member, name: str):
    """A member's bytes. A deflate stream must end exactly where the member does: data
    hidden after its end, or a stream that stops early, is refused."""
    left = member.packed
    inflate = zlib.decompressobj(-15) if member.method == DEFLATED else None
    while left:
        block = handle.read(min(left, CHUNK))
        if not block:
            raise Unreadable(f"{name}: the ZIP is cut short")
        left -= len(block)
        if inflate is None:
            yield block
            continue
        data = block
        while True:
            out = inflate.decompress(data, CHUNK)
            if out:
                yield out
            data = inflate.unconsumed_tail
            if inflate.eof:
                if data or inflate.unused_data or left:
                    raise Unreadable(f"{name}: bytes after the end of its compressed data")
                break
            if not data and len(out) < CHUNK:
                break
    if inflate is not None and not inflate.eof:
        raise Unreadable(f"{name}: its compressed data ends early")


# --------------------------------------------------------------------------- results


class Report:
    def __init__(self, layout: str, version: str):
        self.layout = layout
        self.version = version
        self.problems: list[str] = []
        self.unlisted = 0  # problems past MAX_PROBLEMS, counted only
        self.checked: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(message)
        else:
            self.unlisted += 1

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "verified": self.ok,
            "layout": self.layout,
            "version": self.version,
            "problems": self.problems,
            "problems_not_listed": self.unlisted,
            "checked": self.checked,
            "notes": self.notes,
            "reader": f"keepsight_package.py {__version__}",
        }


def _no_repeats(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"the key {key!r} appears twice in one object")
        seen[key] = value
    return seen


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def _loads(text: str):
    """JSON as every parser reads it: an object that repeats a key, or NaN and Infinity,
    can mean one thing to this reader and another to the next, so they are refused. So is
    a file with more objects and arrays than any package needs (each costs memory)."""
    if text.count("{") + text.count("[") > MAX_CONTAINERS:
        raise ValueError(f"more than {MAX_CONTAINERS} objects and arrays")
    try:
        return json.loads(text, object_pairs_hook=_no_repeats, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("nested too deeply") from None


def _json(container: Container, name: str, report: Report | None = None):
    try:
        return _loads(container.read(name).decode("utf-8"))
    except ValueError as error:  # includes bad UTF-8 and json.JSONDecodeError
        if report is None:
            raise Unreadable(f"{name}: not JSON ({error})") from None
        report.fail(f"{name}: not JSON ({error})")
        return None


def _lines(text: str) -> list[str]:
    # Records end with "\n" only: U+2028 and its kind are allowed inside JSON strings.
    return [line for line in text.split("\n") if line.strip()]


def _jsonl(container: Container, name: str, report: Report) -> list[dict]:
    rows: list[dict] = []
    try:
        text = container.read(name).decode("utf-8")
    except UnicodeDecodeError:
        report.fail(f"{name}: not UTF-8")
        return rows
    if text.count("\n") > MAX_RECORDS:
        report.fail(f"{name}: more than {MAX_RECORDS} lines")
        return rows
    if text.count("{") + text.count("[") > MAX_CONTAINERS:
        report.fail(f"{name}: more than {MAX_CONTAINERS} objects and arrays")
        return rows
    number = 0
    for raw in text.split("\n"):
        number += 1
        if not raw.strip():
            continue
        try:
            row = _loads(raw)
        except ValueError as error:
            report.fail(f"{name} line {number}: not JSON ({getattr(error, 'msg', error)})")
            continue
        if not isinstance(row, dict):
            report.fail(f"{name} line {number}: not a JSON object")
            continue
        rows.append(row)
    return rows


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _span_ok(row: dict, limit: int | None) -> bool:
    start, end = row.get("start_us"), row.get("end_us")
    return _is_int(start) and _is_int(end) and 0 <= start < end and (limit is None or end <= limit)


def _list_of_dicts(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_RECORDS
        and all(isinstance(item, dict) for item in value)
    )


def _webvtt(data: bytes) -> str | None:
    """The file's text if it is UTF-8 WebVTT, else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    first = text.split("\n", 1)[0]
    if "\r" in text or not re.fullmatch(r"WEBVTT([ \t][^\n]*)?", first):
        return None
    return text


def detect(container: Container) -> tuple[str, str]:
    """('upload' | 'link', version) from the package's own files."""
    names = container.names()
    if "export.json" in names:
        export = _json(container, "export.json")
        version = export.get("schema_version") if isinstance(export, dict) else None
        if not isinstance(version, str) or version not in UPLOAD_VERSIONS:
            raise Unreadable(f"export.json format {version!r} is not one this reader knows")
        return "upload", version
    if "manifest.json" in names:
        manifest = _json(container, "manifest.json")
        version = manifest.get("schema_version") if isinstance(manifest, dict) else None
        if isinstance(version, str) and version in LINK_VERSIONS:
            return "link", version
        raise Unreadable(
            f"manifest.json format {version!r} without export.json is not one this reader knows"
        )
    raise Unreadable("neither export.json nor manifest.json: not a Keepsight package")


# --------------------------------------------------------------------------- upload layout

UPLOAD_REQUIRED = (
    "manifest.json",
    "observations.jsonl",
    "transcript.jsonl",
    "timeline.jsonl",
    "coverage.json",
    "capabilities.json",
    "provenance.json",
    "DISCUSSION.txt",
)


def verify_upload(container: Container, version: str) -> Report:
    report = Report("upload", version)
    export = _json(container, "export.json")
    files = export.get("files")
    if not _list_of_dicts(files) or len(files) > MAX_MEMBERS:
        report.fail("export.json has no files list")
        return report
    listed: dict[str, dict] = {}
    for entry in files:
        path = entry.get("path")
        if not safe_name(path):
            report.fail(f"export.json lists an unsafe or missing path: {path!r}")
            continue
        if path in listed:
            report.fail(f"export.json lists {path} twice")
            continue
        if path == "export.json":
            report.fail("export.json lists itself")
            continue
        if not (isinstance(entry.get("sha256"), str) and SHA256.match(entry["sha256"])):
            report.fail(f"export.json: {path} has no valid sha256")
            continue
        if not (_is_int(entry.get("bytes")) and entry["bytes"] >= 0):
            report.fail(f"export.json: {path} has no valid byte count")
            continue
        listed[path] = entry
    present = container.names() - {"export.json"}
    for path in sorted(set(listed) - present):
        report.fail(f"missing: {path} is listed in export.json but not in the package")
    for path in sorted(present - set(listed)):
        report.fail(f"unlisted: {path} is in the package but not in export.json")
    for path in sorted(set(listed) & present):
        sha, length = container.digest(path)
        if length != listed[path]["bytes"]:
            report.fail(f"size: {path} is {length} bytes, export.json says {listed[path]['bytes']}")
        elif sha != listed[path]["sha256"]:
            report.fail(f"checksum: {path} does not match export.json")
    report.checked.append(f"{len(set(listed) & present)} files against export.json (SHA-256)")

    for required in UPLOAD_REQUIRED:
        if required not in present:
            report.fail(f"missing: {required} is required in an upload package")
    if not report.ok:
        return report

    manifest = _json(container, "manifest.json", report)
    if not isinstance(manifest, dict):
        report.fail("manifest.json is not an object")
        return report
    duration = manifest.get("duration_us")
    if not (_is_int(duration) and duration > 0):
        report.fail("manifest.json has no positive duration_us")
        duration = None
    ident, revision = manifest.get("id"), manifest.get("revision")
    if not (isinstance(ident, str) and ident) or export.get("source_package") != ident:
        report.fail("export.json source_package differs from manifest.json id, or one is missing")
    if (
        not (_is_int(revision) and _is_int(export.get("revision")))
        or export["revision"] != revision
    ):
        report.fail("export.json revision differs from manifest.json revision, or one is missing")

    assets, report_rows = manifest.get("assets"), export.get("assets")
    if not _list_of_dicts(assets) or not _list_of_dicts(report_rows):
        report.fail("manifest.json or export.json has no assets list")
        return report
    by_id: dict[str, dict] = {}
    for asset in assets:
        key = asset.get("id")
        if not isinstance(key, str) or key in by_id:
            report.fail(f"manifest.json: asset id {key!r} missing or repeated")
            continue
        by_id[key] = asset
    reported = {row.get("id"): row for row in report_rows}
    if set(reported) != set(by_id) or len(reported) != len(report_rows):
        report.fail("export.json assets do not name exactly the manifest's assets")
    for key, asset in by_id.items():
        row = reported.get(key)
        if row is None:
            continue
        path = asset.get("path")
        if row.get("path") != path or row.get("kind") != asset.get("kind"):
            report.fail(f"asset {key}: export.json and manifest.json disagree on path or kind")
            continue
        if row.get("availability") == "included":
            if path not in listed:
                report.fail(f"asset {key} is marked included but {path!r} is not in the package")
            elif (listed[path]["sha256"], listed[path]["bytes"]) != (
                asset.get("sha256"),
                asset.get("bytes"),
            ):
                report.fail(f"asset {key}: {path} differs from the manifest's hash or size")
        elif row.get("availability") == "omitted":
            if path in present:
                report.fail(f"asset {key} is marked omitted but {path} is in the package")
            if not row.get("reason"):
                report.fail(f"asset {key} is omitted without a reason")
        else:
            report.fail(f"asset {key}: unknown availability {row.get('availability')!r}")
        if duration is not None and not _span_ok(asset, duration):
            report.fail(f"asset {key}: times outside the video")
    included = sorted(k for k, r in reported.items() if r.get("availability") == "included")
    pictures = {k for k in included if by_id.get(k, {}).get("kind") in {"frame", "crop"}}
    images = export.get("images")
    if not _list_of_dicts(images):
        report.fail("export.json has no images list")
    elif len(images) != len(pictures) or {image.get("id") for image in images} != pictures:
        report.fail("export.json images do not name exactly the included frames and crops")
    report.checked.append(
        f"{len(by_id)} assets against manifest.json ({len(included)} included, "
        f"{len(by_id) - len(included)} omitted)"
    )

    references = manifest.get("references", [])
    if not _list_of_dicts(references) or not all(isinstance(r.get("id"), str) for r in references):
        report.fail("manifest.json references is not a list of entries with ids")
        references = []
    known = set(by_id) | {r["id"] for r in references}
    counts = export.get("record_counts")
    if not isinstance(counts, dict):
        report.fail("export.json has no record_counts")
        counts = {}
    record_ids: set[str] = set()
    notes: list[dict] = []
    for name in ("observations.jsonl", "transcript.jsonl", "timeline.jsonl"):
        rows = _jsonl(container, name, report)
        if not _is_int(counts.get(name)) or counts[name] != len(rows):
            report.fail(f"{name}: {len(rows)} records, export.json says {counts.get(name)!r}")
        if name == "observations.jsonl":
            # Only what the disagreement check needs, so the rows can still go.
            notes = [
                {"id": row.get("id"), "facts": row.get("facts")} if isinstance(row, dict) else None
                for row in rows
            ]
        for number, row in enumerate(rows, 1):
            if not _span_ok(row, duration):
                report.fail(f"{name} record {number}: times missing, reversed or past the video")
            if name == "timeline.jsonl":
                continue
            key = row.get("id")
            if not isinstance(key, str) or key in record_ids:
                report.fail(f"{name} record {number}: id missing or repeated")
            record_ids.add(key)
            if not isinstance(row.get("text"), str):
                report.fail(f"{name} record {number}: no text")
            links = row.get("evidence_ids")
            if (
                not isinstance(links, list)
                or not all(isinstance(link, str) for link in links)
                or not set(links) <= known
            ):
                report.fail(f"{name} record {number}: names evidence the package does not have")
        report.checked.append(f"{len(rows)} records in {name}")
        del rows  # one file's rows in memory at a time
    _check_disagreements(notes, "observations.jsonl record", report)

    coverage = _json(container, "coverage.json", report)
    if not isinstance(coverage, dict) or not _is_int(coverage.get("duration_us")):
        report.fail("coverage.json has no duration_us")
    elif coverage["duration_us"] != duration:
        report.fail("coverage.json duration_us differs from manifest.json")
    if "transcript.vtt" in present and _webvtt(container.read("transcript.vtt")) is None:
        report.fail("transcript.vtt is not UTF-8 WebVTT with \\n line ends")
    provenance = _json(container, "provenance.json", report)
    if isinstance(provenance, dict) and "source_file_sha256" in provenance:
        report.notes.append(
            "provenance.json's source_file_sha256 are of the stored files before export and "
            "are not checked; export.json is what this ZIP is verified against."
        )
    return report


def _check_disagreements(notes: list, label: str, report) -> None:
    """SPEC 3.4.1: a note's facts.disagrees_with names other notes of the same file.

    Facts of a shape this reader does not know are left alone (a later version may change
    them; 3.4.1 says to keep such a note): only an id that is named and wrong fails.
    """
    ids = [n.get("id") if isinstance(n, dict) else None for n in notes]
    for number, note in enumerate(notes, 1):
        facts = note.get("facts") if isinstance(note, dict) else None
        others = facts.get("disagrees_with") if isinstance(facts, dict) else None
        if isinstance(others, list) and any(
            other not in ids or other == ids[number - 1] for other in others
        ):
            report.fail(f"{label} {number}: facts name a note that is not in the file")


# --------------------------------------------------------------------------- link layout


def _vtt_stamp(us: int) -> str:
    ms = us // 1000
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


CUE_TIMES = re.compile(
    r"([0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}) --> ([0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3})"
)


def _cues(vtt: str) -> list[tuple[str, str, str] | None] | None:
    """Every cue in a WebVTT file: (start, end, text), or None for a block that is neither
    a cue nor a NOTE. None for the whole file when its header block holds more than the
    WEBVTT line. A cue's number is optional in WebVTT, so a block is a cue when it has a
    times line, numbered or not; a NOTE block with a times line is a cue to a player, so it
    is not passed over as a comment here either."""
    blocks = re.split(r"\n{2,}", vtt.strip("\n"))
    if "\n" in blocks[0]:
        return None
    cues: list[tuple[str, str, str] | None] = []
    for block in blocks[1:]:
        lines = block.split("\n")
        if re.match(r"NOTE($|[ \t])", lines[0]) and "-->" not in block:
            continue
        timing = 0 if "-->" in lines[0] else 1
        times = CUE_TIMES.fullmatch(lines[timing]) if len(lines) > timing else None
        cues.append((*times.groups(), " ".join(lines[timing + 1 :])) if times else None)
    return cues


def verify_link(container: Container, version: str) -> Report:
    report = Report("link", version)
    manifest = _json(container, "manifest.json")
    present = container.names()
    required = {"manifest.json", "transcript.jsonl", "transcript.vtt", "analysis.json"}
    if version != "youtube-evidence-gemini-url-v1":
        required.add("observations.jsonl")
    thumbs = manifest.get("thumbnails", [])
    most = 4 if version == "youtube-evidence-gemini-url-v1" else 1
    if not _list_of_dicts(thumbs) or len(thumbs) > most:
        report.fail(f"manifest.json thumbnails is not a list of at most {most}")
        return report
    expected = set(required)
    for thumb in thumbs:
        sha, path = thumb.get("sha256"), thumb.get("package_path")
        good_sha = isinstance(sha, str) and SHA256.match(sha)
        if not good_sha or path != f"assets/thumbnails/{sha}.jpg":
            report.fail(f"manifest.json: thumbnail entry with a bad hash or path: {path!r}")
            continue
        expected.add(path)
    for name in sorted(expected - present):
        report.fail(f"missing: {name}")
    for name in sorted(present - expected):
        report.fail(f"unexpected: {name} is not part of a link package")
    source, analysis = manifest.get("source"), manifest.get("analysis")
    receipt, transcript = manifest.get("analysis_response"), manifest.get("transcript")
    if not all(isinstance(part, dict) for part in (source, analysis, receipt, transcript)):
        report.fail("manifest.json lacks source, analysis, analysis_response or transcript")
    if not report.ok:
        return report

    sha, length = container.digest("analysis.json")
    if (sha, length) != (receipt.get("sha256"), receipt.get("size_bytes")):
        report.fail("checksum: analysis.json does not match manifest.json analysis_response")
    for thumb in thumbs:
        sha, length = container.digest(thumb["package_path"])
        if (sha, length) != (thumb.get("sha256"), thumb.get("size_bytes")):
            report.fail(f"checksum: {thumb['package_path']} does not match manifest.json")
    report.checked.append(f"{1 + len(thumbs)} files against manifest.json (SHA-256)")

    video = source.get("video_id")
    if (
        not (isinstance(video, str) and VIDEO_ID.match(video))
        or source.get("canonical_url") != f"https://www.youtube.com/watch?v={video}"
    ):
        report.fail("manifest.json source is not a canonical YouTube watch address")
    clip_end = analysis.get("clip_end_us")
    if not (_is_int(clip_end) and clip_end > 0):
        report.fail("manifest.json analysis has no positive clip_end_us")
        clip_end = None

    segments = transcript.get("segments")
    rows = _jsonl(container, "transcript.jsonl", report)
    if not isinstance(segments, list):
        report.fail("manifest.json has no transcript segments")
    elif rows != segments:
        report.fail("transcript.jsonl differs from the transcript in manifest.json")
    previous_end = 0
    for number, row in enumerate(rows, 1):
        if not _span_ok(row, clip_end) or not isinstance(row.get("text"), str):
            report.fail(f"transcript.jsonl line {number}: times or text invalid")
        elif row["start_us"] < previous_end:
            report.fail(f"transcript.jsonl line {number}: overlaps the line before")
        else:
            previous_end = row["end_us"]
    report.checked.append(f"{len(rows)} transcript lines against manifest.json")

    vtt = _webvtt(container.read("transcript.vtt"))
    cues = _cues(vtt) if vtt is not None else None
    if vtt is None:
        report.fail("transcript.vtt is not UTF-8 WebVTT with \\n line ends")
    elif cues is None:
        report.fail("transcript.vtt: its header holds more than the WEBVTT line")
    elif len(cues) != len(rows) or vtt.count("-->") != len(rows):
        report.fail(f"transcript.vtt has {len(cues)} cues for {len(rows)} transcript lines")
    else:
        for number, (row, cue) in enumerate(zip(rows, cues), 1):  # noqa: B905 - 3.9
            if not _span_ok(row, None) or not isinstance(row.get("text"), str):
                continue  # reported above
            # Cues round inward to whole milliseconds; the words are the line's, with its
            # spaces collapsed and <, > and & escaped.
            wanted = (
                _vtt_stamp(-(-row["start_us"] // 1000) * 1000),
                _vtt_stamp(row["end_us"]),
                html.escape(" ".join(row["text"].split()), quote=False),
            )
            if cue != wanted:
                report.fail(f"transcript.vtt cue {number} does not match transcript.jsonl")
                break
        report.checked.append(f"{len(cues)} subtitle cues against transcript.jsonl")
    del rows, cues, segments  # the notes are read next

    if "observations.jsonl" in present:
        notes = _jsonl(container, "observations.jsonl", report)
        copies = manifest.get("observations")
        if not isinstance(copies, list) or not all(isinstance(c, dict) for c in copies):
            report.fail("manifest.json observations is not a list")
        elif copies:
            if len(copies) != len(notes) or any(
                (n.get("start_us"), n.get("end_us"), n.get("text"))
                != (c.get("start_us"), c.get("end_us"), c.get("text"))
                for n, c in zip(notes, copies)  # noqa: B905 - 3.9
            ):
                report.fail("observations.jsonl differs from the notes in manifest.json")
        ids: set[str] = set()
        for number, note in enumerate(notes, 1):
            if not _span_ok(note, clip_end) or not isinstance(note.get("text"), str):
                report.fail(f"observations.jsonl line {number}: times or text invalid")
            key = note.get("id")
            if not isinstance(key, str) or key in ids:
                report.fail(f"observations.jsonl line {number}: id missing or repeated")
            ids.add(key)
            if note.get("evidence_ids") not in ([], None):
                report.fail(f"observations.jsonl line {number}: a link package holds no frames")
        _check_disagreements(notes, "observations.jsonl line", report)
        report.checked.append(f"{len(notes)} on-screen notes")
    if analysis.get("source_bytes") != "not_held_by_keepsight":
        report.notes.append("manifest.json does not say the video's bytes were not held")
    report.notes.append(
        "In this layout only analysis.json and the cover image carry a SHA-256; manifest.json, "
        "the transcript and the notes are checked against each other and the ZIP's CRC-32."
    )
    return report


def verify(source: Path) -> tuple[Container, Report]:
    """Verify a package. The container stays open for reading; close it when done."""
    container = Container(Path(source))
    try:
        layout, version = detect(container)
        try:
            report = (verify_upload if layout == "upload" else verify_link)(container, version)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            # A field holds another kind of value than SPEC.md gives it (a list where an
            # object belongs, say). That is a failed package, not a crash.
            report = Report(layout, version)
            report.fail(f"malformed metadata ({type(error).__name__}: {error})")
    except BaseException:
        container.close()
        raise
    report.notes = container.notes + report.notes
    return container, report


# --------------------------------------------------------------------------- reading


class Package:
    """One view of either layout. Open with Package.open(path); it verifies first."""

    def __init__(self, container: Container, report: Report):
        self.container = container
        self.report = report
        self.layout = report.layout
        try:
            self.manifest = _json(container, "manifest.json")
        except BaseException:
            container.close()
            raise

    @classmethod
    def open(cls, source, *, require_verified: bool = True) -> Package:
        container, report = verify(Path(source))
        if require_verified and not report.ok:
            container.close()
            raise ValueError("package failed verification: " + "; ".join(report.problems))
        return cls(container, report)

    def read(self, name: str) -> bytes:
        return self.container.read(name, MAX_TOTAL_BYTES)

    def close(self) -> None:
        self.container.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def duration_us(self) -> int:
        if self.layout == "upload":
            return self.manifest["duration_us"]
        return self.manifest["analysis"]["clip_end_us"]

    @property
    def source(self) -> dict:
        if self.layout == "link":
            return {"kind": "youtube", "url": self.manifest["source"]["canonical_url"]}
        source = self.manifest.get("source")
        return {
            "kind": "upload",
            "title": source.get("title") if isinstance(source, dict) else None,
        }

    def _rows(self, name: str) -> list[dict]:
        if name not in self.container.names():
            return []
        return [_loads(line) for line in _lines(self.read(name).decode("utf-8"))]

    def _pictures(self) -> list[dict]:
        if self.layout != "upload":
            return []
        export = _json(self.container, "export.json")
        included = {a["id"] for a in export["assets"] if a.get("availability") == "included"}
        return [
            asset
            for asset in self.manifest["assets"]
            if asset.get("kind") in {"frame", "crop"} and asset["id"] in included
        ]

    def transcript(self) -> list[dict]:
        """Spoken lines: start_us, end_us, text."""
        return [
            {"start_us": r["start_us"], "end_us": r["end_us"], "text": r["text"]}
            for r in self._rows("transcript.jsonl")
        ]

    def notes(self) -> list[dict]:
        """What was on screen or happened: start_us, end_us, category, text, and frames
        (paths of included still images that back the note; uploads only) or watch_url
        (links: the public video opened at that second); facts where the package has them
        (SPEC 3.4.1)."""
        paths = {asset["id"]: asset["path"] for asset in self._pictures()}
        notes = []
        for r in self._rows("observations.jsonl"):
            note = {k: r.get(k) for k in ("start_us", "end_us", "category", "text")}
            if "facts" in r:
                note["facts"] = r["facts"]
            if self.layout == "upload":
                note["frames"] = [paths[i] for i in r.get("evidence_ids", []) if i in paths]
            else:
                note["watch_url"] = r.get("watch_url")
            notes.append(note)
        return notes

    def frames(self) -> list[dict]:
        """Still images taken from the video (uploads), with their time on the video."""
        return [
            {k: asset.get(k) for k in ("id", "path", "start_us", "width", "height", "sha256")}
            for asset in self._pictures()
        ]


# --------------------------------------------------------------------------- command line

# Shown as escapes, never sent to the terminal: control characters (escape sequences),
# invisible format characters (bidi overrides, zero-width), line and paragraph separators
# and lone surrogates. Everything in a package is someone else's text.
HIDDEN = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _shown(value) -> str:
    text = value if isinstance(value, str) else str(value)
    return "".join(
        ch
        if ch == "\t" or unicodedata.category(ch) not in HIDDEN
        else (f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        for ch in text
    )


def _say(line="") -> None:
    print(_shown(line))


def _clock(us: int) -> str:
    seconds = us // 1_000_000
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def _print_report(report: Report, source: Path) -> None:
    state = "VERIFIED" if report.ok else "FAILED"
    _say(f"{state}: {source} ({report.layout} package, format {report.version})")
    for line in report.checked:
        _say(f"  checked {line}")
    for line in report.problems:
        _say(f"  problem: {line}")
    if report.unlisted:
        _say(f"  ... and {report.unlisted} more problems")
    for line in report.notes:
        _say(f"  note: {line}")
    if report.ok:
        _say("  Checksums show the files are intact and agree; not who made them or that the")
        _say("  notes are true. Treat everything in a package as untrusted content.")


def _extract(container: Container, destination: Path) -> int:
    """Unpack into a new folder. Files go into a hidden folder beside it first and it is
    renamed only when every file is written and matches what verification read, so a
    failed or interrupted extract never leaves a half package under the name asked for."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise Unreadable(f"{destination} already exists; give a new folder")
    parent = destination.parent
    if not parent.is_dir():
        raise Unreadable(f"{parent} is not a folder; make it first")
    work = Path(tempfile.mkdtemp(prefix=".keepsight-extract-", dir=parent))
    try:
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(work, 0o777 & ~mask)  # mkdtemp's 0700, made an ordinary folder
        for name in sorted(container.names()):
            target = work.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = hashlib.sha256()
            with open(target, "xb") as handle:
                for chunk in container._chunks(name, MAX_TOTAL_BYTES):
                    sha.update(chunk)
                    handle.write(chunk)
            if container.seen(name) != sha.hexdigest():
                raise Unreadable(f"{name} changed after it was verified")
        os.rename(work, destination)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return len(container.names())


def _quiet_stdout() -> None:
    # The reader of our output (head, a pager) has gone; later writes must not fail.
    try:
        descriptor = os.open(os.devnull, os.O_WRONLY)
        os.dup2(descriptor, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="keepsight_package.py", description="Read and verify Keepsight video packages."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in [
        ("verify", "check every file against the package's own checksums"),
        ("info", "verify, then describe the package"),
        ("transcript", "verify, then print the transcript"),
        ("notes", "verify, then print the on-screen notes"),
    ]:
        command = commands.add_parser(name, help=text)
        command.add_argument("package", type=Path, help="a package ZIP or unpacked folder")
        command.add_argument("--json", action="store_true", help="print JSON")
        if name == "transcript":
            command.add_argument("--vtt", action="store_true", help="print the WebVTT file")
            command.add_argument(
                "--raw",
                action="store_true",
                help="with --vtt: write the file's bytes as they are, escape codes included",
            )
    command = commands.add_parser("extract", help="verify, then unpack into a new folder")
    command.add_argument("package", type=Path)
    command.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if getattr(args, "raw", False) and not args.vtt:
        parser.error("--raw goes with --vtt")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    as_json = getattr(args, "json", False)

    state: dict = {}
    try:
        try:
            code = _run(args, as_json, state)
            sys.stdout.flush()
            return code
        except BrokenPipeError:
            _quiet_stdout()
            report = state.get("report")
            return 0 if report is not None and report.ok else 1
    except Unreadable as error:
        try:
            if as_json and args.command == "verify":
                unreadable = {"verified": False, "unreadable": str(error), "reader": __version__}
                print(json.dumps(unreadable, indent=2))
                sys.stdout.flush()
            print(f"UNREADABLE: {_shown(error)}", file=sys.stderr)
        except BrokenPipeError:
            _quiet_stdout()
        return 2
    except BrokenPipeError:
        _quiet_stdout()
        return 2
    except OSError as error:
        print(f"UNREADABLE: {_shown(error)}", file=sys.stderr)
        return 2
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        print(
            f"FAILED: malformed metadata ({type(error).__name__}: {_shown(error)})", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        return 130


def _run(args, as_json: bool, state: dict) -> int:
    container, report = verify(args.package)
    state["report"] = report
    if args.command == "verify":
        if as_json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            _print_report(report, args.package)
        return 0 if report.ok else 1
    if not report.ok:
        _print_report(report, args.package)
        return 1
    package = Package(container, report)
    if args.command == "extract":
        count = _extract(container, args.destination)
        _say(f"verified and unpacked {count} files into {args.destination}")
    elif args.command == "info":
        info = {
            "layout": package.layout,
            "version": report.version,
            "source": package.source,
            "duration_us": package.duration_us,
            "transcript_lines": len(package.transcript()),
            "notes": len(package.notes()),
            "frames": len(package.frames()),
            "verified": True,
        }
        if as_json:
            print(json.dumps(info, indent=2))
        else:
            for key, value in info.items():
                _say(f"{key}: {value}")
    elif args.command == "transcript":
        if args.vtt:
            if "transcript.vtt" not in container.names():
                print("NOT AVAILABLE: this package has no transcript.vtt", file=sys.stderr)
                return 2
            data = package.read("transcript.vtt")
            if args.raw:
                sys.stdout.flush()
                sys.stdout.buffer.write(data)
            else:
                for line in data.decode("utf-8").split("\n"):
                    _say(line)
        elif as_json:
            print(json.dumps(package.transcript(), indent=2))
        else:
            for line in package.transcript():
                _say(f"[{_clock(line['start_us'])}] {line['text']}")
    elif args.command == "notes":
        notes = package.notes()
        if as_json:
            print(json.dumps(notes, indent=2))
        else:
            for note in notes:
                where = ", ".join(note.get("frames") or []) or note.get("watch_url") or ""
                _say(f"[{_clock(note['start_us'])}] {note['category']}: {note['text']}")
                if where:
                    _say(f"    {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
