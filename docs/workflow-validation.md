# Workflow validation and stress evidence

Review date: 2 October 2026. Companion: [readiness review](product-readiness-review.md), [system diagrams](architecture.md), [Go review](../../finance_game_go/docs/product-readiness-review.md), [UI review](../../finance_game_ui/docs/product-readiness-review.md).

> **Historical snapshot:** Measurements and stack observations below were recorded on 2 October 2026 and do not describe the later release gate and recovery rehearsal. SLED-86 and SLED-88 added those checks; see the current manifest and deployment runbook for their evidence. Keep the original observations for comparison and traceability.

## Evidence levels

- **Live isolated stack:** actual local-source Docker images, fresh Postgres, migrations and HTTP operations.
- **Integration/module:** executable Go services/storage using fabricated fixtures; no claim of full browser integration.
- **Mocked browser:** actual React rendering/accessibility with intercepted fabricated API responses.
- **Source/proposed:** code-inspected limitation or a future acceptance case; not executed success.

All figures below are review observations. Single-machine timings and viewport emulation do not establish production capacity or real-phone performance.

## Executed checks

| Check | Result | Evidence boundary |
| --- | --- | --- |
| Bundle `docker compose config --quiet` | Pass | YAML resolution; not service connectivity |
| Exact local-source image build | Pass | UI/API/migrate images built |
| Isolated bundle `up --wait` | **Fail** | DB healthy, migration exit 0, API started; UI repeatedly exits |
| UI Nginx logs | **Invalid listener** | `invalid port in "127.0.0.1:${UI_PORT}"`; bundle supplies no container variables |
| Actual API authoring/finalization | Pass through Scenario creation | Fresh fabricated records, direct private-bridge API request; UI proxy is unavailable |
| Actual rule list/compile | **Blocked** | `rulePacks: []`; valid canonical version returns 404 `rule_pack_not_found` |
| Go deterministic suite / race / vet | Pass | Existing public module and HTTP tests |
| Go Postgres integrations | Pass | New disposable `financegame_review_test` DB; all Justfile targets plus Snapshot package |
| UI unit suite | 73 tests pass | 15 files, mocked transport |
| UI accessibility browser suite | 4 tests pass | Fixture-backed desktop/narrow checks; cached Chromium override |
| UI production build | Pass | JS 635.45 kB raw / 164.05 kB gzip; CSS 67.66 / 11.70 kB gzip |
| Independent documentation review | No remaining material findings after corrections | Source, links, diagrams and recorded evidence checked; does not replace runtime acceptance |

UI browser execution used `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/home/iwilson/.cache/ms-playwright/chromium-1234/chrome-linux/chrome`; its default headless-shell revision was absent. This environment recovery produced passing checks. It does not fix the live Nginx failure.

## Real HTTP journey

The review used Compose project `finance-readiness-review-20261002`, sibling build contexts, UI host port 33017 and a new project-specific named volume. Existing application containers/databases were not used. Direct API access used the isolated bridge IP; no host API/DB ports were published.

| Step | Observed result |
| --- | --- |
| List Canadian rule packs | HTTP 200 with empty array |
| Invalid birth date 1970-02-30 | HTTP 422 with field error |
| Create fabricated Ontario Person | HTTP 201 |
| Create Household and explicit membership | HTTP 201 |
| Create Chequing with Person share `"1"` | HTTP 201 |
| Record balance `"100000.10"` with user-reported provenance | HTTP 201, exact text retained |
| Select that fact revision into dated Snapshot | HTTP 201, inclusion shown |
| Finalize Snapshot | HTTP 200, stable finalized content and 10,000,010 cents |
| Attempt replacement after finalization using returned version | HTTP 409 `stale_snapshot_version`; original baseline remains finalized |
| Create 40-year Scenario from finalized version | HTTP 201 |
| Compile using `canadian-planning-rules-v1` | HTTP 404 `rule_pack_not_found` |
| 200 Snapshot-list requests with 20 threads | 200 HTTP 200; p50 7.831 ms, p95 16.229 ms, maximum 19.928 ms |

The finalized-edit response establishes rejection and preservation, not that its error code is ideal for the consumer. The read load measures a tiny local dataset, not projections, persistence under write load or authentication. Complete Run/compare/reopen was not executed on the shipped API because rule loading blocks it.

Raw fabricated response evidence: [isolated-api-results-2026-10-02.json](review-artifacts/isolated-api-results-2026-10-02.json). The [HTTP probe](review-artifacts/isolated-api-probe.py) writes fabricated records only when explicitly pointed at a disposable test API. It reports observations; it does not silently turn known failures into a passing release gate.

## Engine and browser stress

| Probe | Result | Meaning |
| --- | --- | --- |
| Go: 1,200 monthly periods × 100 ordinary Cashflows | 120,000 movements; 43,291,801 bytes timeline JSON; 1.414 s calculation under race instrumentation | Detail can exceed 41 MiB before report/storage overhead |
| Go: 200 independent 24-month projections, 8 workers, same frozen input | 4.728 s total under race; identical output hashes | Deterministic concurrent pure calculation in this fixture |
| Go: Jan31-only monthly expense of $1,000; cash $250 | Cash -$750, net worth +$670; full monthly amount applied | Positive wealth is insufficient evidence of funding; partial-month schedule convention is visible |
| Go: same schema-2 compilation, $1,000 salary | First-period $1,670 versus timeline first month $2,670 | First-period query omits bound Cashflows |
| UI: 720 periods × 20 positions × 20 movements | 5,853,264-byte mocked report; 193,065 mounted elements, 28,800 detail rows despite collapsed periods | Collapsing detail does not bound DOM construction |
| UI render: desktop 1280 × 900 / narrow 375 × 812 | 3,804 / 3,618 ms, no CPU throttling | Diagnostic local render samples, not mobile hardware measurements |
| UI: first period opening | 161 / 176 ms | Interaction cost after large report mounting |
| UI: narrow Run page | Summary begins y=1,107.9 px after provenance | Retirement answer is buried below implementation evidence |
| UI: clear local/session storage | Run direct URL still works; Scenario discovery disappears | Persistence exists but history discovery depends on browser |

