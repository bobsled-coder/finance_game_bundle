import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import capability_gate


class InspectConfigTests(unittest.TestCase):
    def inspect(self, *, volume_name: str, network_name: str) -> None:
        project = "fgcap-test"
        config = {
            "name": project,
            "volumes": {"postgres_data": {"name": volume_name}},
            "networks": {"backend": {"name": network_name}},
            "services": {
                "db": {"volumes": [], "ports": []},
                "api": {"volumes": [], "ports": []},
                "ui": {
                    "volumes": [],
                    "ports": [{"host_ip": "127.0.0.1", "target": 8080}],
                },
            },
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(config))
        env = {
            "CAPABILITY_PROJECT": project,
            "CAPABILITY_TEMP_ROOT": "/tmp/fgcap-test",
        }
        with patch.object(capability_gate, "run", return_value=result):
            capability_gate.inspect_config(["docker", "compose"], env)

    def test_accepts_project_scoped_resource_names(self) -> None:
        self.inspect(
            volume_name="fgcap-test_postgres_data",
            network_name="fgcap-test_backend",
        )

    def test_rejects_globally_named_volume(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "volume"):
            self.inspect(volume_name="postgres_data", network_name="fgcap-test_backend")

    def test_rejects_globally_named_network(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "network"):
            self.inspect(volume_name="fgcap-test_postgres_data", network_name="backend")

    def test_rejects_different_compose_project(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "expected isolated project"):
            self.inspect_config_with_project("finance_game_bundle")

    def inspect_config_with_project(self, project: str) -> None:
        config = {
            "name": project,
            "volumes": {},
            "networks": {},
            "services": {"ui": {"volumes": [], "ports": [{"host_ip": "127.0.0.1"}]}},
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(config))
        env = {"CAPABILITY_PROJECT": "fgcap-test", "CAPABILITY_TEMP_ROOT": "/tmp/fgcap-test"}
        with patch.object(capability_gate, "run", return_value=result):
            capability_gate.inspect_config(["docker", "compose"], env)


class RunAssertionTests(unittest.TestCase):
    def test_accepts_expected_immutable_run_and_report(self) -> None:
        capability_gate.assert_run_artifact(
            {"id": "run-1", "status": "completed", "compilationHash": "c", "resultHash": "r"},
            "run-1",
            "c",
            "r",
        )
        capability_gate.assert_fixture_report(
            {
                "summary": {
                    "periodCount": 12,
                    "openingNetWorth": "100000.10",
                    "closingNetWorth": "100000.10",
                }
            }
        )

    def test_rejects_changed_run_hashes(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "hash changed"):
            capability_gate.assert_run_artifact(
                {"id": "run-1", "status": "completed", "compilationHash": "c", "resultHash": "changed"},
                "run-1",
                "c",
                "r",
            )

    def test_rejects_unexpected_projection_total(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "does not match"):
            capability_gate.assert_fixture_report(
                {
                    "summary": {
                        "periodCount": 12,
                        "openingNetWorth": "100000.10",
                        "closingNetWorth": "100000.09",
                    }
                }
            )


class ComposeBuildFailureTests(unittest.TestCase):
    def test_retains_build_output_in_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            evidence = Path(directory)
            failure = subprocess.CompletedProcess([], 1, stdout="image build failed: missing Dockerfile\n")
            with patch.object(capability_gate, "run", return_value=failure):
                with self.assertRaisesRegex(capability_gate.GateError, "compose-build-failure.log"):
                    capability_gate.run_compose_build(["docker", "compose"], {}, evidence)
            self.assertEqual(
                (evidence / "compose-build-failure.log").read_text(),
                "image build failed: missing Dockerfile\n",
            )


