"""Load and validate the immutable UI/API release-pair contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-pair.json"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ReleasePairError(RuntimeError):
    pass


def load(path: Path = MANIFEST) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleasePairError(f"Release-pair manifest is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleasePairError("Release-pair manifest must contain a JSON object.")
    if value.get("formatVersion") != 1:
        raise ReleasePairError("Release-pair manifest formatVersion must be 1.")
    if not value.get("releaseId"):
        raise ReleasePairError("Release-pair manifest is missing releaseId.")
    sources = value.get("sources", {})
    if not isinstance(sources, dict):
        raise ReleasePairError("Release-pair sources must be an object.")
    if set(sources) != {"go", "ui"}:
        raise ReleasePairError("Release-pair sources must contain exactly go and ui.")
    for name in ("go", "ui"):
        source = sources.get(name, {})
        if not isinstance(source, dict):
            raise ReleasePairError(f"Release-pair {name} source must be an object.")
        revision = source.get("revision", "")
        if not source.get("repository") or not isinstance(revision, str) or not COMMIT.fullmatch(revision):
            raise ReleasePairError(f"Release-pair {name} source needs a repository and a full lowercase Git SHA.")
    compatibility = value.get("compatibility", {})
    if not isinstance(compatibility, dict):
        raise ReleasePairError("Release-pair compatibility must be an object.")
    fixture = compatibility.get("capabilityFixture", {})
    if not isinstance(fixture, dict):
        raise ReleasePairError("capabilityFixture compatibility must be an object.")
    required = {
        "databaseSchemaVersion": compatibility.get("databaseSchemaVersion"),
        "rulePackVersion": compatibility.get("rulePackVersion"),
        "compiledInputSchemaVersion": fixture.get("compiledInputSchemaVersion"),
        "compilerVersion": fixture.get("compilerVersion"),
        "timelineCalculationVersion": fixture.get("timelineCalculationVersion"),
        "reportSchemaVersion": fixture.get("reportSchemaVersion"),
        "reportCalculationVersion": fixture.get("reportCalculationVersion"),
        "readinessSchemaVersion": fixture.get("readinessSchemaVersion"),
        "readinessCalculationVersion": fixture.get("readinessCalculationVersion"),
        "runArtifactVersion": fixture.get("runArtifactVersion"),
    }
    missing = [key for key, item in required.items() if item is None or item == ""]
    if missing:
        raise ReleasePairError(f"Release-pair compatibility record is incomplete: {', '.join(missing)}.")
    if not isinstance(compatibility["databaseSchemaVersion"], int) or compatibility["databaseSchemaVersion"] < 1:
        raise ReleasePairError("databaseSchemaVersion must be a positive integer.")
    if not isinstance(fixture["compiledInputSchemaVersion"], int) or fixture["compiledInputSchemaVersion"] < 1:
        raise ReleasePairError("compiledInputSchemaVersion must be a positive integer.")
    qualification = value.get("qualification", {})
    if not isinstance(qualification, dict):
        raise ReleasePairError("Release-pair qualification must be an object.")
    if qualification.get("status") not in ("candidate", "qualified"):
        raise ReleasePairError("qualification.status must be candidate or qualified.")
    contract = {key: value[key] for key in ("formatVersion", "releaseId", "sources", "compatibility")}
    contract_bytes = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    contract_sha = hashlib.sha256(contract_bytes).hexdigest()
    if qualification.get("status") == "qualified":
        evidence = qualification.get("evidence")
        if not evidence:
            raise ReleasePairError("A qualified release pair must reference a successful capability-gate receipt.")
        evidence_path = Path(evidence)
        if not evidence_path.is_absolute():
            evidence_path = path.parent / evidence_path
        try:
            receipt = json.loads(evidence_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ReleasePairError(f"Cannot read the referenced release qualification receipt {evidence_path}: {error}") from error
        if receipt.get("status") != "runtime-qualified" or receipt.get("releasePairSha256") != contract_sha:
            raise ReleasePairError("Referenced capability-gate receipt does not qualify this exact release-pair contract.")
        if receipt.get("sources") != {name: source["revision"] for name, source in sources.items()}:
            raise ReleasePairError("Referenced capability-gate receipt has a different UI/API source pair.")
        if receipt.get("compatibility") != compatibility or receipt.get("databaseSchemaVersion") != compatibility["databaseSchemaVersion"]:
            raise ReleasePairError("Referenced capability-gate receipt has a different compatibility record.")
        if receipt.get("cleanup") != "removed" or not COMMIT.fullmatch(str(receipt.get("bundleRevision", ""))):
            raise ReleasePairError("Referenced capability-gate receipt lacks a clean bundle revision or completed cleanup.")
        images = receipt.get("deployedImages", {})
        if not isinstance(images, dict):
            raise ReleasePairError("Referenced capability-gate receipt has an invalid deployedImages record.")
        if any(not images.get(service, {}).get("imageId") for service in ("migrate", "api", "ui")):
            raise ReleasePairError("Referenced capability-gate receipt lacks a deployed image ID for every service.")
        completed = receipt.get("completedPhases", [])
        required_phases = {"fresh-build", "deployed-source-pair", "api-proxy-and-rule-catalog",
                           "fabricated-household-compile-run-reopen-compare", "migration-version-failure-recovery",
                           "api-offline-and-recovery", "complete"}
        if not required_phases.issubset(set(completed)):
            raise ReleasePairError("Referenced capability-gate receipt is missing required fresh-build or runtime phases.")
    return value, contract_sha


def validate_source_revisions(pair: dict, go_revision: str, ui_revision: str, *, go_dirty: bool, ui_dirty: bool) -> None:
    expected = pair["sources"]
    actual = {"go": (go_revision, go_dirty), "ui": (ui_revision, ui_dirty)}
    for name, (revision, dirty) in actual.items():
        wanted = expected[name]["revision"]
        if dirty:
            raise ReleasePairError(f"{name.upper()} source is dirty; release pair requires a clean checkout at {wanted}.")
        if revision != wanted:
            raise ReleasePairError(f"{name.upper()} source revision mismatch: expected {wanted}, received {revision}.")
