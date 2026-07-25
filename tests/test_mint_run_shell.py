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
DIAGNOSTIC_SENTINEL = "diagnostic-config-private-sentinel"


class MintRunShellTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "state" / "mint-runs" / "20260724T190000Z").mkdir(
            parents=True
        )
        tokens_path = self.root / "tokens.toml"
        tokens_path.write_text('tokens = ["original"]\n')
        tokens_path.chmod(0o600)
        source = Path(__file__).resolve().parents[1] / "scripts" / "mint-run.sh"
        shutil.copy2(source, self.root / "scripts" / "mint-run.sh")
        self._write_helper()
        self._write_guard(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            [[ "${ZAVOD_LIVE_LOCK_FD:-}" =~ ^[0-9]+$ ]]
            [[ -e "/proc/$$/fd/$ZAVOD_LIVE_LOCK_FD" ]]
            printf '%s\n' "$ZAVOD_LIVE_LOCK_FD" >> run-guarded.lock-fds
            printf '%s\n' "$*" >> run-guarded.args
            printf 'reason=timeout\nduration_seconds=60\nlog_path=logs/fake.log\n'
            """
        )

    def tearDown(self):
        shutil.rmtree(self.root)

    def _write_executable(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
        path.chmod(0o755)

    def _write_helper(self):
        self._write_executable(
            "scripts/mint_runner.py",
            """\
            #!/usr/bin/env python3
            import pathlib
            import signal
            import sys
            import time

            root_index = sys.argv.index("--root")
            root = pathlib.Path(sys.argv[root_index + 1])
            command = sys.argv[root_index + 2]
            args = sys.argv[root_index + 3:]
            with (root / "helper.calls").open("a") as handle:
                handle.write(command + " " + " ".join(args) + "\\n")

            def write_private(path, content):
                path.write_text(content)
                path.chmod(0o600)

            def restore_workspace():
                original = root / "tokens.original"
                if original.exists():
                    tokens = root / "tokens.toml"
                    tokens.write_bytes(original.read_bytes())
                    tokens.chmod(0o600)
                (root / "state/.mint-run-active").unlink(missing_ok=True)
                (
                    root
                    / "state/mint-runs/20260724T190000Z/"
                    "selector-diagnostic.toml"
                ).unlink(missing_ok=True)

            if command == "prepare":
                if (root / "slow-prepare").exists():
                    def interrupted(signum, frame):
                        del frame
                        (root / "prepare.signal").write_text(
                            signal.Signals(signum).name
                        )
                        raise SystemExit(128 + signum)

                    signal.signal(signal.SIGINT, interrupted)
                    signal.signal(signal.SIGTERM, interrupted)
                    (root / "prepare.started").touch()
                    time.sleep(0.5)
                mint = args[args.index("--mint") + 1]
                original = root / "tokens.original"
                if not original.exists():
                    original.write_bytes((root / "tokens.toml").read_bytes())
                write_private(
                    root / "state/.mint-run-active",
                    "20260724T190000Z\\n",
                )
                write_private(root / "tokens.toml", f'tokens = ["{mint}"]\\n')
                (root / "prepared-mint").write_text(mint)
                (root / "owner-prepared").touch()
                diagnostic = (
                    args[args.index("--diagnostic") + 1]
                    if "--diagnostic" in args
                    else None
                )
                if diagnostic is not None:
                    write_private(
                        root
                        / "state/mint-runs/20260724T190000Z/"
                        "selector-diagnostic.toml",
                        "diagnostic-config-private-sentinel\\n",
                    )
                custom = root / "prepare-output.txt"
                if custom.exists():
                    print(custom.read_text(), end="")
                else:
                    timeout = args[args.index("--timeout") + 1]
                    print("run_id=20260724T190000Z")
                    print("mint=" + mint)
                    print("timeout_seconds=" + timeout)
                    print("cli_version=0.2.2")
                    print("auto_mode=enabled")
                    print("preflight=ok")
                    print("loss_limit_lamports=30000000")
                    print("early_stop_lamports=25000000")
                    if diagnostic is not None:
                        print("diagnostic_mode=" + diagnostic)
                        print(
                            "diagnostic_config="
                            "state/mint-runs/20260724T190000Z/"
                            "selector-diagnostic.toml"
                        )
            elif command == "result-path":
                if (root / "fail-result-path").exists():
                    raise SystemExit(4)
                if (root / "tamper-before-validate").exists():
                    write_private(
                        root / "state/.mint-run-active",
                        "tampered-run-id\\n",
                    )
                    write_private(
                        root / "tokens.toml",
                        'tokens = ["tampered"]\\n',
                    )
                custom = root / "result-path-output.txt"
                if custom.exists():
                    print(custom.read_text(), end="")
                else:
                    if (root / "slow-result-path").exists():
                        (root / "result-path.started").touch()
                        time.sleep(0.25)
                    print(
                        root
                        / "state/mint-runs/20260724T190000Z/guard-result.txt"
                    )
            elif command == "validate-live":
                run_id = args[args.index("--run-id") + 1]
                mint = (root / "prepared-mint").read_text()
                marker = (root / "state/.mint-run-active").read_bytes()
                tokens = (root / "tokens.toml").read_bytes()
                if marker != f"{run_id}\\n".encode():
                    raise SystemExit(7)
                if tokens != f'tokens = ["{mint}"]\\n'.encode():
                    raise SystemExit(7)
                if "--diagnostic" in (
                    root / "helper.calls"
                ).read_text().splitlines()[0]:
                    diagnostic_config = (
                        root
                        / "state/mint-runs/20260724T190000Z/"
                        "selector-diagnostic.toml"
                    )
                    if (
                        diagnostic_config.is_symlink()
                        or not diagnostic_config.is_file()
                        or diagnostic_config.stat().st_mode & 0o777 != 0o600
                    ):
                        raise SystemExit(7)
            elif command == "finalize":
                (root / "finalize.called").write_text(" ".join(sys.argv))
                if (root / "fail-finalize").exists():
                    raise SystemExit(9)
                ended_at = args[args.index("--ended-at") + 1]
                if ended_at == "0":
                    raise SystemExit(1)
                restore_workspace()
                (root / "restore.called").write_text("finalize restored\\n")
            elif command == "restore":
                (root / "restore.called").write_text(" ".join(sys.argv))
                restore_workspace()
                if (root / "fail-restore").exists():
                    raise SystemExit(9)
            elif command == "restore-active":
                (root / "restore-active.called").write_text(" ".join(sys.argv))
                restore_workspace()
                if (root / "fail-restore").exists():
                    raise SystemExit(9)
            """
        )

    def _write_guard(self, content):
        self._write_executable("scripts/run-guarded.sh", content)

    def invoke(self, stdin, *args, env=None):
        return subprocess.run(
            ["bash", "scripts/mint-run.sh", *args],
            cwd=self.root,
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
        )

    def guard_invocations(self):
        path = self.root / "run-guarded.args"
        return path.read_text().splitlines() if path.exists() else []

    def helper_commands(self):
        path = self.root / "helper.calls"
        if not path.exists():
            return []
        return [line.split()[0] for line in path.read_text().splitlines()]

    def guard_lock_fds(self):
        path = self.root / "run-guarded.lock-fds"
        return path.read_text().splitlines() if path.exists() else []

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
            [
                "--live-confirmed --timeout 60 "
                "--profile single-mint-auto"
            ],
        )
        finalize_args = (self.root / "finalize.called").read_text().split()
        started_at = int(finalize_args[finalize_args.index("--started-at") + 1])
        ended_at = int(finalize_args[finalize_args.index("--ended-at") + 1])
        self.assertLessEqual(before, started_at)
        self.assertLessEqual(started_at, ended_at)
        self.assertLessEqual(ended_at, after)
        self.assertTrue((self.root / "restore.called").exists())
        result_path = (
            self.root
            / "state/mint-runs/20260724T190000Z/guard-result.txt"
        )
        self.assertEqual(result_path.stat().st_mode & 0o777, 0o600)

    def test_d0_uses_exact_guarded_test_mode_launch(self):
        phrase = f"DIAGNOSE {TARGET_MINT} FOR 60\n"

        result = self.invoke(
            phrase,
            TARGET_MINT,
            "--diagnostic",
            "d0",
            "--timeout",
            "60",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.guard_invocations(),
            [
                "--live-confirmed --timeout 60 "
                "--profile selector-diagnostic "
                "--config state/mint-runs/20260724T190000Z/"
                "selector-diagnostic.toml --test-mode"
            ],
        )
        self.assertEqual(len(self.guard_lock_fds()), 1)
        self.assertRegex(self.guard_lock_fds()[0], r"^[0-9]+$")
        self.assertEqual(
            [
                line
                for line in (self.root / "helper.calls").read_text().splitlines()
                if line.startswith("prepare ")
            ],
            [
                "prepare --mint "
                f"{TARGET_MINT} --timeout 60 --diagnostic d0"
            ],
        )
        self.assertNotIn(
            DIAGNOSTIC_SENTINEL,
            result.stdout + result.stderr,
        )
        self.assertTrue((self.root / "restore.called").exists())

    def test_diagnostic_confirmation_is_single_use(self):
        exact = f"DIAGNOSE {TARGET_MINT} FOR 60"
        wrong_answers = (
            f"RUN {TARGET_MINT} FOR 60\n",
            f"diagnose {TARGET_MINT} FOR 60\n",
            f"DIAGNOSE {TARGET_MINT} FOR 61\n",
            f"{exact}\n{exact}\n",
        )

        for answer in wrong_answers:
            with self.subTest(answer=answer):
                before = len(self.guard_invocations())
                result = self.invoke(
                    answer,
                    TARGET_MINT,
                    "--diagnostic",
                    "d0",
                    "--timeout",
                    "60",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.guard_invocations()), before)

    def test_diagnostic_rejects_unbound_prepare_config(self):
        (self.root / "prepare-output.txt").write_text(
            "run_id=20260724T190000Z\n"
            f"mint={TARGET_MINT}\n"
            "timeout_seconds=60\n"
            "cli_version=0.2.2\n"
            "auto_mode=selector-diagnostic\n"
            "preflight=deferred\n"
            "loss_limit_lamports=30000000\n"
            "early_stop_lamports=25000000\n"
            "diagnostic_mode=d0\n"
            "diagnostic_config=state/arbitrary.toml\n"
        )

        result = self.invoke(
            f"DIAGNOSE {TARGET_MINT} FOR 60\n",
            TARGET_MINT,
            "--diagnostic",
            "d0",
            "--timeout",
            "60",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.guard_invocations(), [])
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
            (TARGET_MINT, "--diagnostic"),
            (TARGET_MINT, "--diagnostic", ""),
            (TARGET_MINT, "--diagnostic", "d1"),
            (TARGET_MINT, "--diagnostic", "d2"),
            (TARGET_MINT, "--diagnostic", "d0", "--timeout", "29"),
            (TARGET_MINT, "--diagnostic", "d0", "--timeout", "301"),
            (TARGET_MINT, "--diagnostic", "d0", "--diagnostic", "d0"),
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
            [
                "--live-confirmed --timeout 60 "
                "--profile single-mint-auto"
            ],
        )
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_guard_failure_has_priority_over_finalize_failure(self):
        (self.root / "fail-finalize").touch()
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
            [
                "--live-confirmed --timeout 60 "
                "--profile single-mint-auto"
            ],
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

    def test_hostile_result_path_is_rejected_before_guard_launch(self):
        hostile = self.root / "outside-result.txt"
        (self.root / "result-path-output.txt").write_text(f"{hostile}\n")

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.guard_invocations(), [])
        self.assertFalse(hostile.exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_preexisting_result_file_and_symlink_are_rejected(self):
        result_path = (
            self.root
            / "state/mint-runs/20260724T190000Z/guard-result.txt"
        )
        phrase = f"RUN {TARGET_MINT} FOR 60\n"

        result_path.write_text("keep-existing\n")
        regular = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        self.assertEqual(regular.returncode, 1)
        self.assertEqual(result_path.read_text(), "keep-existing\n")
        self.assertEqual(self.guard_invocations(), [])

        result_path.unlink()
        result_path.symlink_to("/dev/null")
        symlink = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        self.assertEqual(symlink.returncode, 1)
        self.assertTrue(result_path.is_symlink())
        self.assertEqual(os.readlink(result_path), "/dev/null")
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

    def test_signal_during_result_path_aborts_before_guard_launch(self):
        (self.root / "slow-result-path").touch()
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
            if (self.root / "result-path.started").exists():
                break
            time.sleep(0.01)
        else:
            process.kill()
            process.communicate()
            self.fail("fake result-path did not start")

        os.kill(process.pid, signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 143, stderr)
        self.assertEqual(self.guard_invocations(), [])
        self.assertFalse((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_signal_during_prepare_output_parsing_aborts_before_prompt(self):
        self._write_executable(
            "fake-bin/awk",
            """\
            #!/usr/bin/env bash
            if [[ ! -f parsing.signal-sent ]]; then
              touch parsing.signal-sent
              shell_pid="$(/usr/bin/awk '{print $4}' "/proc/$PPID/stat")"
              kill -TERM "$shell_pid"
            fi
            exec /usr/bin/awk "$@"
            """,
        )
        environment = os.environ.copy()
        environment["PATH"] = (
            f"{self.root / 'fake-bin'}:{environment.get('PATH', '')}"
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
            env=environment,
        )

        for _ in range(200):
            if (self.root / "parsing.signal-sent").exists():
                break
            time.sleep(0.01)
        else:
            process.kill()
            process.communicate()
            self.fail("prepare-output parser did not run")

        stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 143, stderr)
        self.assertNotIn("Type exactly:", stdout)
        self.assertEqual(self.guard_invocations(), [])
        self.assertTrue((self.root / "restore.called").exists())

    def test_prelaunch_validation_rejects_tampering_and_restores(self):
        original_tokens = (self.root / "tokens.toml").read_bytes()
        (self.root / "tamper-before-validate").touch()

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.guard_invocations(), [])
        self.assertIn("validate-live", self.helper_commands())
        self.assertTrue((self.root / "restore.called").exists())
        self.assertEqual(
            (self.root / "tokens.toml").read_bytes(),
            original_tokens,
        )
        self.assertFalse((self.root / "state/.mint-run-active").exists())

    def test_signal_at_earliest_guard_start_is_latched_and_forwarded(self):
        self._write_guard(
            """\
            #!/usr/bin/env bash
            set -u
            printf '%s\n' "$*" >> run-guarded.args
            trap 'printf "TERM\\n" > signal.received; printf "reason=operator_signal\\nduration_seconds=1\\n"; exit 0' TERM
            touch guard.started
            kill -TERM "$PPID"
            for _ in $(seq 1 100); do sleep 0.02; done
            printf 'reason=timeout\\nduration_seconds=2\\n'
            """
        )

        started = time.monotonic()
        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(
            (self.root / "signal.received").read_text().strip(), "TERM"
        )
        self.assertEqual(
            self.guard_invocations(),
            [
                "--live-confirmed --timeout 60 "
                "--profile single-mint-auto"
            ],
        )
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertTrue((self.root / "restore.called").exists())

    def test_finalize_failure_has_priority_and_exit_trap_restores(self):
        (self.root / "fail-finalize").touch()

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(phrase, TARGET_MINT, "--timeout", "60")

        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertTrue((self.root / "finalize.called").exists())
        self.assertEqual(
            self.helper_commands(),
            [
                "prepare",
                "result-path",
                "validate-live",
                "finalize",
                "restore",
            ],
        )
        self.assertTrue((self.root / "restore.called").exists())

    def test_post_wait_date_failure_still_attempts_finalize_and_restores(self):
        self._write_executable(
            "fake-bin/date",
            """\
            #!/usr/bin/env bash
            count_file=date.calls
            count=0
            if [[ -f "$count_file" ]]; then
              read -r count < "$count_file"
            fi
            count=$((count + 1))
            printf '%s\n' "$count" > "$count_file"
            if [[ "$count" -eq 1 ]]; then
              exec /bin/date "$@"
            fi
            exit 8
            """,
        )
        environment = os.environ.copy()
        environment["PATH"] = (
            f"{self.root / 'fake-bin'}:{environment.get('PATH', '')}"
        )

        phrase = f"RUN {TARGET_MINT} FOR 60\n"
        result = self.invoke(
            phrase,
            TARGET_MINT,
            "--timeout",
            "60",
            env=environment,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        finalize_args = (self.root / "finalize.called").read_text().split()
        self.assertEqual(
            finalize_args[finalize_args.index("--ended-at") + 1], "0"
        )
        self.assertEqual(
            self.helper_commands(),
            [
                "prepare",
                "result-path",
                "validate-live",
                "finalize",
                "restore",
            ],
        )
        self.assertTrue((self.root / "restore.called").exists())

    def test_sigint_and_sigterm_are_forwarded_then_finalized(self):
        for sent_signal, expected_name, expected_status in (
            (signal.SIGINT, "INT", 130),
            (signal.SIGTERM, "TERM", 143),
        ):
            with self.subTest(sent_signal=sent_signal):
                for name in (
                    "guard.started",
                    "signal.received",
                    "finalize.called",
                    "restore.called",
                ):
                    (self.root / name).unlink(missing_ok=True)
                (
                    self.root
                    / "state/mint-runs/20260724T190000Z/guard-result.txt"
                ).unlink(missing_ok=True)
                self._write_guard(
                    """\
                    #!/usr/bin/env bash
                    set -u
                    printf '%s\n' "$*" >> run-guarded.args
                    trap 'printf "INT\\n" > signal.received; printf "reason=operator_signal\\nduration_seconds=1\\n"; exit 23' INT
                    trap 'printf "TERM\\n" > signal.received; printf "reason=operator_signal\\nduration_seconds=1\\n"; exit 23' TERM
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

                self.assertEqual(process.returncode, expected_status, stderr)
                self.assertEqual(
                    (self.root / "signal.received").read_text().strip(),
                    expected_name,
                )
                self.assertTrue((self.root / "finalize.called").exists())
                self.assertTrue((self.root / "restore.called").exists())

    def test_signals_during_preparation_abort_before_prompt_and_restore_active(self):
        for sent_signal, expected_name, expected_status in (
            (signal.SIGINT, "SIGINT", 130),
            (signal.SIGTERM, "SIGTERM", 143),
        ):
            with self.subTest(sent_signal=sent_signal):
                for name in (
                    "prepare.started",
                    "prepare.signal",
                    "restore-active.called",
                ):
                    (self.root / name).unlink(missing_ok=True)
                (self.root / "slow-prepare").touch()
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
                for _ in range(200):
                    if (self.root / "prepare.started").exists():
                        break
                    time.sleep(0.01)
                else:
                    process.kill()
                    process.communicate()
                    self.fail("fake preparation did not start")

                os.kill(process.pid, sent_signal)
                stdout, stderr = process.communicate(timeout=5)

                self.assertEqual(process.returncode, expected_status, stderr)
                self.assertTrue((self.root / "prepare.signal").exists())
                self.assertEqual(
                    (self.root / "prepare.signal").read_text(),
                    expected_name,
                )
                self.assertNotIn("Type exactly:", stdout)
                self.assertEqual(self.guard_invocations(), [])
                self.assertTrue(
                    (self.root / "restore-active.called").exists()
                )
                (self.root / "slow-prepare").unlink(missing_ok=True)

    def test_mint_preparation_lock_blocks_direct_live_invocation(self):
        actual_wrapper = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "run-guarded.sh"
        )
        shutil.copy2(
            actual_wrapper,
            self.root / "scripts" / "direct-run-guarded.sh",
        )
        self._write_executable(
            "scripts/zavod_guard.py",
            """\
            #!/usr/bin/env python3
            from pathlib import Path
            Path("direct-guard.called").touch()
            """,
        )
        (self.root / "slow-prepare").touch()
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
        try:
            for _ in range(200):
                if (self.root / "prepare.started").exists():
                    break
                time.sleep(0.01)
            else:
                self.fail("fake preparation did not start")

            contender = subprocess.run(
                [
                    "bash",
                    "scripts/direct-run-guarded.sh",
                    "--live-confirmed",
                ],
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertNotEqual(contender.returncode, 0)
            self.assertFalse((self.root / "direct-guard.called").exists())
        finally:
            os.kill(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)

    def test_mint_contender_cannot_restore_owner_prepared_state(self):
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
        try:
            for _ in range(200):
                if (self.root / "owner-prepared").exists():
                    break
                time.sleep(0.01)
            else:
                self.fail("owner preparation did not complete")

            marker_path = self.root / "state/.mint-run-active"
            tokens_path = self.root / "tokens.toml"
            marker_before = marker_path.read_bytes()
            tokens_before = tokens_path.read_bytes()

            contender = self.invoke("", TARGET_MINT, "--timeout", "60")

            self.assertNotEqual(contender.returncode, 0)
            self.assertEqual(self.helper_commands(), ["prepare"])
            self.assertFalse((self.root / "restore.called").exists())
            self.assertFalse((self.root / "restore-active.called").exists())
            self.assertEqual(marker_path.read_bytes(), marker_before)
            self.assertEqual(tokens_path.read_bytes(), tokens_before)
        finally:
            os.kill(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
