"""Behavioral checks for the supplied starter, not personal dogfooding evidence."""

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pith.py"
sys.path.insert(0, str(ROOT))
import pith


def python_command(code):
    return "{} -c {}".format(shlex.quote(sys.executable), shlex.quote(code))


class ShellIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_root = ROOT / ".qa"
        self.temp_root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="pith-test-", dir=self.temp_root)
        self.cwd = Path(self.directory.name)

    def tearDown(self):
        # Verify the target remains under this workspace before recursive cleanup.
        self.cwd.resolve().relative_to(self.temp_root.resolve())
        self.directory.cleanup()

    def run_shell(self, command=None, data=None, environment=None):
        argv = [sys.executable, str(SOURCE), "--plain"]
        if command is not None:
            argv.extend(["-c", command])
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        if environment:
            env.update(environment)
        return subprocess.run(argv, input=data, text=True, capture_output=True,
                              cwd=self.cwd, env=env, timeout=12)

    def test_arbitrary_executable_and_arguments(self):
        command = python_command("import sys; print(repr(sys.argv[1:]))")
        result = self.run_shell(command + ' alpha "two words" ""')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "['alpha', 'two words', '']")

    def test_path_lookup(self):
        executable = Path(sys.executable)
        result = self.run_shell(shlex.quote(executable.name) + ' -c "print(17)"',
                                environment={"PATH": str(executable.parent)})
        self.assertEqual(result.stdout, "17\n", result.stderr)

    def test_quoted_operators_stay_arguments(self):
        result = self.run_shell(python_command("import sys; print(repr(sys.argv[1:]))") +
                                ' "|" ">" ";" "&&" "2>"')
        self.assertEqual(result.stdout.strip(), "['|', '>', ';', '&&', '2>']")

    def test_backslash_protects_spaces_and_dollar(self):
        result = self.run_shell(python_command("import sys; print(repr(sys.argv[1:]))") +
                                r" two\ words \$PITH_VALUE", environment={"PITH_VALUE": "expanded"})
        self.assertEqual(result.stdout.strip(), "['two words', '$PITH_VALUE']")

    def test_single_and_double_quotes(self):
        result = self.run_shell(python_command("import sys; print(repr(sys.argv[1:]))") +
                                ' "$PITH_VALUE" \'$PITH_VALUE\'', environment={"PITH_VALUE": "two words"})
        self.assertEqual(result.stdout.strip(), "['two words', '$PITH_VALUE']")

    def test_export_unset_and_sequential_expansion(self):
        check = python_command("import os; print(os.environ.get('PITH_TEST', 'missing'))")
        result = self.run_shell('export PITH_TEST="hello world" ; ' + check + ' ; unset PITH_TEST ; ' + check)
        self.assertEqual(result.stdout, "hello world\nmissing\n", result.stderr)

    def test_invalid_export_is_atomic(self):
        result = self.run_shell('export PITH_TEST=new BAD-NAME=x ; ' +
                                python_command("import os; print(os.environ['PITH_TEST'])"),
                                environment={"PITH_TEST": "old"})
        self.assertEqual(result.stdout, "old\n")
        self.assertIn("expected NAME=value", result.stderr)

    def test_cd_persists_and_cd_dash_returns(self):
        (self.cwd / "two words").mkdir()
        check = python_command("import os; print(os.path.basename(os.getcwd()))")
        result = self.run_shell('cd "two words" ; ' + check + ' ; cd - ; ' + check)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "two words")
        self.assertEqual(result.stdout.splitlines()[-1], self.cwd.name)

    def test_cd_home(self):
        check = python_command("import os; print(os.getcwd())")
        result = self.run_shell("cd ; " + check, environment={"HOME": str(self.cwd)})
        self.assertEqual(Path(result.stdout.strip()), self.cwd)

    def test_cd_failure_does_not_kill_shell(self):
        result = self.run_shell("cd missing ; " + python_command("print('alive')"))
        self.assertEqual(result.stdout, "alive\n")
        self.assertTrue(result.stderr)

    def test_three_stage_pipeline(self):
        first = python_command("print('hello')")
        middle = python_command("import sys; sys.stdout.write(sys.stdin.read().upper())")
        last = python_command("import sys; print(len(sys.stdin.read().strip()))")
        result = self.run_shell(" | ".join((first, middle, last)))
        self.assertEqual(result.stdout, "5\n", result.stderr)

    def test_large_pipeline_does_not_deadlock(self):
        first = python_command("import sys; sys.stdout.write('x' * 300000)")
        last = python_command("import sys; print(len(sys.stdin.read()))")
        result = self.run_shell(first + " | " + last)
        self.assertEqual(result.stdout, "300000\n", result.stderr)

    def test_pipeline_returns_last_status(self):
        failure = python_command("import sys; sys.exit(7)")
        success = python_command("pass")
        self.assertEqual(self.run_shell(failure + " | " + success).returncode, 0)
        self.assertEqual(self.run_shell(success + " | " + failure).returncode, 7)

    def test_redirect_append_and_input(self):
        first = python_command("print('one')")
        second = python_command("print('two')")
        reader = python_command("import sys; sys.stdout.write(sys.stdin.read())")
        result = self.run_shell(first + ' > "two words.txt" ; ' + second +
                                ' >> "two words.txt" ; ' + reader + ' < "two words.txt"')
        self.assertEqual(result.stdout, "one\ntwo\n", result.stderr)

    def test_stderr_redirect_append(self):
        writer = python_command("import sys; print('error', file=sys.stderr)")
        result = self.run_shell(writer + " 2> errors ; " + writer + " 2>> errors")
        self.assertEqual(result.stderr, "")
        self.assertEqual((self.cwd / "errors").read_text(), "error\nerror\n")

    def test_redirection_overrides_pipe(self):
        writer = python_command("print('to file')")
        reader = python_command("import sys; print(repr(sys.stdin.read()))")
        result = self.run_shell(writer + " > saved | " + reader)
        self.assertEqual(result.stdout, "''\n", result.stderr)
        self.assertEqual((self.cwd / "saved").read_text(), "to file\n")

    def test_redirects_open_in_order(self):
        result = self.run_shell(python_command("print('last')") + " > first > second")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.cwd / "first").read_text(), "")
        self.assertEqual((self.cwd / "second").read_text(), "last\n")

    def test_builtin_output_redirect(self):
        result = self.run_shell("help > help.txt ; theme")
        self.assertIn("pith /", (self.cwd / "help.txt").read_text())
        self.assertIn("available:", result.stdout)

    def test_builtin_error_redirect(self):
        result = self.run_shell("cd nonexistent 2> error.txt")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertIn("pith:", (self.cwd / "error.txt").read_text())

    def test_missing_input_is_not_command_not_found(self):
        result = self.run_shell(python_command("pass") + " < missing.txt")
        self.assertEqual(result.returncode, 1)

    def test_glob_respects_quotes_and_escapes(self):
        (self.cwd / "a.txt").touch()
        (self.cwd / "b.txt").touch()
        command = python_command("import sys; print(repr(sys.argv[1:]))")
        result = self.run_shell(command + r' *.txt "*.txt" \*.txt')
        self.assertEqual(result.stdout.strip(), "['a.txt', 'b.txt', '*.txt', '*.txt']")

    def test_unmatched_glob_is_literal(self):
        result = self.run_shell(python_command("import sys; print(sys.argv[1])") + " *.none")
        self.assertEqual(result.stdout, "*.none\n")

    def test_ambiguous_redirect_does_not_execute(self):
        (self.cwd / "a.txt").touch()
        (self.cwd / "b.txt").touch()
        result = self.run_shell(python_command("print('bad')") + " > *.txt")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 1)

    def test_short_circuit_and_dynamic_status(self):
        fail = python_command("import sys; sys.exit(7)")
        show = python_command("import sys; print(sys.argv[1])")
        result = self.run_shell(fail + " && " + show + " skipped || " + show + " $? ; " + show + " $?")
        self.assertEqual(result.stdout, "7\n0\n", result.stderr)

    def test_missing_command_recovers(self):
        result = self.run_shell("pith_no_such_command || " + python_command("print('recovered')"))
        self.assertEqual(result.stdout, "recovered\n")
        self.assertEqual(result.returncode, 0)

    def test_missing_command_status(self):
        self.assertEqual(self.run_shell("pith_no_such_command").returncode, 127)

    def test_invalid_syntax_has_no_partial_execution(self):
        result = self.run_shell(python_command("print('bad')") + " ; | broken")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 2)

    def test_incomplete_input(self):
        for command in ('help "unfinished', "help |", "help >", "help &&", "help \\"):
            with self.subTest(command=command):
                result = self.run_shell(command)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_unsupported_syntax_is_rejected(self):
        for command in ("help &", "help << EOF", "help $(whoami)", "help `whoami`", "help 2>&1", "(help)",
                        "help 3> file", "help 22> file", "help 1> file", "help 0< file"):
            with self.subTest(command=command):
                self.assertEqual(self.run_shell(command).returncode, 2)

    def test_multiline_command_argument_is_rejected(self):
        self.assertEqual(self.run_shell("help\ntheme").returncode, 2)

    def test_failed_pipeline_launch_cleans_up_existing_children(self):
        sleeping = python_command("import time; time.sleep(30)")
        result = self.run_shell(sleeping + " | pith_no_such_command")
        self.assertEqual(result.returncode, 127)

    def test_empty_command_reports_error(self):
        result = self.run_shell('""')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

    def test_comment_at_word_boundary(self):
        result = self.run_shell(python_command("import sys; print(sys.argv[1])") + " abc#def # ignored")
        self.assertEqual(result.stdout, "abc#def\n")

    def test_exit_status_and_inherited_status(self):
        self.assertEqual(self.run_shell("exit 9").returncode, 9)
        self.assertEqual(self.run_shell("exit 257").returncode, 1)
        failure = python_command("import sys; sys.exit(5)")
        self.assertEqual(self.run_shell(failure + " ; exit").returncode, 5)

    def test_invalid_exit_does_not_kill_shell(self):
        result = self.run_shell("exit bad ; " + python_command("print('alive')"))
        self.assertEqual(result.stdout, "alive\n")

    def test_piped_input_has_no_banner_or_prompt(self):
        result = self.run_shell(data="theme\nexit\n")
        self.assertEqual(result.stdout, "mint  |  available: mint, violet, amber\n")
        self.assertEqual(result.stderr, "")

    def test_noninteractive_child_can_read_stdin(self):
        reader = python_command("import sys; print(sys.stdin.readline().strip())")
        result = self.run_shell(data=reader + "\nthis belongs to the child\nexit\n")
        self.assertEqual(result.stdout, "this belongs to the child\n", result.stderr)

    def test_session_history(self):
        result = self.run_shell(data="theme amber\nhistory\nhistory -c\nhistory\nexit\n")
        self.assertIn("1  theme amber", result.stdout)
        self.assertIn("1  history", result.stdout)
        self.assertNotIn("history -c", result.stdout)

    def test_parent_output_precedes_child_output(self):
        result = self.run_shell("theme ; " + python_command("print('child')"))
        self.assertTrue(result.stdout.startswith("mint"), result.stdout)
        self.assertTrue(result.stdout.endswith("child\n"), result.stdout)

    def test_builtin_pipeline_is_clear_error(self):
        result = self.run_shell("history | " + python_command("pass"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("built-ins run as standalone", result.stderr)

    @unittest.skipUnless(os.name == "posix", "Unix executable permissions")
    def test_permission_denied(self):
        script = self.cwd / "no-execute"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o600)
        self.assertEqual(self.run_shell("./no-execute").returncode, 126)

    @unittest.skipUnless(os.name == "posix", "Unix signals and terminal process groups")
    def test_ctrl_c_cancels_foreground_child_and_shell_survives(self):
        ready = self.cwd / "ready"
        code = "from pathlib import Path; import time; Path('ready').touch(); time.sleep(30)"
        process = subprocess.Popen([sys.executable, str(SOURCE), "--plain"], cwd=self.cwd,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            process.stdin.write(python_command(code) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "foreground child did not start")
            os.killpg(process.pid, signal.SIGINT)
            output, error = process.communicate(python_command("print('alive')") + "\nexit\n", timeout=5)
            self.assertIn("alive", output, error)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    @unittest.skipUnless(os.name == "posix", "Unix early-closing pipes")
    def test_early_pipe_reader_exit(self):
        writer = python_command("import os; [os.write(1, b'x' * 4096) for _ in range(1024)]")
        result = self.run_shell(writer + " | head -c 1")
        self.assertEqual(result.stdout, "x")
        self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(os.name == "posix", "Unix working directory removal")
    def test_cd_recovers_from_deleted_working_directory(self):
        (self.cwd / "gone").mkdir()
        remove = python_command("import os; os.rmdir('../gone')")
        result = self.run_shell("cd gone ; " + remove + " ; cd .. ; " + python_command("print('recovered')"))
        self.assertEqual(result.stdout, "recovered\n", result.stderr)


class PromptTests(unittest.TestCase):
    def test_plain_prompt_has_no_ansi_or_unicode(self):
        shell = pith.Shell(plain=True)
        shell.status = 7
        prompt = shell.prompt()
        self.assertNotIn("\033", prompt)
        self.assertIn("[7]", prompt)
        self.assertIn(" > ", prompt)

    def test_no_color_environment(self):
        original = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = ""
        try:
            self.assertNotIn("\033", pith.Shell().prompt())
        finally:
            if original is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = original

    def test_terminal_controls_are_removed(self):
        self.assertEqual(pith.visible("a\033[31m\nb"), "a?[31m?b")


if __name__ == "__main__":
    unittest.main()