class CleanupOwnershipTests(unittest.TestCase):
    def test_cleanup_removes_only_owned_images_after_compose_resources(self) -> None:
        project = "fgcap-test"
        compose = ["docker", "compose", "--project-name", project]
        images = capability_gate.capability_images(project)
        present_images = {images[1]}
        down_completed = False
        removed_images: list[str] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal down_completed
            if command == compose + ["ps", "-aq"]:
                return subprocess.CompletedProcess(command, 0, stdout="" if down_completed else "container-1\n")
            if command[:3] == ["docker", "volume", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout="volume-1\n" if not down_completed else "")
            if command[:3] == ["docker", "network", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout="network-1\n" if not down_completed else "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout=project + "\n")
            if command[:3] in (["docker", "volume", "inspect"], ["docker", "network", "inspect"]):
                return subprocess.CompletedProcess(command, 0, stdout=project + "\n")
            if command[:3] == ["docker", "image", "inspect"]:
                image = command[-1]
                if image not in images:
                    self.fail(f"cleanup inspected an image outside this run: {image}")
                if "-f" in command:
                    if image in present_images:
                        return subprocess.CompletedProcess(command, 0, stdout=project + "\n")
                    return subprocess.CompletedProcess(command, 1, stdout="Error: No such image\n")
                if image in present_images:
                    return subprocess.CompletedProcess(command, 0, stdout=image + "\n")
                return subprocess.CompletedProcess(command, 1, stdout="Error: No such image\n")
            if command[:2] == compose[:2] and "down" in command:
                down_completed = True
                return subprocess.CompletedProcess(command, 0, stdout="project removed\n")
            if command[:3] == ["docker", "image", "rm"]:
                self.assertTrue(down_completed, "project containers must be removed before their images")
                image = command[-1]
                self.assertIn(image, present_images)
                removed_images.append(image)
                present_images.remove(image)
                return subprocess.CompletedProcess(command, 0, stdout=image + "\n")
            self.fail(f"unexpected cleanup command: {command}")

        with TemporaryDirectory() as directory:
            evidence = Path(directory)
            with patch.object(capability_gate, "run", side_effect=fake_run):
                capability_gate.cleanup_project(compose, {}, project, evidence)
            self.assertEqual((evidence / "cleanup.log").read_text(), "project removed\n")

        self.assertEqual(removed_images, [images[1]])

    def test_refuses_to_remove_project_when_a_container_has_another_label(self) -> None:
        project = "fgcap-test"
        compose = ["docker", "compose", "--project-name", project]

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command == compose + ["ps", "-aq"]:
                return subprocess.CompletedProcess(command, 0, stdout="container-1\n")
            if command[:3] == ["docker", "volume", "ls"] or command[:3] == ["docker", "network", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout="")
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 1, stdout="Error: No such image\n")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout="another-project\n")
            if command[:2] == compose[:2] and "down" in command:
                self.fail("cleanup must not run when ownership verification fails")
            self.fail(f"unexpected cleanup command: {command}")

        with TemporaryDirectory() as directory:
            evidence = Path(directory)
            with patch.object(capability_gate, "run", side_effect=fake_run):
                with self.assertRaisesRegex(capability_gate.GateError, "unexpected Compose project label"):
                    capability_gate.cleanup_project(compose, {}, project, evidence)
            self.assertEqual(list(evidence.iterdir()), [])

    def test_refuses_to_remove_a_capability_image_owned_by_another_project(self) -> None:
        project = "fgcap-test"
        compose = ["docker", "compose", "--project-name", project]

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command == compose + ["ps", "-aq"]:
                return subprocess.CompletedProcess(command, 0, stdout="")
            if command[:3] == ["docker", "volume", "ls"] or command[:3] == ["docker", "network", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout="")
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout="another-project\n")
            if command[:2] == compose[:2] and "down" in command:
                self.fail("cleanup must not run when image ownership verification fails")
            self.fail(f"unexpected cleanup command: {command}")

        with TemporaryDirectory() as directory:
            evidence = Path(directory)
            with patch.object(capability_gate, "run", side_effect=fake_run):
                with self.assertRaisesRegex(capability_gate.GateError, "unexpected capability project label"):
                    capability_gate.cleanup_project(compose, {}, project, evidence)
            self.assertEqual(list(evidence.iterdir()), [])


class CapabilityOverrideTests(unittest.TestCase):
    def test_selects_fixed_test_only_api_target(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            project = "fgcap-test"
            override_path = capability_gate.write_override(Path(directory), "https://oidc:8443/", project)
            override = json.loads(override_path.read_text())

        self.assertEqual(override["services"]["api"]["build"]["target"], "capability-api")
        self.assertEqual(capability_gate.RULE_PACK, "canadian-planning-rules-test-v1")
        for service, image in zip(("migrate", "api", "ui"), capability_gate.capability_images(project)):
            self.assertEqual(override["services"][service]["image"], image)
            self.assertEqual(override["services"][service]["build"]["labels"][capability_gate.IMAGE_LABEL], project)


if __name__ == "__main__":
    unittest.main()