Go fixture sources and overlay reproduction appear in the [backend review](../../finance_game_go/docs/product-readiness-review.md#reproduce-the-observations). Browser fixture script: [retirement-workflow-stress.mjs](../../finance_game_ui/docs/review-artifacts/retirement-workflow-stress.mjs). Preserve these as diagnostics until permanent regression/benchmark checks land in their owning issues.

## Household workflow stress matrix

| Household story | Existing evidence | Required acceptance before claiming success |
| --- | --- | --- |
| Solo Canadian creates first plan | Live API through Scenario; mocked UI steps | Entire browser-to-Go journey through host UI port and covered production pack |
| Two adults retire in different years | Existing Go capability fixture manually stops/starts Cashflows | Explicit retirement choices drive correct dated transitions and unchanged historic Run |
| Early retirement with CPP/OAS delayed | Estimate records exist; no compile integration | Freeze sourced estimates, timing and bridge funding; no duplicate benefit flows |
| Cash-poor homeowner | Negative-cash pure probe | Liquid funding outcome fails despite positive household net worth |
| Plan spans Dec31/Jan1 registered contributions | Existing room/binding tests | Project future room with provenance; no invented future observed facts |
| RRSP maturity and RRIF withdrawals | Explicit event fixtures | Covered statutory minimum policy or prominent unsupported/manual policy |
| Person tax and cash settlement | Opt-in tests; explicit Cashflow settlement path | Include taxes in affordability exactly once; withholding/refund timing explicit |
| Mortgage renewal shock | Backend explicit renewal support | Single/compare authoring parity and adverse rate alternative |
| Same input viewed through first-period and timeline | Reproduced disagreement | Exact balances/movements/identity agree |
| Compare a survivor/tax/maturity plan | Individual module fixtures; source shows omitted comparison options | Full frozen choice parity; unchanged alternative yields no financial delta |
| Edit facts after finalization or completed Run | Unit/integration immutable artifact checks; live edit rejection | UI explains new draft/version; old hashes and results remain readable |
| Run retry after timeout / double click | Persistence idempotency tests | No duplicate permanent outcome; recover result ID when client loses response |
| Comparison fails after alternative is saved | Existing failure tests retain partial IDs | Reload/reopen surviving artifacts and intentionally retry without lost context |
| Family helper handles parent's plan | Planning memberships implemented | Helper identity/consent/roles/revocation separate from Person ownership |
| Two users with unrelated families | No hosted authorization module | Consistent isolation on every list/object/report and cache clear |
| New device or cleared browser history | Browser confirms lost discovery | Server listing and URL-addressable comparison library |
| Leave an unsaved Scenario or switch family | Source-only local form state | Explicit draft save/discard/recover without cross-family restoration |
| 60/100-year plan, many facts and candidate Runs | Pure and mocked-browser diagnostic stress | Bounded payload, service budget/cancellation, DB/browser measurements |
| Bad returns immediately after retirement, inflation shock | Manual growth windows, deterministic flat inflation | Explicit time-varying paths/assumptions, clear shortfall and repeatable sensitivity |
| Unknown province/year/rule or US plan | Fail-closed rule design; CA/CAD validation | Actionable unsupported status; no silent Canadian fallback |
| Upgrade with migration failure | Migration succeeded in fresh review stack only | Rehearse failed migration and backup restore with matching release versions |

Unexecuted rows are future gates, not a list of tests that passed.

## Repeat safely with fabricated data

From the bundle repo, choose a fresh, unique review project name. The commands below use local sibling contexts to avoid testing unrelated remote revisions:

```sh
review_project="finance-review-$(date +%s)-$$"
GO_SOURCE=../finance_game_go UI_SOURCE=../finance_game_ui UI_PORT=33017 \
  rtk proxy docker compose -p "$review_project" up --build --detach --wait --wait-timeout 30
rtk proxy docker compose -p "$review_project" ps -a
rtk proxy docker compose -p "$review_project" logs --tail 30 ui migrate
```

The current startup is expected to fail at UI. Read the API IP from this project's API container; on Linux the Docker bridge is reachable from the host. If bridge access is unavailable, use a temporary probe container on this project's network rather than publishing the normal API/DB:

```sh
rtk proxy docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  "${review_project}-api-1"
rtk proxy python3 docs/review-artifacts/isolated-api-probe.py \
  --base-url http://REPLACE_WITH_REVIEW_API_IP:8080 --disposable-fixtures
```

Do not point the writer at an existing household database. Use a new database containing `test` for Go integration fixtures as required by `docs/testing.md`. Repeat browser stress from the UI repo with a local Vite server and the documented Chromium path; the script intercepts API responses and writes artifacts to a temp directory.

After collecting evidence, remove **only the named project created above**, including its exclusively fabricated review volume:

```sh
rtk proxy docker compose -p "$review_project" down --volumes
```

Do not run unqualified `down --volumes` against the deployed bundle. Review cleanup removed the isolated project, review volume and test database; existing application containers were retained.
