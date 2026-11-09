"""Permission requests the runner answers by itself.

In bypassPermissions mode Claude Code still asks before a Bash command whenever
it cannot rule out a dangerous removal. That happens for removals it cannot
analyse, but also for commands without any removal that merely confuse its
parser, such as a Python heredoc full of triple quotes ("the shell may read this
command differently than it was parsed"). With RUNNER_ALLOW_TMP_REMOVAL on, the
runner reads the command itself and approves when

- every `rm`, `rmdir`, `unlink` or `shred` in it removes only literal paths inside
  a project's `tmp` folder (<workspace>/<project>/.../tmp), or
- there is no removal in it at all and Claude Code asked only because of a possible
  removal (the reason it gives mentions rm).

Its reading is deliberately stricter than the shell's:

- heredocs are split off the way bash does it (quotes respected); the body of a
  quoted heredoc (<<'EOF') is data for the program reading it, the body of an
  unquoted one must not contain `$(...)` or backticks, which the shell would run;
- the rest is tokenised like the shell (quotes removed, so r''m is still rm);
  nothing may run another shell or commands from data: no bash/sh/..., eval,
  source, xargs, sudo, exec, find -delete/-exec, backticks, $'...', and no command
  whose name is a variable;
- if a word like rm appears anywhere in the text, even inside a heredoc, it must
  be one of the removals checked above;
- removal targets are literal (no variables, `~`, `..`); glob characters only
  after a part of the path inside tmp, or a bare glob in a folder known to be one;
- `cd`s are followed only where their effect is certain. A `cd` inside a subshell,
  group, loop, condition or pipeline, or to `..`, `~` or a variable, is refused.
  Claude Code's Bash tool keeps the folder of an earlier `cd`, so the command may
  already run in a subfolder of the session's folder: a relative path must name
  the tmp folder itself or follow a `cd` into one. A failed `cd` does not stop
  commands after `;` or `||`, so from there a path must be inside tmp both where
  the `cd` led and where the command was before it;
- the command must not mention Claude Code's settings (`.claude`, settings.json),
  whose protection is a different check that stays with the user.

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
# Words after which the next word is still a command name.
PREFIXES = {"time", "nohup", "nice", "command", "builtin", "env", "then", "do", "else", "elif", "!", "if", "while", "until"}
BLOCK_OPEN = {"for", "while", "until", "if", "case", "select", "function"}
BLOCK_CLOSE = {"done", "fi", "esac"}
GLOB = set("*?[")
SENSITIVE = re.compile(r"\.claude|settings(\.local)?\.json", re.I)
REMOVAL_REASON = re.compile(r"\brm\b|\brmdir\b|remov|delet|unlink", re.I)
REMOVAL_WORD = re.compile(r"(^|[\s;&|(){}`])\\?(?:/\S*/)?(rm|rmdir|unlink|shred)(\s|$)", re.M)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC_OP = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?[A-Za-z0-9_.-]+))")


def _split_heredocs(text: str) -> tuple[str, list[tuple[bool, str]]] | None:
    """(shell text, [(delimiter quoted, body)]) the way bash reads heredocs; None if unclear."""
    out: list[str] = []
    bodies: list[tuple[bool, str]] = []
    pending: list[tuple[str, bool, bool]] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            out.append(ch)
            quote = None if ch == "'" else quote
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            out.append(ch)
            quote = None if ch == '"' else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            match = HEREDOC_OP.match(text, i)
            if not match:
                return None
            if match.group(2) is not None or match.group(3) is not None:
                delim, quoted = match.group(2) if match.group(2) is not None else match.group(3), True
            else:
                delim, quoted = match.group(4).lstrip("\\"), match.group(4).startswith("\\")
            pending.append((delim, quoted, bool(match.group(1))))
            out.append(match.group(0))
            i = match.end()
            continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, quoted, dash in pending:
                body: list[str] = []
                while True:
                    if i >= n:
                        return None  # no closing delimiter
                    j = text.find("\n", i)
                    j = n if j == -1 else j
                    line = text[i:j]
                    i = min(j + 1, n)
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
                    body.append(line)
                bodies.append((quoted, "\n".join(body)))
            pending = []
            continue
        out.append(ch)
        i += 1
    if quote or pending:
        return None
    return "".join(out), bodies


def _tokens(shell_text: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(shell_text.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _in_project_tmp(path: str, root: Path) -> bool:
    """Is `path` (absolute, normalised) a project's tmp folder or inside one?"""
    try:
        rel = Path(path).relative_to(root)
    except ValueError:
        return False
    # parts[0] is the project; a tmp folder at the workspace's top level is not a project's.
    return "tmp" in rel.parts[1:]


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
        # A bare glob ("*", "*.png") is relative to the folder the command runs in.
        head = arg[:glob_at]
        fixed = (head.rsplit("/", 1)[0] or "/") if "/" in head else "."
    parts = [p for p in fixed.split("/") if p not in ("", ".")]
    if os.path.isabs(fixed):
        path = os.path.normpath(fixed)
    elif where[0] == "abs":
        path = os.path.normpath(os.path.join(where[1], fixed))
    else:
        if "tmp" not in where[1] + parts:
            return False
        path = os.path.normpath(os.path.join(cwd, *where[1], fixed))
    return _in_project_tmp(path, root) and _in_project_tmp(os.path.realpath(path), root.resolve())


