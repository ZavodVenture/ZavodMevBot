import os
import signal
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


TARGET_MINT = "So11111111111111111111111111111111111111112"


class MintRunShellTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "state" / "mint-runs" / "20260724T190000Z").mkdir(
            parents=True
        )
        source = Path(__file__).resolve().parents[1] / "scripts" / "mint-run.sh"
        shutil.copy2(source, self.root / "scripts" / "mint-run.sh")
        self._write_helper()
        self._write_guard(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> run-guarded.args
            printf 'reason=timeout\nduration_seconds=60\nlog_path=logs/fake.log\n'
            """
        )

    def tearDown(self):
        shutil.rmtree(self.root)

    def _write_executable(self, relative_path, content):
        path = self.root / relative_path
        path.write_text(textwrap.dedent(content))
        path.chmod(0o755)

    def _write_helper(self):
        self._write_executable(
            "scripts/mint_runner.py",
            """\
            #!/usr/bin/env python3
            import pathlib
            import sys

            root_index = sys.argv.index("--root")
            root = pathlib.Path(sys.argv[root_index + 1])
            command = sys.argv[root_index + 2]
            args = sys.argv[root_index + 3:]
            with (root / "helper.calls").open("a") as handle:
                handle.write(command + " " + " ".join(args) + "\\n")

            if command == "prepare":
                custom = root / "prepare-output.txt"
                if custom.exists():
                    print(custom.read_text(), end="")
                else:
                    mint = args[args.index("--mint") + 1]
                    timeout = args[args.index("--timeout") + 1]
                    print("run_id=20260724T190000Z")
                    print("mint=" + mint)
                    print("timeout_seconds=" + timeout)
                    print("cli_version=0.2.2")
                    print("auto_mode=enabled")
                    print("preflight=ok")
                    print("loss_limit_lamports=30000000")
                    print("early_stop_lamports=25000000")
            elif command == "result-path":
                if (root / "fail-result-path").exists():
                    raise SystemExit(4)
                print(
                    root
                    / "state/mint-runs/20260724T190000Z/guard-result.txt"
                )
            elif command == "finalize":
                (root / "finalize.called").write_text(" ".join(sys.argv))
                (root / "restore.called").write_text("finalize restored\\n")
                if (root / "fail-finalize").exists():
                    raise SystemExit(9)
            elif command == "restore":
                (root / "restore.called").write_text(" ".join(sys.argv))
                if (root / "fail-restore").exists():
                    raise SystemExit(9)
            elif command == "restore-active":
                (root / "restore-active.called").write_text(" ".join(sys.argv))
                if (root / "fail-restore").exists():
                    raise SystemExit(9)
            """
        )

    def _write_guard(self, content):
        self._write_executable("scripts/run-guarded.sh", content)

    def invoke(self, stdin, *args):
        return subprocess.run(
            ["bash", "scripts/mint-run.sh", *args],
            cwd=self.root,
            input=stdin,
            text=True,
            capture_output=True,
        )

    def guard_invocations(self):
        path = self.root / "run-guarded.args"
        return path.read_text().splitlines() if path.exists() else []

    def helper_commands(self):
        path = self.root / "helper.calls"
        if not path.exists():
            return []
        return [line.split()[0] for line in path.read_text().splitlines()]

    def test_declined_confirmation_never_runs_guard_and_restores(self):
        result = self.invoke("no\n", TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.guard_invocations(), [])
        self.assertEqual(self.helper_commands(), ["prepare", "restore"])
        self.assertTrue((self.root / "restore.called").exists())

    def test_only_exact_confirmation_runs_guard(self):
        wrong_answers = (
            f"run {TARGET_MINT} FOR 60\n",
            f" RUN {TARGET_MINT} FOR 60\n",
            f"RUN {TARGET_MINT} FOR 60 \n",
            f"RUN {TARGET_MINT} FOR 61\n",
            f"RUN {TARGET_MINT} FOR 60 now\n",
            "",
        )

        for answer in wrong_answers:
            with self.subTest(answer=answer):
                result = self.invoke(
                    answer, TARGET_MINT, "--timeout", "60"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.guard_invocations(), [])

    def test_exact_confirmation_runs_once_and_records_actual_epoch_window(self):
        before = int(time.time())
        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        after = int(time.time())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.guard_invocations(),
            ["--live-confirmed --timeout 60"],
        )
        finalize_args = (self.root / "finalize.called").read_text().split()
        started_at = int(finalize_args[finalize_args.index("--started-at") + 1])
        ended_at = int(finalize_args[finalize_args.index("--ended-at") + 1])
        self.assertLessEqual(before, started_at)
        self.assertLessEqual(started_at, ended_at)
        self.assertLessEqual(ended_at, after)
        self.assertTrue((self.root / "restore.called").exists())

    def test_invalid_arguments_fail_before_prepare(self):
        invalid_argv = (
            (),
            ("",),
            ("--timeout",),
            (TARGET_MINT, "--timeout"),
            (TARGET_MINT, "--timeout", "29"),
            (TARGET_MINT, "--timeout", "301"),
            (TARGET_MINT, "--timeout", "3x"),
            (TARGET_MINT, "--unknown", "60"),
            (TARGET_MINT, "--timeout", "60", "extra"),
        )

        for argv in invalid_argv:
            with self.subTest(argv=argv):
                result = self.invoke("", *argv)
                self.assertEqual(result.returncode, 64)
                self.assertEqual(self.helper_commands(), [])
                self.assertEqual(self.guard_invocations(), [])

    def test_prepare_output_is_allowlisted_before_printing(self):
        (self.root / "prepare-output.txt").write_text(
            "run_id=20260724T190000Z\n"
            f"mint={TARGET_MINT}\n"
            "timeout_seconds=60\n"
            "cli_version=0.2.2\n"
            "auto_mode=enabled\n"
            "preflight=ok\n"
            "loss_limit_lamports=30000000\n"
            "early_stop_lamports=25000000\n"
            "rpc_url=do-not-print-this\n"
        )

        result = self.invoke("no\n", TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preflight=ok", result.stdout)
        self.assertNotIn("do-not-print-this", result.stdout)
        self.assertNotIn("rpc_url", result.stdout)

    def test_guard_failure_is_not_retried_and_still_finalizes(self):
        self._write_guard(
            """\
            #!/usr/bin/env bash
            printf '%s\n' "$*" >> run-guarded.args
            printf 'reason=rpc_error\nduration_seconds=1\nlog_path=logs/fake.log\n'
            exit 23
            """
        )

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(
            self.guard_invocations(),
            ["--live-confirmed --timeout 60"],
        )
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_invalid_prepare_run_id_uses_active_marker_recovery(self):
        (self.root / "prepare-output.txt").write_text(
            "run_id=broken\nrpc_url=do-not-print-this\n"
        )

        result = self.invoke("", TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.root / "restore-active.called").exists())
        self.assertFalse((self.root / "restore.called").exists())
        self.assertEqual(self.guard_invocations(), [])
        self.assertNotIn("do-not-print-this", result.stdout)

    def test_failure_after_run_id_parsing_restores_that_run(self):
        (self.root / "fail-result-path").touch()

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 4)
        self.assertTrue((self.root / "restore.called").exists())
        self.assertEqual(self.guard_invocations(), [])

    def test_exit_trap_preserves_status_when_restore_fails(self):
        (self.root / "fail-restore").touch()

        declined = self.invoke("no\n", TARGET_MINT, "--timeout", "60")

        self.assertEqual(declined.returncode, 0, declined.stderr)
        self.assertTrue((self.root / "restore.called").exists())

        (self.root / "prepare-output.txt").write_text("run_id=broken\n")
        invalid_prepare = self.invoke("", TARGET_MINT, "--timeout", "60")
        self.assertEqual(invalid_prepare.returncode, 1)
        self.assertTrue((self.root / "restore-active.called").exists())

    def test_sigint_and_sigterm_are_forwarded_then_finalized(self):
        for sent_signal, expected_name in (
            (signal.SIGINT, "INT"),
            (signal.SIGTERM, "TERM"),
        ):
            with self.subTest(sent_signal=sent_signal):
                for name in (
                    "guard.started",
                    "signal.received",
                    "finalize.called",
                    "restore.called",
                ):
                    (self.root / name).unlink(missing_ok=True)
                self._write_guard(
                    """\
                    #!/usr/bin/env bash
                    set -u
                    printf '%s\n' "$*" >> run-guarded.args
                    trap 'printf "INT\\n" > signal.received; printf "reason=operator_signal\\nduration_seconds=1\\n"; exit 0' INT
                    trap 'printf "TERM\\n" > signal.received; printf "reason=operator_signal\\nduration_seconds=1\\n"; exit 0' TERM
                    touch guard.started
                    while :; do sleep 0.02; done
                    """
                )
                process = subprocess.Popen(
                    [
                        "bash",
                        "scripts/mint-run.sh",
                        TARGET_MINT,
                        "--timeout",
                        "60",
                    ],
                    cwd=self.root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                process.stdin.write(f"RUN {TARGET_MINT} FOR 60\n")
                process.stdin.flush()
                for _ in range(200):
                    if (self.root / "guard.started").exists():
                        break
                    time.sleep(0.01)
                else:
                    process.kill()
                    process.communicate()
                    self.fail("fake guard did not start")

                os.kill(process.pid, sent_signal)
                _stdout, stderr = process.communicate(timeout=5)

                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(
                    (self.root / "signal.received").read_text().strip(),
                    expected_name,
                )
                self.assertTrue((self.root / "finalize.called").exists())
                self.assertTrue((self.root / "restore.called").exists())


if __name__ == "__main__":
    unittest.main()
