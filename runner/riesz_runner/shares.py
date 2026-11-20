"""Files a session's agent sent to the user (the send_file tool, tools.py), kept by the runner.

A sent file is copied to <state dir>/shared/<session id>/<key>/<name>, the key standing for the
original path: sending the same path again replaces the copy. The web app shows them where the
agent sent them and opens them in its file viewer as the pseudo-project "@shared/<session id>"
(server.py's _project_root), so only files an agent chose to send are reachable this way.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 200 * 1024 * 1024
PROJECT_PREFIX = "@shared/"
_SID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ShareError(Exception):
    """Shown to the agent (and user) as why a file could not be sent."""


class Shares:
    def __init__(self, state_dir: Path) -> None:
        self.base = state_dir / "shared"

    def root(self, session_id: str) -> Path:
        if not _SID.match(session_id):
            raise ShareError("not a session id")
        return self.base / session_id

    def project_root(self, project: str) -> Path | None:
        """The folder behind the viewer's pseudo-project "@shared/<session id>", if it is one."""
        if not project.startswith(PROJECT_PREFIX):
            return None
        try:
            return self.root(project[len(PROJECT_PREFIX):])
        except ShareError:
            return None

    def _index(self, session_id: str) -> dict[str, Any]:
        try:
            return json.loads((self.root(session_id) / "index.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def share(self, session_id: str, src: str, caption: str | None = None) -> dict[str, Any]:
        path = Path(src).expanduser()
        if not path.is_absolute():
            raise ShareError(f"需要绝对路径：{src}")
        path = Path(os.path.realpath(path))
        if not path.is_file():
            raise ShareError(f"不是文件或不存在：{src}")
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ShareError(f"文件太大（{size // 1024 // 1024} MB，最多 {MAX_BYTES // 1024 // 1024} MB）：{src}")
        key = hashlib.sha1(str(path).encode()).hexdigest()[:16]
        folder = self.root(session_id) / key
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        shutil.copy2(path, folder / path.name)
        entry = {"ref": f"{key}/{path.name}", "name": path.name, "path": str(path), "size": size,
                 "mime": mimetypes.guess_type(path.name)[0], "caption": caption or None, "ts": time.time()}
        index = self._index(session_id)
        index[str(path)] = entry
        tmp = self.root(session_id) / "index.json.tmp"
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.root(session_id) / "index.json")
        return {**entry, "project": PROJECT_PREFIX + session_id}

    def list(self, session_id: str) -> list[dict[str, Any]]:
        return [{**e, "project": PROJECT_PREFIX + session_id} for e in self._index(session_id).values()]
