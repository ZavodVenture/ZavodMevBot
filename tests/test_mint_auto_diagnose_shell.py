import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


TARGET_MINT = "So11111111111111111111111111111111111111112"
CONFIRMATION = f"AUTODIAGNOSE {TARGET_MINT} WITH 0.03 SOL\n"


class MintAutoDiagnoseShellTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "state").mkdir(mode=0o700)
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "mint-auto-diagnose.sh"
        )
        if source.exists():
            shutil.copy2(source, self.root / "scripts" / source.name)
        self._write_helper()
        self._write_guard()

    def tearDown(self):
        shutil.rmtree(self.root)

    def _write_executable(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
        path.chmod(0o755)

    def _write_helper(self):
        self._write_executable(
            "scripts/mint_auto_diagnoser.py",
            """\
            #!/usr/bin/env python3
            import json
            import pathlib
            import sys

            command = sys.argv[1]
            root = pathlib.Path(sys.argv[2])
            with (root / "helper.calls").open("a") as handle:
                handle.write(command + " " + " ".join(sys.argv[3:]) + "\\n")
            batch_id = "20260726T190000Z"
            batch_root = root / "state/auto-diagnose-runs" / batch_id
            names = (
                "baseline", "offchain", "activity", "aggregate_profit",
                "per_arb_profit", "roi", "volume", "pool_liquidity",
            )
            if command == "prepare":
                stages = []
                for index, name in enumerate(names):
                    stage_root = batch_root / "stages" / f"{index}-{name}"
                    stage_root.mkdir(parents=True, mode=0o700)
                    stage_root.chmod(0o700)
                    contract = stage_root / "stage-contract.json"
                    contract.write_text("{}\\n")
                    contract.chmod(0o600)
                    skipped = index >= 2
                    stages.append({
                        "name": name,
                        "skipped": skipped,
                        "skip_reason": (
                            "already_permissive" if skipped else None
                        ),
                    })
                marker = root / "state/.mint-auto-diagnose-active"
                marker.write_text(batch_id + "\\n")
                marker.chmod(0o600)
                if (root / "bad-prepare-output").exists():
                    print("{}")
                    raise SystemExit
                print(json.dumps({
                    "batch_id": batch_id,
                    "target_mint": sys.argv[3],
                    "timeout_seconds": 300,
                    "early_stop_lamports": 25000000,
                    "loss_limit_lamports": 30000000,
                    "stages": stages,
                }))
            elif command == "stage-contract-path":
                index = int(sys.argv[3])
                name = sys.argv[4]
                print(
                    f"state/auto-diagnose-runs/{batch_id}/"
                    f"stages/{index}-{name}/stage-contract.json"
                )
            elif command == "evaluate-stage":
                decisions = root / "decisions"
                values = decisions.read_text().splitlines()
                decision = values.pop(0)
                decisions.write_text(
                    ("\\n".join(values) + "\\n") if values else ""
                )
                print(json.dumps({
                    "stage_name": sys.argv[4],
                    "decision": decision,
                    "stop_reason": "fixture",
                    "target_status": (
                        "positive" if decision == "target_positive" else "absent"
                    ),
                    "three_hop_status": "unproven",
                    "sender_accepted": 0,
                    "sender_rejected": 0,
                    "target_landed": 0,
                    "cumulative_loss_lamports": 0,
                }))
            elif command == "write-batch-result":
                result = batch_root / "batch-result.json"
                result.write_text("{}\\n")
                result.chmod(0o600)
            elif command in {"restore", "restore-active"}:
                (root / "state/.mint-auto-diagnose-active").unlink(
                    missing_ok=True
                )
            else:
                raise SystemExit(2)
            """,
        )

    def _write_guard(self):
        self._write_executable(
            "scripts/run-guarded.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            [[ "${ZAVOD_LIVE_LOCK_FD:-}" =~ ^[0-9]+$ ]]
            [[ "${ZAVOD_BATCH_CONTRACT_FD:-}" =~ ^[0-9]+$ ]]
            [[ -e "/proc/$$/fd/$ZAVOD_LIVE_LOCK_FD" ]]
            [[ -e "/proc/$$/fd/$ZAVOD_BATCH_CONTRACT_FD" ]]
            printf '%s\\n' "$*" >> guard.calls
            """,
        )

    def invoke(self, stdin=CONFIRMATION):
        return subprocess.run(
            ["bash", "scripts/mint-auto-diagnose.sh", TARGET_MINT],
            cwd=self.root,
            input=stdin,
            text=True,
            capture_output=True,
        )

    def helper_commands(self):
        path = self.root / "helper.calls"
        return path.read_text().splitlines() if path.exists() else []

    def guard_calls(self):
        path = self.root / "guard.calls"
        return path.read_text().splitlines() if path.exists() else []

    def set_decisions(self, *decisions):
        (self.root / "decisions").write_text("\n".join(decisions) + "\n")

    def test_exact_confirmation_is_required_once(self):
        self.set_decisions("target_positive")
        declined = self.invoke("wrong\n")
        self.assertNotEqual(declined.returncode, 0)
        self.assertEqual(self.helper_commands(), [])

        accepted = self.invoke()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(
            sum(line.startswith("prepare ") for line in self.helper_commands()),
            1,
        )

    def test_declared_non_skipped_stages_execute_in_order(self):
        self.set_decisions("continue", "continue")

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.guard_calls()), 2)
        self.assertIn("/stages/0-baseline", self.guard_calls()[0])
        self.assertIn("/stages/1-offchain", self.guard_calls()[1])

    def test_target_positive_stops_immediately(self):
        self.set_decisions("target_positive", "continue")

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.guard_calls()), 1)

    def test_continue_advances_once_without_retry(self):
        self.set_decisions("continue", "target_positive")

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        evaluated = [
            line for line in self.helper_commands()
            if line.startswith("evaluate-stage ")
        ]
        self.assertEqual(len(evaluated), 2)
        self.assertIn(" baseline ", evaluated[0])
        self.assertIn(" offchain ", evaluated[1])

    def test_failed_decision_stops_and_restores(self):
        self.set_decisions("failed", "target_positive")

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.guard_calls()), 1)
        self.assertTrue(
            any(line.startswith("restore ") for line in self.helper_commands())
        )
        self.assertFalse(
            (self.root / "state/.mint-auto-diagnose-active").exists()
        )

    def test_invalid_prepare_output_restores_active_marker(self):
        (self.root / "bad-prepare-output").touch()
        self.set_decisions("target_positive")

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            any(
                line.startswith("restore-active ")
                for line in self.helper_commands()
            )
        )
        self.assertFalse(
            (self.root / "state/.mint-auto-diagnose-active").exists()
        )

    def test_only_guarded_wrapper_launches_a_stage(self):
        self.set_decisions("target_positive")

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.guard_calls()), 1)
        script = (self.root / "scripts/mint-auto-diagnose.sh").read_text()
        self.assertIn("scripts/run-guarded.sh", script)
        self.assertNotIn("zavod-mev-bot-rust-version-cli run", script)


if __name__ == "__main__":
    unittest.main()
