#!/usr/bin/env python3
"""Pith: a small, single-file shell for Python 3.8+ and Linux.

AI disclosure: this starter implementation and its documentation were written
with OpenAI Codex at the student's request. They are not a record of personal
dogfooding. The student must identify and document their own observations.

Conceptual source: the CPSC 399.07 F26 "Shell pith" lecture transcript supplied
by the student. API references: https://docs.python.org/3/library/subprocess.html
and https://docs.python.org/3/library/readline.html . No third-party packages.
"""

import argparse
import contextlib
import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field

try:
    import readline
except ImportError:
    readline = None


VERSION = "0.1.0"
BUILTINS = ("cd", "exit", "export", "unset", "history", "help", "theme")
REDIRECTS = ("<", ">", ">>", "2>", "2>>")
VARIABLE = re.compile(r"\$(\?|[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
THEMES = {
    "mint": ("36", "35"),
    "violet": ("35", "36"),
    "amber": ("33", "36"),
}


class ShellError(Exception):
    """A recoverable input error."""


class ExitShell(Exception):
    def __init__(self, status):
        self.status = status


@dataclass
class Word:
    # Keep quote context until execution, including through skipped &&/|| lists.
    parts: list = field(default_factory=list)

    def expand(self, status, pathname=True):
        values, patterns = [], []
        for index, (text, mode) in enumerate(self.parts):
            if mode in ("unquoted", "double"):
                text = VARIABLE.sub(
                    lambda match: str(status) if match[1] == "?"
                    else os.environ.get(match[1].strip("{}"), ""), text
                )
            if index == 0 and mode == "unquoted" and text.startswith("~"):
                text = os.path.expanduser(text)
            values.append(text)
            patterns.append(text if mode == "unquoted" else glob.escape(text))
        value = "".join(values)
        pattern = "".join(patterns)
        if pathname and glob.has_magic(pattern):
            matches = sorted(glob.glob(pattern))
            if matches:
                return matches
        return [value]


def tokenize(line):
    """Scan words and operators without confusing quoted operators with syntax."""
    tokens, parts, chunk = [], [], []
    mode = "unquoted"
    index = 0

    def flush_chunk():
        if chunk:
            parts.append(("".join(chunk), mode))
            chunk.clear()

    def flush_word():
        flush_chunk()
        if parts:
            tokens.append(Word(parts.copy()))
            parts.clear()

    while index < len(line):
        char = line[index]
        if mode == "single":
            if char == "'":
                flush_chunk()
                mode = "unquoted"
            else:
                chunk.append(char)
        elif char == "\\":
            if index + 1 == len(line):
                raise ShellError("unfinished escape; enter one complete command line")
            following = line[index + 1]
            if mode == "double" and following not in '$"\\`':
                chunk.append(char)
            else:
                flush_chunk()
                parts.append((following, "literal"))
                index += 1
        elif mode == "double":
            if char == '"':
                flush_chunk()
                mode = "unquoted"
            elif char == "`" or line.startswith("$(", index):
                raise ShellError("command substitution is not supported")
            else:
                chunk.append(char)
        elif char in "'\"":
            flush_chunk()
            mode = "single" if char == "'" else "double"
            parts.append(("", mode))  # Preserve an explicitly empty argument.
        elif char.isspace():
            if char in "\r\n":
                raise ShellError("enter one command line at a time; use ; to separate commands")
            flush_word()
        elif char == "#" and not parts and not chunk:
            break
        elif char == "`" or line.startswith("$(", index):
            raise ShellError("command substitution is not supported")
        elif char in "()":
            raise ShellError("subshells and grouping are not supported")
        elif char.isdigit() and not parts and not chunk and re.match(r"\d+[<>]", line[index:]) and not line.startswith("2>", index):
            raise ShellError("supported file redirects are <, >, >>, 2>, and 2>>")
        elif char in "|&;<>" or (
            char == "2" and not parts and not chunk and line.startswith("2>", index)
        ):
            flush_word()
            operator = char
            if line.startswith("2>>", index):
                operator = "2>>"
            elif line.startswith("2>", index):
                operator = "2>"
            elif line[index:index + 2] in ("||", "&&", ">>", "<<"):
                operator = line[index:index + 2]
            if operator in ("&", "<<"):
                raise ShellError("background jobs and here-documents are not supported")
            tokens.append(operator)
            index += len(operator) - 1
        else:
            chunk.append(char)
        index += 1
    if mode != "unquoted":
        raise ShellError("unclosed quote; enter one complete command line")
    flush_word()
    return tokens


@dataclass
class Command:
    words: list = field(default_factory=list)
    redirects: list = field(default_factory=list)


def parse(line):
    """Return a list of (condition, pipeline), validating before any execution."""
    tokens = tokenize(line)
    if not tokens:
        return []
    result, pipeline, command = [], [], Command()
    condition, index = ";", 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Word):
            command.words.append(token)
        elif token in REDIRECTS:
            index += 1
            if index >= len(tokens) or not isinstance(tokens[index], Word):
                raise ShellError("{} needs a filename".format(token))
            command.redirects.append((token, tokens[index]))
        elif token in ("|", ";", "&&", "||"):
            if not command.words:
                raise ShellError("expected a command before {}".format(token))
            pipeline.append(command)
            command = Command()
            if token != "|":
                result.append((condition, pipeline))
                pipeline, condition = [], token
            if index == len(tokens) - 1:
                if token == ";":
                    return result
                raise ShellError("expected a command after {}".format(token))
        index += 1
    if not command.words:
        raise ShellError("a redirection needs a command")
    pipeline.append(command)
    result.append((condition, pipeline))
    return result


