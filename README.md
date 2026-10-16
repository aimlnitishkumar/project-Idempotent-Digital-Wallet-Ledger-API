# Idempotent Digital Wallet & Ledger API

> **Long-form project guide for developers, operators, reviewers, and viva
> examiners.** This document describes the implementation currently in this
> repository. It separates working features from future production work, and
> includes setup instructions, request examples, data-model notes, testing
> guidance, operational considerations, and a demonstration walkthrough.

**Project type:** Django REST Framework API and server-rendered operations
dashboard  
**Primary database:** PostgreSQL  
**Idempotency coordination:** Redis  
**Runtime:** Python 3.10+ compatible Django dependencies (the requirements file
allows Django 5 through 6.x)  
**Current scope:** two-wallet transfers, a small dashboard, and a viva demo
runner; not a complete consumer wallet product or regulated payment platform.

---

## Document map

1. [At a glance](#1-at-a-glance)
2. [The problem and project idea](#2-the-problem-and-project-idea)
3. [Goals, scope, and non-goals](#3-goals-scope-and-non-goals)
4. [Feature status](#4-feature-status)
5. [System architecture](#5-system-architecture)
6. [Repository layout](#6-repository-layout)
7. [Technology stack](#7-technology-stack)
8. [Prerequisites](#8-prerequisites)
9. [Configuration](#9-configuration)
10. [Local setup](#10-local-setup)
11. [Starting the services](#11-starting-the-services)
12. [Database initialization](#12-database-initialization)
13. [Starting Django](#13-starting-django)
14. [Dashboard guide](#14-dashboard-guide)
15. [API endpoint reference](#15-api-endpoint-reference)
16. [Transfer request validation](#16-transfer-request-validation)
17. [Transfer request examples](#17-transfer-request-examples)
18. [Idempotency contract](#18-idempotency-contract)
19. [Transfer lifecycle](#19-transfer-lifecycle)
20. [Concurrency and locking](#20-concurrency-and-locking)
21. [Double-entry accounting](#21-double-entry-accounting)
22. [Data model: Wallet](#22-data-model-wallet)
23. [Data model: LedgerTransaction](#23-data-model-ledgertransaction)
24. [Data model: LedgerEntry](#24-data-model-ledgerentry)
25. [Data model: IdempotencyRecord](#25-data-model-idempotencyrecord)
26. [Database invariants](#26-database-invariants)
27. [Dashboard summary response](#27-dashboard-summary-response)
28. [Demo runner](#28-demo-runner)
29. [Running the viva demonstration](#29-running-the-viva-demonstration)
30. [Understanding the demo scenarios](#30-understanding-the-demo-scenarios)
31. [Testing and validation](#31-testing-and-validation)
32. [Troubleshooting](#32-troubleshooting)
33. [Security and privacy](#33-security-and-privacy)
34. [Reliability and failure handling](#34-reliability-and-failure-handling)
35. [Observability](#35-observability)
36. [Deployment considerations](#36-deployment-considerations)
37. [Scaling considerations](#37-scaling-considerations)
38. [Data lifecycle and recovery](#38-data-lifecycle-and-recovery)
39. [Accessibility and browser support](#39-accessibility-and-browser-support)
40. [API extension guidelines](#40-api-extension-guidelines)
41. [Frontend extension guidelines](#41-frontend-extension-guidelines)
42. [Known limitations](#42-known-limitations)
43. [Recommended roadmap](#43-recommended-roadmap)
44. [Glossary](#44-glossary)
45. [Viva questions and answers](#45-viva-questions-and-answers)
46. [Review checklist](#46-review-checklist)
47. [Environment variable reference](#47-environment-variable-reference)
48. [Quick command reference](#48-quick-command-reference)
49. [Example end-to-end demonstration](#49-example-end-to-end-demonstration)
50. [Final project summary](#50-final-project-summary)

> **About page count:** This is intentionally a substantial, sectioned
> reference. A rendered README has no fixed page size; font, margins, and paper
> size determine the printed count. Use the table of contents to navigate.

---

## 1. At a glance

This repository contains a backend for moving funds between two existing
wallets. Its central engineering concern is preserving a correct balance when
multiple requests arrive at once or when a client retries after a timeout.

A successful transfer updates both wallet balances within one database
transaction and records two ledger legs: one debit and one equal credit. Wallet
rows are locked in stable UUID order while the balances are checked and
updated. An `Idempotency-Key` header identifies a logical request so a retry
can return the previously stored HTTP result instead of performing another
transfer.

The application is built with Django and Django REST Framework. PostgreSQL
stores wallet and ledger state; Redis provides an atomic, short-lived lock for
requests that are processing. A Django template dashboard displays current
wallets, debit and credit aggregates, recent posted transfers, and transfer
controls. `scripts/demo_runner.py` is designed to exercise the transfer
endpoint over HTTP and inspect the database for a viva demonstration.

This is a learning and demonstration project. It does not currently implement
user registration, authentication, wallet creation APIs, deposits,
withdrawals, payment rails, exchange rates, or a production compliance
workflow.

---

## 2. The problem and project idea

### 2.1 Why a wallet needs more than a balance column

A simplistic wallet might store a single number and subtract from it whenever
money is spent. That is inadequate for financial software. It does not explain
why the balance changed, it can allow race conditions if two requests read the
same starting balance, and it cannot reliably distinguish a client retry from
a new operation.

The project demonstrates several complementary controls:

1. **Exact decimal arithmetic** for money, rather than binary floating point.
2. **An atomic transaction** so the debit and credit either both happen or
   neither happens.
3. **Pessimistic row locking** so concurrent operations see serialized wallet
   balance changes.
4. **A ledger** that records the debit and credit used to explain each
   successful movement.
5. **Idempotency** so a repeat request with the same key and payload replays a
   stored response instead of charging twice.

These controls answer different questions. A database transaction protects
atomicity; row locks protect read-modify-write consistency; the ledger
provides an audit trail; idempotency protects client retries. None should be
treated as a substitute for the others.

### 2.2 A simple example

Suppose Alice has USD 100.0000 and Bob has USD 20.0000. Alice sends Bob USD
30.0000. After a successful operation:

| Wallet | Before | Movement | After |
|---|---:|---:|---:|
| Alice | 100.0000 | debit 30.0000 | 70.0000 |
| Bob | 20.0000 | credit 30.0000 | 50.0000 |

The total value across those two wallets remains USD 120.0000. The ledger has
two equal legs for this operation. If the client does not receive the first
response and retries with the same idempotency key, it should receive the
original result and not create another debit.

---

## 3. Goals, scope, and non-goals

### 3.1 Current goals

- Demonstrate an atomic wallet-to-wallet transfer in Django.
- Reject zero or negative transfer amounts and transfers between the same
  wallet.
- Prevent a wallet balance from becoming negative through the service's
  concurrency checks and a database constraint.
- Keep source and destination currencies consistent for a transfer.
- Record paired debit and credit rows for successful transfers.
- Require an idempotency key on POST/PUT requests decorated with the
  idempotency helper.
- Show current data through a browser dashboard.
- Make it possible to run a scripted viva demonstration against a running
  local API and database.

### 3.2 Explicit non-goals

The repository is not a bank, a payment processor, or a ready-to-deploy
financial institution platform. It does not currently provide:

- user identity verification, accounts, login, or API authentication;
- wallet provisioning or ownership authorization endpoints;
- funding, cash-out, settlement, or payment-provider integration;
- foreign exchange, fees, reversals, refunds, or chargebacks;
- multiple ledger accounts per transaction or configurable accounting rules;
- a formally versioned public API specification;
- production secrets management, monitoring, alerting, or disaster recovery;
- an administrative web application for managing wallets.

The dashboard can issue transfer requests, but it is not an authorization
boundary. Anyone who can reach an unauthenticated development server may be
able to invoke the API. Do not expose it to an untrusted network.

---

## 4. Feature status

The status below reflects the source currently in this repository, not an
aspirational architecture document.

| Capability | Status | Notes |
|---|---|---|
| Wallet database model | Implemented | UUID IDs, owner UUID, currency, Decimal balance, active flag |
| Ledger transaction model | Implemented | Has status, description, idempotency key, timestamp |
| Ledger entry model | Implemented | Debit/credit, amount, balance after; updates rejected in model `save()` |
| Atomic transfer service | Implemented | `transaction.atomic()` and `select_for_update()` |
| Ordered wallet locks | Implemented | IDs are sorted before querying and rows ordered by ID |
| Transfer HTTP endpoint | Implemented | `POST /api/v1/wallets/transfer/` |
| Request serializer | Implemented | Validates IDs, decimal amount, description, distinct wallets |
| Idempotency decorator | Implemented | Redis lock and persisted response record |
| Dashboard route | Implemented | `GET /` renders a same-origin HTML template |
| Dashboard summary API | Implemented | `GET /api/v1/dashboard/summary/` |
| Viva runner | Implemented | `scripts/demo_runner.py`; requires running API and services |
| Automated repository tests | Not present | Django test discovery previously found no tests |
| Wallet creation endpoint | Not implemented | Wallets must be provisioned separately |
| Authentication/authorization | Not implemented | API views are currently open by default |
| Replay timestamp field | Not implemented | Dashboard/runner can only observe replay time locally |
| CORS setup | Not present | Same-origin dashboard does not need CORS |

The service is designed around PostgreSQL row locks. SQLite or a database
backend that does not provide equivalent locking semantics is not an adequate
substitute for demonstrating the intended concurrency behavior.

---

## 5. System architecture

```text
Browser
  ├── GET /                         Django dashboard template
  ├── GET /api/v1/dashboard/summary/  Wallets, ledger totals, recent transactions
  └── POST /api/v1/wallets/transfer/  Transfer request + Idempotency-Key
                                          │
                                          ├── DRF TransferView
                                          ├── TransferRequestSerializer
                                          ├── idempotent_operation decorator
                                          │     ├── Redis lock
                                          │     └── PostgreSQL response record
                                          └── TransferService
                                                ├── transaction.atomic()
                                                ├── sorted SELECT FOR UPDATE
                                                ├── wallet balance updates
                                                └── LedgerTransaction + two LedgerEntry rows
```

### 5.1 Layers

- **URL routing** maps paths to Django views.
- **The dashboard view** renders HTML; it does not perform transfers itself.
- **The dashboard summary view** serializes selected database data for the
  browser.
- **The transfer view** validates input and translates service exceptions into
  HTTP statuses.
- **The serializer** is the boundary for request shape and simple input
  validation.
- **The service** implements transfer rules and database writes.
- **Models** define persisted state, relationships, and a portion of the
  database invariants.
- **The idempotency helper** wraps a request and stores or replays a response.

The API and dashboard are on the same Django origin. This avoids needing a
separate CORS configuration for the included browser interface.

---

## 6. Repository layout

```text
.
├── apps/
│   ├── common/
│   │   └── idempotency.py
│   └── ledger/
│       ├── migrations/
│       │   └── 0001_initial.py
│       ├── services/
│       │   └── transfer_service.py
│       ├── templates/
│       │   └── ledger/
│       │       └── dashboard.html
│       ├── models.py
│       ├── serializers.py
│       ├── urls.py
│       └── views.py
├── config/
│   ├── asgi.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── scripts/
│   └── demo_runner.py
├── docker-compose.yml
├── manage.py
└── requirements.txt
```

`apps.ledger` contains the money-related models, validation, transfer service,
HTTP endpoints, and HTML dashboard template. `apps.common` currently provides
the idempotency decorator. `config` is the Django project package. The
migration in `apps/ledger/migrations` creates the persisted schema. The demo
script seeds and exercises a controlled pair of wallets.

The repository may contain local environment files or virtual environments
that are not part of the application source. Do not commit secret values,
database data, caches, or a local Python environment.

---

## 7. Technology stack

### 7.1 Backend

- **Python** provides the runtime and exact `Decimal` arithmetic.
- **Django** supplies configuration, ORM, migrations, URL dispatch, templates,
  and database transaction management.
- **Django REST Framework (DRF)** provides API views, request parsing,
  serializers, and JSON responses.
- **PostgreSQL** is the intended durable datastore and supports
  `SELECT ... FOR UPDATE` row locking.
- **Redis** coordinates in-flight idempotent requests using atomic set-if-absent
  semantics with an expiry.
- **django-environ** reads configuration from environment variables and the
  project `.env` file.
- **django-redis** connects Django's cache configuration to Redis.
- **requests** is used by the live demonstration runner.

### 7.2 Dashboard

The dashboard is a Django template with plain JavaScript and CSS. Tailwind CSS,
Lucide icons, and web fonts are loaded from public CDNs. Therefore the page can
be served by Django without a Node.js build, but the external styling and icon
assets may not load in an offline or restricted environment.

### 7.3 Version range

`requirements.txt` specifies version ranges rather than a fully locked
dependency set. The Django range is `>=5.0,<7.0`, and DRF is `>=3.15,<4.0`.
For repeatable deployments, create and maintain a lock/constraints file and
test a specific Python/Django/PostgreSQL combination.

---

## 8. Prerequisites

For a local development run, install:

1. A currently supported Python installation compatible with the package set.
2. Docker Desktop with Docker Compose, or separately managed PostgreSQL and
   Redis services.
3. Git, if cloning this repository.
4. An HTTP client such as `curl`, PowerShell `Invoke-RestMethod`, or Postman.
5. Internet access if the dashboard should load its CDN assets.

The application configuration requires `SECRET_KEY` to be supplied by the
environment or a project `.env` file. The settings module intentionally raises
`ImproperlyConfigured` when it is missing.

The local Compose file publishes PostgreSQL on host port `5433` and Redis on
host port `6379`. The Django default database URL uses port `5432`. This is a
configuration distinction to resolve deliberately: when Django runs on the
host and connects to the Compose-published database, set the database port to
`5433`. When Django itself runs inside a Compose network, the database
hostname/port should target the Compose service, usually `postgres:5432`.

---

## 9. Configuration

### 9.1 Configuration sources

`config/settings.py` reads `.env` from the repository root and then accesses
settings from the process environment. Environment variables are the normal
deployment mechanism. The `.env` file is suitable for local development only
and must not contain credentials that are shared or checked into source
control.

### 9.2 Required and useful variables

| Name | Required? | Purpose |
|---|---:|---|
| `SECRET_KEY` | Yes | Django secret key; settings fail if it is empty |
| `DEBUG` | No | Enables/disables Django debug behavior; defaults to false |
| `ALLOWED_HOSTS` | Deployment-specific | Host names accepted by Django; defaults to an empty list |
| `DATABASE_URL` | Recommended | PostgreSQL connection URL |
| `DB_HOST` | Optional override | Replaces host parsed from `DATABASE_URL` |
| `DB_PORT` | Optional override | Replaces port parsed from `DATABASE_URL` |
| `REDIS_URL` | Optional | Redis cache URL; defaults to local Redis DB 1 |

Example local `.env` shape (replace all values with your own; do not reuse
example secrets in a real deployment):

```dotenv
SECRET_KEY=replace-with-a-long-random-development-secret
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost
DATABASE_URL=postgres://postgres:replace-me@127.0.0.1:5433/wallet_db
REDIS_URL=redis://127.0.0.1:6379/1
```

The project’s Compose file currently provides service credentials through its
container environment configuration. Prefer injecting secrets from a local
ignored environment file, secret manager, or orchestrator rather than
hardcoding passwords in Compose for shared or production environments.

### 9.3 Host and container networking

Use `127.0.0.1:5433` when the Django process runs on the host and connects
through the published Compose port. Use the Compose service hostname and
container port from another container on the same Docker network. `localhost`
inside a Django container means that Django container itself, not PostgreSQL.

---

## 10. Local setup

From the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create or edit the root `.env` file with a local `SECRET_KEY`, host, database,
and Redis settings. Keep it out of version control. If PowerShell blocks
activation, invoke the environment's Python executable directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py check
```

On macOS or Linux, the equivalent virtual-environment commands use
`source .venv/bin/activate`. Use the project's selected Python interpreter
consistently for installation, migrations, checks, server execution, and the
demo runner.

The repository may already include a local `myenv` on a developer machine.
That folder is machine-specific; a fresh contributor should create their own
virtual environment rather than depend on it.

---

## 11. Starting the services

Start PostgreSQL and Redis from the repository root:

```powershell
docker compose up -d postgres redis
docker compose ps
```

The Compose configuration maps PostgreSQL's container port 5432 to host port
5433 and Redis port 6379 to the same host port. Verify that the database URL
used by Django points at the mapped host port. If Docker reports that a port
is already occupied, inspect the process using it or change the port mapping
and corresponding Django configuration consistently.

To view service logs:

```powershell
docker compose logs --tail 100 postgres
docker compose logs --tail 100 redis
```

To stop the local services:

```powershell
docker compose stop postgres redis
```

Stopping preserves the named PostgreSQL volume. Removing a container is not
the same as deleting the database volume. Be careful with commands that remove
volumes: a volume deletion destroys local database data. Never run a volume
prune or Compose down-with-volumes command unless you intend to delete that
data.

---

## 12. Database initialization

After configuring the database connection, apply Django migrations:

```powershell
python manage.py migrate
```

Inspect migration state:

```powershell
python manage.py showmigrations
python manage.py makemigrations --check --dry-run
```

`migrate` creates the project tables described by the initial ledger migration
as well as Django's built-in auth/admin/session tables. `makemigrations
--check --dry-run` checks whether model changes would require an additional
migration; it does not prove that the configured database is reachable or that
its migration history is consistent if connection validation times out.

The current application does not expose a wallet creation endpoint. To use the
transfer API, create at least two wallet rows through a controlled seed script
or Django shell, or run the demo runner, which inserts its fixed demo wallets
after confirming the transfer route is reachable.

For production, do not manually edit the schema in the database. Make model
changes, generate reviewed migrations, apply them through a deployment step,
and keep backup/rollback plans appropriate for the change.

---

## 13. Starting Django

Run the development server on the address used by the viva demo:

```powershell
python manage.py runserver 127.0.0.1:8000
```

The dashboard is available at:

```text
http://127.0.0.1:8000/
```

The server is Django's development server. It is suitable for local
development and demonstrations, not for deployment behind a public Internet
endpoint. For deployment, use an appropriate production WSGI/ASGI server,
reverse proxy, TLS termination, secure settings, and process supervision.

At startup, Django imports the configured URL modules and views. A missing
template or import error appears as a server error; run `manage.py check` and
inspect server logs before testing browser behavior. The transfer endpoint
depends on PostgreSQL and Redis being accessible when a POST is submitted.

---

## 14. Dashboard guide

### 14.1 Main display

Open `/` in the browser. The header checks the dashboard summary endpoint and
shows whether the Django API is connected. The summary cards compare aggregate
debits and credits from ledger entries. A mismatch is shown as an invariant
failure; a matching total is shown as zero-sum verified.

The wallet cards show a friendly owner label, currency, cached balance,
wallet UUID, active flag, and the last updated time. Wallet names Alice and Bob
are assigned when the owner's ID matches the deterministic UUIDs used by
`scripts/demo_runner.py`. Other wallets receive a fallback label based on the
wallet ID.

### 14.2 Transfer controls

The source and destination dropdowns are populated from current wallet data.
The form accepts a positive decimal amount with at most four decimal places,
an idempotency key, and an optional description. The key is initially
generated using `crypto.randomUUID()`. Regenerate creates a new key. The
"Keep same key after success" option preserves it after completion; otherwise
the browser rotates the key after a successful operation.

The network retry action sends two sequential requests with the exact same
key and payload. The race action submits ten concurrent requests with distinct
keys. It is deliberately a live state-changing simulation; it is not a
read-only preview.

### 14.3 Audit and examiner panels

The audit panel displays up to 25 recent database transactions and their
paired entries, when present. Browser-session execution details are shown
separately and include HTTP status and measured request latency. The examiner
drawer displays the latest request and response captured in the current page
session, and explains the idempotency and row-locking strategy.

### 14.4 Refresh behavior

The wallet and aggregate data refreshes at a ten-second interval and can also
be refreshed manually. A failed or timed-out summary request sets the API
indicator offline and leaves the previously displayed data visible. The
browser request uses a same-origin URL; no CORS configuration is required for
this page.

---

## 15. API endpoint reference

### `POST /api/v1/wallets/transfer/`

Transfers funds between two existing wallets.

Required request header:

```http
Idempotency-Key: <unique-key-for-this-logical-operation>
Content-Type: application/json
```

Request body:

```json
{
  "source_wallet_id": "source-wallet-uuid",
  "dest_wallet_id": "destination-wallet-uuid",
  "amount": "30.0000",
  "description": "Optional transfer memo"
}
```

Successful response: HTTP `201 Created` with a JSON object containing
`status`, `transaction_id`, `amount`, and `description`.

The idempotency wrapper may return a stored response status on replay. The
current wrapper stores the status code produced by the first decorated
request, so a replay is not guaranteed to use HTTP 200; the existing view's
first successful response is 201 and the replay uses that stored code.

### `GET /api/v1/dashboard/summary/`

Returns wallets, total debit and credit aggregates, and up to 25 recent
transactions with associated debit and credit information. It is designed for
the included dashboard, not as a complete wallet-history API.

### `GET /`

Returns the dashboard HTML page.

No wallet listing, wallet creation, deposit, withdrawal, or transaction detail
endpoint is currently implemented.

---

## 16. Transfer request validation

The `TransferRequestSerializer` expects:

| Field | Type | Required | Rules |
|---|---|---:|---|
| `source_wallet_id` | UUID | Yes | Must parse as a UUID |
| `dest_wallet_id` | UUID | Yes | Must parse as a UUID and differ from source |
| `amount` | Decimal string/number | Yes | Up to 18 digits, 4 decimal places, minimum 0.0001 |
| `description` | String | No | At most 255 characters; blank is allowed; default is empty |

The serializer rejects a source equal to destination. The service independently
checks that the IDs differ and that the amount is greater than zero. This
defense-in-depth is useful because service methods can be called outside HTTP.

The serializer's amount minimum permits a one ten-thousandth unit transfer.
The decimal precision is four fractional digits. Values that exceed the
maximum precision or range are rejected during serializer validation and
produce HTTP 400.

The transfer service also checks wallet existence, currency compatibility,
and sufficient funds. Its current logic does not check `Wallet.is_active`, so
an inactive wallet is not presently prevented from participating by the
service. Production wallet policy should enforce active status at the service
layer and have tests for it.

---

## 17. Transfer request examples

### 17.1 PowerShell example

```powershell
$body = @{
  source_wallet_id = "replace-with-source-wallet-uuid"
  dest_wallet_id   = "replace-with-destination-wallet-uuid"
  amount           = "30.0000"
  description      = "Example wallet transfer"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/wallets/transfer/" `
  -Method Post `
  -Headers @{ "Idempotency-Key" = "example-transfer-001" } `
  -ContentType "application/json" `
  -Body $body
```

Use real wallet UUIDs from the database or dashboard. Do not use the same
idempotency key for a distinct operation. For a retry of the same logical
operation, reuse the original key and identical serialized request body.

### 17.2 curl example

```bash
curl --include \
  --request POST \
  --header "Content-Type: application/json" \
  --header "Idempotency-Key: example-transfer-001" \
  --data '{
    "source_wallet_id": "replace-with-source-wallet-uuid",
    "dest_wallet_id": "replace-with-destination-wallet-uuid",
    "amount": "30.0000",
    "description": "Example wallet transfer"
  }' \
  http://127.0.0.1:8000/api/v1/wallets/transfer/
```

The example is intended for Bash-compatible shells. In PowerShell, use
`Invoke-RestMethod` or adjust quoting for the installed curl executable.

---

## 18. Idempotency contract

### 18.1 What idempotency means here

An idempotent request is a logical operation identified by an
`Idempotency-Key`. A client that times out after the server completed a
transfer can retry using the same key and the same body. If a matching
persisted record exists, the server returns the stored response rather than
calling the view again.

The decorator hashes the raw request body with SHA-256. Reuse of a known key
with a different body is rejected with HTTP 422. A missing key or key longer
than 255 characters is rejected with HTTP 400. If Redis is unavailable, the
helper returns HTTP 503. If another request currently owns the Redis lock for
that key, the helper returns HTTP 409.

### 18.2 Persistence and lock details

The Redis lock key is derived from the request's key and expires after 120
seconds. Acquisition uses `SET` with `NX` and `EX`. Release uses a Lua script
that deletes the lock only if its stored value still matches the request hash.
Completed HTTP response details are persisted in the `idempotency_records`
table.

### 18.3 Practical limitations

- The hash covers raw bytes. JSON objects with equivalent meaning but
  different serialization can produce different hashes.
- The key is globally unique in the response-record table, not scoped by
  user, route, or tenant.
- The stored request path is not currently compared during replay.
- Decorated error responses are persisted too, because the wrapper saves any
  DRF `Response` returned by the view.
- Redis locking and PostgreSQL response persistence are not a single atomic
  distributed transaction. Process crashes between ledger commit and response
  record insertion require careful recovery design.
- The view accepts a stored response status on replay; successful replay uses
  the original success status rather than always changing to 200.

These limitations should be addressed before treating the helper as a
general-purpose production idempotency system.

---

## 19. Transfer lifecycle

For a valid `POST /api/v1/wallets/transfer/`:

1. Django resolves the URL to `TransferView`.
2. The idempotency wrapper checks method and reads `Idempotency-Key`.
3. The wrapper computes a SHA-256 digest from the raw request body.
4. Redis attempts to acquire the per-key processing lock.
5. The database is checked for an existing idempotency response.
6. The view validates the JSON body with `TransferRequestSerializer`.
7. The view calls `TransferService.transfer_funds`.
8. The service rejects same-wallet/nonpositive values, then begins an atomic
   database transaction.
9. The service locks both wallet rows in sorted UUID order and loads them.
10. It checks wallet existence, matching currency, and source funds.
11. It updates both balances, inserts the posted transaction, and bulk-inserts
    one debit and one credit entry.
12. The database transaction commits.
13. The response is serialized and saved to the idempotency record table.
14. The Redis lock is released if still owned by this request.

If an exception occurs inside `transaction.atomic()`, Django rolls the
transaction back. The view maps expected service exceptions to HTTP responses.
Unexpected view-level exceptions are logged and returned with HTTP 500 and a
detail string. Because the wrapper stores returned responses, an expected
failure response may be replayed for the same key even if conditions later
change.

---

## 20. Concurrency and locking

### 20.1 Why concurrent balance updates are dangerous

Assume a wallet has 10 units and two requests each attempt to spend 8. If both
requests read 10 before either writes, each may conclude that funds are
sufficient. A naive read-modify-write implementation can overspend.

### 20.2 Current locking approach

`TransferService` uses `transaction.atomic()` together with
`Wallet.objects.select_for_update()`. Before acquiring locks, it sorts source
and destination UUIDs and orders the query by ID. If all code paths follow the
same lock order, two transfers involving the same pair are less likely to
deadlock due to opposite lock acquisition order.

Once a transaction owns the row lock, another transfer that needs the same
wallet rows waits until commit or rollback. It then reads the updated balance
and checks available funds. The wallet's database check constraint provides a
last line of defense against a negative stored balance.

### 20.3 Important caveats

The behavior depends on a database backend that supports row-level
`SELECT FOR UPDATE` locks and on transactions being used correctly. PostgreSQL
is the intended backend. A development demo using SQLite is not evidence that
this locking strategy works in production. The API's 400 response for
insufficient funds is produced only when the view maps
`InsufficientFundsException`; current code does so.

The concurrency control shown here protects this transfer service path. Any
new code that writes wallet balances must use the same consistency policy.

---

## 21. Double-entry accounting

A ledger transaction groups individual ledger entries. A transfer between two
wallets is represented by:

| Wallet | Entry type | Amount | Meaning |
|---|---|---:|---|
| Source | `DEBIT` | Positive decimal | Value leaves the source |
| Destination | `CREDIT` | Same positive decimal | Value enters the destination |

The entries use a positive `amount` field; the direction is represented by
`entry_type`. Each entry records `balance_after`, the wallet balance after
that leg. `LedgerEntry` is intended to be immutable: its `save()` method
rejects saving an already-persisted instance.

The dashboard compares global aggregate debit and credit totals. This is a
useful demonstration invariant, but it does not prove every transaction is
balanced individually, nor does it verify an external bank balance. The
summary endpoint currently returns the 25 latest transactions and the
aggregate across all entries.

For a robust accounting system, ledger immutability should also be enforced at
the database permission/trigger level; bulk updates bypass a model's
`save()` method. Additional constraints or a transaction-level audit process
should guarantee every posted transaction has balanced entries.

---

## 22. Data model: Wallet

`Wallet` represents a currency-specific stored balance:

- `id`: UUID primary key generated by Django.
- `owner_id`: UUID identifying the owner; it is not a foreign key to an
  identity model in the current project.
- `currency`: three-character string; defaults to `USD`.
- `balance`: decimal with up to 18 total digits and 4 fractional digits;
  defaults to zero.
- `is_active`: boolean, defaults to true.
- `created_at` and `updated_at`: timestamps managed by Django.

The model has a database check constraint named
`non_negative_balance`, and an index over `owner_id` and `currency`. The
balance is a cached aggregate for fast display and transfer decisions. Ledger
entries are intended to provide the explanation for movements.

There is no wallet HTTP resource for listing, creating, suspending, or
deactivating wallets. The dashboard summary returns all wallets available to
the connected database; it is not filtered by authenticated owner because
authentication and tenant scoping are not implemented.

---

## 23. Data model: LedgerTransaction

`LedgerTransaction` is the operation that groups ledger entries. Fields:

- `id`: UUID primary key.
- `idempotency_key`: up to 255 characters, unique and indexed.
- `status`: `PENDING`, `POSTED`, or `FAILED`, with `PENDING` as the model
  default.
- `description`: text field.
- `created_at`: creation timestamp.

The transfer service creates its transaction with status `POSTED` after the
wallet checks and balance updates are ready inside the database transaction.
It does not currently create a separate pending record before attempting the
transfer, nor does it persist failed transfer attempts as
`LedgerTransaction` rows. An unsuccessful attempt may still have an
`IdempotencyRecord` if the wrapped view returns a response.

The transaction's idempotency key is also unique in this table. The
idempotency record table stores HTTP replay information separately.

---

## 24. Data model: LedgerEntry

`LedgerEntry` contains:

- `id`: UUID primary key.
- `transaction`: protected foreign key to `LedgerTransaction`.
- `wallet`: protected foreign key to `Wallet`.
- `entry_type`: either `DEBIT` or `CREDIT`.
- `amount`: decimal with 18 total digits and 4 decimal places.
- `balance_after`: decimal snapshot of the wallet after the entry.

The database requires `amount > 0` through the
`positive_ledger_entry_amount` check constraint. Foreign-key deletion is
protected to avoid deleting referenced wallets or transactions. The model's
`save()` method prevents updates to already-saved entries, while the transfer
service creates entries using `bulk_create()`.

`bulk_create()` is efficient but bypasses model `save()` hooks and signals.
The current code is safe for insertion because it constructs new entries; it
does mean the Python `save()` override is not a complete immutability
boundary. Production-grade append-only behavior must be enforced in the
database and tested against all write paths.

---

## 25. Data model: IdempotencyRecord

`IdempotencyRecord` stores information needed to replay a response:

- `key`: primary key up to 255 characters.
- `request_path`: path of the original request.
- `request_hash`: SHA-256 hex digest of the raw request body.
- `response_code`: original HTTP status.
- `response_body`: JSON response body.
- `created_at`: record creation timestamp.

The model's explicit database table name is `idempotency_records`. Do not
assume its table name follows the default Django app/model naming convention.
The idempotency helper queries this model by key after acquiring the Redis
lock. On a matching request hash it constructs a response with the stored
body and status. On a hash mismatch, it returns an HTTP 422 response.

There is no explicit replay counter, response hash, or replay timestamp
column. A UI can measure the time it observed a replay, but that timestamp is
local to the client and should not be described as a database-stored replay
time.

---

## 26. Database invariants

### 26.1 Constraints present

- Wallet balance must be greater than or equal to zero.
- Ledger entry amount must be greater than zero.
- Ledger transaction idempotency key is unique.
- Idempotency record key is the table's primary key.
- Ledger rows protect referenced wallets and transactions from deletion.

### 26.2 Invariants primarily implemented in application logic

- Source and destination wallets must differ.
- Transfer amount must be positive.
- Both wallets must exist.
- Both wallets must use the same currency.
- Source wallet must have enough funds.
- Debit and credit entries use an equal amount.
- Wallet IDs are locked in consistent sorted order.

### 26.3 Recommended database hardening

If database-level assurance is required, add a transaction grouping rule that
enforces balanced posted entries, protect append-only tables with database
roles or triggers, and audit all balance changes. The current schema does not
have a deferred constraint that verifies each transaction's debit total
equals its credit total.

---

## 27. Dashboard summary response

`GET /api/v1/dashboard/summary/` returns a JSON object shaped like:

```json
{
  "wallets": [
    {
      "id": "wallet-uuid",
      "owner": "Alice",
      "balance": "70.0000",
      "currency": "USD",
      "is_active": true,
      "updated_at": "2026-01-01T12:00:00+00:00"
    }
  ],
  "ledger": {
    "debits": "30.0000",
    "credits": "30.0000"
  },
  "transactions": [
    {
      "id": "transaction-uuid",
      "timestamp": "2026-01-01T12:00:00+00:00",
      "status": "POSTED",
      "idempotency_key": "example-transfer-001",
      "description": "Example transfer",
      "amount": "30.0000",
      "source": "Alice",
      "destination": "Bob",
      "debit": "30.0000",
      "credit": "30.0000"
    }
  ]
}
```

Decimal quantities are returned as strings to avoid introducing binary
floating-point rounding in JSON clients. The endpoint selects all wallets,
aggregates all ledger entries, and returns up to 25 most recent ledger
transactions. It prefetches transaction entries to avoid one extra database
query per transaction.

The response can contain missing `source`, `destination`, or amount values
when a transaction does not have the expected debit/credit legs. The UI should
display such records as incomplete rather than interpreting them as balanced.

---

## 28. Demo runner

The `scripts/demo_runner.py` script is a terminal demonstration harness. It
uses real HTTP POSTs to `http://127.0.0.1:8000/api/v1/wallets/transfer/` and
uses Django ORM access to seed wallets and inspect ledger rows. It imports
the Django settings and runs `django.setup()` after checking that the HTTP
server and transfer route respond.

The script uses deterministic wallet UUIDs and deterministic owner UUIDs so
that the dashboard can label the demo accounts Alice and Bob. It resets data
for its known demo key set and checks for unrelated ledger entries before
deleting or recreating demo wallet data. This is a safety check, not a
substitute for a disposable demonstration database.

The runner demonstrates:

1. Initial state with Alice USD 100.0000 and Bob USD 20.0000.
2. A USD 30.0000 transfer.
3. Replaying the same body with `DEMO-TX-001`.
4. Reusing that key with a changed amount.
5. Twenty parallel USD 10.0000 transfers from Alice.
6. A global SQL sum comparing all ledger debits and credits.

The `requests` package is declared in `requirements.txt`. The script requires
the PostgreSQL schema, Redis, server route, and valid environment setup. It
does not run with the server absent or if the transfer endpoint returns 404.

---

## 29. Running the viva demonstration

Prepare the environment and database first:

```powershell
docker compose up -d postgres redis
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open a second terminal in the repository root and run:

```powershell
python .\scripts\demo_runner.py
```

The runner will not call the API or reset demo data if the server is
unreachable. It also probes the transfer path before proceeding. Keep the
server terminal visible if the examiner should see Django request logs. The
browser dashboard at `/` can be opened in parallel to show the balances and
ledger totals.

The demonstration is state-changing. It deletes and reseeds its fixed demo
wallets and known idempotency key records after its pre-flight checks. Run it
only against a local/disposable database where this action is acceptable. It
also assumes the HTTP server and the script process use the same database
configuration, otherwise the script's ORM checks may query a different
database from the API.

---

## 30. Understanding the demo scenarios

### Scenario 1: transfer

Alice starts at USD 100.0000 and Bob at USD 20.0000. The API should return a
successful response, after which the script checks for USD 70.0000 and USD
50.0000. It also confirms two ledger entries exist for the transfer key.

### Scenario 2: retry

The same payload and key are posted again. The script expects the stored
response and unchanged wallet balances. It prints a response hash computed
locally from the cached response body, the stored request-body hash, the
idempotency record creation time, and the runner-observed replay time.

### Scenario 3: payload drift

The same key is sent with a different amount. The helper should detect the
body hash difference and respond with 422 (or an in-flight 409 under a race).
The script expects the key-reuse mismatch result.

### Scenario 4: competing spends

Twenty unique keys each request USD 10.0000 from Alice's USD 70.0000. Under
the configured endpoint and service semantics, seven should succeed and
thirteen should fail for insufficient funds. The script checks Alice ends at
zero and Bob at USD 120.0000.

### Scenario 5: zero-sum audit

The script sums all entries in the ledger table and compares total debits
with total credits. Since this is a global query, unrelated unbalanced data
would also cause the demonstration to fail.

---

## 31. Testing and validation

### 31.1 Available commands

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

`manage.py check` validates Django configuration and model setup. The
migration check detects model changes that are not represented by a
migration, subject to database connectivity warnings. `manage.py test` runs
Django's test discovery.

At the time this guide was written, repository test discovery found zero
tests. Therefore successful system checks do not establish correctness of
transfer rollback, idempotency races, exception mapping, or concurrency. Add
automated tests before relying on these behaviors outside a demonstration.

### 31.2 Recommended test layers

1. Serializer unit tests for valid/invalid UUIDs, precision, and same-wallet
   rejection.
2. Service tests for insufficient funds, missing wallet, currency mismatch,
   valid paired entries, and rollback.
3. API tests for HTTP status and payload shapes, missing keys, and error
   handling.
4. Idempotency tests with Redis behavior covered, including retries,
   mismatch, lock contention, and Redis failure.
5. PostgreSQL integration tests for `select_for_update()` and the race
   scenario.
6. Browser tests for dashboard summary display, disabled buttons during work,
   network failure, and accessibility.

Use a separate test database. Never point stress tests or reset scripts at a
shared or production database.

---

## 32. Troubleshooting

### Django says `SECRET_KEY` is missing

Set `SECRET_KEY` in the process environment or root `.env`. Confirm the
environment file is at the repository root and the server is started from
this project.

### PostgreSQL connection timeout

Check that Compose is running, confirm the port mapping, and compare the
configured database host and port with where Django is running. Host-run
Django should normally connect through port 5433 for the supplied Compose
mapping. Container-run Django should use the service name and port 5432.

### Redis service is unavailable

The idempotency decorator returns 503 if it cannot acquire a Redis lock.
Check `REDIS_URL`, ensure Redis is running, and inspect Redis container logs.
The dashboard summary can still load without Redis, but transfer POSTs
decorated with the helper cannot safely proceed.

### Transfer returns 404

Check that the running process uses this checkout and current code. Confirm
`config/urls.py` includes the ledger URL configuration and that the URL is
exactly `/api/v1/wallets/transfer/`. Restart the development server after
changing URL modules if autoreload is disabled.

### Transfer returns 400

Inspect the response JSON. Common causes include missing or invalid request
fields, same source and destination, amount outside the allowed decimal
range, insufficient source funds, or currency mismatch.

### Transfer returns 409

The same idempotency key may currently be processing. Wait for completion and
retry with the same payload and key. For a new business operation use a new
key.

### Dashboard displays no wallets

The dashboard does not create wallets. Run the demo runner against a
disposable database, or create wallets through a controlled seed process.

### Dashboard styles or icons do not load

The template references public CDNs. Check network access, browser extension
blocking, and content security policy. The page structure and JavaScript are
served by Django, but CDN failure may reduce styling/icons.

---

## 33. Security and privacy

### 33.1 Current posture

The current API has no configured authentication or authorization. The
dashboard summary exposes every wallet, recent transaction data, and
idempotency keys available in the configured database. The transfer endpoint
accepts wallet UUIDs without checking whether a caller owns either wallet.
This is suitable only for a controlled local environment.

The dashboard sends a CSRF token in the transfer request, but CSRF protection
is not a substitute for authentication, authorization, or abuse protection.
The API is not currently protected by a user identity layer.

### 33.2 Before any shared deployment

- Add authentication and per-wallet authorization.
- Enforce tenant isolation in every query.
- Require TLS outside localhost.
- Set `DEBUG=False`, use a strong secret from a secret manager, and configure
  `ALLOWED_HOSTS`.
- Remove hardcoded credentials and rotate any credentials that may have been
  exposed.
- Rate-limit requests and cap request sizes.
- Avoid returning raw internal exception details to clients.
- Protect logs and dashboard output from sensitive data exposure.
- Establish audit retention and access controls.
- Review CSRF, CORS, security headers, cookie, and session policies.

The dashboard uses `innerHTML` to render values received from its own API and
includes an HTML-escaping helper for interpolated text. Continue treating
database fields as untrusted when expanding the page.

---

## 34. Reliability and failure handling

The transfer endpoint uses database transactions so partial wallet updates
should roll back if an error occurs before commit. Expected domain failures
are translated to structured HTTP responses. Redis connection errors are
logged and produce HTTP 503. Unexpected errors in `TransferView` are logged
and produce HTTP 500 with a detail string.

The idempotency wrapper's Redis lock has a 120-second expiry. This avoids
permanent stale locks after process failure, but it does not by itself prove
exactly-once execution across every crash point. For example, the business
transaction may commit before the response record has been saved. A retry in
that window requires a reconciliation strategy; a unique transaction key may
cause a database error rather than a replay.

The dashboard uses an eight-second timeout for summary reads and a
fifteen-second timeout for transfers. A timeout does not prove that the server
did not commit the transfer. The safe user action is to refresh server state
and retry the same logical request with the same idempotency key and identical
body—not to create a new key immediately.

---

## 35. Observability

Current observability is basic:

- Django logs incoming requests when run with `runserver`.
- The idempotency module logs Redis acquire and release failures.
- The transfer view logs unexpected exceptions.
- The dashboard displays live API reachability, balance data, aggregate
  debit/credit totals, and request latency measured in the browser.
- The demo runner prints response statuses and direct database audit results.

The system does not currently define structured JSON logs, request IDs,
metrics export, tracing, alerting, health/readiness probes, or durable
per-request latency records. The dashboard execution monitor is a
browser-session view; it is not a server-side HTTP log.

For production, correlate API, database, and Redis operations using a
request/correlation ID. Record outcome and duration metrics without logging
credentials, raw payment secrets, or unnecessary personal data.

---

## 36. Deployment considerations

Do not deploy Django's development server to production. A production
deployment should include:

- an application server configured for Django's WSGI or ASGI entry point;
- PostgreSQL with backups, connection limits, monitoring, and TLS where
  appropriate;
- Redis with availability, persistence policy, authentication, and network
  isolation appropriate to its role;
- environment-based configuration and managed secrets;
- `DEBUG=False`, explicit allowed hosts, secure cookies, and HTTPS;
- static/template asset strategy and an appropriate content security policy;
- health checks and graceful shutdown;
- deployment-managed migrations with a reviewed rollback plan;
- alerting for database, Redis, request error, and invariant failures.

The provided Compose file is a local supporting-services configuration. It
does not compose the Django application, reverse proxy, TLS, or production
secrets infrastructure. The dashboard relies on CDNs; for locked-down
deployments, vendor assets locally and serve them under a reviewed CSP.

---

## 37. Scaling considerations

### 37.1 Database bottlenecks

Transfers touching the same wallet serialize on that wallet's row lock. This
is expected for correctness, but a very hot wallet may limit throughput.
Measure lock waits and transaction duration before changing the design.
Keeping the locked section short is important.

### 37.2 Redis and idempotency

The configured pool allows up to 50 Redis connections. Production capacity,
timeouts, key prefixing, and Redis topology should be deliberately sized.
The current global idempotency keys can collide across tenants or endpoints;
scope keys by authenticated principal and operation before multi-tenant use.

### 37.3 Read paths

The dashboard summary reads all wallets and computes global ledger aggregates.
For a small demo this is straightforward. At larger scale, paginate or scope
wallets and history, consider indexed/reporting aggregates, and avoid loading
unbounded result sets.

### 37.4 Ledger growth

Ledger entries are append-oriented and grow continuously. Define partitioning,
archival, retention, backup, and audit query strategies before volume becomes
large. Financial records should not be casually deleted to keep the table
small.

---

## 38. Data lifecycle and recovery

Wallet balances are mutable cached values, while ledger entries are intended
to be the historical explanation for changes. A recovery procedure should be
able to recompute balances from a trusted opening balance and ledger events,
or reconcile them with an independently controlled accounting record.

Current code does not implement balance reconstruction, reconciliation,
backup automation, point-in-time recovery, or administrative corrections.
Do not directly update a production balance to "fix" a discrepancy without a
documented and auditable process. Corrections should be represented as
explicit ledger operations with authorization and traceability.

The local Postgres database uses a named Docker volume. A local volume is
convenient for persistence between container restarts, but it is not a backup.
Back up important data separately and test restore procedures.

The demo runner intentionally resets only known demo data after safety checks.
That reset logic is suitable for the demonstration environment only. Never
adapt it into a generic database cleanup command.

---

## 39. Accessibility and browser support

The dashboard uses semantic headings, buttons, labels, status text, a
keyboard-dismissable inspector drawer, and an `aria-live` toast region. It has
responsive layouts and honors the reduced-motion preference for CSS
transitions. The inspector uses dialog semantics.

Further accessibility work should include focus trapping and focus return for
the modal drawer, stronger visible keyboard focus states, screen-reader
announcements for balance refreshes and request results, contrast audits, and
tests with keyboard-only navigation and assistive technologies.

The template uses modern browser APIs including `crypto.randomUUID`,
`AbortSignal.timeout`, `fetch`, and Clipboard API with a fallback. Older
browsers may not implement all these features. If broader compatibility is
required, add feature detection or supported polyfills and test the browser
matrix explicitly.

---

## 40. API extension guidelines

When adding a new operation:

1. Define and document its request/response contract.
2. Validate primitive request fields in a serializer.
3. Place money and ledger rules in a service, not in a template or view.
4. Use exact decimals, never binary floats.
5. Wrap related database changes in `transaction.atomic()`.
6. Acquire wallet/account locks in deterministic order.
7. Add required database constraints and migrations.
8. Define idempotency semantics for retries and failure results.
9. Map known domain exceptions to intentional HTTP statuses.
10. Add unit, API, database, and concurrency tests.
11. Update this README and any API schema.
12. Verify both a fresh database migration and an upgrade path.

Avoid having views directly update wallet balances. Avoid adding a second,
inconsistent transfer path that bypasses the service. A good API extension
keeps all balance-changing behavior reviewable in one explicit business
service.

---

## 41. Frontend extension guidelines

The current dashboard is intentionally same-origin. Keep browser requests
relative to the Django host unless there is a deliberate architecture change.
If the frontend is moved to another origin, configure CORS and CSRF
intentionally; do not solve cross-origin errors by disabling security checks.

For interactive money flows:

- preserve decimal text as a string through JSON serialization;
- validate UX constraints but treat the server as authoritative;
- disable duplicate form submissions while an operation is in flight;
- show server error details in a safe, readable way;
- treat timeouts as ambiguous outcomes and preserve the idempotency key;
- refresh balances from the server after completed requests;
- label live versus browser-session-only data clearly;
- do not infer durable transaction state solely from an optimistic UI row.

The current page uses a simple script block rather than a bundler. As the
frontend grows, consider extracting versioned JavaScript and CSS assets,
adding linting and browser tests, and pinning/vendoring third-party assets.

---

## 42. Known limitations

These items are important when interpreting a demo:

1. No authentication, authorization, or owner isolation is configured.
2. Wallet provisioning is outside the HTTP API.
3. `Wallet.is_active` is not checked by the transfer service.
4. Ledger immutability is enforced by the model `save()` override, but not
   comprehensively at the database layer; bulk updates can bypass it.
5. The idempotency hash is based on raw body bytes and the key is global.
6. A stored response records the original HTTP status and body; replay may
   return 201 rather than 200.
7. A returned failure response can be stored and replayed for that key.
8. Redis and database writes are not coordinated as one distributed atomic
   transaction.
9. The dashboard exposes all wallets and recent activity to any reachable
   client because authorization is not present.
10. The dashboard history shows at most 25 persisted transactions. HTTP
    status/latency for persisted rows is not part of the transaction model.
11. The aggregate zero-sum indicator compares global totals, not every
    transaction individually.
12. Automated tests are not currently included in the repository.
13. The demo requires real PostgreSQL and Redis service behavior for its
    concurrency claims.
14. Dashboard styling/icons/fonts rely on external CDNs.
15. A development server is not a production deployment.

These points are improvement opportunities, not reasons to misrepresent the
implemented behavior.

---

## 43. Recommended roadmap

### Phase 1: correctness and confidence

- Add tests for serializers, service exceptions, rollback, and response
  shapes.
- Add PostgreSQL concurrency tests.
- Test the Redis lock and persisted replay under contention and failures.
- Validate inactive wallets, decimal edge cases, and ledger balance per
  transaction.

### Phase 2: safe API usage

- Add authentication, wallet ownership authorization, and tenant scoping.
- Add wallet creation/list/detail and transaction history endpoints.
- Define idempotency key scope and a canonical payload hash.
- Handle crash windows between committed transfer and response persistence.
- Avoid leaking internal exception details in HTTP 500 responses.

### Phase 3: accounting hardening

- Enforce append-only ledger permissions or triggers at the database layer.
- Add per-transaction balancing guarantees and reconciliation tools.
- Add explicit correction/reversal operations.
- Define audit retention and compliant backup/restore processes.

### Phase 4: operations

- Add structured logs, metrics, traces, and health/readiness checks.
- Use a production server and managed Postgres/Redis.
- Establish secret rotation, TLS, rate limiting, and an incident response
  playbook.
- Pin dependencies and run CI against supported Python and database versions.

### Phase 5: dashboard maturity

- Add pagination and filters for ledger history.
- Add focus management and browser accessibility tests.
- Vendor third-party assets or build a production static asset pipeline.
- Add a confirmation workflow for race simulations and other destructive
  demonstrations.

---

## 44. Glossary

| Term | Meaning |
|---|---|
| Balance | The current cached amount associated with a wallet |
| Cached balance | A stored aggregate updated alongside ledger entries |
| Credit | A ledger leg representing value entering a wallet |
| Debit | A ledger leg representing value leaving a wallet |
| Double-entry | Recording each movement with balancing debit and credit legs |
| Idempotency key | Client-provided identifier for one logical request |
| Ledger | Append-oriented record of financial movements |
| Lock contention | Multiple transactions competing for the same locked row |
| `select_for_update()` | ORM operation that locks selected rows until transaction end |
| Transaction | Atomic database work that commits as one unit or rolls back |
| Zero-sum | A comparison showing total debits equal total credits |
| UUID | Universally unique identifier used for wallet/transaction IDs |

---

## 45. Viva questions and answers

### Why use `Decimal` instead of `float`?

Binary floating-point cannot exactly represent many decimal fractions. Money
needs predictable base-10 precision. The project uses Python `Decimal` and
Django `DecimalField` with four fractional places.

### What is the idempotency key for?

It allows a client to retry one logical operation after an ambiguous network
failure without executing the operation twice. The server stores the request
hash and response and replays a matching result.

### Does idempotency stop two different requests from overspending?

No. Idempotency handles duplicate logical requests. The row lock and funds
check handle concurrent distinct operations attempting to spend the same
wallet.

### Why sort wallet IDs before locking?

Consistent lock ordering reduces the possibility of deadlocks when concurrent
transfers touch the same wallets in opposite directions.

### Why use `transaction.atomic()`?

It ensures balance updates, transaction creation, and ledger-entry creation
commit together. If an exception occurs before commit, the database rolls
back the partial writes.

### What does a debit/credit pair demonstrate?

The source wallet is debited by the same amount the destination wallet is
credited. The movement preserves the total across the two wallets.

### Is this production ready?

No. Authentication, ownership authorization, automated tests, hardened
idempotency crash handling, production deployment, and accounting controls
remain necessary.

### Does the replay return HTTP 200?

Not necessarily. The idempotency helper stores the original response status.
The first successful transfer returns 201, and the replay may return that same
201 status with the cached body.

---

## 46. Review checklist

Before reviewing or extending a change, ask:

- Does the change preserve exact decimal arithmetic?
- Can it modify a wallet balance outside `TransferService`?
- Are all affected rows locked in deterministic order?
- Are related writes in one database transaction?
- Does a successful transfer create balanced ledger legs?
- Can the same request be retried safely?
- Is the idempotency key scoped to the right identity and operation?
- Are expected domain errors returned with deliberate status codes?
- Could unexpected errors leak internals?
- Are data reads scoped to an authorized owner?
- Are schema changes represented by migrations?
- Are tests using PostgreSQL for locking behavior?
- Does the dashboard distinguish server data from local execution data?
- Does documentation describe the real response codes and limitations?
- Is the demo pointed at a disposable database?

---

## 47. Environment variable reference

The settings use django-environ:

```dotenv
# Required by config/settings.py
SECRET_KEY=replace-with-a-long-random-secret

# Optional application behavior
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1

# Database; use the port and hostname appropriate to where Django runs
DATABASE_URL=postgres://user:password@127.0.0.1:5433/wallet_db

# Optional parsed URL overrides
DB_HOST=127.0.0.1
DB_PORT=5433

# Redis cache and idempotency lock backend
REDIS_URL=redis://127.0.0.1:6379/1
```

The values above are illustrative and should be replaced. Protect the
environment source file and avoid printing the environment to shared logs.
Changing a password or URL may require restarting Django processes so they
reload configuration.

The Redis setting points Django's cache backend to Redis. The idempotency
module uses `get_redis_connection("default")`, so it depends on this cache
configuration as well as a reachable Redis service.

---

## 48. Quick command reference

```powershell
# Install dependencies
python -m pip install -r requirements.txt

# Check Django configuration
python manage.py check

# Start local dependencies
docker compose up -d postgres redis

# Apply schema
python manage.py migrate

# Start local API and dashboard
python manage.py runserver 127.0.0.1:8000

# Run repository tests
python manage.py test

# Check model/migration consistency
python manage.py makemigrations --check --dry-run

# Run the viva demonstration against the running server
python .\scripts\demo_runner.py

# Inspect route names in a Django shell
python manage.py shell

# Stop local services (keeps data volume)
docker compose stop postgres redis
```

Use a project virtual environment for every command. On systems where the
`python` command is not the interpreter selected for the project, substitute
the full path to the intended virtual environment executable.

---

## 49. Example end-to-end demonstration

Assume a disposable database with the migrations applied and the API server
running. The viva runner seeds deterministic Alice and Bob wallets:

```text
Alice starting balance: USD 100.0000
Bob starting balance:   USD  20.0000
```

The runner submits USD 30.0000 with key `DEMO-TX-001`. Expected balances
become Alice USD 70.0000 and Bob USD 50.0000. The ledger should have a USD
30.0000 debit row and an equal credit row.

The runner repeats the exact body and key. The balance should remain
unchanged, and the response should match the stored result. It then changes
the amount to USD 50.0000 while reusing the same key; the API should reject
that payload as key reuse with changed parameters.

Finally, the runner launches twenty separate USD 10.0000 transfer requests
against Alice's remaining USD 70.0000. Seven should commit and thirteen
should receive insufficient-funds responses. Alice should finish at
USD 0.0000; Bob should finish at USD 120.0000. The final audit compares
global debit and credit totals.

These expected results depend on the API route, PostgreSQL locking, Redis
idempotency service, current database state, and demo data all being aligned.
The runner reports a failure if actual results differ; do not present an
unverified expectation as a successful test.

---

## 50. Final project summary

This project is a practical demonstration of financial API foundations:
precise money values, database transactions, pessimistic row locks,
idempotent request replay, and double-entry ledger records. The code is
organized so the HTTP layer validates and translates requests while the
transfer service owns the balance-changing operation. PostgreSQL stores the
durable state, and Redis coordinates in-flight duplicate requests.

The accompanying dashboard makes those ideas visible. It can display wallets,
compare total debits and credits, submit transfers, retry a request with the
same idempotency key, launch a concurrency simulation, and inspect recent
ledger records. The demo runner automates a related sequence for a local viva.

The most important engineering conclusion is that reliable money movement is
not provided by one feature alone. Atomicity, concurrency control, idempotency,
ledger integrity, authorization, observability, and recovery each address a
different risk. The repository implements a useful subset for learning and
demonstration; it also has clearly identified gaps before it should be
considered safe for real customers or production funds.

---

## License and use

No license file is currently included in this repository. Confirm the
project's intended license and third-party dependency terms before
redistributing or deploying it.
