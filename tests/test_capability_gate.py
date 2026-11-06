import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import capability_gate, release


class InspectConfigTests(unittest.TestCase):
    def inspect(self, *, volume_name: str, network_name: str) -> None:
        project = "fgcap-012345abcdef"
        config = {
            "name": project,
            "volumes": {"postgres_data": {"name": volume_name}},
            "networks": {"backend": {"name": network_name}},
            "services": {
                "db": {"volumes": [], "ports": []},
                "migrate": {
                    "volumes": [],
                    "ports": [],
                    "build": {"context": "/tmp/go"},
                    "labels": {"org.financegame.source.revision": "a" * 40},
                },
                "api": {
                    "volumes": [],
                    "ports": [],
                    "build": {"context": "/tmp/go"},
                    "labels": {"org.financegame.source.revision": "a" * 40},
                },
                "ui": {
                    "volumes": [],
                    "ports": [{"host_ip": "127.0.0.1", "target": 8080}],
                    "labels": {"org.financegame.source.revision": "b" * 40},
                },
            },
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(config))
        env = {
            "CAPABILITY_PROJECT": project,
            "CAPABILITY_TEMP_ROOT": "/tmp/fgcap-012345abcdef",
            "GO_SOURCE_REVISION": "a" * 40,
            "UI_SOURCE_REVISION": "b" * 40,
        }
        with patch.object(capability_gate, "run", return_value=result):
            capability_gate.inspect_config(["docker", "compose"], env)


    def test_accepts_project_scoped_resource_names(self) -> None:
        self.inspect(
            volume_name="fgcap-012345abcdef_postgres_data",
            network_name="fgcap-012345abcdef_backend",
        )

    def test_rejects_globally_named_volume(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "volume"):
            self.inspect(volume_name="postgres_data", network_name="fgcap-012345abcdef_backend")

    def test_rejects_globally_named_network(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "network"):
            self.inspect(volume_name="fgcap-012345abcdef_postgres_data", network_name="backend")

    def test_rejects_different_compose_project(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "expected generated project"):
            self.inspect_config_with_project("finance_game_bundle")

    def inspect_config_with_project(self, project: str) -> None:
        config = {
            "name": project,
            "volumes": {},
            "networks": {},
            "services": {"ui": {"volumes": [], "ports": [{"host_ip": "127.0.0.1"}]}},
        }
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(config))
        env = {"CAPABILITY_PROJECT": "fgcap-012345abcdef", "CAPABILITY_TEMP_ROOT": "/tmp/fgcap-012345abcdef"}
        with patch.object(capability_gate, "run", return_value=result):
            capability_gate.inspect_config(["docker", "compose"], env)