def visible(text):
    """Keep filenames and error messages from injecting terminal controls."""
    return "".join("?" if unicodedata.category(char).startswith("C") else char for char in str(text))


def short_path(width):
    try:
        cwd = os.getcwd()
    except OSError:
        return "[directory unavailable]"
    home = os.path.expanduser("~")
    if cwd == home or cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]
    cwd = visible(cwd)
    # Reserve two columns for wide glyphs instead of overflowing narrow terminals.
    limit = max(8, width - 3)
    total = 0
    suffix = []
    for char in reversed(cwd):
        total += 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if total > limit:
            return "..." + "".join(reversed(suffix))
        suffix.append(char)
    return cwd


class Shell:
    def __init__(self, plain=False, color=True, theme="mint"):
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        encoding = (sys.stdout.encoding or "ascii").lower().replace("-", "")
        self.plain = plain or os.environ.get("TERM") == "dumb" or encoding != "utf8"
        self.color = color and not self.plain and "NO_COLOR" not in os.environ
        self.theme = theme
        self.status = 0
        self.elapsed = 0.0
        self.history = []
        self.matches = []
        self.readline_active = False

    def paint(self, text, code, prompt=False):
        if not self.color:
            return text
        start, end = "\033[{}m".format(code), "\033[0m"
        if prompt and self.readline_active:
            start, end = "\001" + start + "\002", "\001" + end + "\002"
        return start + text + end

    def banner(self):
        print()
        print("  " + self.paint("pith", "1;" + THEMES[self.theme][0]) + "  /  a little shell of your own")
        print("  " + self.paint("help for commands   |   Ctrl+D to leave", "90"))
        print()

    def prompt(self):
        width = shutil.get_terminal_size((80, 24)).columns
        state = " [{}]".format(self.status) if self.status else ""
        duration = "  {:.1f}s".format(self.elapsed) if self.elapsed >= 1 else ""
        path = short_path(width - 12 - len(state) - len(duration))
        if self.plain or width < 48:
            return "pith {}{} > ".format(path, state)
        primary, secondary = THEMES[self.theme]
        top = self.paint("╭─ ", "90", True)
        top += self.paint("pith", "1;" + primary, True)
        top += self.paint(" · ", "90", True) + self.paint(path, secondary, True)
        top += self.paint(state, "31", True) + self.paint(duration, "90", True)
        return top + "\n" + self.paint("╰─ › ", primary, True)

    def error(self, message):
        label = self.paint("pith:", "31") if sys.stderr.isatty() else "pith:"
        print("{} {}".format(label, visible(message)), file=sys.stderr)

    def configure_readline(self):
        if readline is None or not self.interactive:
            return
        readline.set_auto_history(False)
        readline.set_completer_delims(" \t\n;|&<>")
        readline.set_completer(self.complete)
        backend = getattr(readline, "backend", "")
        if backend == "editline" or "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
        self.readline_active = True

    def complete(self, text, state):
        if state == 0:
            line = readline.get_line_buffer()[:readline.get_begidx()]
            segment = re.split(r"[;|&]", line)[-1]
            first = not segment.strip()
            matches = set()
            if first and not any(separator in text for separator in ("/", os.sep)):
                matches.update(name for name in BUILTINS if name.startswith(text))
                for folder in os.get_exec_path():
                    try:
                        with os.scandir(folder or ".") as entries:
                            matches.update(entry.name for entry in entries if entry.name.startswith(text)
                                           and entry.is_file() and os.access(entry.path, os.X_OK))
                    except OSError:
                        continue
            for path in glob.glob(glob.escape(os.path.expanduser(text)) + "*"):
                shown = path
                home = os.path.expanduser("~")
                if text.startswith("~/") and path.startswith(home + os.sep):
                    shown = "~" + path[len(home):]
                if os.path.isdir(path):
                    shown += "/"
                matches.add(shown)
            self.matches = [shlex.quote(match) for match in sorted(matches)]
        return self.matches[state] if state < len(self.matches) else None

    def builtin(self, argv):
        name, args = argv[0], argv[1:]
        if name == "cd":
            if len(args) > 1:
                raise ShellError("usage: cd [directory|-]")
            if not args:
                target = os.environ.get("HOME") or os.path.expanduser("~")
            elif args[0] == "-":
                target = os.environ.get("OLDPWD")
                if not target:
                    raise ShellError("cd: no previous directory")
            else:
                target = args[0]
            try:
                previous = os.getcwd()
            except OSError:
                previous = None
            os.chdir(target)
            if previous is not None:
                os.environ["OLDPWD"] = previous
            os.environ["PWD"] = os.getcwd()
            if args == ["-"]:
                print(os.getcwd())
        elif name == "exit":
            if len(args) > 1:
                raise ShellError("usage: exit [status]")
            try:
                status = int(args[0]) % 256 if args else self.status
            except ValueError:
                raise ShellError("exit: status must be an integer")
            raise ExitShell(status)
        elif name == "export":
            if not args:
                raise ShellError("usage: export NAME=value [NAME=value ...]")
            changes = []
            for argument in args:
                key, separator, value = argument.partition("=")
                if not separator or not NAME.fullmatch(key):
                    raise ShellError("export: expected NAME=value, got " + argument)
                if "\x00" in value:
                    raise ShellError("export: a value cannot contain a NUL character")
                changes.append((key, value))
            os.environ.update(changes)
        elif name == "unset":
            if not args or any(not NAME.fullmatch(key) for key in args):
                raise ShellError("usage: unset NAME [NAME ...]")
            for key in args:
                os.environ.pop(key, None)
        elif name == "history":
            if args == ["-c"]:
                self.history.clear()
                if self.readline_active:
                    readline.clear_history()
            elif not args:
                for number, line in enumerate(self.history, 1):
                    print("{:>4}  {}".format(number, visible(line)))
            else:
                raise ShellError("usage: history [-c]")
        elif name == "theme":
            if not args:
                print("{}  |  available: {}".format(self.theme, ", ".join(THEMES)))
            elif len(args) == 1 and args[0] in THEMES:
                self.theme = args[0]
            else:
                raise ShellError("usage: theme [mint|violet|amber]")
        elif name == "help":
            if args:
                raise ShellError("usage: help")
            print(HELP)
        return 0

    @contextlib.contextmanager
    def redirections(self, redirects):
        # Open in source order, even when a later redirect replaces an earlier one.
        with contextlib.ExitStack() as stack:
            streams = {}
            for operator, path in redirects:
                mode = "r" if operator == "<" else "a" if operator.endswith(">>") else "w"
                stream = stack.enter_context(open(path, mode, encoding="utf-8"))
                descriptor = 0 if operator == "<" else 2 if operator.startswith("2") else 1
                streams[descriptor] = stream
            yield streams

    def pipeline(self, commands):
        expanded = []
        for command in commands:
            argv = [value for word in command.words for value in word.expand(self.status)]
            if not argv or not argv[0]:
                raise ShellError("empty command name")
            redirects = []
            for operator, word in command.redirects:
                paths = word.expand(self.status)
                if len(paths) != 1 or not paths[0]:
                    raise ShellError("redirection requires exactly one nonempty filename")
                redirects.append((operator, paths[0]))
            expanded.append((argv, redirects))
        if len(expanded) > 1 and any(argv[0] in BUILTINS for argv, _ in expanded):
            raise ShellError("built-ins run as standalone commands; use external programs in pipes")
        if expanded[0][0][0] in BUILTINS:
            argv, redirects = expanded[0]
            with self.redirections(redirects) as streams:
                with contextlib.ExitStack() as stack:
                    if 0 in streams:
                        old_input = sys.stdin
                        sys.stdin = streams[0]
                        stack.callback(setattr, sys, "stdin", old_input)
                    if 1 in streams:
                        stack.enter_context(contextlib.redirect_stdout(streams[1]))
                    if 2 in streams:
                        stack.enter_context(contextlib.redirect_stderr(streams[2]))
                    try:
                        return self.builtin(argv)
                    except (ShellError, OSError, ValueError) as error:
                        self.error(error)
                        return 1
        return self.external(expanded)

    def external(self, expanded):
        processes, previous_pipe = [], None
        saved_signals = {}
        failure_status = 1
        sys.stdout.flush()
        sys.stderr.flush()
        # Caught handlers reset to the default on exec on Unix. Ctrl+C reaches
        # foreground children while the parent remains available to reap them.
        if os.name == "posix":
            saved_signals[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
            saved_signals[signal.SIGQUIT] = signal.signal(signal.SIGQUIT, lambda *_: None)
            if hasattr(signal, "SIGTSTP"):
                saved_signals[signal.SIGTSTP] = signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        try:
            for position, (argv, redirects) in enumerate(expanded):
                failure_status = 1
                with self.redirections(redirects) as streams:
                    last = position == len(expanded) - 1
                    failure_status = 126
                    process = subprocess.Popen(
                        argv, stdin=streams.get(0, previous_pipe),
                        stdout=streams.get(1, None if last else subprocess.PIPE),
                        stderr=streams.get(2), shell=False,
                    )
                processes.append(process)
                if previous_pipe is not None:
                    previous_pipe.close()
                # If a redirect replaces a pipeline's output, feed EOF downstream.
                previous_pipe = process.stdout
                if not last and previous_pipe is None:
                    previous_pipe = open(os.devnull, "rb")
            for process in processes:
                process.wait()
            code = processes[-1].returncode
            return 128 - code if code < 0 else code
        except (OSError, ValueError, KeyboardInterrupt) as error:
            for process in processes:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            for process in processes:
                process.wait()
            if isinstance(error, KeyboardInterrupt):
                return 130
            self.error(error)
            return 127 if failure_status == 126 and isinstance(error, FileNotFoundError) else failure_status
        finally:
            if previous_pipe is not None:
                previous_pipe.close()
            for number, handler in saved_signals.items():
                signal.signal(number, handler)

    def execute(self, line):
        started = time.monotonic()
        try:
            for condition, commands in parse(line):
                if condition == "&&" and self.status != 0:
                    continue
                if condition == "||" and self.status == 0:
                    continue
                try:
                    self.status = self.pipeline(commands)
                except (ShellError, OSError, ValueError) as error:
                    self.error(error)
                    self.status = 1
        except ShellError as error:
            self.error(error)
            self.status = 2
        finally:
            self.elapsed = time.monotonic() - started
        return self.status

    def run(self, banner=True):
        self.configure_readline()
        if self.interactive and banner:
            self.banner()
        while True:
            try:
                if self.interactive:
                    line = input(self.prompt())
                else:
                    # Avoid TextIOWrapper read-ahead stealing a child's stdin.
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        char = os.read(sys.stdin.fileno(), 1)
                        if not char:
                            break
                        data.extend(char)
                    if not data:
                        return self.status
                    line = data.decode(sys.stdin.encoding or "utf-8").rstrip("\r\n")
                if line.strip():
                    self.history.append(line)
                    if self.readline_active:
                        readline.add_history(line)
                    self.execute(line)
            except KeyboardInterrupt:
                print()
                self.status = 130
            except EOFError:
                if self.interactive:
                    print()
                return self.status
            except ExitShell as exit_request:
                return exit_request.status


HELP = """pith / your shell, with a little room to grow

  cd [directory|-]         Change this shell's directory; no argument goes home.
  export NAME=value       Set an environment variable for future commands.
  unset NAME              Remove an environment variable.
  history [-c]            Show or clear this session's command history.
  theme [mint|violet|amber] Change the prompt's accent colors.
  exit [status]           Leave the shell; Ctrl+D also exits.
  help                    Show this page.

  program arg "two words"  Run an external program, using PATH or an explicit path.
  left | right            Connect external programs.
  command < in > out      Read from a file and write to a file.
  command >> out 2> err   Append stdout and redirect stderr.
  first && second        Run second if first succeeds; || runs it on failure.
  first ; second          Run both, in order.

Quotes and backslashes protect literal characters. $NAME, ${NAME}, $? and ~
expand; unquoted * ? [] match filenames. Variable values stay one argument.
Single quotes suppress expansion. Pipeline status is the last program's status.
With readline: Tab completes, Up/Down recall, Ctrl+R searches. Ctrl+C cancels.

Foreground commands only. No background jobs, Ctrl+Z suspension, command
substitution, here-documents, fd duplication, or shell scripting constructs.
Built-ins run standalone, with redirection allowed. Use man for external tools.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pith: a small shell with a considered terminal interface.")
    parser.add_argument("-c", "--command", help="execute one command line and return its status")
    parser.add_argument("--plain", action="store_true", help="ASCII prompt with no color")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--no-banner", action="store_true", help="skip the interactive welcome")
    parser.add_argument("--theme", choices=tuple(THEMES), default="mint", help="prompt accent palette")
    parser.add_argument("--version", action="version", version="pith " + VERSION)
    args = parser.parse_args(argv)
    shell = Shell(plain=args.plain, color=not args.no_color, theme=args.theme)
    try:
        if args.command is not None:
            shell.color = shell.color and sys.stdout.isatty()
            return shell.execute(args.command)
        with contextlib.ExitStack() as signals:
            if shell.interactive and os.name == "posix":
                for number in (signal.SIGQUIT, signal.SIGTSTP):
                    previous = signal.signal(number, signal.SIG_IGN)
                    signals.callback(signal.signal, number, previous)
            return shell.run(banner=not args.no_banner)
    except ExitShell as exit_request:
        return exit_request.status
    except BrokenPipeError:
        # A downstream reader, such as head, may deliberately close its pipe.
        return 141


if __name__ == "__main__":
    sys.exit(main())
