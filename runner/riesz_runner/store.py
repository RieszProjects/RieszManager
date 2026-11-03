"""Index of Claude Code session transcripts on disk, with incremental reads."""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .transcript import MetaScanner, TranscriptView

VIEW_CACHE_SIZE = 12


def _read_increment(path: Path, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    """Read complete lines appended after `offset`. Returns ([(line_offset, line)], new_offset)."""
    lines: list[tuple[int, bytes]] = []
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return lines, offset
    end = data.rfind(b"\n")
    if end == -1:
        return lines, offset  # only a partial line so far
    pos = 0
    chunk = data[: end + 1]
    for raw in chunk.split(b"\n")[:-1]:
        if raw.strip():
            lines.append((offset + pos, raw))
        pos += len(raw) + 1
    return lines, offset + end + 1


@dataclass
class _Cursor:
    offset: int = 0
    inode: int = 0


@dataclass
class SessionEntry:
    session_id: str
    path: Path
    size: int = 0
    mtime: float = 0.0
    meta_cursor: _Cursor = field(default_factory=_Cursor)
    scanner: MetaScanner = field(default_factory=MetaScanner)
    custom_title_file: str | None = None

    def summary(self) -> dict[str, Any]:
        meta = self.scanner.meta.to_dict()
        if self.custom_title_file and not meta.get("customTitle"):
            meta["customTitle"] = self.custom_title_file
            meta["title"] = self.custom_title_file
        meta.update(
            sessionId=self.session_id,
            projectDir=self.path.parent.name,
            size=self.size,
            mtime=self.mtime,
        )
        return meta


@dataclass
class LoadedView:
    view: TranscriptView
    cursor: _Cursor


class SessionStore:
    """Keeps lightweight metadata for every transcript and full views for a few."""

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir
        self.entries: dict[str, SessionEntry] = {}
        self.views: OrderedDict[str, LoadedView] = OrderedDict()
        self._last_miss_refresh = 0.0

    # ---- discovery ----------------------------------------------------------

    def _iter_files(self) -> Iterator[Path]:
        if not self.projects_dir.is_dir():
            return
        for project in self.projects_dir.iterdir():
            if not project.is_dir():
                continue
            try:
                for file in project.iterdir():
                    if file.suffix == ".jsonl" and file.is_file():
                        yield file
            except OSError:
                continue

    def refresh(self) -> list[str]:
        """Rescan the projects dir; returns ids of sessions whose metadata changed."""
        changed: list[str] = []
        seen: set[str] = set()
        for file in self._iter_files():
            sid = file.stem
            seen.add(sid)
            try:
                st = file.stat()
            except OSError:
                continue
            entry = self.entries.get(sid)
            if entry is None or entry.path != file:
                entry = SessionEntry(session_id=sid, path=file)
                self.entries[sid] = entry
            if st.st_size != entry.size or st.st_mtime != entry.mtime:
                self._scan_meta(entry, st)
                changed.append(sid)
        for sid in list(self.entries):
            if sid not in seen:
                del self.entries[sid]
                self.views.pop(sid, None)
                changed.append(sid)
        return changed

    def _scan_meta(self, entry: SessionEntry, st: os.stat_result) -> None:
        cursor = entry.meta_cursor
        if st.st_ino != cursor.inode or st.st_size < cursor.offset:
            entry.scanner = MetaScanner()
            cursor.offset = 0
            cursor.inode = st.st_ino
        try:
            lines, cursor.offset = _read_increment(entry.path, cursor.offset)
        except OSError:
            return
        for _, raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                entry.scanner.feed(rec)
        entry.size = st.st_size
        entry.mtime = st.st_mtime
        title_file = entry.path.parent / entry.session_id / "custom-title.json"
        if title_file.is_file():
            try:
                entry.custom_title_file = json.loads(title_file.read_text()).get("customTitle")
            except (OSError, ValueError):
                pass

    def get(self, session_id: str) -> SessionEntry | None:
        entry = self.entries.get(session_id)
        if entry is None and time.monotonic() - self._last_miss_refresh > 1.0:
            self._last_miss_refresh = time.monotonic()
            self.refresh()
            entry = self.entries.get(session_id)
        return entry

    # ---- full views ---------------------------------------------------------

    def view(self, session_id: str) -> tuple[TranscriptView, list[dict[str, Any]], bool] | None:
        """Return (view, changed_items, was_reset) after catching up with the file."""
        entry = self.get(session_id)
        if entry is None:
            return None
        loaded = self.views.get(session_id)
        reset = False
        try:
            st = entry.path.stat()
        except OSError:
            return None
        if loaded is None or st.st_ino != loaded.cursor.inode or st.st_size < loaded.cursor.offset:
            loaded = LoadedView(view=TranscriptView(), cursor=_Cursor(0, st.st_ino))
            self.views[session_id] = loaded
            reset = True
        self.views.move_to_end(session_id)
        while len(self.views) > VIEW_CACHE_SIZE:
            self.views.popitem(last=False)
        if st.st_size > loaded.cursor.offset:
            lines, loaded.cursor.offset = _read_increment(entry.path, loaded.cursor.offset)
            for off, raw in lines:
                loaded.view.feed_line(raw, off)
        changed = loaded.view.take_dirty()
        return loaded.view, changed, reset

    def append_record(self, session_id: str, record: dict[str, Any]) -> bool:
        """Append one JSON record to a transcript (as Claude Code's own SDK does for renames).

        Waits briefly if another process is in the middle of writing a line, so the
        new record never gets glued onto a partial one.
        """
        entry = self.get(session_id)
        if entry is None:
            return False
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        for _ in range(20):
            with entry.path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size:
                    fh.seek(size - 1)
                    last = fh.read(1)
                else:
                    last = b"\n"
            if last == b"\n":
                break
            time.sleep(0.05)
        else:
            line = b"\n" + line
        with entry.path.open("ab") as fh:
            fh.write(line)
        return True

    def read_line_at(self, session_id: str, offset: int) -> bytes | None:
        entry = self.get(session_id)
        if entry is None or offset < 0:
            return None
        try:
            with entry.path.open("rb") as fh:
                fh.seek(offset)
                return fh.readline()
        except OSError:
            return None

    def find(self, predicate: Callable[[SessionEntry], bool]) -> list[SessionEntry]:
        return [e for e in self.entries.values() if predicate(e)]
