#!/usr/bin/env python3
"""Run the disposable real-stack household planning capability gate."""

from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

if __package__:
    from . import release, release_pair
else:
    import release
    import release_pair

ROOT = Path(__file__).resolve().parents[1]

RULE_PACK = "canadian-planning-rules-test-v1"
TIMEOUT = 90
IMAGE_LABEL = "io.bobsled.capability-project"
DISPOSABLE_PROJECT = re.compile(r"^fgcap-[0-9a-f]{12}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class GateError(RuntimeError):
    pass


def load_release_pair() -> tuple[dict, str]:
    try:
        return release_pair.load()
    except release_pair.ReleasePairError as error:
        raise GateError(str(error)) from error


def load_candidate_manifest(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.expanduser().resolve().read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError(f"Cannot read candidate source manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("formatVersion") != 1:
        raise GateError("Candidate source manifest must be a JSON object with formatVersion 1.")
    if set(value) != {"formatVersion", "goRevision", "uiRevision"}:
        raise GateError("Candidate source manifest must contain exactly formatVersion, goRevision, and uiRevision.")
    for key in ("goRevision", "uiRevision"):
        if not isinstance(value[key], str) or not COMMIT.fullmatch(value[key]):
            raise GateError(f"Candidate source manifest {key} must be a full lowercase Git SHA.")
    return value, hashlib.sha256(raw).hexdigest()


def validate_candidate_revisions(candidate: dict[str, object], go_revision: str, ui_revision: str) -> None:
    for name, actual in (("goRevision", go_revision), ("uiRevision", ui_revision)):
        expected = candidate[name]
        if actual != expected:
            raise GateError(f"Candidate {name} mismatch: manifest requires {expected}, received {actual}.")


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


def bundle_worktree_fingerprint(root: Path = ROOT) -> tuple[str, str]:
    status = run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"], capture=True).stdout
    diff = run(["git", "-C", str(root), "diff", "--binary", "HEAD"], capture=True).stdout
    untracked = run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"], capture=True).stdout
    digest = hashlib.sha256()
    digest.update(status.encode())
    digest.update(diff.encode())
    for relative in sorted(path for path in untracked.split("\0") if path):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
    return status, digest.hexdigest()


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


def run_capability_browser(ui_path: Path, base_url: str, fixture_manifest: Path, evidence: Path, *, grep: str) -> None:
    if not (ui_path / "playwright.capability.config.ts").is_file():
        raise GateError(f"UI source is missing playwright.capability.config.ts: {ui_path}")
    if not fixture_manifest.is_file():
        raise GateError(f"Browser fixture manifest is missing: {fixture_manifest}")
    phase_name = re.sub(r"[^a-z0-9]+", "-", grep.lower()).strip("-")
    output = evidence / "browser-artifacts" / phase_name
    output.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.upper().endswith("TOKEN") and key.upper() not in ("AUTHORIZATION", "OIDC_ENABLED", "OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL") and not key.upper().startswith("LOCAL_PRINCIPAL_")}
    env.update({
        "CAPABILITY_UI_BASE_URL": base_url,
        "CAPABILITY_FIXTURE_MANIFEST": str(fixture_manifest.resolve()),
        "CAPABILITY_PLAYWRIGHT_OUTPUT_DIR": str(output.resolve()),
    })
    command = ["npm", "exec", "--", "playwright", "test", "--config=playwright.capability.config.ts", "--grep", grep]
    log_path = evidence / ("browser-" + phase_name + ".log")
    try:
        result = subprocess.run(command, cwd=ui_path, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    except subprocess.TimeoutExpired as error:
        output_text = error.stdout or ""
        if isinstance(output_text, bytes):
            output_text = output_text.decode(errors="replace")
        log_path.write_text(output_text[-200_000:])
        raise GateError(f"Capability browser phase timed out after 600 seconds; see {log_path}.") from error
    log_path.write_text((result.stdout or "")[-200_000:])
    if result.returncode:
        raise GateError(f"Capability browser phase {grep!r} failed ({result.returncode}); see {log_path}.\n" + (result.stdout or "")[-6000:])


def verify_browser_runner(ui_path: Path) -> dict[str, str]:
    if not shutil.which("npm") or not shutil.which("node"):
        raise GateError("Candidate browser validation requires host Node.js and npm.")
    if not (ui_path / "playwright.capability.config.ts").is_file():
        raise GateError(f"UI source is missing playwright.capability.config.ts: {ui_path}")
    if not (ui_path / "node_modules" / "@playwright" / "test" / "package.json").is_file():
        raise GateError(f"Playwright is not installed in the UI checkout; run `npm ci` in {ui_path}.")

    env = {**os.environ, "npm_config_offline": "true"}
    try:
        runner = subprocess.run(
            ["npm", "exec", "--", "playwright", "--version"], cwd=ui_path, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise GateError("Playwright CLI preflight timed out before Docker build.") from error
    if runner.returncode:
        raise GateError(f"Playwright CLI preflight failed before Docker build:\n{(runner.stdout or '')[-2000:]}")
    version = (runner.stdout or "").strip().splitlines()[-1]

    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    if configured:
        browser_path = Path(configured).expanduser().resolve()
    else:
        try:
            browser = subprocess.run(
                ["node", "-e", "process.stdout.write(require('playwright').chromium.executablePath())"],
                cwd=ui_path, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            raise GateError("Playwright Chromium path preflight timed out before Docker build.") from error
        if browser.returncode:
            raise GateError(f"Could not resolve Playwright Chromium before Docker build:\n{(browser.stdout or '')[-2000:]}")
        browser_path = Path((browser.stdout or "").strip())
    if not browser_path.is_file() or not os.access(browser_path, os.X_OK):
        raise GateError(
            f"Chromium is unavailable at {browser_path}; run `npx playwright install chromium` in {ui_path} "
            "or set PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH to an executable system Chromium."
        )
    return {"playwrightVersion": version, "chromiumExecutable": str(browser_path)}


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def request_json(base: str, path: str, token: str, *, body: dict | None = None, expected: int = 200, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method or ("POST" if data is not None else "GET"))
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


def request_json_local(base: str, path: str, *, body: dict | None = None, expected: int = 200, method: str | None = None) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method or ("POST" if data is not None else "GET"))
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


def create_borrowing_browser_fixture(base: str, token: str, household_id: str, person_id: str, chequing_id: str) -> dict[str, object]:
    start, end = "2026-01-01", "2080-12-31"

    richer_chequing = request_json(base, f"/api/v1/accounts/{chequing_id}/balances", token, body={
        "effectiveDate": start,
        "expectedRevision": 1,
        "amount": "2500000.10",
        "sourceKind": "user_reported",
        "sourceLabel": "Disposable SLED-180 long-horizon fabricated balance",
    }, expected=201)
    if richer_chequing.get("amount") != "2500000.10" or richer_chequing.get("revision") != 2:
        raise GateError(f"Long-horizon Chequing revision did not retain its exact amount: {richer_chequing}")

    def create_account(name: str, account_type: str, amount: str) -> tuple[str, int]:
        account = request_json(base, "/api/v1/accounts", token, body={
            "householdId": household_id,
            "displayName": name,
            "accountType": account_type,
            "defaultCurrency": "CAD",
            "ownerships": [{"personId": person_id, "share": "1", "effectiveStart": start}],
        }, expected=201)
        account_id = account.get("id")
        if not account_id:
            raise GateError(f"{account_type} fixture Account response omitted its ID: {account}")
        ownerships = account.get("ownerships") or []
        if len(ownerships) != 1 or ownerships[0].get("personId") != person_id or ownerships[0].get("share") != "1" or ownerships[0].get("effectiveStart") != start:
            raise GateError(f"{account_type} fixture did not persist the expected sole-owner share and effective date: {ownerships}")
        balance = request_json(base, f"/api/v1/accounts/{account_id}/balances", token, body={
            "effectiveDate": start,
            "expectedRevision": 0,
            "amount": amount,
            "sourceKind": "user_reported",
            "sourceLabel": "Disposable SLED-180 fabricated fixture",
        }, expected=201)
        if balance.get("amount") != amount or balance.get("revision") != 1:
            raise GateError(f"{account_type} fixture balance did not retain exact amount/revision: {balance}")
        return account_id, balance["revision"]

    non_registered_id, non_registered_revision = create_account("Disposable Non-Registered", "Non-Registered", "250000.00")
    rrsp_id, rrsp_revision = create_account("Disposable RRSP", "RRSP", "250000.00")
    classifications = request_json(base, f"/api/v1/people/{person_id}/registered-account-classifications", token, method="PUT", body={
        "classifications": [{
            "accountId": rrsp_id,
            "effectiveStart": start,
            "effectiveEnd": end,
            "sourceKind": "user_reported",
            "sourceLabel": "Disposable SLED-180 fabricated RRSP classification",
        }],
    })
    classified_accounts = classifications.get("classifications", [])
    if not any(item.get("accountId") == rrsp_id and item.get("effectiveStart") == start and item.get("effectiveEnd") == end for item in classified_accounts):
        raise GateError(f"RRSP classification fixture was not persisted over the planning horizon: {classifications}")
    # The compiler needs observed RRSP room for contribution years and for the
    # active retirement-funding source. A deliberately generous fabricated
    # amount also lets the stress-suite candidate compile before it fails on
    # available cash.
    for year in range(2026, 2081):
        room_date = f"{year}-01-01"
        room = request_json(base, f"/api/v1/people/{person_id}/registered-plan-room-revisions", token, body={
            "planKind": "RRSP",
            "effectiveDate": room_date,
            "expectedRevision": 0,
            "amount": "3000000.00",
            "sourceKind": "user_reported",
            "sourceLabel": "Disposable SLED-180 fabricated RRSP room",
        }, expected=201)
        if room.get("planKind") != "RRSP" or room.get("effectiveDate") != room_date or room.get("amount") != "3000000.00" or room.get("revision") != 1:
            raise GateError(f"RRSP room fixture did not retain its exact dated observation: {room}")
    basis = request_json(base, f"/api/v1/accounts/{non_registered_id}/cost-basis-revisions", token, body={
        "effectiveDate": start,
        "expectedRevision": 0,
        "amount": "180000.00",
        "sourceKind": "user_reported",
        "sourceLabel": "Disposable SLED-180 fabricated basis",
    }, expected=201)
    if basis.get("amount") != "180000.00" or basis.get("revision") != 1:
        raise GateError(f"Non-Registered basis fixture did not retain exact amount/revision: {basis}")

    liability = request_json(base, "/api/v1/liabilities", token, body={
        "householdId": household_id,
        "displayName": "Disposable unpriced credit line",
        "liabilityType": "Line of Credit",
        "defaultCurrency": "CAD",
        "purpose": "investment",
        "responsibilities": [{"personId": person_id, "share": "1", "effectiveStart": start}],
        "rateHistory": [],
    }, expected=201)
    liability_id = liability.get("id")
    if not liability_id:
        raise GateError(f"Credit-line fixture Liability response omitted its ID: {liability}")
    liability_balance = request_json(base, f"/api/v1/liabilities/{liability_id}/balances", token, body={
        "effectiveDate": start,
        "expectedRevision": 0,
        "amount": "0.00",
        "sourceKind": "user_reported",
        "sourceLabel": "Disposable SLED-180 unpriced credit-line baseline",
    }, expected=201)
    if liability_balance.get("amount") != "0.00" or liability_balance.get("revision") != 1:
        raise GateError(f"Credit-line fixture balance did not retain exact amount/revision: {liability_balance}")

    payment = request_json(base, "/api/v1/cashflows", token, body={
        "name": "Disposable existing credit-line payment",
        "direction": "outflow",
        "category": "debt_payment",
        "frequency": "monthly",
        "amount": "250.00",
        "indexation": "none",
        "allocations": [{"personId": person_id, "share": "1"}],
        "startDate": start,
        "endDate": end,
        "notes": "Fabricated payment binding for the disposable real-stack gate",
    }, expected=201)
    payment_id = payment.get("id")
    if not payment_id:
        raise GateError(f"Credit-line Cashflow fixture response omitted its ID: {payment}")

    contribution = request_json(base, "/api/v1/cashflows", token, body={
        "name": "Disposable RRSP contribution",
        "direction": "outflow",
        "category": "investment_contribution",
        "frequency": "monthly",
        "amount": "1000.00",
        "indexation": "none",
        "allocations": [{"personId": person_id, "share": "1"}],
        "startDate": start,
        "endDate": "2045-12-31",
        "notes": "Fabricated registered contribution for the disposable real-stack gate",
    }, expected=201)
    contribution_id = contribution.get("id")
    if not contribution_id:
        raise GateError(f"Registered contribution fixture Cashflow response omitted its ID: {contribution}")

    withdrawal = request_json(base, "/api/v1/cashflows", token, body={
        "name": "Disposable RRSP retirement withdrawal",
        "direction": "inflow",
        "category": "investment_withdrawal",
        "frequency": "annual",
        # $50,000 is large enough to produce positive modeled Canadian tax
        # after long-horizon indexed credits, while remaining well inside the
        # projected RRSP balance through the fixture's 2080 horizon.
        "amount": "50000.00",
        "indexation": "none",
        "allocations": [{"personId": person_id, "share": "1"}],
        "startDate": "2060-01-01",
        "endDate": end,
        "notes": "Fabricated registered withdrawal for the disposable real-stack gate",
    }, expected=201)
    withdrawal_id = withdrawal.get("id")
    if not withdrawal_id:
        raise GateError(f"Registered withdrawal fixture Cashflow response omitted its ID: {withdrawal}")

    retirement_spending = request_json(base, "/api/v1/cashflows", token, body={
        "name": "Disposable retirement living expense",
        "direction": "outflow",
        "category": "living_expense",
        "frequency": "monthly",
        "amount": "50000.00",
        "indexation": "none",
        "allocations": [{"personId": person_id, "share": "1"}],
        "startDate": "2080-01-01",
        "endDate": end,
        "notes": "Fabricated final-year retirement expense for the one-year terminal funding stress",
    }, expected=201)
    retirement_spending_id = retirement_spending.get("id")
    if not retirement_spending_id:
        raise GateError(f"Retirement living-expense fixture Cashflow response omitted its ID: {retirement_spending}")

    attributions = request_json(base, f"/api/v1/people/{person_id}/tax-attributions", token, method="PUT", body={
        "attributions": [{
            "sourceKind": "account",
            "sourceId": rrsp_id,
            "category": "taxable_withdrawal",
            "allocationMethod": "owner_share",
            "effectiveStart": start,
            "effectiveEnd": end,
            "notes": "Disposable SLED-180 fabricated taxable RRSP withdrawal attribution",
        }, {
            "sourceKind": "account",
            "sourceId": non_registered_id,
            "category": "realized_capital_gain",
            "allocationMethod": "owner_share",
            "effectiveStart": start,
            "effectiveEnd": end,
            "notes": "Disposable SLED-175 fabricated Non-Registered gain attribution",
        }, {
            "sourceKind": "account",
            "sourceId": non_registered_id,
            "category": "realized_capital_loss",
            "allocationMethod": "owner_share",
            "effectiveStart": start,
            "effectiveEnd": end,
            "notes": "Disposable SLED-175 fabricated Non-Registered loss attribution",
        }],
    })
    persisted_attributions = attributions.get("attributions", [])
    for account_id, category in ((rrsp_id, "taxable_withdrawal"),
                                 (non_registered_id, "realized_capital_gain"),
                                 (non_registered_id, "realized_capital_loss")):
        if not any(item.get("sourceId") == account_id and item.get("category") == category
                   and item.get("effectiveStart") == start and item.get("effectiveEnd") == end
                   for item in persisted_attributions):
            raise GateError(f"Dated {category} tax attribution for Account {account_id} was not persisted: {attributions}")

    profiles = request_json(base, f"/api/v1/people/{person_id}/tax-profiles", token, method="PUT", body={
        "profiles": [{"taxCountry": "CA", "taxRegion": "ON", "residencyStatus": "resident", "effectiveStart": start, "effectiveEnd": end,
                      "notes": "Fabricated resident tax profile for SLED-180 disposable forecast"}],
    })
    if len(profiles.get("profiles", [])) != 1:
        raise GateError(f"Person tax-profile fixture was not persisted: {profiles}")

    selections = [
        {"kind": "account_balance", "recordId": chequing_id, "effectiveDate": start, "revision": 2},
        {"kind": "account_balance", "recordId": non_registered_id, "effectiveDate": start, "revision": non_registered_revision},
        {"kind": "account_balance", "recordId": rrsp_id, "effectiveDate": start, "revision": rrsp_revision},
        {"kind": "liability_balance", "recordId": liability_id, "effectiveDate": start, "revision": 1},
    ]
    snapshot = request_json(base, f"/api/v1/households/{household_id}/snapshots", token, body={
        "name": "Disposable long-horizon borrowing baseline",
        "asOfDate": start,
        "notes": "SLED-180 fabricated long-horizon fixture",
        "selections": selections,
    }, expected=201)
    snapshot_id = snapshot.get("draft", {}).get("id")
    version = snapshot.get("draft", {}).get("version")
    if not snapshot_id or not version or len(snapshot.get("included", [])) != len(selections):
        raise GateError(f"Long-horizon fixture Snapshot did not include every selected balance: {snapshot}")
    finalized = request_json(base, f"/api/v1/households/{household_id}/snapshots/{snapshot_id}/finalize", token,
                             body={"expectedVersion": version}, expected=200)
    snapshot_hash = finalized.get("finalized", {}).get("contentHash")
    if not snapshot_hash:
        raise GateError("Long-horizon fixture Snapshot did not finalize with a content hash.")
    return {
        "householdId": household_id,
        "personId": person_id,
        "snapshotId": snapshot_id,
        "snapshotAsOfDate": start,
        "scenarioStartDate": start,
        "scenarioEndDate": end,
        "rulePackVersion": RULE_PACK,
        "accountIds": {"chequing": chequing_id, "nonRegistered": non_registered_id, "rrsp": rrsp_id},
        "accountLabels": {"chequing": "Fixture Chequing", "nonRegistered": "Disposable Non-Registered", "rrsp": "Disposable RRSP"},
        "liabilityIds": {"creditLine": liability_id},
        "cashflowIds": {"debtPayment": payment_id, "registeredContribution": contribution_id,
                        "registeredWithdrawal": withdrawal_id, "retirementSpending": retirement_spending_id},
        "basis": {"accountId": non_registered_id, "effectiveDate": start, "revision": 1, "amount": "180000.00"},
        "expected": {"drawAmount": "5000.00", "annualRate": "0.06", "monthlyPayment": "250.00", "basisDate": start,
                     "taxModelVersion": "ca-on-personal-tax-2026-v3-investment", "registeredWithdrawalAmount": "50000.00",
                     "registeredWithdrawalMonthlyAmount": "4166.67", "registeredWithdrawalStartDate": "2060-01-01",
                     "retirementSpendingMonthlyAmount": "190000.00", "retirementFundingStartDate": "2080-01-01",
                     "saleCostRate": "0.01", "saleCostAssumptionLabel": "Disposable SLED-175 automatic sale costs",
                     "registeredContributionCandidateAmount": "2600000.00"},
        "snapshotHash": snapshot_hash,
    }


def create_unfunded_contribution_suite(base: str, fixture: dict[str, object], browser_artifacts: dict[str, object]) -> dict[str, object]:
    household_id = str(fixture["householdId"])
    scenario_id = str(browser_artifacts.get("scenarioId", ""))
    compilation_version = browser_artifacts.get("compilationVersion")
    if not scenario_id or not isinstance(compilation_version, int) or compilation_version < 1:
        raise GateError(f"Browser Scenario artifacts omitted a valid Scenario ID or compilation version: {browser_artifacts}")
    recipe_history = request_json_local(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/planning-recipes")
    revision = recipe_history.get("headRevision")
    if not isinstance(revision, int) or revision < 1:
        raise GateError(f"Browser-authored Scenario has no saved planning recipe: {recipe_history}")
    recipe = request_json_local(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/planning-recipes/{revision}")
    choices = recipe.get("choices")
    if not isinstance(choices, dict):
        raise GateError(f"Browser-authored recipe has no complete compiler choices: {recipe}")
    contribution_id = fixture["cashflowIds"]["registeredContribution"]
    rrsp_id = fixture["accountIds"]["rrsp"]
    bindings = choices.get("cashflowBindings", [])
    contribution_binding = next((item for item in bindings if item.get("cashflowId") == contribution_id), None)
    if contribution_binding is None or contribution_binding.get("destinationAccountId") != rrsp_id:
        raise GateError(f"Browser recipe does not bind the registered contribution to the seeded RRSP: {bindings}")

    unfundable_choices = copy.deepcopy(choices)
    cashflow_choices = unfundable_choices.setdefault("cashflowChoices", [])
    selected = next((item for item in cashflow_choices if item.get("cashflowId") == contribution_id), None)
    if selected is None:
        selected = {"cashflowId": contribution_id}
        cashflow_choices.append(selected)
    selected["included"] = True
    selected["schedule"] = {
        "amount": str(fixture["expected"]["registeredContributionCandidateAmount"]),
        "frequency": "monthly",
        "effectiveStart": str(fixture["scenarioStartDate"]),
        "effectiveEnd": str(fixture["scenarioStartDate"]),
        "indexation": "none",
        "assumptionLabel": "Deliberately unfundable registered contribution",
    }
    command = {
        "compilationVersion": compilation_version,
        "rulePackVersion": fixture["rulePackVersion"],
        "referenceCandidateId": "base",
        "candidates": [
            {"candidateId": "funded-peer", "label": "Funded contribution peer", "changes": {"name": "Funded contribution peer"}, "choices": choices},
            {"candidateId": "unfunded-contribution", "label": "Deliberately unfundable RRSP contribution", "changes": {"name": "Deliberately unfundable RRSP contribution"}, "choices": unfundable_choices},
        ],
    }
    suite = request_json_local(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/stress-suites", body=command, expected=201)
    suite_id = suite.get("suiteId")
    suite_candidates = {item.get("candidateId"): item for item in suite.get("candidates", [])}
    if not suite_id or suite_candidates.get("funded-peer", {}).get("status") != "ready" or suite_candidates.get("unfunded-contribution", {}).get("status") != "ready":
        raise GateError(f"Prepared contribution suite is missing a ready candidate: {suite}")
    return {"suite": {
        "scenarioId": scenario_id,
        "suiteId": suite_id,
        "successCandidateId": "funded-peer",
        "failedCandidateId": "unfunded-contribution",
        "recipeRevision": revision,
        "compilationVersion": compilation_version,
    }}


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


def assert_taxable_funding_compilation(compilation: dict, account_id: str, *, basis_revision: int,
                                       sale_cost_rate: str, sale_cost_label: str, start: str,
                                       source_start: str, end: str) -> None:
    content = compilation.get("content", {})
    if not isinstance(content, dict):
        raise GateError("Compiled Scenario input has no frozen content object.")
    policy = content.get("retirementFundingPolicy", {})
    sources = policy.get("sources", []) if isinstance(policy, dict) else []
    source = next((item for item in sources if isinstance(item, dict) and item.get("accountId") == account_id), None)
    if source is None:
        raise GateError(f"Frozen retirement funding policy omitted Non-Registered Account {account_id}.")
    if (source.get("fundingKind") != "taxable_sale" or source.get("saleCostRate") != sale_cost_rate
            or source.get("saleCostAssumptionLabel") != sale_cost_label
            or source.get("effectiveStart") != source_start or source.get("effectiveEnd") != end):
        raise GateError(f"Frozen Non-Registered sale source does not preserve its reviewed cost assumptions: {source}")
    basis_rows = content.get("portfolioBasis", [])
    if not any(isinstance(item, dict) and item.get("accountId") == account_id
               and item.get("revision") == basis_revision for item in basis_rows):
        raise GateError(f"Frozen Scenario input did not pin Non-Registered cost-basis revision {basis_revision}: {basis_rows}")
    attributions = content.get("taxAttributions", [])
    for category in ("realized_capital_gain", "realized_capital_loss"):
        if not any(isinstance(item, dict) and item.get("sourceId") == account_id
                   and item.get("sourceKind") == "account" and item.get("allocationMethod") == "owner_share"
                   and item.get("category") == category
                   and item.get("effectiveStart") <= start and (item.get("effectiveEnd") is None or item.get("effectiveEnd") >= end)
                   for item in attributions):
            raise GateError(f"Frozen Scenario input omitted dated {category} owner-share attribution for Account {account_id}.")


def assert_taxable_retirement_funding_report(report: dict, account_id: str) -> None:
    periods = report.get("periods", [])
    if not isinstance(periods, list) or not periods:
        raise GateError("Taxable retirement funding Run report has no projected periods.")
    trades: list[dict] = []
    trade_ids: set[str] = set()
    for period in periods:
        if not isinstance(period, dict):
            raise GateError("Taxable retirement funding Run report contains an invalid period.")
        reconciliation = period.get("reconciliation", {})
        if isinstance(reconciliation, dict):
            for key in ("openingNetWorthResidual", "movementDeltaResidual", "closingNetWorthResidual"):
                if key in reconciliation and reconciliation[key] != "0.00":
                    raise GateError(f"Taxable retirement sale period is not reconciled: {period.get('periodStart')} {key}={reconciliation[key]!r}.")
        for trade in period.get("portfolioTrades", []):
            if isinstance(trade, dict) and trade.get("fundingSourceId"):
                if trade.get("accountId") != account_id:
                    raise GateError(f"Automatic retirement funding sale used an unexpected Account: {trade}")
                trade_id = trade.get("id")
                if not isinstance(trade_id, str) or not trade_id or trade_id in trade_ids:
                    raise GateError(f"Automatic retirement funding trade has missing or duplicate identity: {trade}")
                trade_ids.add(trade_id)
                trades.append(trade)
    funding_events = [event for event in report.get("fundingEvents", [])
                      if isinstance(event, dict) and event.get("funded") not in (None, "0.00")]
    if not trades:
        raise GateError(f"Run report contains no generated retirement sale for Non-Registered Account {account_id}.")
    if not funding_events:
        raise GateError("Run report contains no funded retirement cash-deficit event.")
    all_events = [event for event in report.get("fundingEvents", []) if isinstance(event, dict)]
    spending_events = [event for event in all_events if event.get("purpose") == "retirement_spending"]
    if not spending_events:
        raise GateError("Run report contains no retirement-spending funding events.")
    for event in spending_events:
        try:
            requested, funded, unfunded = (Decimal(event[key]) for key in ("requested", "funded", "unfunded"))
        except (KeyError, InvalidOperation, TypeError) as error:
            raise GateError(f"Retirement-spending funding event omits exact-money totals: {event}") from error
        if funded != requested or unfunded != 0:
            raise GateError(f"Terminal-year retirement-spending event was not fully funded: {event}")
    tax_events = [event for event in all_events if event.get("purpose") == "tax_settlement"]
    if not any(Decimal(event.get("unfunded", "0.00")) > 0 for event in tax_events):
        raise GateError("Run report did not expose a positive unpaid year-end tax-settlement amount.")
    purposes: set[str] = set()
    has_costly_sale = False
    has_depleted_sale = False
    for trade in trades:
        if trade.get("fundingPurpose") not in ("retirement_spending", "tax_settlement"):
            raise GateError(f"Generated sale has missing or unsupported funding purpose: {trade}")
        purposes.add(str(trade["fundingPurpose"]))
        try:
            gross = Decimal(trade["grossProceeds"])
            costs = Decimal(trade["transactionCosts"])
            basis = Decimal(trade["disposedBasis"])
            gain = Decimal(trade["realizedGainLoss"])
            net = Decimal(trade["netProceeds"])
            closing_market = Decimal(trade["closingMarketValue"])
            closing_basis = Decimal(trade["closingBasis"])
        except (KeyError, InvalidOperation, TypeError) as error:
            raise GateError(f"Generated sale omits an exact-money result field: {trade}") from error
        if gross <= 0 or costs < 0 or costs > gross or basis < 0 or net != gross - costs or gain != net - basis:
            raise GateError(f"Generated sale proceeds, fees, basis, and gain do not reconcile: {trade}")
        if closing_market < 0 or closing_basis < 0:
            raise GateError(f"Generated sale left a negative market value or pooled basis: {trade}")
        has_costly_sale = has_costly_sale or costs > 0
        has_depleted_sale = has_depleted_sale or closing_market == 0
        allocations = trade.get("taxableByPerson", [])
        try:
            allocated_gain = sum((Decimal(item["amount"]) for item in allocations), Decimal(0))
        except (InvalidOperation, TypeError, KeyError) as error:
            raise GateError(f"Generated sale has malformed Person tax allocations: {trade}") from error
        if allocated_gain != gain:
            raise GateError(f"Generated sale Person gain/loss allocations do not reconcile: {trade}")
    if purposes != {"retirement_spending", "tax_settlement"}:
        raise GateError(f"Run report did not exercise both spending and year-end tax-funded sales: {sorted(purposes)}.")
    if not has_costly_sale:
        raise GateError("Run report did not apply the configured automatic-sale transaction-cost assumption.")
    if not has_depleted_sale:
        raise GateError("Run report did not expose full Non-Registered account depletion.")
    if not any(trade.get("fundingPurpose") == "tax_settlement"
               and trade.get("effectiveDate") == "2080-12-31"
               and Decimal(trade["closingMarketValue"]) == 0
               and Decimal(trade["closingBasis"]) == 0
               for trade in trades):
        raise GateError("Final-day tax-settlement sale did not fully deplete Non-Registered market value and basis.")


def assert_taxable_retirement_funding_suite(detail: dict) -> None:
    outcome = detail.get("outcome", {})
    annual = outcome.get("annual", []) if isinstance(outcome, dict) else []
    if not isinstance(annual, list):
        raise GateError("Stress-suite candidate detail has no annual outcome rows.")
    taxable_years = []
    for row in annual:
        if not isinstance(row, dict):
            continue
        try:
            gross = Decimal(row.get("taxableSaleGrossProceeds", "0.00"))
        except (InvalidOperation, TypeError) as error:
            raise GateError(f"Stress-suite annual taxable-sale gross proceeds are invalid: {row}") from error
        if gross <= 0:
            continue
        try:
            costs = Decimal(row["taxableSaleTransactionCosts"])
            basis = Decimal(row["taxableSaleBasisConsumed"])
            gain = Decimal(row["taxableRealizedGainLoss"])
            net = Decimal(row["taxableSaleNetProceeds"])
        except (KeyError, InvalidOperation, TypeError) as error:
            raise GateError(f"Stress-suite annual taxable-sale totals are incomplete: {row}") from error
        if costs < 0 or basis < 0 or net != gross - costs or gain != net - basis:
            raise GateError(f"Stress-suite annual taxable-sale totals do not reconcile: {row}")
        for key in ("estimatedTax", "taxSettlementPaid", "taxRefundReceived", "remainingTaxDueOrRefund", "debtBalances"):
            try:
                Decimal(row[key])
            except (KeyError, InvalidOperation, TypeError) as error:
                raise GateError(f"Stress-suite annual taxable-sale outcome omits tax settlement or debt result {key}: {row}") from error
        taxable_years.append(row.get("year"))
    if not taxable_years:
        raise GateError("Stress-suite candidate detail omitted the Non-Registered retirement sales from its annual outcomes.")


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
    if not DISPOSABLE_PROJECT.fullmatch(project) or config.get("name") != project:
        raise GateError(f"Refusing non-disposable Compose project {config.get('name')!r}; expected generated project {project!r}.")
    for name, volume in config.get("volumes", {}).items():
        if volume.get("external") or volume.get("driver_opts") or volume.get("name") != f"{project}_{name}":
            raise GateError(f"Refusing non-project-scoped or external volume {name!r}.")
    for name, network in config.get("networks", {}).items():
        if network.get("external") or network.get("name") != f"{project}_{name}":
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
        if service_name != "ui" and service.get("ports"):
            raise GateError(f"{service_name} must remain private; resolved Compose config publishes a port.")
    api_context = config["services"]["api"].get("build", {}).get("context")
    migrate_context = config["services"]["migrate"].get("build", {}).get("context")
    if api_context != migrate_context:
        raise GateError("API and migration services must use the same resolved Go source context.")
    for service, revision_key in (("api", "GO_SOURCE_REVISION"), ("migrate", "GO_SOURCE_REVISION"), ("ui", "UI_SOURCE_REVISION")):
        labels = config["services"][service].get("labels", {})
        if labels.get("org.financegame.source.revision") != env[revision_key]:
            raise GateError(f"Resolved {service} source revision label does not match the selected source pair.")
    ui_ports = config["services"]["ui"].get("ports", [])
    if len(ui_ports) != 1 or ui_ports[0].get("host_ip") != "127.0.0.1":
        raise GateError("The only published port must bind the UI to loopback.")


def inspect_deployed_pair(compose: list[str], env: dict[str, str], expected: dict[str, str]) -> dict[str, dict[str, object]]:
    deployed: dict[str, dict[str, object]] = {}
    for service in ("migrate", "api", "ui"):
        container_id = run(compose + ["ps", "--all", "-q", service], env=env, capture=True).stdout.strip()
        if not container_id:
            raise GateError(f"The disposable stack has no container for {service}; cannot verify its source identity.")
        inspected = json.loads(run(["docker", "inspect", container_id], capture=True).stdout)[0]
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        source_role = "go" if service in ("migrate", "api") else "ui"
        expected_revision = expected[source_role]
        for label, wanted in (("org.financegame.source.revision", expected_revision),
                              ("org.financegame.release.pair-sha256", env["RELEASE_PAIR_SHA256"])):
            if labels.get(label) != wanted:
                raise GateError(f"Deployed {service} label {label} mismatch: expected {wanted!r}, received {labels.get(label)!r}.")
        deployed[service] = {"containerId": container_id, "imageId": inspected.get("Image"), "sourceRevision": expected_revision,
                             "labels": {key: labels.get(key) for key in ("org.financegame.release.id", "org.financegame.release.pair-sha256", "org.financegame.release.qualification", "org.financegame.source.revision")}}
        if not deployed[service]["imageId"]:
            raise GateError(f"Could not record the immutable image ID for {service}.")
    if deployed["api"]["sourceRevision"] != deployed["migrate"]["sourceRevision"]:
        raise GateError("API and migration containers report different Go source revisions.")
    return deployed


def verify_disposable_database(compose: list[str], env: dict[str, str]) -> None:
    project = env["CAPABILITY_PROJECT"]
    if not DISPOSABLE_PROJECT.fullmatch(project):
        raise GateError(f"Refusing database operation for non-generated project name {project!r}.")
    config = json.loads(run(compose + ["config", "--format", "json"], env=env, capture=True).stdout)
    if config.get("name") != project:
        raise GateError(f"Refusing database operation: Compose resolved project {config.get('name')!r}, expected {project!r}.")
    volume_config = config.get("volumes", {}).get("postgres_data", {})
    volume_name = volume_config.get("name")
    expected_volume = f"{project}_postgres_data"
    if volume_config.get("external") or volume_config.get("driver_opts") or volume_name != expected_volume:
        raise GateError(f"Refusing database operation on non-disposable volume {volume_name!r}.")
    container_id = run(compose + ["ps", "-q", "db"], env=env, capture=True).stdout.strip()
    if not container_id:
        raise GateError("Refusing database operation: the disposable project's db container is not running.")
    inspected = json.loads(run(["docker", "inspect", container_id], capture=True).stdout)[0]
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != "db":
        raise GateError("Refusing database operation: the db container does not belong to this disposable Compose project.")
    mounts = [mount for mount in inspected.get("Mounts", []) if mount.get("Destination") == "/var/lib/postgresql"]
    if len(mounts) != 1 or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != expected_volume:
        raise GateError("Refusing database operation: the db container does not use the generated project volume.")
    volume = json.loads(run(["docker", "volume", "inspect", expected_volume], capture=True).stdout)[0]
    if volume.get("Labels", {}).get("com.docker.compose.project") != project:
        raise GateError("Refusing database operation: the database volume is not owned by this disposable project.")


def pg_dump_to_file(compose: list[str], env: dict[str, str], destination: Path) -> None:
    command = compose + ["exec", "-T", "db", "pg_dump", "--format=custom", "--no-owner", "--no-acl", "--username=financegame", "--dbname=financegame"]
    temporary = destination.with_suffix(destination.suffix + ".partial")
    with temporary.open("wb") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        raise GateError(f"Database backup command failed ({result.returncode}): {(result.stderr or b'').decode(errors='replace')[-4000:]}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise GateError("Database backup command produced an empty archive.")
    temporary.replace(destination)


def pg_restore_file(compose: list[str], env: dict[str, str], source: Path) -> None:
    command = compose + ["exec", "-T", "db", "pg_restore", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl", "--username=financegame", "--dbname=financegame"]
    with source.open("rb") as archive:
        result = subprocess.run(command, stdin=archive, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if result.returncode:
        output = (result.stdout or b"").decode(errors="replace")
        raise GateError(f"Database restore command failed ({result.returncode}): {output[-6000:]}")


def database_schema_version(compose: list[str], env: dict[str, str]) -> int:
    value = run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-Atc", "SELECT COALESCE(MAX(version),0) FROM schema_migrations"], capture=True, env=env).stdout.strip()
    try:
        return int(value)
    except ValueError as error:
        raise GateError(f"Database schema version query returned invalid value {value!r}.") from error


def cleanup_project(compose: list[str], env: dict[str, str], project: str, evidence: Path, label: str = "primary") -> None:
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
        resource_project = run(inspect, capture=True).stdout.strip()
        if resource_project != project:
            raise GateError(f"Refusing cleanup: {kind} {item} has unexpected Compose project label {resource_project!r}.")
    result = run(compose + ["down", "--volumes", "--remove-orphans"], check=False, env=env, capture=True)
    (evidence / f"cleanup-{label}.log").write_text(result.stdout or "")
    if result.returncode:
        raise GateError(f"Compose cleanup failed ({result.returncode}); see {evidence / f'cleanup-{label}.log'}.")
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
    build_sources = temp / "build-sources"
    if build_sources.exists():
        shutil.rmtree(build_sources)
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


def archive_source(source: Path, revision: str, destination: Path) -> Path:
    if not shutil.which("tar"):
        raise GateError("tar is required to materialize immutable capability-gate build contexts.")
    destination.mkdir(parents=True)
    archive = destination.parent / f"{destination.name}.tar"
    run(["git", "-C", str(source), "archive", "--format=tar", "-o", str(archive), revision], capture=True)
    try:
        run(["tar", "-xf", str(archive), "-C", str(destination)], capture=True)
    finally:
        archive.unlink(missing_ok=True)
    return destination


def write_override(temp: Path, issuer: str, project: str, pair_sha: str = "test-only") -> Path:
    migrate_image, api_image, ui_image = capability_images(project)
    build_labels = {IMAGE_LABEL: project, "org.financegame.release.pair-sha256": pair_sha}
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
                    "OIDC_ENABLED": "true",
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


def write_local_auth_override(temp: Path, principal_issuer: str, principal_subject: str) -> Path:
    override = {
        "services": {
            "api": {
                "environment": {
                    "OIDC_ENABLED": "false",
                    "LOCAL_PRINCIPAL_ISSUER": principal_issuer,
                    "LOCAL_PRINCIPAL_SUBJECT": principal_subject,
                },
            },
        },
    }
    path = temp / "compose.local-auth.yaml"
    path.write_text(json.dumps(override, indent=2) + "\n")
    return path


def write_recovery_override(temp: Path, images: dict[str, str]) -> Path:
    override = {"services": {}}
    for service in ("db", "migrate", "api", "ui"):
        override["services"][service] = {"image": images[service], "pull_policy": "never"}
    # A template0 database lets pg_restore recreate the dumped public schema cleanly.
    override["services"]["db"]["environment"] = {"POSTGRES_DB": "postgres"}
    path = temp / "compose.recovery.yaml"
    path.write_text(json.dumps(override, indent=2) + "\n")
    return path


def tag_recovery_images(images: dict[str, str], project: str) -> tuple[dict[str, str], list[str]]:
    references: dict[str, str] = {}
    created: list[str] = []
    try:
        for service, image_id in images.items():
            reference = f"financegame-recovery:{project}-{service}"
            existing = run(["docker", "image", "inspect", reference], check=False, capture=True)
            if existing.returncode == 0:
                raise GateError(f"Generated recovery image tag already exists: {reference}")
            run(["docker", "image", "tag", image_id, reference], capture=True)
            created.append(reference)
            tagged = json.loads(run(["docker", "image", "inspect", reference], capture=True).stdout)[0]
            if tagged.get("Id") != image_id:
                raise GateError(f"Recovery image tag {reference} does not resolve to captured image {image_id}.")
            references[service] = reference
    except Exception:
        remove_recovery_image_tags(created)
        raise
    return references, created


def remove_recovery_image_tags(tags: list[str]) -> None:
    for reference in tags:
        if not reference.startswith("financegame-recovery:fgcap-"):
            raise GateError(f"Refusing to remove non-generated image reference {reference!r}.")
        existing = run(["docker", "image", "inspect", reference], check=False, capture=True)
        if existing.returncode == 0:
            run(["docker", "image", "rm", reference], capture=True)


def compose_command(temp: Path, override: Path, project: str, recovery_override: Path | None = None, extra_overrides: list[Path] | None = None) -> list[str]:
    empty_env = temp / "empty.env"
    empty_env.write_text("# Isolated capability run; no repository .env is loaded.\n")
    command = ["docker", "compose", "--env-file", str(empty_env), "--project-directory", str(ROOT), "-p", project, "-f", str(ROOT / "compose.yaml"), "-f", str(override)]
    if recovery_override:
        command.extend(["-f", str(recovery_override)])
    for extra in extra_overrides or []:
        command.extend(["-f", str(extra)])
    return command


def project_resources_are_empty(compose: list[str], env: dict[str, str], project: str) -> None:
    existing = run(compose + ["ps", "-aq"], check=False, env=env, capture=True).stdout.strip()
    if existing:
        raise GateError(f"Generated project name unexpectedly already owns containers: {project}")
    for service in ("db", "migrate", "api", "ui", "oidc-fixture"):
        name = f"{project}-{service}-1"
        collision = run(["docker", "container", "inspect", name], check=False, capture=True)
        if collision.returncode == 0:
            raise GateError(f"Generated container name already exists without this project's labels: {name}")
    volumes = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip()
    if volumes:
        raise GateError(f"Generated project name unexpectedly already owns volumes: {project}")
    volume_collision = run(["docker", "volume", "inspect", f"{project}_postgres_data"], check=False, capture=True)
    if volume_collision.returncode == 0:
        raise GateError(f"Generated database volume name already exists without this project's labels: {project}_postgres_data")
    networks = run(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], capture=True).stdout.strip()
    if networks:
        raise GateError(f"Generated project name unexpectedly already owns networks: {project}")
    network_collision = run(["docker", "network", "inspect", f"{project}_backend"], check=False, capture=True)
    if network_collision.returncode == 0:
        raise GateError(f"Generated network name already exists without this project's labels: {project}_backend")


def postgres_image_version(compose: list[str], env: dict[str, str]) -> str:
    return run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "postgres", "-Atc", "SHOW server_version"], capture=True, env=env).stdout.strip()


def migration_versions(compose: list[str], env: dict[str, str]) -> list[int]:
    output = run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-Atc", "SELECT COALESCE(json_agg(version ORDER BY version), '[]'::json)::text FROM schema_migrations"], capture=True, env=env).stdout.strip()
    try:
        values = json.loads(output)
    except json.JSONDecodeError as error:
        raise GateError(f"Database migration list returned invalid JSON {output!r}.") from error
    if not isinstance(values, list) or any(not isinstance(item, int) for item in values):
        raise GateError(f"Database migration list has an invalid shape: {values!r}.")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-source", required=True)
    parser.add_argument("--ui-source", required=True)
    parser.add_argument("--keep", action="store_true", help="retain the project and evidence for inspection")
    parser.add_argument("--evidence-dir", type=Path, help="directory for manifest, logs and checks")
    parser.add_argument("--no-cache", action="store_true", help="build all source images without Docker cache")
    parser.add_argument("--recovery-rehearsal", action="store_true", help="back up the fabricated database, rehearse an incompatible-schema failure, and restore into a second isolated project")
    parser.add_argument("--precommit-validation", action="store_true", help="validate a dirty candidate worktree without issuing runtime qualification")
    parser.add_argument("--candidate-manifest", type=Path, help="exact Go/UI revisions for non-qualifying candidate validation")
    parser.add_argument("--borrowing-browser", action="store_true", help="run the opt-in real-browser long-horizon borrowing workflow")
    args = parser.parse_args()

    if args.borrowing_browser != bool(args.candidate_manifest):
        raise GateError("--borrowing-browser and --candidate-manifest must be supplied together.")
    if args.candidate_manifest and not args.no_cache:
        raise GateError("Candidate validation requires --no-cache so every source image is built from the selected revisions.")
    if args.candidate_manifest and args.recovery_rehearsal:
        raise GateError("Candidate browser validation cannot issue recovery or release qualification evidence.")
    candidate_sources = None
    candidate_manifest_sha = None
    if args.candidate_manifest:
        candidate_sources, candidate_manifest_sha = load_candidate_manifest(args.candidate_manifest)

    go_path, go_info = source_info(args.go_source, "Go")
    ui_path, ui_info = source_info(args.ui_source, "UI")
    browser_runner = verify_browser_runner(ui_path) if args.borrowing_browser else None
    pair, pair_sha = load_release_pair()
    if candidate_sources is not None:
        validate_candidate_revisions(candidate_sources, str(go_info["revision"]), str(ui_info["revision"]))
        candidate_pair_sha = hashlib.sha256(json.dumps(
            {"baselineReleasePairSha256": pair_sha, "candidateManifestSha256": candidate_manifest_sha},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        release_id = f"candidate-validation-{candidate_pair_sha[:12]}"
        qualification_status = "candidate-validation"
        schema_version_expected = release.deployed_schema_version(
            {"mode": "local-override", "path": str(go_path)}, pair,
        )
    else:
        try:
            release_pair.validate_source_revisions(
                pair, str(go_info["revision"]), str(ui_info["revision"]),
                go_dirty=bool(go_info["dirty"]), ui_dirty=bool(ui_info["dirty"]),
            )
        except release_pair.ReleasePairError as error:
            raise GateError(str(error)) from error
        candidate_pair_sha = pair_sha
        release_id = pair["releaseId"]
        qualification_status = pair["qualification"]["status"]
        schema_version_expected = pair["compatibility"]["databaseSchemaVersion"]
    compatibility = pair["compatibility"]
    fixture_contract = compatibility["capabilityFixture"]
    rule_pack = compatibility["rulePackVersion"]
    if rule_pack != RULE_PACK:
        raise GateError(f"Capability gate requires the fixed test-only rule pack {RULE_PACK!r}; release pair selects {rule_pack!r}.")
    bundle_revision = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture=True).stdout.strip()
    bundle_dirty, bundle_tree_sha = bundle_worktree_fingerprint()
    if bundle_dirty and not args.precommit_validation:
        raise GateError("Bundle checkout is dirty; use --precommit-validation for runtime evidence without qualification, or run a clean checkout.")
    if args.precommit_validation and not bundle_dirty:
        raise GateError("--precommit-validation requires a dirty candidate bundle checkout.")
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
    build_sources = temp / "build-sources"
    build_sources.mkdir()
    evidence = args.evidence_dir.resolve() if args.evidence_dir else ROOT / "outputs" / "capability-gates" / project
    if evidence.exists() and any(evidence.iterdir()):
        raise GateError(f"Evidence directory must be empty so old qualification receipts cannot survive a failed run: {evidence}")
    evidence.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    try:
        go_build_source = archive_source(go_path, str(go_info["revision"]), build_sources / "go")
        ui_build_source = archive_source(ui_path, str(ui_info["revision"]), build_sources / "ui")
    except BaseException:
        cleanup_temp_directory(temp)
        raise
    env.update({"GO_SOURCE": str(go_build_source), "UI_SOURCE": str(ui_build_source), "UI_PORT": str(port), "BIND_ADDRESS": "127.0.0.1", "POSTGRES_PASSWORD": secrets.token_urlsafe(32), "CAPABILITY_TEMP_ROOT": str(temp), "CAPABILITY_PROJECT": project,
                "OIDC_ENABLED": "true", "RELEASE_ID": release_id, "RELEASE_PAIR_SHA256": candidate_pair_sha, "RELEASE_QUALIFICATION": qualification_status,
                "GO_SOURCE_REVISION": go_info["revision"], "UI_SOURCE_REVISION": ui_info["revision"]})
    recovery_project = "fgcap-" + uuid.uuid4().hex[:12] if args.recovery_rehearsal else ""
    recovery_port = free_port() if args.recovery_rehearsal else 0
    while args.recovery_rehearsal and recovery_port == port:
        recovery_port = free_port()
    recovery_env = dict(env)
    if args.recovery_rehearsal:
        recovery_env.update({"CAPABILITY_PROJECT": recovery_project, "UI_PORT": str(recovery_port), "POSTGRES_PASSWORD": secrets.token_urlsafe(32)})
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
        override = write_override(temp, issuer, project, candidate_pair_sha)
        compose = compose_command(temp, override, project)
    except BaseException:
        cleanup_temp_directory(temp)
        raise
    manifest: dict[str, object] = {"project": project, "uiPort": port, "goSource": go_info, "uiSource": ui_info,
                                   "releaseId": release_id, "releasePairSha256": candidate_pair_sha,
                                   "baselineReleasePairSha256": pair_sha,
                                   "qualificationStatus": qualification_status, "compatibility": compatibility,
                                   "bundleRevision": bundle_revision, "bundleWorktreeSha256": bundle_tree_sha,
                                   "browserRunner": browser_runner,
                                   "validationMode": "candidate-browser" if candidate_sources is not None else ("precommit" if args.precommit_validation else "clean-checkout"),
                                   "rulePackVersion": rule_pack, "fixtureOnly": True, "recoveryRehearsal": args.recovery_rehearsal,
                                   "evidenceDirectory": str(evidence), "phases": []}
    if candidate_sources is not None:
        manifest["candidateManifest"] = {
            "sha256": candidate_manifest_sha,
            "goRevision": candidate_sources["goRevision"],
            "uiRevision": candidate_sources["uiRevision"],
            "path": str(args.candidate_manifest.expanduser().resolve()),
            "runtimeQualification": False,
        }
    created_projects: list[tuple[list[str], dict[str, str], str, str]] = []
    recovery_image_tags: list[str] = []
    projects_cleaned = False
    temp_cleaned = False

    def phase(name: str, status: str, **details: object) -> None:
        manifest["phases"].append({"name": name, "status": status, **details})
        (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def collect_logs() -> None:
        for project_compose, project_env, _, label in created_projects:
            result = run(project_compose + ["logs", "--no-color", "--tail", "500"], check=False, env=project_env, capture=True)
            (evidence / f"compose-{label}.log").write_text((result.stdout or "")[-100_000:])

    try:
        def interrupt(signum: int, _frame: object) -> None:
            raise GateError(f"interrupted by signal {signum}; removing only projects created by this run")

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
        created_projects.append((compose, env, project, "primary"))
        if args.no_cache:
            run(compose + ["build", "--no-cache", "migrate", "api", "ui"], env=env, capture=True)
            phase("fresh-build", "passed", noCache=True, goRevision=go_info["revision"], uiRevision=ui_info["revision"])
        build_flag = ["--no-build"] if args.no_cache else ["--build"]
        run(compose + ["up", *build_flag, "--detach", "--wait", "db"], env=env)
        verify_disposable_database(compose, env)
        # Deliberately wrong credentials prove failed migration is visible and recoverable.
        failed = run(compose + ["run", "--rm", "--no-deps", "-e", "PGPASSWORD=capability-intentionally-wrong", "migrate"], check=False, env=env, capture=True)
        (evidence / "migration-failure.log").write_text((failed.stdout or "")[-20_000:])
        if failed.returncode == 0:
            raise GateError("Migration failure rehearsal unexpectedly succeeded with intentionally wrong credentials.")
        if "password authentication failed" not in (failed.stdout or "").lower():
            raise GateError(f"Migration failure did not report the expected database credential error:\n{(failed.stdout or '')[-1200:]}")
        phase("migration-connection-failure", "passed", exitCode=failed.returncode)

        build_flag = ["--no-build"] if args.no_cache else ["--build"]
        run(compose + ["up", *build_flag, "--detach", "--wait"], env=env)
        deployed_pair = inspect_deployed_pair(compose, env, {"go": str(go_info["revision"]), "ui": str(ui_info["revision"])})
        db_container_id = run(compose + ["ps", "-q", "db"], env=env, capture=True).stdout.strip()
        db_inspected = json.loads(run(["docker", "inspect", db_container_id], capture=True).stdout)[0]
        db_labels = db_inspected.get("Config", {}).get("Labels", {}) or {}
        if db_labels.get("com.docker.compose.project") != project or db_labels.get("com.docker.compose.service") != "db":
            raise GateError("The database container does not belong to this generated disposable project.")
        deployed_pair["db"] = {"containerId": db_container_id, "imageId": db_inspected.get("Image"), "postgresVersion": postgres_image_version(compose, env)}
        if not deployed_pair["db"]["imageId"]:
            raise GateError("Could not record the immutable Postgres image ID.")
        manifest["deployedImages"] = deployed_pair
        phase("deployed-source-pair", "passed", images=deployed_pair)
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
        if not any(item.get("version") == rule_pack for item in packs):
            raise GateError(f"The explicitly selected rule pack {rule_pack} is absent from the API catalog: {json.dumps(catalog)[:1200]}")
        # A real Nginx-proxied API business endpoint proves proxy routing and auth.
        request_json(base, "/api/v1/reference/canadian-rule-packs", token)
        phase("api-proxy-and-rule-catalog", "passed", selectedRulePack=rule_pack)

        person = request_json(base, "/api/v1/people", token, body={"name": "Capability Fixture", "birthDate": "1980-06", "region": "ON", "preferredCurrency": "CAD", "notes": "Disposable SLED-86 fixture"}, expected=201)
        household = request_json(base, "/api/v1/households", token, body={"name": "Capability Fixture Household", "personIds": [person["id"]], "notes": "Disposable SLED-86 fixture"}, expected=201)
        household_id, person_id = household["id"], person["id"]
        account = request_json(base, "/api/v1/accounts", token, body={"householdId": household_id, "displayName": "Fixture Chequing", "accountType": "Chequing", "defaultCurrency": "CAD", "ownerships": [{"personId": person_id, "share": "1", "effectiveStart": "2026-01-01"}]}, expected=201)
        account_id = account.get("id", account.get("account", {}).get("id"))
        if not account_id:
            raise GateError(f"Account response did not contain its ID: {account}")
        ownerships = account.get("ownerships") or []
        if len(ownerships) != 1 or ownerships[0].get("share") != "1" or ownerships[0].get("effectiveStart") != "2026-01-01":
            raise GateError(f"Account ownership share/effective start was not preserved: {ownerships}")
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
        compiled = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs", token, body={"rulePackVersion": rule_pack}, expected=201)
        compiled_content = compiled.get("content") or compiled.get("compilation", {}).get("content") or {}
        observed_compiler = {"compiledInputSchemaVersion": compiled_content.get("schemaVersion"), "compilerVersion": compiled_content.get("compilerVersion")}
        expected_compiler = {"compiledInputSchemaVersion": fixture_contract["compiledInputSchemaVersion"], "compilerVersion": fixture_contract["compilerVersion"]}
        if candidate_sources is None and observed_compiler != expected_compiler:
            raise GateError(f"Compiled-input compatibility mismatch: expected {expected_compiler}, received {observed_compiler}.")
        if candidate_sources is not None and not all(observed_compiler.values()):
            raise GateError(f"Candidate compiled-input compatibility values are incomplete: {observed_compiler}.")
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
        run_hash = run_response.get("resultHash")
        if run_response.get("status") != "completed" or not run_hash:
            raise GateError(f"The fixture Run did not complete successfully: {run_response}")
        result_hash = run_response.get("resultHash")
        if not result_hash or run_response.get("compilationHash") != compilation_hash:
            raise GateError(f"The fixture Run did not retain its compiled input and result hashes: {run_response}")
        report = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}/results", token)
        assert_fixture_report(report)
        reopened = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
        assert_run_artifact(reopened, run_id, compilation_hash, result_hash)
        comparison = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/comparisons", token, body={"rulePackVersion": rule_pack, "baseCashflowBindings": [], "alternativeCashflowBindings": [], "baseMaturityConversions": [], "alternativeMaturityConversions": [], "baseLiabilityRenewalRates": [], "alternativeLiabilityRenewalRates": [], "alternative": {"name": "Unchanged fixture comparison", "annualInflationRate": "0", "annualNominalReturnRate": "0", "growthOverrides": []}}, expected=201)
        comparison_id = comparison.get("comparisonId")
        if not comparison_id:
            raise GateError(f"Comparison response missing immutable comparison ID: {comparison}")
        reopened_comparison = request_json(base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/comparisons/{comparison_id}", token)
        if reopened_comparison.get("id") != comparison_id:
            raise GateError("Reopened scenario comparison ID does not match the saved immutable comparison.")
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
        observed_run = {"timelineCalculationVersion": run_response.get("calculationVersion"), "runArtifactVersion": run_response.get("artifactVersion")}
        expected_run = {"timelineCalculationVersion": fixture_contract["timelineCalculationVersion"], "runArtifactVersion": fixture_contract["runArtifactVersion"]}
        if candidate_sources is None and observed_run != expected_run:
            raise GateError(f"Run compatibility mismatch: expected {expected_run}, received {observed_run}.")
        if candidate_sources is not None and not all(observed_run.values()):
            raise GateError(f"Candidate Run compatibility values are incomplete: {observed_run}.")
        result_body = run_response.get("result") or {}
        report = result_body.get("report") or {}
        readiness = result_body.get("readiness") or {}
        observed_report = {"reportSchemaVersion": report.get("schemaVersion"), "reportCalculationVersion": report.get("calculationVersion"),
                           "readinessSchemaVersion": readiness.get("schemaVersion"), "readinessCalculationVersion": readiness.get("calculationVersion")}
        expected_report = {key: fixture_contract[key] for key in observed_report}
        if candidate_sources is None and observed_report != expected_report:
            raise GateError(f"Run report compatibility mismatch: expected {expected_report}, received {observed_report}.")
        if candidate_sources is not None and not all(observed_report.values()):
            raise GateError(f"Candidate report compatibility values are incomplete: {observed_report}.")
        manifest["observedCompatibility"] = {"rulePackVersion": rule_pack, **observed_compiler, **observed_run, **observed_report}
        manifest["artifacts"] = {"householdId": household_id, "personId": person_id, "accountId": account_id, "exactOwnershipShare": "1", "exactBalance": "100000.10", "snapshotId": snapshot_id, "snapshotHash": finalized_hash, "scenarioId": scenario_id, "compilationVersion": compilation_version, "compilationHash": compilation_hash, "runId": run_id, "runHash": run_hash, "secondRunId": second_run_id, "historyGoalId": goal.get("id"), "householdRunHistoryPageIds": household_history_ids, "householdRunHistoryPageCount": household_history_page_count, "scenarioRunHistoryIds": sorted(scenario_history_ids), "runResultHash": result_hash, "alternativeScenarioId": comparison_alternative, "comparisonId": comparison_id, "comparisonReopened": reopened_comparison.get("id") == comparison_id, "comparisonDeltas": deltas, "missingPackError": missing_code}
        phase("fabricated-household-compile-run-reopen-compare", "passed", artifacts=manifest["artifacts"])

        if args.borrowing_browser:
            browser_fixture = create_borrowing_browser_fixture(base, token, household_id, person_id, account_id)
            browser_fixture_path = evidence / "browser-fixture.json"
            browser_fixture_path.write_text(json.dumps(browser_fixture, indent=2, sort_keys=True) + "\n")
            local_override = write_local_auth_override(temp, issuer, "capability-runner")
            local_compose = compose_command(temp, override, project, extra_overrides=[local_override])
            inspect_config(local_compose, env)
            run(local_compose + ["up", "--no-build", "--detach", "--force-recreate", "api"], env=env, capture=True)
            wait_http(base + "/health/ready")
            request_json_local(base, "/api/v1/reference/canadian-rule-packs")
            phase("browser-fixture-seeded", "passed", fixture=browser_fixture)

            run_capability_browser(ui_path, base, browser_fixture_path, evidence, grep="@scenario")
            scenario_artifacts_path = evidence / "browser-artifacts" / "scenario" / "scenario-artifacts.json"
            try:
                scenario_artifacts = json.loads(scenario_artifacts_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise GateError(f"Real-browser Scenario test did not write valid artifacts to {scenario_artifacts_path}: {error}") from error
            if (not isinstance(scenario_artifacts.get("scenarioId"), str)
                    or not scenario_artifacts["scenarioId"].strip()
                    or type(scenario_artifacts.get("compilationVersion")) is not int
                    or scenario_artifacts["compilationVersion"] < 1):
                raise GateError(f"Real-browser Scenario test artifacts are incomplete: {scenario_artifacts}")
            scenario_run_id = scenario_artifacts.get("runId")
            if not isinstance(scenario_run_id, str) or not scenario_run_id:
                raise GateError(f"Real-browser Scenario artifacts omitted the Run identity needed for taxable funding verification: {scenario_artifacts}")
            scenario_compilation = request_json_local(
                base,
                f"/api/v1/households/{household_id}/scenarios/{scenario_artifacts['scenarioId']}/compiled-inputs/{scenario_artifacts['compilationVersion']}",
            )
            assert_taxable_funding_compilation(
                scenario_compilation,
                str(browser_fixture["accountIds"]["nonRegistered"]),
                basis_revision=int(browser_fixture["basis"]["revision"]),
                sale_cost_rate=str(browser_fixture["expected"]["saleCostRate"]),
                sale_cost_label=str(browser_fixture["expected"]["saleCostAssumptionLabel"]),
                start=str(browser_fixture["scenarioStartDate"]),
                source_start=str(browser_fixture["expected"]["retirementFundingStartDate"]),
                end=str(browser_fixture["scenarioEndDate"]),
            )
            scenario_report = request_json_local(
                base,
                f"/api/v1/households/{household_id}/scenarios/{scenario_artifacts['scenarioId']}/runs/{scenario_run_id}/results",
            )
            assert_taxable_retirement_funding_report(scenario_report, str(browser_fixture["accountIds"]["nonRegistered"]))
            phase("browser-taxable-retirement-funding-report", "passed", runId=scenario_run_id,
                  accountId=browser_fixture["accountIds"]["nonRegistered"])
            suite_artifacts = create_unfunded_contribution_suite(base, browser_fixture, scenario_artifacts)
            browser_fixture.update(suite_artifacts)
            browser_fixture_path.write_text(json.dumps(browser_fixture, indent=2, sort_keys=True) + "\n")
            phase("browser-scenario-compile-run", "passed", artifacts=scenario_artifacts)

            run_capability_browser(ui_path, base, browser_fixture_path, evidence, grep="@suite")
            suite_identity = suite_artifacts["suite"]
            suite_candidate_detail = request_json_local(
                base,
                f"/api/v1/households/{household_id}/scenarios/{suite_identity['scenarioId']}/stress-suites/{suite_identity['suiteId']}/candidates/{suite_identity['successCandidateId']}",
            )
            assert_taxable_retirement_funding_suite(suite_candidate_detail)
            phase("browser-taxable-retirement-funding-suite-report", "passed",
                  candidateId=suite_identity["successCandidateId"])
            manifest["browserFixture"] = browser_fixture
            phase("browser-suite-failure-and-reopen", "passed", artifacts=suite_artifacts,
                  browserArtifacts=str((evidence / "browser-artifacts" / "suite").resolve()))

            run(compose + ["up", "--no-build", "--detach", "--force-recreate", "api"], env=env, capture=True)
            wait_http(base + "/health/ready")
            wait_http(base + "/api/v1/reference/canadian-rule-packs", headers=headers)
            phase("oidc-mode-restored", "passed")

        # A recovery rehearsal captures a consistent backup before introducing an incompatible schema marker.
        max_version = database_schema_version(compose, env)
        if max_version != schema_version_expected:
            raise GateError(f"Database schema mismatch: expected migration {schema_version_expected}, observed {max_version}.")
        schema_migrations = migration_versions(compose, env)
        if args.recovery_rehearsal:
            verify_disposable_database(compose, env)
            run(compose + ["stop", "api"], env=env, capture=True)
            backup_path = evidence / "fabricated-database.dump"
            pg_dump_to_file(compose, env, backup_path)
            backup_record = {
                "format": "PostgreSQL custom archive",
                "location": backup_path.resolve().as_uri(),
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                "sizeBytes": backup_path.stat().st_size,
                "releaseId": pair["releaseId"],
                "releasePairSha256": pair_sha,
                "sources": {"go": {"revision": go_info["revision"]}, "ui": {"revision": ui_info["revision"]}},
                "schemaVersion": max_version,
                "schemaMigrations": schema_migrations,
                "postgresVersion": deployed_pair["db"]["postgresVersion"],
                "images": {name: value["imageId"] for name, value in deployed_pair.items()},
                "artifacts": {"householdId": household_id, "snapshotId": snapshot_id, "snapshotHash": finalized_hash,
                              "scenarioId": scenario_id, "compilationVersion": compilation_version, "compilationHash": compilation_hash,
                              "runId": run_id, "runHash": run_hash},
            }
            (evidence / "backup-reference.json").write_text(json.dumps(backup_record, indent=2) + "\n")
            release.read_backup_reference(str(evidence / "backup-reference.json"), {
                "releaseId": pair["releaseId"], "databaseSchemaVersion": max_version,
                "releasePairSha256": pair_sha,
                "sources": {"go": {"revision": go_info["revision"]}, "ui": {"revision": ui_info["revision"]}},
            })
            manifest["backup"] = backup_record
            phase("consistent-backup-created", "passed", backup=backup_record)
            run(compose + ["start", "api"], env=env, capture=True)
            readiness = run(compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
            deadline = time.monotonic() + TIMEOUT
            while readiness.returncode and time.monotonic() < deadline:
                time.sleep(1)
                readiness = run(compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
            if readiness.returncode:
                raise GateError("API did not return ready after the backup was captured.")
        sentinel = max_version + 1000
        verify_disposable_database(compose, env)
        run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-c", f"INSERT INTO schema_migrations(version) VALUES ({sentinel})"], env=env, capture=True)
        mismatch = run(compose + ["run", "--rm", "--no-deps", "migrate"], check=False, env=env, capture=True)
        (evidence / "migration-version-failure.log").write_text((mismatch.stdout or "")[-20_000:])
        if mismatch.returncode == 0 or "schema version does not match" not in (mismatch.stdout or ""):
            raise GateError("Future schema marker did not produce the expected actionable migration incompatibility failure.")
        ready_probe = run(compose + ["exec", "-T", "ui", "wget", "-S", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=env, capture=True)
        if ready_probe.returncode == 0 or "503" not in (ready_probe.stdout or ""):
            raise GateError(f"API readiness did not fail for the future schema marker as expected: {(ready_probe.stdout or '')[-1200:]}")
        if args.recovery_rehearsal:
            phase("migration-version-incompatibility", "passed", insertedVersion=sentinel, databaseSchemaVersion=max_version)
            images = {name: value["imageId"] for name, value in deployed_pair.items()}
            image_references, recovery_image_tags = tag_recovery_images(images, recovery_project)
            recovery_override = write_recovery_override(temp, image_references)
            recovery_compose = compose_command(temp, override, recovery_project, recovery_override)
            inspect_config(recovery_compose, recovery_env)
            project_resources_are_empty(recovery_compose, recovery_env, recovery_project)
            created_projects.append((recovery_compose, recovery_env, recovery_project, "recovery"))
            manifest["recoveryProject"] = {"name": recovery_project, "uiPort": recovery_port, "restoresFrom": project}
            run(recovery_compose + ["up", "--no-build", "--detach", "--wait", "db"], env=recovery_env, capture=True)
            verify_disposable_database(recovery_compose, recovery_env)
            existing_database = run(recovery_compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "postgres", "-Atc", "SELECT 1 FROM pg_database WHERE datname='financegame'"], capture=True, env=recovery_env).stdout.strip()
            if existing_database:
                raise GateError("Recovery database name already exists; refusing to restore into a non-fresh database.")
            run(recovery_compose + ["exec", "-T", "db", "createdb", "--template=template0", "--owner=financegame", "--maintenance-db=postgres", "--username=financegame", "financegame"], env=recovery_env, capture=True)
            blank_database = run(recovery_compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-Atc", "SELECT to_regclass('public.schema_migrations')"], capture=True, env=recovery_env).stdout.strip()
            if blank_database:
                raise GateError("New recovery database contains a schema migration table before restore.")
            backup_path = evidence / "fabricated-database.dump"
            backup_record = manifest.get("backup", {})
            actual_sha = hashlib.sha256(backup_path.read_bytes()).hexdigest()
            if actual_sha != backup_record.get("sha256") or backup_path.stat().st_size != backup_record.get("sizeBytes"):
                raise GateError("Database backup archive checksum or length changed before restore.")
            pg_restore_file(recovery_compose, recovery_env, backup_path)
            restored_schema = database_schema_version(recovery_compose, recovery_env)
            restored_migrations = migration_versions(recovery_compose, recovery_env)
            if restored_schema != max_version or restored_migrations != schema_migrations:
                raise GateError(f"Restored migration state differs from the backup: expected {schema_migrations}, received {restored_migrations}.")
            run(recovery_compose + ["run", "--rm", "--no-deps", "migrate"], env=recovery_env, capture=True)
            run(recovery_compose + ["up", "--no-build", "--detach", "--wait"], env=recovery_env, capture=True)
            recovery_pair = inspect_deployed_pair(recovery_compose, recovery_env, {"go": str(go_info["revision"]), "ui": str(ui_info["revision"])})
            recovery_db_id = run(recovery_compose + ["ps", "-q", "db"], env=recovery_env, capture=True).stdout.strip()
            recovery_db = json.loads(run(["docker", "inspect", recovery_db_id], capture=True).stdout)[0]
            recovery_pair["db"] = {"containerId": recovery_db_id, "imageId": recovery_db.get("Image"), "postgresVersion": postgres_image_version(recovery_compose, recovery_env)}
            for service in ("db", "migrate", "api", "ui"):
                if recovery_pair.get(service, {}).get("imageId") != deployed_pair.get(service, {}).get("imageId"):
                    raise GateError(f"Recovery service {service} is not using the same captured image ID as the source deployment.")
            recovery_base = f"http://127.0.0.1:{recovery_port}"
            wait_http(recovery_base + "/health/live")
            wait_http(recovery_base + "/households")
            restored_snapshot = request_json(recovery_base, f"/api/v1/households/{household_id}/snapshots/{snapshot_id}", token)
            restored_compilation = request_json(recovery_base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/compiled-inputs/{compilation_version}", token)
            restored_run = request_json(recovery_base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
            restored_snapshot_hash = restored_snapshot.get("finalized", {}).get("contentHash")
            if restored_snapshot_hash != finalized_hash:
                raise GateError(f"Finalized Snapshot hash changed after restore: expected {finalized_hash}, received {restored_snapshot_hash!r}.")
            if restored_compilation.get("contentHash") != compilation_hash:
                raise GateError("Compilation hash changed after restore.")
            if restored_run.get("id") != run_id or restored_run.get("resultHash") != run_hash:
                raise GateError(f"Immutable Run identity or result hash changed after restore: expected {run_id}/{run_hash}, received {restored_run.get('id')}/{restored_run.get('resultHash')}.")
            if restored_schema != compatibility["databaseSchemaVersion"] or migration_versions(recovery_compose, recovery_env) != schema_migrations:
                raise GateError("Restored database does not match the release's expected migration set.")
            manifest["recoveredImages"] = recovery_pair
            phase("backup-restored-to-matching-release", "passed", backupSha256=actual_sha, databaseSchemaVersion=restored_schema,
                  schemaMigrations=restored_migrations, snapshotHash=restored_snapshot_hash, compilationHash=restored_compilation.get("contentHash"),
                  runId=restored_run.get("id"), runHash=restored_run.get("resultHash"), images=recovery_pair)
            # Keep the canonical qualification phase consumed by release_pair.load;
            # recovery mode has now recovered from the migration-version failure
            # by restoring the matching backup into a matching release project.
            phase("migration-version-failure-recovery", "passed", insertedVersion=sentinel,
                  backupSha256=actual_sha, recoveredProject=recovery_project)
            offline_compose, offline_env, offline_base = recovery_compose, recovery_env, recovery_base
        else:
            # The ordinary capability gate removes only the sentinel it inserted, then retries migration.
            verify_disposable_database(compose, env)
            run(compose + ["exec", "-T", "db", "psql", "-U", "financegame", "-d", "financegame", "-c", f"DELETE FROM schema_migrations WHERE version = {sentinel}"], env=env, capture=True)
            run(compose + ["run", "--rm", "--no-deps", "migrate"], env=env, capture=True)
            phase("migration-version-failure-recovery", "passed", insertedAndRemovedVersion=sentinel)
            offline_compose, offline_env, offline_base = compose, env, base

        # Stop only a generated project's API and verify Nginx stays live with structured 503.
        run(offline_compose + ["stop", "api"], env=offline_env, capture=True)
        wait_http(offline_base + "/health/live")
        status, body, _ = wait_http(offline_base + "/api/v1", expected=503, headers=headers, timeout=30)
        unavailable = json.loads(body).get("error", {}).get("code")
        if unavailable != "api_unavailable":
            raise GateError(f"API outage was not returned as structured api_unavailable: HTTP {status} {body[:500]!r}")
        run(offline_compose + ["start", "api"], env=offline_env, capture=True)
        ready_after_recovery = run(offline_compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=offline_env, capture=True)
        deadline = time.monotonic() + TIMEOUT
        while ready_after_recovery.returncode and time.monotonic() < deadline:
            time.sleep(1)
            ready_after_recovery = run(offline_compose + ["exec", "-T", "ui", "wget", "-q", "-O", "/dev/null", "http://api:8080/health/ready"], check=False, env=offline_env, capture=True)
        if ready_after_recovery.returncode:
            raise GateError("API readiness did not recover after removing the inserted schema marker and restarting the API.")
        wait_http(offline_base + "/api/v1/reference/canadian-rule-packs", headers=headers, timeout=TIMEOUT)
        recovered_run = request_json(offline_base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}", token)
        assert_run_artifact(recovered_run, run_id, compilation_hash, result_hash)
        assert_fixture_report(request_json(offline_base, f"/api/v1/households/{household_id}/scenarios/{scenario_id}/runs/{run_id}/results", token))
        phase("api-offline-and-recovery", "passed", outageCode=unavailable)
        collect_logs()
        if args.keep:
            phase("complete", "passed", cleanup="kept", fixtureDirectory=str(temp), projects=[item[2] for item in created_projects])
        else:
            for project_compose, project_env, project_name, label in created_projects:
                cleanup_project(project_compose, project_env, project_name, evidence, label)
            projects_cleaned = True
            remove_recovery_image_tags(recovery_image_tags)
            cleanup_temp_directory(temp)
            temp_cleaned = True
            phase("complete", "passed", cleanup="removed", projects=[item[2] for item in created_projects])
        print(f"Capability gate passed. Evidence: {evidence}")
        if args.precommit_validation:
            current_status, current_tree_sha = bundle_worktree_fingerprint()
            if current_status != bundle_dirty or current_tree_sha != bundle_tree_sha:
                raise GateError("Bundle candidate changed during precommit validation; evidence does not match the final worktree.")
            manifest["candidateWorktreeUnchanged"] = True
            (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if not args.keep and args.no_cache and not args.precommit_validation and candidate_sources is None:
            receipt = {
                "status": "runtime-qualified", "releaseId": pair["releaseId"], "releasePairSha256": pair_sha,
                "sources": {"go": go_info["revision"], "ui": ui_info["revision"]},
                "compatibility": compatibility, "observedCompatibility": manifest.get("observedCompatibility"),
                "databaseSchemaVersion": max_version, "deployedImages": manifest.get("deployedImages"),
                "bundleRevision": bundle_revision,
                "completedPhases": [item["name"] for item in manifest["phases"] if item.get("status") == "passed"],
                "evidenceDirectory": str(evidence), "completedAt": int(time.time()), "cleanup": "removed",
            }
            (evidence / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
            manifest["qualificationReceipt"] = str(evidence / "qualification.json")
            (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return 0
    except Exception as error:
        phase("failure", "failed", error=str(error))
        collect_logs()
        print(f"Capability gate failed: {error}\nEvidence: {evidence}", file=sys.stderr)
        return 1
    finally:
        if created_projects and not args.keep and not projects_cleaned:
            cleanup_succeeded = True
            for project_compose, project_env, project_name, label in created_projects:
                try:
                    cleanup_project(project_compose, project_env, project_name, evidence, label)
                except Exception as error:
                    cleanup_succeeded = False
                    print(f"Capability gate cleanup failed for {project_name}: {error}", file=sys.stderr)
            projects_cleaned = cleanup_succeeded
            if projects_cleaned and recovery_image_tags:
                try:
                    remove_recovery_image_tags(recovery_image_tags)
                except Exception as error:
                    print(f"Recovery image-tag cleanup failed: {error}", file=sys.stderr)
        if args.keep:
            print(f"Kept disposable projects {[item[2] for item in created_projects]} and fixture files at {temp} for inspection.")
        elif (not created_projects or projects_cleaned) and not temp_cleaned:
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
