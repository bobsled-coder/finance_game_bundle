#!/usr/bin/env python3
"""Record and report the source identity for the bundle Compose deployment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

if __package__:
    from . import release_pair
else:
    import release_pair


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / ".release-deployments"
BACKUP_SHA = re.compile(r"^[0-9a-f]{64}$")


def run(command: list[str], *, env: dict[str, str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    if check and result.returncode:
        raise release_pair.ReleasePairError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n{(result.stdout or '')[-6000:]}"
        )
    return result


def source_revision(value: str | None, name: str, pair: dict) -> tuple[str, dict[str, object]]:
    expected = pair["sources"][name]
    if not value:
        return expected["revision"], {"repository": expected["repository"], "revision": expected["revision"], "mode": "pinned"}
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if path.is_dir():
        path = path.resolve()
        if not (path / ".git").exists() and not (path / "HEAD").is_file():
            raise release_pair.ReleasePairError(f"{name.upper()} override is not a Git checkout: {path}")
        revision = run(["git", "-C", str(path), "rev-parse", "HEAD"], env=dict(os.environ), capture=True).stdout.strip()
        dirty_text = run(["git", "-C", str(path), "status", "--porcelain"], env=dict(os.environ), capture=True).stdout.strip()
        return revision, {"repository": expected["repository"], "path": str(path), "revision": revision,
                          "mode": "local-override", "dirty": bool(dirty_text)}
    text = value.strip()
    if text == f"{expected['repository']}#{expected['revision']}":
        return expected["revision"], {"repository": expected["repository"], "revision": expected["revision"], "mode": "pinned"}
    raise release_pair.ReleasePairError(f"{name.upper()} override must be a local Git checkout; remote contexts must use the manifest's pinned source pair.")


def deployed_schema_version(go_source: dict[str, object], pair: dict) -> int:
    if go_source["mode"] != "local-override":
        return pair["compatibility"]["databaseSchemaVersion"]
    migration_dir = Path(str(go_source["path"])) / "internal" / "migrations" / "sql"
    versions = []
    for path in migration_dir.glob("*.sql"):
        match = re.match(r"^(\d+)_.*\.sql$", path.name)
        if not match:
            raise release_pair.ReleasePairError(f"Invalid Go migration filename: {path.name}")
        version = int(match.group(1))
        if version < 1:
            raise release_pair.ReleasePairError(f"Invalid Go migration version: {path.name}")
        versions.append(version)
    versions.sort()
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise release_pair.ReleasePairError(f"Go source migration versions must be consecutive from 1: {migration_dir}")
    return len(versions)


def read_backup_reference(path_text: str, previous: dict) -> dict:
    path = Path(path_text).expanduser().resolve()
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise release_pair.ReleasePairError(f"Cannot read backup reference {path}: {error}") from error
    if not isinstance(value, dict):
        raise release_pair.ReleasePairError("Backup reference must contain a JSON object.")
    required = ("location", "createdAt", "sha256", "schemaVersion", "releaseId", "releasePairSha256", "sources")
    if any(value.get(key) in (None, "") for key in required):
        raise release_pair.ReleasePairError("Backup reference requires location, createdAt, sha256, schemaVersion, releaseId, releasePairSha256, and source revisions.")
    if not isinstance(value["sha256"], str) or not BACKUP_SHA.fullmatch(value["sha256"]):
        raise release_pair.ReleasePairError("Backup reference sha256 must be 64 lowercase hexadecimal characters.")
    if not isinstance(value["releasePairSha256"], str) or not BACKUP_SHA.fullmatch(value["releasePairSha256"]):
        raise release_pair.ReleasePairError("Backup reference releasePairSha256 must be 64 lowercase hexadecimal characters.")
    try:
        datetime.fromisoformat(str(value["createdAt"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise release_pair.ReleasePairError("Backup reference createdAt must be an ISO-8601 timestamp.") from error
    if value["releaseId"] != previous.get("releaseId"):
        raise release_pair.ReleasePairError("Backup reference releaseId does not match the previous deployment record.")
    if value["schemaVersion"] != previous.get("databaseSchemaVersion"):
        raise release_pair.ReleasePairError("Backup reference schemaVersion does not match the previous deployment record.")
    if value["releasePairSha256"] != previous.get("releasePairSha256"):
        raise release_pair.ReleasePairError("Backup reference releasePairSha256 does not match the previous deployment record.")
    if not isinstance(value["sources"], dict):
        raise release_pair.ReleasePairError("Backup reference sources must identify the previous Go/API and UI revisions.")
    for name in ("go", "ui"):
        source = value["sources"].get(name)
        if not isinstance(source, dict) or not release_pair.COMMIT.fullmatch(str(source.get("revision", ""))):
            raise release_pair.ReleasePairError(f"Backup reference needs a full {name.upper()} source revision.")
        if source["revision"] != previous.get("sources", {}).get(name, {}).get("revision"):
            raise release_pair.ReleasePairError(f"Backup reference {name.upper()} revision does not match the previous deployment record.")
    return {"path": str(path), **value}


def read_previous_release(path_text: str) -> dict:
    path = Path(path_text).expanduser().resolve()
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise release_pair.ReleasePairError(f"Cannot read previous-release reference {path}: {error}") from error
    if not isinstance(value, dict):
        raise release_pair.ReleasePairError("Previous-release reference must contain a JSON object.")
    sources = value.get("sources")
    if (not value.get("releaseId") or not isinstance(value.get("databaseSchemaVersion"), int)
            or value["databaseSchemaVersion"] < 1
            or not isinstance(sources, dict) or not BACKUP_SHA.fullmatch(str(value.get("releasePairSha256", "")))):
        raise release_pair.ReleasePairError("Previous-release reference needs releaseId, databaseSchemaVersion, releasePairSha256, and source revisions.")
    for name in ("go", "ui"):
        source = sources.get(name)
        if not isinstance(source, dict) or not release_pair.COMMIT.fullmatch(str(source.get("revision", ""))):
            raise release_pair.ReleasePairError(f"Previous-release reference needs a full {name.upper()} source revision.")
    return {"path": str(path), **value}


def resolve_config(pair: dict, contract_sha: str, env: dict[str, str]) -> tuple[dict, dict[str, object]]:
    go_revision, go_source = source_revision(env.get("GO_SOURCE"), "go", pair)
    ui_revision, ui_source = source_revision(env.get("UI_SOURCE"), "ui", pair)
    schema_version = deployed_schema_version(go_source, pair)
    env.update({"RELEASE_ID": pair["releaseId"], "RELEASE_PAIR_SHA256": contract_sha,
                "RELEASE_QUALIFICATION": "local-override" if "local-override" in (go_source["mode"], ui_source["mode"]) else pair["qualification"]["status"],
                "GO_SOURCE_REVISION": go_revision, "UI_SOURCE_REVISION": ui_revision})
    result = run(["docker", "compose", "config", "--format", "json"], env=env, capture=True)
    config = json.loads(result.stdout)
    services = config.get("services", {})
    go_context = services.get("api", {}).get("build", {}).get("context")
    migrate_context = services.get("migrate", {}).get("build", {}).get("context")
    if go_context != migrate_context:
        raise release_pair.ReleasePairError("Resolved API and migration services do not use the same Go source context.")
    def context(name: str) -> str:
        override = env.get("GO_SOURCE" if name in ("api", "migrate") else "UI_SOURCE")
        override_path = Path(override).expanduser() if override else None
        if override_path and not override_path.is_absolute():
            override_path = ROOT / override_path
        if override_path and override_path.is_dir():
            return str(override_path.resolve())
        if override:
            return override
        source = pair["sources"]["go" if name in ("api", "migrate") else "ui"]
        return f"{source['repository']}#{source['revision']}"

    expected_contexts = {service: context(service) for service in ("api", "migrate", "ui")}
    for service, expected in expected_contexts.items():
        actual = services.get(service, {}).get("build", {}).get("context")
        if actual != expected:
            raise release_pair.ReleasePairError(f"Resolved {service} build context mismatch: expected {expected!r}, received {actual!r}.")
        expected_revision = go_revision if service in ("api", "migrate") else ui_revision
        label = services.get(service, {}).get("labels", {}).get("org.financegame.source.revision")
        if label != expected_revision:
            raise release_pair.ReleasePairError(f"Resolved {service} source label mismatch: expected {expected_revision}, received {label!r}.")
    identity = {"releaseId": pair["releaseId"], "releasePairSha256": contract_sha,
                "qualification": env["RELEASE_QUALIFICATION"], "goSource": go_source, "uiSource": ui_source,
                "apiAndMigrationContext": go_context, "uiContext": expected_contexts["ui"],
                "databaseSchemaVersion": schema_version}
    return config, identity


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def deployment_up(backup_reference: str, previous_release_reference: str) -> int:
    pair, contract_sha = release_pair.load()
    now = datetime.now(timezone.utc).isoformat()
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    _, identity = resolve_config(pair, contract_sha, env)
    go_source, ui_source = identity["goSource"], identity["uiSource"]
    go_revision, ui_revision = go_source["revision"], ui_source["revision"]
    qualification = str(identity["qualification"])
    if qualification == "qualified" and not pair["qualification"].get("evidence"):
        raise release_pair.ReleasePairError("Qualified release pair has no successful capability-gate evidence reference.")

    compose = ["docker", "compose"]
    resolved = run(compose + ["config", "--format", "json"], env=env, capture=True)
    resolved = json.loads(resolved.stdout)
    project = resolved.get("name") or "finance_game_bundle"
    volume_name = resolved.get("volumes", {}).get("postgres_data", {}).get("name", f"{project}_postgres_data")
    run(["docker", "info"], env=env, capture=True)
    volume_result = run(["docker", "volume", "inspect", volume_name], env=env, check=False, capture=True)
    volume_exists = volume_result.returncode == 0
    if volume_result.returncode and "no such volume" not in (volume_result.stdout or "").lower():
        raise release_pair.ReleasePairError(f"Could not determine whether database volume {volume_name!r} exists: {(volume_result.stdout or '').strip()}")

    active_path = RECORDS / "active.json"
    previous = None
    backup = None
    install_kind = "fresh-install"
    if volume_exists:
        if active_path.is_file():
            previous = json.loads(active_path.read_text())
            if previous_release_reference:
                supplied_previous = read_previous_release(previous_release_reference)
                for key in ("releaseId", "releasePairSha256", "databaseSchemaVersion"):
                    if supplied_previous.get(key) != previous.get(key):
                        raise release_pair.ReleasePairError(f"Previous-release reference {key} does not match the active deployment record.")
        elif previous_release_reference:
            previous = read_previous_release(previous_release_reference)
        else:
            raise release_pair.ReleasePairError(
                f"Persistent database volume {volume_name!r} exists without a prior deployment record. "
                "Supply --previous-release-reference and --backup-reference before migration."
            )
        previous_sources = previous.get("sources", {})
        same_pair = (previous.get("releasePairSha256") == contract_sha
                     and previous_sources.get("go", {}).get("revision") == go_revision
                     and previous_sources.get("ui", {}).get("revision") == ui_revision
                     and previous.get("databaseSchemaVersion") == identity["databaseSchemaVersion"]
                     and not previous_sources.get("go", {}).get("dirty", False)
                     and not previous_sources.get("ui", {}).get("dirty", False)
                     and not go_source.get("dirty", False)
                     and not ui_source.get("dirty", False))
        if same_pair:
            if backup_reference:
                raise release_pair.ReleasePairError("The current release pair is already active; omit --backup-reference for a restart.")
            install_kind = "restart"
        else:
            if not backup_reference:
                raise release_pair.ReleasePairError("A changed release pair with an existing database volume requires --backup-reference before migration.")
            backup = read_backup_reference(backup_reference, previous)
            install_kind = "upgrade"
    elif backup_reference:
        raise release_pair.ReleasePairError("A backup reference was supplied, but this Compose project has no existing database volume.")
    elif previous_release_reference:
        raise release_pair.ReleasePairError("A previous-release reference was supplied, but this Compose project has no existing database volume.")

    attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    attempt_path = RECORDS / f"{attempt_id}.json"
    attempt = {
        "attemptId": attempt_id, "startedAt": now, "state": "starting", "project": project,
        "installKind": install_kind, "releaseId": pair["releaseId"], "qualification": qualification,
        "releasePairSha256": contract_sha, "databaseSchemaVersion": identity["databaseSchemaVersion"],
        "sources": {"go": go_source, "ui": ui_source},
        "compatibility": pair["compatibility"], "resolvedContexts": identity,
        "previousRelease": previous, "backupReference": backup,
    }
    atomic_write(attempt_path, attempt)
    print(f"Starting {qualification} {pair['releaseId']} (pair {contract_sha}).")
    print(f"Go/migration source: {go_revision} ({go_source['mode']}); UI source: {ui_revision} ({ui_source['mode']}).")
    fixture = pair["compatibility"]["capabilityFixture"]
    print(f"Compatibility: database schema {identity['databaseSchemaVersion']} (manifest {pair['compatibility']['databaseSchemaVersion']}), rule pack {pair['compatibility']['rulePackVersion']}, compiler {fixture['compilerVersion']} / input schema {fixture['compiledInputSchemaVersion']}.")
    if install_kind == "upgrade":
        print(f"Upgrade references previous release {previous.get('releaseId')} and backup {backup['location']}.")
    else:
        print("Fresh install; no prior release or database backup is referenced.")
    print(f"Deployment attempt record: {attempt_path}")

    try:
        run(compose + ["up", "--build", "--detach", "--wait"], env=env, capture=True)
        for service in ("migrate", "api", "ui"):
            container_id = run(compose + ["ps", "--all", "-q", service], env=env, capture=True).stdout.strip()
            if not container_id:
                raise release_pair.ReleasePairError(f"Compose did not return a container for {service}.")
            inspected = json.loads(run(["docker", "inspect", container_id], env=env, capture=True).stdout)[0]
            labels = inspected.get("Config", {}).get("Labels", {}) or {}
            expected_revision = go_revision if service in ("api", "migrate") else ui_revision
            if labels.get("org.financegame.source.revision") != expected_revision:
                raise release_pair.ReleasePairError(
                    f"Container {service} source label mismatch: expected {expected_revision}, "
                    f"received {labels.get('org.financegame.source.revision')!r}."
                )
            attempt.setdefault("containers", {})[service] = {
                "id": container_id, "imageId": inspected.get("Image"), "labels": labels,
            }
        if attempt["containers"]["api"]["imageId"] is None or attempt["containers"]["migrate"]["imageId"] is None:
            raise release_pair.ReleasePairError("API and migration image identities could not be recorded.")
        attempt["state"] = "started"
        attempt["completedAt"] = datetime.now(timezone.utc).isoformat()
        atomic_write(attempt_path, attempt)
        atomic_write(active_path, {
            "releaseId": pair["releaseId"], "releasePairSha256": contract_sha,
            "qualification": qualification, "databaseSchemaVersion": identity["databaseSchemaVersion"],
            "sources": {"go": go_source, "ui": ui_source}, "containers": attempt["containers"],
            "deploymentRecord": str(attempt_path), "startedAt": attempt["completedAt"],
        })
        print("Started containers report matching source revisions; API and migration use the same Go SHA.")
        print(f"Deployment record: {attempt_path}")
        return 0
    except Exception as error:
        observed = {}
        for service in ("migrate", "api", "ui"):
            container_id = run(compose + ["ps", "--all", "-q", service], env=env, check=False, capture=True).stdout.strip()
            if not container_id:
                continue
            inspected_result = run(["docker", "inspect", container_id], env=env, check=False, capture=True)
            if not inspected_result.returncode:
                inspected = json.loads(inspected_result.stdout)[0]
                observed[service] = {"containerId": container_id, "imageId": inspected.get("Image"),
                                     "labels": inspected.get("Config", {}).get("Labels", {}) or {}}
        if observed:
            attempt["observedContainersOnFailure"] = observed
        attempt["state"] = "failed"
        attempt["failedAt"] = datetime.now(timezone.utc).isoformat()
        attempt["error"] = str(error)
        atomic_write(attempt_path, attempt)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "up"))
    parser.add_argument("--backup-reference", default="", help="JSON backup record required when the project database volume already exists")
    parser.add_argument("--previous-release-reference", default="", help="JSON record for the release currently using an existing volume")
    args = parser.parse_args()
    try:
        if args.command == "check":
            pair, contract_sha = release_pair.load()
            env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
            _, identity = resolve_config(pair, contract_sha, env)
            print(json.dumps(identity, indent=2))
            return 0
        return deployment_up(args.backup_reference, args.previous_release_reference)
    except (release_pair.ReleasePairError, OSError, json.JSONDecodeError) as error:
        print(f"Release deployment failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
