"""Permission requests the runner answers by itself.

In bypassPermissions mode Claude Code still asks before a Bash command that
removes files whenever it cannot rule out a dangerous removal, which is the case
for most real commands: ones with a heredoc, `$(...)`, loops or several steps.
That stalls sessions that merely clean up their scratch space. With
RUNNER_ALLOW_TMP_REMOVAL on, such a command is approved when every removal in it
is plainly inside a project's `tmp` folder (at any depth below a workspace
project, e.g. <workspace>/<project>/tmp or .../sub/tmp):

- every `rm`, `rmdir`, `unlink` or `shred` in the shell text names only literal
  paths: no variables, `$(...)`, `~` or `..`; glob characters only after a part of
  the path that is already inside a tmp folder;
- an absolute path, and the real path behind any symlink, lies in a project's tmp
  folder; a relative path names the tmp folder itself (`tmp/x`, `web/tmp/x`) or
  follows a plain `cd` into one. Claude Code's Bash tool keeps the directory of
  an earlier `cd`, so the command may run in a subfolder of the session's folder
  and a bare name could point anywhere;
- heredoc bodies (a script fed to python and the like) are not shell text and
  are skipped, but nothing may hand text to another shell or run commands from
  data: no `bash`/`sh`/... , `eval`, `source`, `xargs`, `sudo`, `exec`, backticks,
  `find -delete`/`-exec`;
- the command must not mention Claude Code's settings (`.claude`, `settings.json`),
  whose protection is a different check that must stay with the user.

Anything else is left for the user to answer.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

REMOVERS = {"rm", "rmdir", "unlink", "shred"}
# Words that run other commands or hand text to a shell: never approve a command containing them.
FORBIDDEN = {"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "source", ".", "xargs", "sudo", "exec",
             "-delete", "-exec", "-execdir", "-ok", "-okdir"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "{", "}"}
REDIRECTS = {">", ">>", "<", "<<<", ">&", "<&", "&>", "&>>", ">|"}
BLOCK_OPEN = {"for", "while", "until", "if", "case", "select", "function"}
BLOCK_CLOSE = {"done", "fi", "esac"}
GLOB = set("*?[")
SENSITIVE = re.compile(r"\.claude|settings(\.local)?\.json", re.I)
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
REMOVAL_REASON = re.compile(r"\brm\b|\brmdir\b|remov|delet|unlink", re.I)


def _in_project_tmp(path: str, root: Path) -> bool:
    """Is `path` (absolute, normalised) a project's tmp folder or inside one?"""
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return False
    # parts[0] is the project; a tmp folder at the workspace's top level is not a project's.
    return "tmp" in rel.parts[1:]


def _strip_heredocs(text: str) -> str | None:
    """The shell text without heredoc bodies; None if a heredoc is not closed."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in HEREDOC.finditer(line):
            dash, delim = match.group(1), match.group(3)
            while i < len(lines) and (lines[i].lstrip("\t") if dash else lines[i]) != delim:
                i += 1
            if i >= len(lines):
                return None
            i += 1  # the delimiter line
    return "\n".join(out)


def _tokens(shell_text: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(shell_text.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _target_ok(arg: str, cwd: str, where: tuple, root: Path) -> bool:
    """Is a removal target plainly inside a project's tmp folder?

    `where` is the folder the command runs in as far as its `cd`s tell: ("rel", parts)
    relative to the session's folder (where the Bash tool may already be in a subfolder,
    so the path itself must name the tmp folder), or ("abs", path) after an absolute `cd`.
    """
    if not arg or arg.startswith("~") or "$" in arg or ".." in arg.split("/"):
        return False
    glob_at = next((i for i, ch in enumerate(arg) if ch in GLOB), None)
    fixed = arg
    if glob_at is not None:
        # Only the part before the last "/" preceding the glob is fixed; it must already be inside tmp.
        head = arg[:glob_at]
        fixed = head.rsplit("/", 1)[0] if "/" in head else ""
        if not fixed:
            return False
    parts = fixed.split("/")
    if os.path.isabs(fixed):
        path = os.path.normpath(fixed)
    elif where[0] == "abs":
        path = os.path.normpath(os.path.join(where[1], fixed))
    else:
        if "tmp" not in where[1] + parts:
            return False
        path = os.path.normpath(os.path.join(cwd, *where[1], fixed))
    return _in_project_tmp(path, root) and _in_project_tmp(os.path.realpath(path), root.resolve())


def removes_only_project_tmp(command: str, cwd: str | None, root: Path, reason: str | None = None) -> bool:
    if not command or not cwd or "`" in command or SENSITIVE.search(command):
        return False
    if reason and not REMOVAL_REASON.search(reason):
        return False
    shell_text = _strip_heredocs(command)
    if shell_text is None:
        return False
    tokens = _tokens(shell_text)
    if tokens is None or any(t in FORBIDDEN for t in tokens):
        return False
    # Follow `cd`s in order. A `cd` inside a subshell, group, loop or condition, or one that is part of a
    # pipeline, has an effect that depends on how the shell runs it: refuse those, and any `cd` whose target
    # is not a plain literal (`..`, `~`, variables), since it could lead anywhere, even to a system folder.
    where: tuple = ("rel", [])
    depth = blocks = 0
    removed = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name = os.path.basename(tok.lstrip("\\"))
        if tok in ("(", "{"):
            depth += 1
        elif tok in (")", "}"):
            depth -= 1
        elif tok in BLOCK_OPEN:
            blocks += 1
        elif tok in BLOCK_CLOSE:
            blocks -= 1
        if name == "cd":
            arg = tokens[i + 1] if i + 1 < len(tokens) else ""
            after = tokens[i + 2] if i + 2 < len(tokens) else ""
            if depth or blocks or after in ("|", "|&") or not arg or arg in SEPARATORS or "$" in arg \
                    or arg.startswith("~") or ".." in arg.split("/") or any(ch in GLOB for ch in arg):
                return False
            if os.path.isabs(arg):
                where = ("abs", os.path.normpath(arg))
            elif where[0] == "abs":
                where = ("abs", os.path.normpath(os.path.join(where[1], arg)))
            else:
                where = ("rel", where[1] + arg.split("/"))
            i += 2
            continue
        if name in REMOVERS:
            paths: list[str] = []
            i += 1
            while i < len(tokens) and tokens[i] not in SEPARATORS:
                t = tokens[i]
                if t in REDIRECTS:
                    i += 2  # the operator and its target
                    continue
                if t.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in REDIRECTS:
                    i += 1  # the file descriptor of "2>/dev/null"
                    continue
                if t == "--":
                    return False
                if not t.startswith("-"):
                    paths.append(t)
                i += 1
            if not paths or not all(_target_ok(p, cwd, where, root) for p in paths):
                return False
            removed = True
            continue
        i += 1
    return removed
