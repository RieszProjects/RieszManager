"""Removal prompts the runner answers by itself.

In bypassPermissions mode Claude Code still asks before a Bash command whenever
it cannot rule out a dangerous removal: removals it cannot analyse, and also
commands that remove nothing but confuse its parser (a Python heredoc full of
triple quotes: "the shell may read this command differently than it was
parsed"). With RUNNER_AUTO_ALLOW_REMOVALS on, such a prompt (its reason mentions
a removal) is approved unless the command may remove system files.

A removal is fine when everything it can remove lies in one of these places
(checked for the path as written and for its real path behind symlinks):

- the workspace, below its root (<workspace>/<project>/...; not the root itself);
- /tmp or /var/tmp, below them;
- the home folder, below it, outside hidden entries (~/backups/x, not ~/.ssh,
  ~/.claude, ~/.local, nor the home folder itself).

Everything else (/, /etc, /usr, /var, other users, ...) is left to the user, and
so is any removal whose target cannot be worked out before it runs: a path
starting with an unknown variable (`rm -rf "$DIR"/*` is `rm -rf /*` when DIR is
empty), `xargs rm`, a command named by a variable, a `cd` to an unknown folder.

How the command is read:

- heredocs are split off the way bash does it; a body fed to a shell (bash <<EOF)
  is checked as commands, one fed to another program (python3 - <<'EOF') is that
  program's data; `bash -c '...'` and `eval '...'` are checked as commands too;
- the rest is tokenised like the shell (quotes removed, so r''m is still rm), and
  every rm/rmdir/unlink/shred anywhere in it is checked, as are the paths of a
  `find ... -delete` or `find ... -exec rm`;
- `$PWD`, `$HOME`, `~` and variables set to a literal earlier in the command are
  filled in; glob characters and other variables end the part of a path that is
  known, and what follows them may be anything below that part;
- `cd`s are followed, including `..`. A `cd` inside a subshell, loop or pipeline,
  or after which a later command runs even if it failed (`;`, `||`), leaves both
  folders possible, and every possibility must be fine. Claude Code's Bash tool
  may already be in a subfolder of the session's folder from an earlier `cd`;
  paths are worked out from the session's folder, which is never less safe;
- a command that mentions Claude Code's settings (`.claude`, settings.json) is
  left to the user, since their protection is a different check.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

REMOVERS = {"rm", "rmdir", "unlink", "shred"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "{", "}"}
REDIRECTS = {">", ">>", "<", "<<<", ">&", "<&", "&>", "&>>", ">|"}
# Words after which the next word is still a command name.
PREFIXES = {"time", "nohup", "nice", "command", "builtin", "env", "sudo", "then", "do", "else", "elif", "!",
            "if", "while", "until", "export", "local", "declare", "readonly"}
BLOCK_OPEN = {"for", "while", "until", "if", "case", "select", "function"}
BLOCK_CLOSE = {"done", "fi", "esac"}
DYNAMIC = set("$*?[{`")
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
SENSITIVE = re.compile(r"\.claude|settings(\.local)?\.json", re.I)
REMOVAL_REASON = re.compile(r"\brm\b|\brmdir\b|remov|delet|unlink", re.I)
REMOVAL_WORD = re.compile(r"(^|[\s;&|(){}`])\\?(?:/\S*/)?(rm|rmdir|unlink|shred)(\s|$)", re.M)
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
HEREDOC_OP = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?[A-Za-z0-9_.-]+))")
MAX_DEPTH = 4


class Unsure(Exception):
    """The command may remove something that is not clearly fine."""


def _split_heredocs(text: str) -> tuple[str, list[tuple[bool, str, int]]] | None:
    """(shell text, [(delimiter quoted, body, position of its operator)]) the way bash reads heredocs."""
    out: list[str] = []
    bodies: list[tuple[bool, str, int]] = []
    pending: list[tuple[str, bool, bool, int]] = []
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
            pending.append((delim, quoted, bool(match.group(1)), len("".join(out))))
            out.append(match.group(0))
            i = match.end()
            continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, quoted, dash, at in pending:
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
                bodies.append((quoted, "\n".join(body), at))
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


def _under(path: str, base: str) -> bool:
    """Strictly below `base`."""
    return path != base and path.startswith(base.rstrip("/") + "/")


class Checker:
    def __init__(self, root: Path, home: Path) -> None:
        self.root = os.path.normpath(str(root))
        self.home = os.path.normpath(str(home))

    # ---- places -----------------------------------------------------------------

    def _fine_place(self, path: str) -> bool:
        if _under(path, self.root):
            return True
        if _under(path, "/tmp") or _under(path, "/var/tmp"):
            return True
        if _under(path, self.home):
            return not os.path.relpath(path, self.home).split("/")[0].startswith(".")
        return False

    def fine(self, path: str) -> bool:
        """May everything at or below `path` be removed?"""
        path = os.path.normpath(path)
        return self._fine_place(path) and self._fine_place(os.path.realpath(path))

    # ---- paths ------------------------------------------------------------------

    def expand(self, word: str, cwd: str | None, env: dict[str, str]) -> str:
        """`word` with ~, $HOME, $PWD and known variables filled in (others left as they are)."""
        if word == "~" or word.startswith("~/"):
            word = self.home + word[1:]

        def sub(m: re.Match[str]) -> str:
            name = m.group(1) or m.group(2)
            if name == "HOME":
                return self.home
            if name == "PWD" and cwd is not None:
                return cwd
            return env.get(name, m.group(0))

        return VARIABLE.sub(sub, word)

    def target_fine(self, word: str, cwds: list[str | None], env: dict[str, str]) -> bool:
        for cwd in cwds:
            arg = self.expand(word, cwd, env)
            if arg.startswith("~"):
                return False  # ~user
            dyn = next((i for i, ch in enumerate(arg) if ch in DYNAMIC), None)
            if dyn is None:
                fixed = arg
            elif dyn == 0 and arg[0] in "$`{":
                return False  # could be any path, absolute ones included
            else:
                # What follows a glob or unknown variable may be anything below the part before it.
                head = arg[:dyn]
                fixed = head.rsplit("/", 1)[0] if "/" in head else "."
                fixed = fixed or "/"
            if not os.path.isabs(fixed):
                if cwd is None:
                    return False
                fixed = os.path.join(cwd, fixed)
            if not self.fine(fixed):
                return False
        return True

    # ---- commands ---------------------------------------------------------------

    def check(self, command: str, cwds: list[str | None], depth: int = 0) -> None:
        """Raise Unsure unless every removal in `command` is fine."""
        if depth > MAX_DEPTH:
            raise Unsure("nested too deeply")
        split = _split_heredocs(command)
        if split is None:
            raise Unsure("unclear heredoc")
        shell_text, bodies = split
        if "`" in shell_text or "$'" in shell_text:
            raise Unsure("backticks")
        for quoted, body, at in bodies:
            if not quoted and ("$(" in body or "`" in body):
                raise Unsure("unquoted heredoc runs commands")
            fed = _tokens(shell_text[:at].rsplit("\n", 1)[-1]) or []
            if any(os.path.basename(t) in SHELLS for t in fed):
                self.check(body, cwds, depth + 1)  # a script fed to a shell
        tokens = _tokens(shell_text)
        if tokens is None:
            raise Unsure("cannot tokenise")
        self._walk(tokens, list(cwds), depth)
        found = sum(1 for t in tokens if os.path.basename(t.lstrip("\\")) in REMOVERS)
        if len(REMOVAL_WORD.findall(shell_text)) > found:
            raise Unsure("a removal word not accounted for")

    def _walk(self, tokens: list[str], cwds: list[str | None], depth: int) -> None:
        env: dict[str, str] = {}
        before_cd: list[str | None] = []  # folders before a `cd` whose && chain is still running
        group = 0  # subshells, groups and loops: a cd in them may or may not last
        command_word = True
        xargs = False
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            name = os.path.basename(tok.lstrip("\\"))
            if tok in SEPARATORS:
                if tok in ("(", "{"):
                    group += 1
                elif tok in (")", "}"):
                    group -= 1
                if tok != "&&":
                    cwds, before_cd = _merge(cwds, before_cd), []
                if tok not in ("|", "|&"):
                    xargs = False
                command_word = True
                i += 1
                continue
            if tok in BLOCK_OPEN:
                group += 1
            elif tok in BLOCK_CLOSE:
                group -= 1
            if command_word:
                assign = ASSIGNMENT.match(tok)
                if assign:
                    value = self.expand(assign.group(2), cwds[0] if len(cwds) == 1 else None, env)
                    if any(ch in DYNAMIC for ch in value):
                        env.pop(assign.group(1), None)
                    else:
                        env[assign.group(1)] = value
                    i += 1
                    continue
                if tok in PREFIXES or tok in BLOCK_OPEN or tok in BLOCK_CLOSE:
                    i += 1
                    continue
                if tok.startswith("$"):
                    raise Unsure("command named by a variable")
                command_word = False
                if name == "xargs":
                    xargs = True
                if name in SHELLS or name == "eval":
                    i = self._nested(tokens, i, name, cwds, depth)
                    continue
                if name == "find":
                    i = self._find(tokens, i, cwds, env)
                    continue
            if name == "cd":
                arg = tokens[i + 1] if i + 1 < len(tokens) and tokens[i + 1] not in SEPARATORS else "~"
                after = tokens[i + 2] if i + 2 < len(tokens) else ""
                moved = [self._cd(c, arg, env) for c in cwds]
                if group or after in ("|", "|&"):
                    cwds = _merge(cwds, moved)  # it may or may not last
                elif after == "&&":
                    before_cd, cwds = _merge(before_cd, cwds), moved
                else:
                    cwds = _merge(cwds, moved)
                i += 2
                continue
            if name in REMOVERS:
                if xargs:
                    raise Unsure("xargs removal")
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
                    if not t.startswith("-") or t == "-":
                        paths.append(t)
                    i += 1
                if not paths:
                    raise Unsure("removal without a path")
                if not all(self.target_fine(p, cwds, env) for p in paths):
                    raise Unsure("removal outside the allowed places")
                continue
            i += 1

    def _cd(self, cwd: str | None, arg: str, env: dict[str, str]) -> str | None:
        if cwd is None:
            return None
        target = self.expand(arg, cwd, env)
        if target == "-" or any(ch in DYNAMIC for ch in target):
            return None
        return os.path.normpath(os.path.join(cwd, target))

    def _nested(self, tokens: list[str], i: int, name: str, cwds: list[str | None], depth: int) -> int:
        """`bash -c '...'` / `eval '...'`: check the script; returns the index after the command."""
        j = i + 1
        script: str | None = None
        if name == "eval":
            words = []
            while j < len(tokens) and tokens[j] not in SEPARATORS:
                words.append(tokens[j])
                j += 1
            script = " ".join(words)
            if "$" in script and REMOVAL_WORD.search(script):
                raise Unsure("eval of a removal with variables")
        else:
            while j < len(tokens) and tokens[j] not in SEPARATORS:
                if tokens[j] == "-c" and j + 1 < len(tokens):
                    script = tokens[j + 1]
                    j += 2
                    continue
                j += 1
        if script:
            self.check(script, cwds, depth + 1)
        return j

    def _find(self, tokens: list[str], i: int, cwds: list[str | None], env: dict[str, str]) -> int:
        """`find PATHS ... -delete | -exec rm ...`: the paths must be fine; returns the index after the command."""
        j = i + 1
        paths: list[str] = []
        while j < len(tokens) and tokens[j] not in SEPARATORS and not tokens[j].startswith(("-", "!", "(")):
            paths.append(tokens[j])
            j += 1
        removes = False
        while j < len(tokens) and tokens[j] not in SEPARATORS:
            t = tokens[j]
            if t == "-delete":
                removes = True
            elif t in FIND_ACTIONS:
                # The -exec clause runs until its terminator (";" arrives as a separator token, or "+").
                k = j + 1
                clause: list[str] = []
                while k < len(tokens) and tokens[k] not in (";", "+"):
                    clause.append(tokens[k])
                    k += 1
                if any(os.path.basename(c) in REMOVERS for c in clause):
                    removes = True
                elif any(os.path.basename(c) in SHELLS for c in clause):
                    raise Unsure("find runs a shell")
                j = k + 1
                continue
            j += 1
        if removes and not all(self.target_fine(p, cwds, env) for p in (paths or ["."])):
            raise Unsure("find removes outside the allowed places")
        return j


def _merge(a: list[str | None], b: list[str | None]) -> list[str | None]:
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return out


def auto_approvable(command: str, cwd: str | None, root: Path, reason: str | None = None,
                    home: Path | None = None) -> bool:
    """Should the runner approve this removal prompt for the user? See the module docstring."""
    if not command or not cwd or SENSITIVE.search(command):
        return False
    if reason is not None and not REMOVAL_REASON.search(reason):
        return False
    try:
        Checker(root, home or Path.home()).check(command, [os.path.normpath(cwd)])
    except Unsure:
        return False
    return True
