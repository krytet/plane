from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_hooks


class InstallHookTests(unittest.TestCase):
    def test_cli_default_service_config_uses_localappdata_for_spool_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_app_data = root / "LocalAppData"
            service = local_app_data / "plane-chat-sync" / "service.json"
            service.parent.mkdir(parents=True)
            service.write_text(json.dumps({"stateDir": str(root / "state")}), encoding="utf-8")

            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                arguments = install_hooks._parser().parse_args(
                    [
                        "--hooks-json",
                        str(root / "hooks.json"),
                        "--install-dir",
                        str(root / "installed"),
                        "--spool-dir",
                        str(root / "wrong-spool"),
                    ]
                )

                self.assertEqual(arguments.service_config, service)
                with self.assertRaisesRegex(ValueError, "stateDir/hook-spool"):
                    install_hooks.install(
                        hooks_path=arguments.hooks_json,
                        install_dir=arguments.install_dir,
                        spool_dir=arguments.spool_dir,
                        service_config=arguments.service_config,
                        interpreter=Path(sys.executable),
                        apply=False,
                    )

    def test_derives_canonical_spool_from_service_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            service = root / "service.json"
            service.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "workspace": "personal",
                        "stateDir": str(state_dir),
                        "preserved": "value",
                    }
                ),
                encoding="utf-8",
            )

            result = install_hooks.install(
                hooks_path=root / "hooks.json",
                install_dir=root / "installed",
                spool_dir=None,
                service_config=service,
                interpreter=Path(sys.executable),
                apply=False,
            )

            self.assertEqual(result["spool_dir"], str((state_dir / "hook-spool").resolve()))
            self.assertEqual(json.loads(service.read_text(encoding="utf-8"))["preserved"], "value")

    def test_rejects_spool_that_disagrees_with_service_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / "service.json"
            service.write_text(json.dumps({"stateDir": str(root / "state")}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "stateDir/hook-spool"):
                install_hooks.install(
                    hooks_path=root / "hooks.json",
                    install_dir=root / "installed",
                    spool_dir=root / "other-spool",
                    service_config=service,
                    interpreter=Path(sys.executable),
                    apply=False,
                )

    def test_dry_run_has_no_filesystem_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / "hooks.json"
            original = {"description": "existing", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}
            hooks.write_text(json.dumps(original), encoding="utf-8")
            before = hooks.read_bytes()

            result = install_hooks.install(
                hooks_path=hooks,
                install_dir=root / "installed",
                spool_dir=root / "spool",
                interpreter=Path(sys.executable),
                apply=False,
            )

            self.assertTrue(result["config_changed"])
            self.assertTrue(result["script_changed"])
            self.assertFalse((root / "installed").exists())
            self.assertFalse((root / "spool").exists())
            self.assertEqual(hooks.read_bytes(), before)

    def test_apply_preserves_existing_config_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / "hooks.json"
            existing_handler = {"type": "command", "command": "echo keep", "custom": [1, 2]}
            original = {
                "description": "keep this",
                "custom": {"nested": True},
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [existing_handler]}],
                    "Stop": [{"hooks": [{"type": "command", "command": "existing-stop"}]}],
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing-prompt"}]}],
                },
            }
            hooks.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
            options = {
                "hooks_path": hooks,
                "install_dir": root / "installed",
                "spool_dir": root / "spool",
                "interpreter": Path(sys.executable),
                "apply": True,
            }

            first = install_hooks.install(**options)
            first_bytes = hooks.read_bytes()
            installed_script = root / "installed" / install_hooks.HOOK_FILENAME
            self.assertTrue(installed_script.is_file())
            self.assertIsNotNone(first["backup_path"])
            self.assertTrue(Path(first["backup_path"]).is_file())
            self.assertFalse((root / "config.toml").exists())

            updated = json.loads(first_bytes)
            self.assertEqual(updated["description"], original["description"])
            self.assertEqual(updated["custom"], original["custom"])
            self.assertEqual(updated["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
            for event in ("Stop", "UserPromptSubmit"):
                commands = [
                    handler.get("command")
                    for group in updated["hooks"][event]
                    for handler in group["hooks"]
                ]
                self.assertIn(original["hooks"][event][0]["hooks"][0]["command"], commands)

            for event in install_hooks.EVENTS:
                managed = [
                    handler
                    for group in updated["hooks"][event]
                    for handler in group["hooks"]
                    if install_hooks._managed_handler(handler)
                ]
                self.assertEqual(len(managed), 1)
                command = managed[0]["command"]
                self.assertIn(str(Path(sys.executable).resolve()), command)
                self.assertIn(str(installed_script.resolve()), command)
                self.assertIn(str((root / "spool").resolve()), command)
                self.assertEqual(managed[0]["timeout"], 3)
                self.assertNotIn("PreToolUse", install_hooks.EVENTS)

            second = install_hooks.install(**options)
            self.assertTrue(second["config_changed"] is False)
            self.assertTrue(second["script_changed"] is False)
            self.assertIsNone(second["backup_path"])
            self.assertEqual(hooks.read_bytes(), first_bytes)
            self.assertEqual(installed_script.read_bytes(), Path(install_hooks.__file__).with_name("hook_handler.py").read_bytes())

    def test_reapply_replaces_only_the_managed_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / "hooks.json"
            hooks.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "existing"},
                                        {
                                            "type": "command",
                                            "command": "old --hook-owner plane-codex-sync",
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            install_hooks.install(
                hooks_path=hooks,
                install_dir=root / "installed",
                spool_dir=root / "spool",
                interpreter=Path(sys.executable),
                apply=True,
            )
            updated = json.loads(hooks.read_text(encoding="utf-8"))
            commands = [
                handler["command"]
                for group in updated["hooks"]["Stop"]
                for handler in group["hooks"]
            ]
            self.assertIn("existing", commands)
            self.assertNotIn("old --hook-owner plane-codex-sync", commands)
            self.assertEqual(
                sum(install_hooks._managed_handler(handler) for group in updated["hooks"]["Stop"] for handler in group["hooks"]),
                1,
            )

    def test_spool_inside_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "outside the Plane repository"):
                install_hooks.install(
                    hooks_path=Path(directory) / "hooks.json",
                    install_dir=Path(directory) / "installed",
                    spool_dir=install_hooks._repository_root() / "hook-spool",
                    interpreter=Path(sys.executable),
                    apply=False,
                )


if __name__ == "__main__":
    unittest.main()
