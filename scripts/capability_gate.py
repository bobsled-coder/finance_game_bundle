#!/usr/bin/env python3
"""Run the disposable real-stack household planning capability gate."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
RULE_PACK = "canadian-planning-rules-test-v1"
TIMEOUT = 90
IMAGE_LABEL = "io.bobsled.capability-project"


class GateError(RuntimeError):
    pass


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def der_value(data: bytes, offset: int, expected_tag: int) -> tuple[bytes, int]:
    if offset >= len(data) or data[offset] != expected_tag or offset + 1 >= len(data):
        raise GateError("Could not parse the generated RSA public key.")
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or start + count > len(data):
            raise GateError("Could not parse the generated RSA public key length.")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise GateError("Generated RSA public key is truncated.")
    return data[start:end], end


def rsa_jwk(public_der: bytes) -> dict[str, str]:
    subject_public_key_info, _ = der_value(public_der, 0, 0x30)
    _, offset = der_value(subject_public_key_info, 0, 0x30)  # AlgorithmIdentifier
    bit_string, _ = der_value(subject_public_key_info, offset, 0x03)
    if not bit_string or bit_string[0] != 0:
        raise GateError("Generated RSA public key has an invalid BIT STRING.")
    rsa_sequence, _ = der_value(bit_string, 1, 0x30)
    modulus, offset = der_value(rsa_sequence, 0, 0x02)
    exponent, _ = der_value(rsa_sequence, offset, 0x02)
    modulus = modulus.lstrip(b"\x00")
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "capability", "n": b64url(modulus), "e": b64url(exponent)}


def run(command: list[str], *, check: bool = True, env: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None, env=env)
    if check and result.returncode:
        output = result.stdout or ""
        raise GateError(f"Command failed ({result.returncode}): {' '.join(command)}\n{output[-6000:]}")
    return result


def run_compose_build(compose: list[str], env: dict[str, str], evidence: Path) -> None:
    command = compose + ["up", "--build", "--detach", "--wait"]
    result = run(command, check=False, env=env, capture=True)
    if result.returncode:
        output = result.stdout or ""
        log_path = evidence / "compose-build-failure.log"
        log_path.write_text(output[-200_000:])
        raise GateError(
            f"Compose build or startup failed ({result.returncode}); see {log_path}.\n"
            + output[-6000:]
        )


def source_info(path_text: str, label: str) -> tuple[Path, dict[str, object]]:
    path = Path(path_text).expanduser().resolve()
    if not path.is_dir():
        raise GateError(f"{label} source must be an explicit existing local Git checkout: {path}")
    if label == "Go" and not (path / "go.mod").is_file():
        raise GateError(f"Go source does not contain go.mod: {path}")
    if label == "UI":
        try:
            package = json.loads((path / "package.json").read_text())
        except Exception as error:
            raise GateError(f"UI source does not contain a readable package.json: {path}: {error}") from error
        if package.get("name") != "finance-game-ui":
            raise GateError(f"UI source package name is {package.get('name')!r}; expected 'finance-game-ui'.")
    revision = run(["git", "-C", str(path), "rev-parse", "HEAD"], capture=True).stdout.strip()
    dirty = run(["git", "-C", str(path), "status", "--porcelain"], capture=True).stdout.strip()
    if dirty:
        raise GateError(f"{label} source is dirty; use a clean committed checkout.\n{dirty}")
    return path, {"path": str(path), "revision": revision, "dirty": False}


def candidate_port() -> int:
    # Select from the dynamic/private range without opening a host socket. The
    # Compose bind is the authoritative collision check and reports the port.
    return 49152 + secrets.randbelow(16384)


def wait_http(url: str, *, expected: int = 200, headers: dict[str, str] | None = None, timeout: int = TIMEOUT) -> tuple[int, bytes, dict[str, str]]:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                status, body, response_headers = response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            status, body, response_headers = error.code, error.read(), dict(error.headers)
        except Exception as error:  # connection races while services start
            last = str(error)
            time.sleep(1)
            continue
        if status == expected:
            return status, body, response_headers
        last = f"HTTP {status}: {body[:500].decode(errors='replace')}"
        time.sleep(1)
    raise GateError(f"Timed out waiting for {url} to return HTTP {expected}; last response: {last}")


def request_json(base: str, path: str, token: str, *, body: dict | None = None, expected: int = 200) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, content = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, content = error.code, error.read()
    try:
        value = json.loads(content)
    except Exception:
        value = {"raw": content[:500].decode(errors="replace")}
    if status != expected:
        raise GateError(f"{request.method} {path} returned HTTP {status}, expected {expected}: {json.dumps(value)[:1200]}")
    return value


def assert_run_artifact(run: dict, run_id: str, compilation_hash: str, result_hash: str) -> None:
    if run.get("id") != run_id or run.get("status") != "completed":
        raise GateError(f"Reopened Scenario Run identity or status changed: {run}")
    if run.get("compilationHash") != compilation_hash or run.get("resultHash") != result_hash:
        raise GateError(
            "Scenario Run frozen input or result hash changed after reopen: "
            f"compilationHash={run.get('compilationHash')!r}, resultHash={run.get('resultHash')!r}"
        )


def assert_fixture_report(report: dict) -> None:
    summary = report.get("summary", {})
    actual = {
        "periodCount": summary.get("periodCount"),
        "openingNetWorth": summary.get("openingNetWorth"),
        "closingNetWorth": summary.get("closingNetWorth"),
    }
    expected = {"periodCount": 12, "openingNetWorth": "100000.10", "closingNetWorth": "100000.10"}
    if actual != expected:
        raise GateError(f"Scenario Run report does not match the fabricated fixture: expected {expected}, got {actual}.")


def make_token(key_path: Path, issuer: str, audience: str) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "capability"}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({"iss": issuer, "sub": "capability-runner", "aud": audience, "iat": now, "exp": now + 3600}, separators=(",", ":")).encode())
    message = f"{header}.{claims}".encode()
    signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_path)], input=message, stdout=subprocess.PIPE, check=True).stdout
    return f"{message.decode()}.{b64url(signature)}"


def inspect_config(compose: list[str], env: dict[str, str]) -> None:
    result = run(compose + ["config", "--format", "json"], capture=True, env=env)
    config = json.loads(result.stdout)
    project = env["CAPABILITY_PROJECT"]
    if config.get("name") != project:
        raise GateError(
            f"Compose resolved project name {config.get('name')!r}; expected isolated project {project!r}."
        )
    for name, volume in config.get("volumes", {}).items():
        if volume.get("external") or volume.get("name") not in (None, f"{project}_{name}"):
            raise GateError(f"Refusing non-project-scoped or external volume {name!r}.")
    for name, network in config.get("networks", {}).items():
        if network.get("external") or network.get("name") not in (None, f"{project}_{name}"):
            raise GateError(f"Refusing non-project-scoped or external network {name!r}.")
    for service_name, service in config.get("services", {}).items():
        if service.get("container_name"):
            raise GateError(f"Refusing fixed container name in {service_name}.")
        for mount in service.get("volumes", []):
            source = Path(mount.get("source", "")).resolve()
            temp_root = Path(env["CAPABILITY_TEMP_ROOT"]).resolve()
            allowed_targets = {"/fixture", "/capability-ca.crt"}
            unexpected_bind = mount.get("type") == "bind" and (source != temp_root and temp_root not in source.parents or mount.get("target") not in allowed_targets)
            if unexpected_bind:
                raise GateError(f"Refusing unexpected host bind mount in {service_name}: {mount.get('source')} -> {mount.get('target')}")
        if service_name in ("db", "api") and service.get("ports"):
            raise GateError(f"{service_name} must remain private; resolved Compose config publishes a port.")
    ui_ports = config["services"]["ui"].get("ports", [])
    if len(ui_ports) != 1 or ui_ports[0].get("host_ip") != "127.0.0.1":
        raise GateError("The only published port must bind the UI to loopback.")


def cleanup_project(compose: list[str], env: dict[str, str], project: str, evidence: Path) -> None:
    containers = run(compose + ["ps", "-aq"], env=env, capture=True).stdout.strip().split()
    resources = [("container", item, ["docker", "inspect", "-f", "{{ index .Config.Labels \"com.docker.compose.project\" }}", item]) for item in containers]
    volumes = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip().split()
    resources.extend(("volume", item, ["docker", "volume", "inspect", "-f", "{{ index .Labels \"com.docker.compose.project\" }}", item]) for item in volumes)
    networks = run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip().split()
    resources.extend(("network", item, ["docker", "network", "inspect", "-f", "{{ index .Labels \"com.docker.compose.project\" }}", item]) for item in networks)
    images = capability_images(project)
    present_images: list[str] = []
    for item in images:
        inspected = run(["docker", "image", "inspect", "-f", f"{{{{ index .Config.Labels \"{IMAGE_LABEL}\" }}}}", item], check=False, capture=True)
        if inspected.returncode == 0:
            present_images.append(item)
            if inspected.stdout.strip() != project:
                raise GateError(f"Refusing cleanup: image {item} has unexpected capability project label {inspected.stdout.strip()!r}.")
    for kind, item, inspect in resources:
        label = run(inspect, capture=True).stdout.strip()
        if label != project:
            raise GateError(f"Refusing cleanup: {kind} {item} has unexpected Compose project label {label!r}.")
    result = run(compose + ["down", "--volumes", "--remove-orphans"], check=False, env=env, capture=True)
    (evidence / "cleanup.log").write_text(result.stdout or "")
    if result.returncode:
        raise GateError(f"Compose cleanup failed ({result.returncode}); see {evidence / 'cleanup.log'}.")
    for item in present_images:
        run(["docker", "image", "rm", item], capture=True)
    leftovers = {
        "containers": run(compose + ["ps", "-aq"], env=env, capture=True).stdout.strip(),
        "volumes": run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip(),
        "networks": run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip(),
        "images": [item for item in images if run(["docker", "image", "inspect", item], check=False, capture=True).returncode == 0],
    }
    if any(leftovers.values()):
        raise GateError(f"Project cleanup left resources behind: {leftovers}.")


def cleanup_temp_directory(temp: Path) -> None:
    fixture = temp / "fixture"
    if fixture.exists():
        for child in fixture.iterdir():
            if child.is_dir():
                raise GateError(f"Refusing to remove unexpected nested fixture directory {child}.")
            child.unlink()
        fixture.rmdir()
    for child in temp.iterdir():
        if child.is_dir():
            raise GateError(f"Refusing to remove unexpected nested fixture directory {child}.")
        child.unlink()
    temp.rmdir()


def capability_images(project: str) -> list[str]:
    return [f"finance-game-capability/{project}-{service}:local" for service in ("migrate", "api", "ui")]


def write_override(temp: Path, issuer: str, project: str) -> Path:
    migrate_image, api_image, ui_image = capability_images(project)
    build_labels = {IMAGE_LABEL: project}
    override = {
        "services": {
            "migrate": {
                "image": migrate_image,
                "build": {"labels": build_labels},
            },
            "api": {
                "image": api_image,
                "build": {"target": "capability-api", "labels": build_labels},
                "environment": {
                    "OIDC_ISSUER": issuer,
                    "OIDC_AUDIENCE": "finance-game-capability",
                    "OIDC_JWKS_URL": issuer + ".well-known/jwks.json",
                    "SSL_CERT_FILE": "/capability-ca.crt",
                },
                "volumes": [f"{temp / 'fixture' / 'tls.crt'}:/capability-ca.crt:ro"],
                "depends_on": {"oidc-fixture": {"condition": "service_healthy"}},
            },
            "ui": {
                "image": ui_image,
                "build": {"labels": build_labels},
            },
            "oidc-fixture": {
                "image": "python:3.13.7-alpine3.22",
                "command": ["python3", "/fixture/capability_oidc.py"],
                "volumes": [f"{temp / 'fixture'}:/fixture:ro"],
                "networks": {"backend": {"aliases": ["oidc"]}},
                "healthcheck": {
                    "test": ["CMD", "python3", "-c", "import ssl,urllib.request; urllib.request.urlopen('https://127.0.0.1:8443/.well-known/jwks.json',context=ssl._create_unverified_context(),timeout=2).read()"],
                    "interval": "1s",
                    "timeout": "3s",
                    "retries": 15,
                    "start_period": "2s",
                },
                "restart": "no",
            },
        }
    }
    path = temp / "compose.capability.yaml"
    path.write_text(json.dumps(override, indent=2) + "\n")
    return path


def compose_command(temp: Path, override: Path, project: str) -> list[str]:
    empty_env = temp / "empty.env"
    empty_env.write_text("# Isolated capability run; no repository .env is loaded.\n")
    return ["docker", "compose", "--env-file", str(empty_env), "--project-directory", str(ROOT), "-p", project, "-f", str(ROOT / "compose.yaml"), "-f", str(override)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-source", required=True)
    parser.add_argument("--ui-source", required=True)
    parser.add_argument("--keep", action="store_true", help="retain the project and evidence for inspection")
    parser.add_argument("--evidence-dir", type=Path, help="directory for manifest, logs and checks")
    args = parser.parse_args()

    go_path, go_info = source_info(args.go_source, "Go")
    ui_path, ui_info = source_info(args.ui_source, "UI")
    if not shutil.which("docker") or not shutil.which("openssl"):
        raise GateError("Docker Compose and openssl are required.")
    daemon = run(["docker", "info"], check=False, capture=True)
    if daemon.returncode:
        raise GateError(
            "Docker daemon is unavailable or the current user cannot access it. "
            "Start Docker and check access to its socket.\n"
            + (daemon.stdout or "")[-2000:]
        )
    compose_version = run(["docker", "compose", "version"], check=False, capture=True)
    if compose_version.returncode:
        raise GateError(
            "Docker Compose is unavailable; install the Docker Compose plugin.\n"
            + (compose_version.stdout or "")[-2000:]
        )
    project = "fgcap-" + uuid.uuid4().hex[:12]
    port = candidate_port()
    temp = Path(tempfile.mkdtemp(prefix=project + "-"))
    evidence = args.evidence_dir.resolve() if args.evidence_dir else ROOT / "outputs" / "capability-gates" / project
    evidence.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    env.update({"GO_SOURCE": str(go_path), "UI_SOURCE": str(ui_path), "UI_PORT": str(port), "BIND_ADDRESS": "127.0.0.1", "POSTGRES_PASSWORD": secrets.token_urlsafe(32), "CAPABILITY_TEMP_ROOT": str(temp), "CAPABILITY_PROJECT": project})
    try:
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(temp / "tls.key"), "-out", str(temp / "tls.crt"), "-subj", "/CN=oidc", "-addext", "subjectAltName=DNS:oidc", "-days", "1"], capture=True)
        (temp / "tls.crt").chmod(0o644)
        run(["openssl", "genrsa", "-out", str(temp / "signing.key"), "2048"], capture=True)
        run(["openssl", "rsa", "-in", str(temp / "signing.key"), "-pubout", "-outform", "DER", "-out", str(temp / "signing.pub.der")], capture=True)
        fixture_dir = temp / "fixture"
        fixture_dir.mkdir(mode=0o755)
        shutil.copy2(temp / "tls.crt", fixture_dir / "tls.crt")
        shutil.copy2(temp / "tls.key", fixture_dir / "tls.key")
        (fixture_dir / "tls.key").chmod(0o600)
        shutil.copy2(ROOT / "scripts" / "capability_oidc.py", fixture_dir / "capability_oidc.py")
        (fixture_dir / "jwks.json").write_text(json.dumps({"keys": [rsa_jwk((temp / "signing.pub.der").read_bytes())]}, separators=(",", ":")))
        issuer = "https://oidc:8443/"
        override = write_override(temp, issuer, project)
        compose = compose_command(temp, override, project)
    except BaseException:
        cleanup_temp_directory(temp)
        raise
    manifest: dict[str, object] = {"project": project, "uiPort": port, "goSource": go_info, "uiSource": ui_info, "rulePackVersion": RULE_PACK, "fixtureOnly": True, "evidenceDirectory": str(evidence), "phases": []}
    created = False
    cleanup_attempted = False
    project_cleaned = False
    temp_cleaned = False
    logs: list[str] = []

    def phase(name: str, status: str, **details: object) -> None:
        manifest["phases"].append({"name": name, "status": status, **details})
        (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def collect_logs() -> None:
        result = run(compose + ["logs", "--no-color", "--tail", "500"], check=False, env=env, capture=True)
        logs.append(result.stdout or "")
        (evidence / "compose.log").write_text("\n".join(logs)[-200_000:])

    try:
        def interrupt(signum: int, _frame: object) -> None:
            raise GateError(f"interrupted by signal {signum}; removing only project {project}")

        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGTERM, interrupt)
        inspect_config(compose, env)
        existing_result = run(compose + ["ps", "-aq"], check=False, env=env, capture=True)
        if existing_result.returncode:
            raise GateError(
                f"Could not verify that generated Compose project {project} is isolated:\n"
                + (existing_result.stdout or "")[-2000:]
            )
        existing = existing_result.stdout.strip()
        if existing:
            raise GateError(f"Generated project name unexpectedly already owns containers: {project}")
        volumes = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip()
        if volumes:
            raise GateError(f"Generated project name unexpectedly already owns volumes: {project}")
        networks = run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip()
        if networks:
            raise GateError(f"Generated project name unexpectedly already owns networks: {project}")
        existing_images = set(run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture=True).stdout.splitlines())
        collisions = sorted(set(capability_images(project)) & existing_images)
        if collisions:
            raise GateError(f"Generated project name unexpectedly already owns capability images: {collisions}")
        created = True
        run(compose + ["up", "--build", "--detach", "--wait", "db"], env=env)
        # Deliberately wrong credentials prove failed migration is visible and recoverable.
        failed = run(compose + ["run", "--rm", "--no-deps", "-e", "PGPASSWORD=capability-intentionally-wrong", "migrate"], check=False, env=env, capture=True)
        (evidence / "migration-failure.log").write_text((failed.stdout or "")[-20_000:])
        if failed.returncode == 0:
            raise GateError("Migration failure rehearsal unexpectedly succeeded with intentionally wrong credentials.")
        if "password authentication failed" not in (failed.stdout or "").lower():
            raise GateError(f"Migration failure did not report the expected database credential error:\n{(failed.stdout or '')[-1200:]}")
        phase("migration-connection-failure", "passed", exitCode=failed.returncode)

        run_compose_build(compose, env, evidence)
        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/health/live")
        _, home_body, _ = wait_http(base + "/households")
        _, deep_link_body, _ = wait_http(base + "/households/not-a-real-household/scenarios/not-a-real-scenario")
        if b'<div id="root"></div>' not in home_body or deep_link_body != home_body:
            raise GateError("The UI did not serve the same SPA shell for the home and direct deep-link paths.")
        phase("spa-and-deep-links", "passed", baseUrl=base)
        token = make_token(temp / "signing.key", issuer, "finance-game-capability")
        headers = {"Authorization": f"Bearer {token}"}
        catalog = request_json(base, "/api/v1/reference/canadian-rule-packs", token)
        packs = catalog.get("rulePacks", [])
        if not any(item.get("version") == RULE_PACK for item in packs):
            raise GateError(f"The explicitly selected rule pack {RULE_PACK} is absent from the API catalog: {json.dumps(catalog)[:1200]}")
        # A real Nginx-proxied API business endpoint proves proxy routing and auth.
        request_json(base, "/api/v1/reference/canadian-rule-packs", token)
        phase("api-proxy-and-rule-catalog", "passed", selectedRulePack=RULE_PACK)

        person = request_json(base, "/api/v1/people", token, body={"name": "Capability Fixture", "birthDate": "1980-06", "region": "ON", "preferredCurrency": "CAD", "notes": "Disposable SLED-86 fixture"}, expected=201)
        household = request_json(base, "/api/v1/households", token, body={"name": "Capability Fixture Household", "personIds": [person["id"]], "notes": "Disposable SLED-86 fixture"}, expected=201)
        household_id, person_id = household["id"], person["id"]
        account = request_json(base, "/api/v1/accounts", token, body={"householdId": household_id, "displayName": "Fixture Chequing", "accountType": "Chequing", "defaultCurrency": "CAD", "ownerships": [{"personId": person_id, "share": "1"}]}, expected=201)
        account_id = account.get("id", account.get("account", {}).get("id"))
        if not account_id:
            raise GateError(f"Account response did not contain its ID: {account}")
        ownerships = account.get("ownerships") or []
        if len(ownerships) != 1 or ownerships[0].get("share") != "1":
            raise GateError(f"Account ownership share was not preserved as exact decimal text: {ownerships}")
        balance = request_json(base, f"/api/v1/accounts/{account_id}/balances", token, body={"effectiveDate": "2026-01-01", "expectedRevision": 0, "amount": "100000.10", "sourceKind": "user_reported", "sourceLabel": "Disposable capability fixture"}, expected=201)
        if balance.get("amount") != "100000.10":
            raise GateError(f"Account balance did not round-trip as the exact decimal string: {balance}")
        snapshot = request_json(base, f"/api/v1/households/{household_id}/snapshots", token, body={"name": "Disposable 2026 baseline", "asOfDate": "2026-01-01", "notes": "SLED-86 fabricated fixture", "selections": [{"kind": "account_balance", "recordId": account_id, "effectiveDate": "2026-01-01", "revision": 1}]}, expected=201)
        snapshot_id = snapshot.get("draft", {}).get("id")
        snapshot_version = snapshot.get("draft", {}).get("version")
        if not snapshot_id or not snapshot_version:
            raise GateError(f"Snapshot draft response did not contain id/version: {snapshot}")
        included = snapshot.get("included", [])
        if len(included) != 1 or included[0].get("amount") != "100000.10":
            raise GateError(f"Snapshot did not preserve the exact fixture amount string: {included}")
        finalized = request_json(base, f"/api/v1/households/{household_id}/snapshots/{snapshot_id}/finalize", token, body={"expectedVersion": snapshot_version}, expected=200)
        finalized_hash = finalized.get("finalized", {}).get("contentHash")
        if not finalized_hash:
            raise GateError(f"Finalized Snapshot response has no content hash: {finalized}")
        scenario = request_json(base, f"/api/v1/households/{household_id}/scenarios", token, body={"name": "Disposable 2026 plan", "baselineSnapshotId": snapshot_id, "baselineSnapshotVersion": finalized.get("draft", {}).get("version", snapshot_version + 1), "startDate": "2026-01-01", "endDate": "2026-12-31", "annualInflationRate": "0", "annualNominalReturnRate": "0", "growthOverrides": []}, expected=201)
        scenario_id = scenario.get("id", scenario.get("scenario", {}).get("id"))
        compiled = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs", token, body={"rulePackVersion": RULE_PACK}, expected=201)
        compilation_version = compiled.get("version", compiled.get("compilation", {}).get("version"))
        compilation_hash = compiled.get("contentHash", compiled.get("compilation", {}).get("contentHash"))
        if not compilation_version or not compilation_hash:
            raise GateError(f"Compilation response missing version/content hash: {compiled}")
        missing_pack = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs", token, body={"rulePackVersion": "canadian-planning-rules-v99"}, expected=404)
        missing_code = missing_pack.get("error", {}).get("code")
        if missing_code != "rule_pack_not_found":
            raise GateError(f"Missing pack response should be actionable rule_pack_not_found, received: {missing_pack}")
        run_response = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs/{compilation_version}/runs", token, body={}, expected=201)
        run_id = run_response.get("id", run_response.get("run", {}).get("id"))
        if run_response.get("status") != "completed":
            raise GateError(f"The fixture Run did not complete successfully: {run_response}")
        result_hash = run_response.get("resultHash")
        if not result_hash or run_response.get("compilationHash") != compilation_hash:
            raise GateError(f"The fixture Run did not retain its compiled input and result hashes: {run_response}")
        report = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}/results", token)
        assert_fixture_report(report)
        reopened = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
        assert_run_artifact(reopened, run_id, compilation_hash, result_hash)
        comparison = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/comparisons", token, body={"rulePackVersion": RULE_PACK, "baseCashflowBindings": [], "alternativeCashflowBindings": [], "baseMaturityConversions": [], "alternativeMaturityConversions": [], "baseLiabilityRenewalRates": [], "alternativeLiabilityRenewalRates": [], "alternative": {"name": "Unchanged fixture comparison", "annualInflationRate": "0", "annualNominalReturnRate": "0", "growthOverrides": []}}, expected=201)
        comparison_id = comparison.get("comparisonId")
        if not comparison_id:
            raise GateError(f"Comparison response missing immutable comparison ID: {comparison}")
        comparison_history = request_json(base, f"/api/v1/households/{household_id}/comparisons", token)
        comparison_history_ids = [item.get("comparisonId") for item in comparison_history.get("comparisons", [])]
        if comparison_id not in comparison_history_ids:
            raise GateError(f"Household comparison history did not include saved comparison {comparison_id}: {comparison_history}")
        reopened_comparison = request_json(base, f"/api/v1/households/{household_id}/comparisons/{comparison_id}", token)
        if reopened_comparison.get("comparisonId") != comparison_id:
            raise GateError("Reopened comparison ID does not match the saved immutable comparison.")
        comparison_alternative = comparison.get("alternativeScenario", {}).get("id")
        if not comparison_alternative:
            raise GateError(f"Comparison response missing alternative Scenario: {comparison}")
        comparison_alternative_run = comparison.get("alternativeRunId")
        if not comparison_alternative_run:
            raise GateError(f"Comparison response missing alternative Run: {comparison}")
        alternative = comparison.get("alternativeScenario", {})
        if alternative.get("baselineSnapshotId") != snapshot_id or comparison.get("comparison", {}).get("baseScenarioId") != scenario_id:
            raise GateError("Comparison did not preserve the expected base Scenario and finalized Snapshot references.")
        deltas = [comparison.get("comparison", {}).get("summary", {}).get(key, {}).get("delta") for key in ("openingNetWorth", "closingNetWorth", "netWorthChange")]
        if any(delta != "0.00" for delta in deltas):
            raise GateError(f"An unchanged comparison should have exact zero summary deltas, received {deltas}.")
        if comparison.get("comparison", {}).get("baseRunId") != run_id:
            raise GateError("Comparison did not reuse the recorded base Run.")
        # Run identity includes the household's Goal/Guardrail inputs. Add a
        # fabricated Goal so this same compilation produces a distinct Run.
        goal = request_json(base, f"/api/v1/households/{household_id}/goals", token, body={"displayName": "Disposable history fixture goal", "goalType": "emergency_fund", "priority": "must", "targetAmount": "25000.00", "targetDate": "2026-12-31", "measureKey": "period_end_household_net_worth", "definitionVersion": "household-net-worth-goals-v1", "effectiveStart": "2026-01-01", "effectiveEnd": "2026-12-31"}, expected=201)
        second_run_response = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs/{compilation_version}/runs", token, body={}, expected=201)
        second_run_id = second_run_response.get("id", second_run_response.get("run", {}).get("id"))
        if not second_run_id or second_run_id == run_id or second_run_response.get("status") != "completed":
            raise GateError(f"A second immutable Run was not created for history pagination: {second_run_response}")
        household_history_ids = []
        history_cursor = ""
        household_history_page_count = 0
        for _ in range(10):
            query = f"/api/v1/households/{household_id}/runs?limit=1"
            if history_cursor:
                query += f"&cursor={history_cursor}"
            history_page = request_json(base, query, token)
            page_runs = history_page.get("runs", [])
            if len(page_runs) != 1:
                raise GateError(f"Household Run history page should contain exactly one result: {history_page}")
            household_history_page_count += 1
            household_history_ids.extend(item.get("id") for item in page_runs)
            history_cursor = history_page.get("nextCursor") or ""
            if not history_cursor:
                break
        expected_household_run_ids = {run_id, second_run_id, comparison_alternative_run}
        if history_cursor or household_history_page_count < 3 or len(household_history_ids) != len(expected_household_run_ids) or set(household_history_ids) != expected_household_run_ids:
            raise GateError(f"Household Run history continuation did not recover all expected immutable Runs: {household_history_ids}")
        scenario_history_first = request_json(base, f"/api/v1/households/{household_id}/runs?scenarioId={scenario_id}&limit=1", token)
        scenario_history_first_ids = [item.get("id") for item in scenario_history_first.get("runs", [])]
        scenario_cursor = scenario_history_first.get("nextCursor")
        if len(scenario_history_first_ids) != 1 or not scenario_cursor:
            raise GateError(f"Scenario Run history did not return one result and a continuation cursor: {scenario_history_first}")
        scenario_history_next = request_json(base, f"/api/v1/households/{household_id}/runs?scenarioId={scenario_id}&limit=1&cursor={scenario_cursor}", token)
        scenario_history_ids_list = [*scenario_history_first_ids, *(item.get("id") for item in scenario_history_next.get("runs", []))]
        scenario_history_ids = set(scenario_history_ids_list)
        if len(scenario_history_next.get("runs", [])) != 1 or scenario_history_next.get("nextCursor") or scenario_history_ids != {run_id, second_run_id}:
            raise GateError(f"Scenario-filtered Run history continuation did not recover both immutable attempts: {scenario_history_next}")
        reread_snapshot = request_json(base, f"/api/v1/households/{household_id}/snapshots/{snapshot_id}", token)
        if reread_snapshot.get("finalized", {}).get("contentHash") != finalized_hash:
            raise GateError("Snapshot hash changed after Run/comparison; immutable artifact preservation failed.")
        reread_compilation = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs/{compilation_version}", token)
        reread_run = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
        assert_run_artifact(reread_run, run_id, compilation_hash, result_hash)
        if reread_compilation.get("contentHash") != compilation_hash:
            raise GateError("Compilation or Run identity changed after comparison.")
        assert_fixture_report(request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}/results", token))
        manifest["artifacts"] = {"householdId": household_id, "personId": person_id, "accountId": account_id, "exactOwnershipShare": "1", "exactBalance": "100000.10", "snapshotId": snapshot_id, "snapshotHash": finalized_hash, "scenarioId": scenario_id, "compilationVersion": compilation_version, "compilationHash": compilation_hash, "runId": run_id, "secondRunId": second_run_id, "historyGoalId": goal.get("id"), "householdRunHistoryPageIds": household_history_ids, "householdRunHistoryPageCount": household_history_page_count, "scenarioRunHistoryIds": sorted(scenario_history_ids), "runResultHash": result_hash, "alternativeScenarioId": comparison_alternative, "comparisonId": comparison_id, "comparisonHistoryContainsSavedId": comparison_id in comparison_history_ids, "comparisonDeltas": deltas, "missingPackError": missing_code}
        phase("fabricated-household-compile-run-reopen-compare", "passed", artifacts=manifest["artifacts"])

        # A future schema marker must fail closed and be recoverable by removing only that marker.
        max_version = int(run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-Atc", "SELECT COALESCE(MAX(version),0) FROM schema_migrations"], capture=True, env=env).stdout.strip())
        sentinel = max_version + 1000
        run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-c", f"INSERT INTO schema_migrations(version) VALUES ({sentinel})"], env=env, capture=True)
        mismatch = run(compose + ["run", "--rm", "--no-deps", "migrate"], check=False, env=env, capture=True)
        (evidence / "migration-version-failure.log").write_text((mismatch.stdout or "")[-20_000:])
        if mismatch.returncode == 0 or "schema version does not match" not in (mismatch.stdout or ""):
            raise GateError("Future schema marker did not produce the expected actionable migration incompatibility failure.")
        ready_probe = run(compose + ["exec", "-T", "ui", "wget", "-S", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
        if ready_probe.returncode == 0 or "503" not in (ready_probe.stdout or ""):
            raise GateError(f"API readiness did not fail for the future schema marker as expected: {(ready_probe.stdout or '')[-1200:]}")
        # Delete exactly the sentinel inserted by this invocation.
        run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-c", f"DELETE FROM schema_migrations WHERE version = {sentinel}"], env=env, capture=True)
        run(compose + ["run", "--rm", "--no-deps", "migrate"], env=env, capture=True)
        phase("migration-version-failure-recovery", "passed", insertedAndRemovedVersion=sentinel)

        # Stop only this project's API and verify Nginx stays live with structured 503.
        run(compose + ["stop", "api"], env=env, capture=True)
        wait_http(base + "/health/live")
        status, body, _ = wait_http(base + "/api/v1", expected=503, headers=headers, timeout=30)
        unavailable = json.loads(body).get("error", {}).get("code")
        if unavailable != "api_unavailable":
            raise GateError(f"API outage was not returned as structured api_unavailable: HTTP {status} {body[:500]!r}")
        run(compose + ["start", "api"], env=env, capture=True)
        ready_after_recovery = run(compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
        deadline = time.monotonic() + TIMEOUT
        while ready_after_recovery.returncode and time.monotonic() < deadline:
            time.sleep(1)
            ready_after_recovery = run(compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
        if ready_after_recovery.returncode:
            raise GateError("API readiness did not recover after removing the inserted schema marker and restarting the API.")
        wait_http(base + "/api/v1/reference/canadian-rule-packs", headers=headers, timeout=TIMEOUT)
        recovered_run = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
        assert_run_artifact(recovered_run, run_id, compilation_hash, result_hash)
        assert_fixture_report(request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}/results", token))
        phase("api-offline-and-recovery", "passed", outageCode=unavailable)
        collect_logs()
        if args.keep:
            phase("complete", "passed", cleanup="kept", fixtureDirectory=str(temp))
        else:
            cleanup_project(compose, env, project, evidence)
            cleanup_attempted = True
            project_cleaned = True
            cleanup_temp_directory(temp)
            temp_cleaned = True
            phase("complete", "passed", cleanup="removed")
        print(f"Capability gate passed. Evidence: {evidence}")
        return 0
    except Exception as error:
        phase("failure", "failed", error=str(error))
        collect_logs()
        print(f"Capability gate failed: {error}\nEvidence: {evidence}", file=sys.stderr)
        return 1
    finally:
        if created and not args.keep and not cleanup_attempted:
            try:
                cleanup_project(compose, env, project, evidence)
                project_cleaned = True
            except Exception as error:
                print(f"Capability gate cleanup failed: {error}", file=sys.stderr)
        if args.keep:
            print(f"Kept disposable project {project} and fixture files at {temp} for inspection.")
        elif (not created or project_cleaned) and not temp_cleaned:
            try:
                cleanup_temp_directory(temp)
                temp_cleaned = True
            except Exception as error:
                print(f"Capability fixture file cleanup failed: {error}", file=sys.stderr)
        elif not temp_cleaned:
            print(f"Retaining fixture files at {temp} because project cleanup did not complete.", file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        print(f"Capability gate preflight failed: {error}", file=sys.stderr)
        raise SystemExit(2)