def _cd(where: tuple, arg: str) -> tuple:
    if os.path.isabs(arg):
        return ("abs", os.path.normpath(arg))
    if where[0] == "abs":
        return ("abs", os.path.normpath(os.path.join(where[1], arg)))
    return ("rel", where[1] + [p for p in arg.split("/") if p not in ("", ".")])


def auto_approvable(command: str, cwd: str | None, root: Path, reason: str | None = None) -> bool:
    """Should the runner approve this Bash command for the user? See the module docstring."""
    if not command or not cwd or SENSITIVE.search(command):
        return False
    if reason is not None and not REMOVAL_REASON.search(reason):
        return False
    split = _split_heredocs(command)
    if split is None:
        return False
    shell_text, bodies = split
    if "`" in shell_text or "$'" in shell_text:
        return False
    if any(not quoted and ("$(" in body or "`" in body) for quoted, body in bodies):
        return False
    tokens = _tokens(shell_text)
    if tokens is None or any(t in FORBIDDEN for t in tokens):
        return False

    possible: list[tuple] = [("rel", [])]
    before_cd: list[tuple] = []  # where the command was before a `cd` whose && chain is still running
    depth = blocks = 0
    removals = 0
    command_word = True
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name = os.path.basename(tok.lstrip("\\"))
        if tok in SEPARATORS:
            if tok in ("(", "{"):
                depth += 1
            elif tok in (")", "}"):
                depth -= 1
            if tok != "&&":
                possible, before_cd = possible + before_cd, []
            command_word = True
            i += 1
            continue
        if tok in BLOCK_OPEN:
            blocks += 1
        elif tok in BLOCK_CLOSE:
            blocks -= 1
        if command_word:
            if ASSIGNMENT.match(tok) or tok in PREFIXES or tok in BLOCK_OPEN or tok in BLOCK_CLOSE:
                i += 1
                continue
            if tok.startswith("$"):
                return False  # the command's name comes from a variable or substitution
            command_word = False
        if name == "cd":
            arg = tokens[i + 1] if i + 1 < len(tokens) else ""
            after = tokens[i + 2] if i + 2 < len(tokens) else ""
            if depth or blocks or after in ("|", "|&") or not arg or arg in SEPARATORS or "$" in arg \
                    or arg.startswith("~") or ".." in arg.split("/") or any(ch in GLOB for ch in arg):
                return False
            moved = [_cd(w, arg) for w in possible]
            if after == "&&":
                before_cd, possible = before_cd + possible, moved
            else:
                possible = possible + moved
            i += 2
            continue
        if name in REMOVERS:
            # Wherever the word stands, treat it as a removal and check what follows it.
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
            if not paths or not all(_target_ok(p, cwd, w, root) for p in paths for w in possible):
                return False
            removals += 1
            continue
        i += 1
    # A removal word the shell text does not account for (say, inside a heredoc): leave it to the user.
    if len(REMOVAL_WORD.findall(command)) > removals:
        return False
    return True