class CandidateManifestTests(unittest.TestCase):
    go_revision = "a" * 40
    ui_revision = "b" * 40

    def test_loads_exact_candidate_pair_without_changing_release_contract(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps({"formatVersion": 1, "goRevision": self.go_revision, "uiRevision": self.ui_revision}))
            candidate, digest = capability_gate.load_candidate_manifest(path)
        self.assertEqual(candidate["goRevision"], self.go_revision)
        self.assertEqual(candidate["uiRevision"], self.ui_revision)
        self.assertEqual(len(digest), 64)

    def test_rejects_extra_fields_and_short_or_uppercase_revisions(self) -> None:
        cases = [
            {"formatVersion": 1, "goRevision": self.go_revision, "uiRevision": self.ui_revision, "qualification": "qualified"},
            {"formatVersion": 1, "goRevision": "A" * 40, "uiRevision": self.ui_revision},
            {"formatVersion": 1, "goRevision": self.go_revision, "uiRevision": "short"},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            for value in cases:
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(capability_gate.GateError):
                    capability_gate.load_candidate_manifest(path)

    def test_requires_exact_source_revisions(self) -> None:
        candidate = {"formatVersion": 1, "goRevision": self.go_revision, "uiRevision": self.ui_revision}
        capability_gate.validate_candidate_revisions(candidate, self.go_revision, self.ui_revision)
        with self.assertRaisesRegex(capability_gate.GateError, "uiRevision mismatch"):
            capability_gate.validate_candidate_revisions(candidate, self.go_revision, "c" * 40)

    def test_candidate_validation_requires_uncached_builds(self) -> None:
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "candidate.json"
            manifest.write_text(json.dumps({"formatVersion": 1, "goRevision": self.go_revision, "uiRevision": self.ui_revision}))
            argv = ["capability_gate.py", "--go-source", directory, "--ui-source", directory,
                    "--candidate-manifest", str(manifest), "--borrowing-browser"]
            with patch.object(capability_gate.sys, "argv", argv):
                with self.assertRaisesRegex(capability_gate.GateError, "requires --no-cache"):
                    capability_gate.main()


class CapabilityBrowserRunnerTests(unittest.TestCase):
    def test_uses_explicit_fixture_and_does_not_forward_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ui = root / "ui"
            ui.mkdir()
            (ui / "playwright.capability.config.ts").write_text("export default {};\n")
            fixture = root / "fixture.json"
            fixture.write_text("{}\n")
            evidence = root / "evidence"
            observed: dict[str, object] = {}

            def completed(command: list[str], *, cwd: Path, env: dict[str, str], **_: object) -> subprocess.CompletedProcess[str]:
                observed.update({"command": command, "cwd": cwd, "env": env})
                return subprocess.CompletedProcess(command, 0, stdout="1 passed\n")

            with patch.dict("os.environ", {"API_ACCESS_TOKEN": "secret", "OIDC_ISSUER": "https://issuer.test", "OIDC_ENABLED": "true"}, clear=True), \
                 patch.object(capability_gate.subprocess, "run", side_effect=completed):
                capability_gate.run_capability_browser(ui, "http://127.0.0.1:49152", fixture, evidence, grep="@scenario")
            env = observed["env"]
            self.assertEqual(env["CAPABILITY_UI_BASE_URL"], "http://127.0.0.1:49152")
            self.assertEqual(env["CAPABILITY_FIXTURE_MANIFEST"], str(fixture.resolve()))
            self.assertEqual(
                env["CAPABILITY_PLAYWRIGHT_OUTPUT_DIR"],
                str((evidence / "browser-artifacts" / "scenario").resolve()),
            )
            self.assertNotIn("API_ACCESS_TOKEN", env)
            self.assertNotIn("OIDC_ISSUER", env)
            self.assertNotIn("OIDC_ENABLED", env)
            self.assertEqual((evidence / "browser-scenario.log").read_text(), "1 passed\n")

    def test_browser_failure_retains_bounded_log(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ui = root / "ui"
            ui.mkdir()
            (ui / "playwright.capability.config.ts").write_text("export default {};\n")
            fixture = root / "fixture.json"
            fixture.write_text("{}\n")
            evidence = root / "evidence"
            with patch.object(capability_gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="browser failed\n")):
                with self.assertRaisesRegex(capability_gate.GateError, "browser-.*log"):
                    capability_gate.run_capability_browser(ui, "http://127.0.0.1:49152", fixture, evidence, grep="@suite")
            self.assertEqual((evidence / "browser-suite.log").read_text(), "browser failed\n")

    def test_browser_timeout_retains_partial_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ui = root / "ui"
            ui.mkdir()
            (ui / "playwright.capability.config.ts").write_text("export default {};\n")
            fixture = root / "fixture.json"
            fixture.write_text("{}\n")
            evidence = root / "evidence"
            error = subprocess.TimeoutExpired(["playwright"], 600, output="browser still running\n")
            with patch.object(capability_gate.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(capability_gate.GateError, "timed out"):
                    capability_gate.run_capability_browser(ui, "http://127.0.0.1:49152", fixture, evidence, grep="@suite")
            self.assertEqual((evidence / "browser-suite.log").read_text(), "browser still running\n")


class RunAssertionTests(unittest.TestCase):
    def test_accepts_frozen_taxable_source_cost_basis_and_dated_attribution(self) -> None:
        capability_gate.assert_taxable_funding_compilation({"content": {
            "retirementFundingPolicy": {"sources": [{
                "accountId": "non-registered-1",
                "fundingKind": "taxable_sale",
                "effectiveStart": "2080-01-01",
                "effectiveEnd": "2080-12-31",
                "saleCostRate": "0.01",
                "saleCostAssumptionLabel": "Disposable sale costs",
            }]},
            "portfolioBasis": [{"accountId": "non-registered-1", "revision": 1}],
            "taxAttributions": [{
                "sourceKind": "account",
                "sourceId": "non-registered-1",
                "category": category,
                "allocationMethod": "owner_share",
                "effectiveStart": "2026-01-01",
                "effectiveEnd": "2080-12-31",
            } for category in ("realized_capital_gain", "realized_capital_loss")],
        }}, "non-registered-1", basis_revision=1, sale_cost_rate="0.01",
            sale_cost_label="Disposable sale costs", start="2026-01-01", source_start="2080-01-01", end="2080-12-31")

    def test_rejects_stale_pinned_basis_revision(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "did not pin"):
            capability_gate.assert_taxable_funding_compilation({"content": {
                "retirementFundingPolicy": {"sources": [{
                    "accountId": "non-registered-1",
                    "fundingKind": "taxable_sale",
                    "effectiveStart": "2080-01-01",
                    "effectiveEnd": "2080-12-31",
                    "saleCostRate": "0.01",
                    "saleCostAssumptionLabel": "Disposable sale costs",
                }]},
                "portfolioBasis": [{"accountId": "non-registered-1", "revision": 2}],
                "taxAttributions": [],
            }}, "non-registered-1", basis_revision=1, sale_cost_rate="0.01",
                sale_cost_label="Disposable sale costs", start="2026-01-01", source_start="2080-01-01", end="2080-12-31")

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

    def test_accepts_reconciled_taxable_retirement_sale_and_person_tax_allocation(self) -> None:
        capability_gate.assert_taxable_retirement_funding_report(
            {
                "fundingEvents": [
                    {"purpose": "retirement_spending", "requested": "100.00", "funded": "100.00", "unfunded": "0.00"},
                    {"purpose": "tax_settlement", "requested": "45.00", "funded": "40.00", "unfunded": "5.00"},
                ],
                "periods": [{
                    "periodStart": "2080-01-01",
                    "reconciliation": {
                        "openingNetWorthResidual": "0.00",
                        "movementDeltaResidual": "0.00",
                        "closingNetWorthResidual": "0.00",
                    },
                    "portfolioTrades": [{
                        "id": "generated-sale-1",
                        "accountId": "non-registered-1",
                        "effectiveDate": "2080-01-01",
                        "fundingSourceId": "taxable-source-1",
                        "fundingPurpose": "retirement_spending",
                        "grossProceeds": "100.00",
                        "transactionCosts": "1.00",
                        "disposedBasis": "60.00",
                        "realizedGainLoss": "39.00",
                        "netProceeds": "99.00",
                        "closingMarketValue": "900.00",
                        "closingBasis": "540.00",
                        "taxableByPerson": [{"personId": "person-1", "amount": "39.00"}],
                    }],
                }, {
                    "periodStart": "2080-12-01",
                    "reconciliation": {
                        "openingNetWorthResidual": "0.00",
                        "movementDeltaResidual": "0.00",
                        "closingNetWorthResidual": "0.00",
                    },
                    "portfolioTrades": [{
                        "id": "generated-sale-2",
                        "accountId": "non-registered-1",
                        "effectiveDate": "2080-12-31",
                        "fundingSourceId": "taxable-source-1",
                        "fundingPurpose": "tax_settlement",
                        "grossProceeds": "40.00",
                        "transactionCosts": "0.40",
                        "disposedBasis": "24.00",
                        "realizedGainLoss": "15.60",
                        "netProceeds": "39.60",
                        "closingMarketValue": "0.00",
                        "closingBasis": "0.00",
                        "taxableByPerson": [{"personId": "person-1", "amount": "15.60"}],
                    }],
                }],
            },
            "non-registered-1",
        )

    def test_rejects_unreconciled_taxable_sale_or_missing_funding_evidence(self) -> None:
        report = {
            "fundingEvents": [{"purpose": "retirement_spending", "requested": "100.00", "funded": "100.00", "unfunded": "0.00"}],
            "periods": [{
                "periodStart": "2080-01-01",
                "reconciliation": {"movementDeltaResidual": "0.01"},
                "portfolioTrades": [{
                    "id": "generated-sale-1",
                    "accountId": "non-registered-1",
                    "effectiveDate": "2080-01-01",
                    "fundingSourceId": "taxable-source-1",
                    "fundingPurpose": "retirement_spending",
                    "grossProceeds": "100.00",
                    "transactionCosts": "1.00",
                    "disposedBasis": "60.00",
                    "realizedGainLoss": "39.00",
                    "netProceeds": "99.00",
                    "closingMarketValue": "900.00",
                    "closingBasis": "540.00",
                    "taxableByPerson": [{"personId": "person-1", "amount": "39.00"}],
                }],
            }],
        }
        with self.assertRaisesRegex(capability_gate.GateError, "not reconciled"):
            capability_gate.assert_taxable_retirement_funding_report(report, "non-registered-1")
        report["periods"][0]["reconciliation"]["movementDeltaResidual"] = "0.00"
        report["fundingEvents"] = []
        with self.assertRaisesRegex(capability_gate.GateError, "no funded retirement cash-deficit"):
            capability_gate.assert_taxable_retirement_funding_report(report, "non-registered-1")

    def test_rejects_tax_settlement_sale_without_retirement_spending_sale(self) -> None:
        report = {
            "fundingEvents": [{"purpose": "tax_settlement", "funded": "99.00", "unfunded": "0.00"}],
            "periods": [{
                "reconciliation": {"movementDeltaResidual": "0.00"},
                "portfolioTrades": [{
                    "id": "generated-sale-1",
                    "accountId": "non-registered-1",
                    "effectiveDate": "2080-12-31",
                    "fundingSourceId": "taxable-source-1",
                    "fundingPurpose": "tax_settlement",
                    "grossProceeds": "100.00",
                    "transactionCosts": "1.00",
                    "disposedBasis": "60.00",
                    "realizedGainLoss": "39.00",
                    "netProceeds": "99.00",
                    "closingMarketValue": "0.00",
                    "closingBasis": "0.00",
                    "taxableByPerson": [{"personId": "person-1", "amount": "39.00"}],
                }],
            }],
        }
        with self.assertRaisesRegex(capability_gate.GateError, "no retirement-spending funding events"):
            capability_gate.assert_taxable_retirement_funding_report(report, "non-registered-1")

    def test_accepts_reconciled_taxable_sale_aggregates_in_suite_annual_outcomes(self) -> None:
        capability_gate.assert_taxable_retirement_funding_suite({"outcome": {"annual": [{
            "year": 2065,
            "taxableSaleGrossProceeds": "100.00",
            "taxableSaleTransactionCosts": "1.00",
            "taxableSaleBasisConsumed": "60.00",
            "taxableRealizedGainLoss": "39.00",
            "taxableSaleNetProceeds": "99.00",
            "estimatedTax": "12.00",
            "taxSettlementPaid": "10.00",
            "taxRefundReceived": "0.00",
            "remainingTaxDueOrRefund": "2.00",
            "debtBalances": "0.00",
        }]}})

    def test_rejects_suite_annual_sale_aggregate_mismatch(self) -> None:
        with self.assertRaisesRegex(capability_gate.GateError, "do not reconcile"):
            capability_gate.assert_taxable_retirement_funding_suite({"outcome": {"annual": [{
                "year": 2065,
                "taxableSaleGrossProceeds": "100.00",
                "taxableSaleTransactionCosts": "1.00",
                "taxableSaleBasisConsumed": "60.00",
                "taxableRealizedGainLoss": "40.00",
                "taxableSaleNetProceeds": "99.00",
                "estimatedTax": "12.00",
                "taxSettlementPaid": "10.00",
                "taxRefundReceived": "0.00",
                "remainingTaxDueOrRefund": "2.00",
                "debtBalances": "0.00",
            }]}})


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
        project = "fgcap-012345abcdef"
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
            self.assertEqual((evidence / "cleanup-primary.log").read_text(), "project removed\n")

        self.assertEqual(removed_images, [images[1]])

    def test_refuses_to_remove_project_when_a_container_has_another_label(self) -> None:
        project = "fgcap-012345abcdef"
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
        project = "fgcap-012345abcdef"
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
            project = "fgcap-012345abcdef"
            override_path = capability_gate.write_override(Path(directory), "https://oidc:8443/", project)
            override = json.loads(override_path.read_text())

        self.assertEqual(override["services"]["api"]["build"]["target"], "capability-api")
        self.assertEqual(capability_gate.RULE_PACK, "canadian-planning-rules-test-v1")
        for service, image in zip(("migrate", "api", "ui"), capability_gate.capability_images(project)):
            self.assertEqual(override["services"][service]["image"], image)
            self.assertEqual(override["services"][service]["build"]["labels"][capability_gate.IMAGE_LABEL], project)

    def test_candidate_pair_sha_is_baked_into_image_provenance(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        candidate_pair_sha = "c" * 64
        with TemporaryDirectory() as directory:
            override_path = capability_gate.write_override(
                Path(directory), "https://oidc:8443/", "fgcap-012345abcdef", candidate_pair_sha,
            )
            override = json.loads(override_path.read_text())

        for service in ("migrate", "api", "ui"):
            self.assertEqual(
                override["services"][service]["build"]["labels"]["org.financegame.release.pair-sha256"],
                candidate_pair_sha,
            )


class RecoverySafetyTests(unittest.TestCase):
    def test_refuses_external_database_volume_before_container_inspection(self) -> None:
        project = "fgcap-012345abcdef"
        compose = ["docker", "compose", "--project-name", project]
        config = {"name": project, "volumes": {"postgres_data": {"name": "live_database", "external": True}}}
        with patch.object(capability_gate, "run", return_value=subprocess.CompletedProcess(compose, 0, stdout=json.dumps(config))) as mocked:
            with self.assertRaisesRegex(capability_gate.GateError, "non-disposable volume"):
                capability_gate.verify_disposable_database(compose, {"CAPABILITY_PROJECT": project})
        self.assertEqual(mocked.call_count, 1)

    def test_accepts_backup_reference_for_matching_release_and_sources(self) -> None:
        pair_sha = "a" * 64
        sources = {"go": {"revision": "b" * 40}, "ui": {"revision": "c" * 40}}
        previous = {"releaseId": "release-1", "databaseSchemaVersion": 27,
                    "releasePairSha256": pair_sha, "sources": sources}
        backup = {"location": "file:///tmp/backup.dump", "createdAt": "2026-10-16T12:00:00Z",
                  "sha256": "d" * 64, "schemaVersion": 27, "releaseId": "release-1",
                  "releasePairSha256": pair_sha, "sources": sources}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backup-reference.json"
            path.write_text(json.dumps(backup))
            self.assertEqual(release.read_backup_reference(str(path), previous)["sources"], sources)

    def test_rejects_backup_reference_with_mismatched_source_revision(self) -> None:
        sources = {"go": {"revision": "b" * 40}, "ui": {"revision": "c" * 40}}
        previous = {"releaseId": "release-1", "databaseSchemaVersion": 27,
                    "releasePairSha256": "a" * 64, "sources": sources}
        backup = {"location": "file:///tmp/backup.dump", "createdAt": "2026-10-16T12:00:00Z",
                  "sha256": "d" * 64, "schemaVersion": 27, "releaseId": "release-1",
                  "releasePairSha256": "a" * 64,
                  "sources": {"go": {"revision": "e" * 40}, "ui": sources["ui"]}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backup-reference.json"
            path.write_text(json.dumps(backup))
            with self.assertRaisesRegex(release.release_pair.ReleasePairError, "does not match the previous deployment"):
                release.read_backup_reference(str(path), previous)

    def test_dirty_candidate_fingerprint_includes_untracked_file_contents(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            (root / "tracked.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True)
            (root / "tracked.txt").write_text("candidate\n")
            untracked = root / "candidate.json"
            untracked.write_text('{"phase":"one"}\n')
            first_status, first_digest = capability_gate.bundle_worktree_fingerprint(root)
            untracked.write_text('{"phase":"two"}\n')
            second_status, second_digest = capability_gate.bundle_worktree_fingerprint(root)
        self.assertTrue(first_status)
        self.assertTrue(second_status)
        self.assertNotEqual(first_digest, second_digest)


if __name__ == "__main__":
    unittest.main()
