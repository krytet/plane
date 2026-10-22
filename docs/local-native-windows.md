# Native Windows local development

This workflow runs Plane without Docker. It uses the existing Windows OSPanel
PostgreSQL 15, Redis, and RabbitMQ 3.13 modules, native Node/pnpm, an isolated
uv-managed Python 3.12 environment, and a checksum-verified Silo executable.

All Plane listeners are loopback-only.

Browser API and authentication traffic use `http://localhost:3059/api/` and
`http://localhost:3059/auth/`. The native web server proxies those paths to
`http://127.0.0.1:8059`, preserving `localhost` cookies and CSRF behavior.
Direct API access uses the IPv4 URL above; do not use `localhost:8059` in the
browser because another IPv6 service may occupy that endpoint.

| Service             | Local URL                        |
| ------------------- | -------------------------------- |
| Web                 | `http://localhost:3059`          |
| Admin               | `http://localhost:3001/god-mode` |
| Space               | `http://localhost:3002/spaces`   |
| API                 | `http://127.0.0.1:8059`          |
| Live                | `http://localhost:3100/live`     |
| S3 API              | `http://127.0.0.1:9000`          |
| S3 console          | `http://127.0.0.1:9090`          |
| RabbitMQ management | `http://127.0.1.22:15672`        |

The native launcher pins the web server to `3059` with `--strictPort`; the
package's generic development default remains `3000` for non-native workflows.

## Prerequisites

- Windows PowerShell 5.1 or newer.
- Node.js 22.22 or newer and Corepack. The scripts require the repository's
  exact pnpm `11.10.0` through `corepack pnpm`.
- `uvx` on `PATH`. The scripts run the exact uv `0.12.21` without changing the
  global uv installation.
- OSPanel at `C:\OSPanel` with the PostgreSQL-15, Redis, and RabbitMQ-3.13
  modules. The scripts start the OSPanel desktop process when necessary and
  start only those three modules.
- No Docker requirement.

The first setup downloads the pinned Silo release
`RELEASE.2026-09-16T00-00-00Z`. The Windows archive and checksum manifest are
resolved from the official GitHub release API, and setup fails closed if the
SHA256 is absent or does not match.

## First setup

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev\setup-native.ps1
```

Setup performs these one-time or idempotent actions:

1. Starts OSPanel if needed and requests only PostgreSQL-15, Redis, and
   RabbitMQ-3.13.
2. Generates any missing `.env` files. Existing files are never replaced.
3. Runs `corepack pnpm install --frozen-lockfile` and exact uv
   `sync --frozen`.
4. Runs a filtered Turbo build for web, admin, space, live, and their shared
   package dependencies.
5. Verifies the dedicated `plane` database and `plane_native` PostgreSQL role.
6. Enables RabbitMQ management, binds it to `127.0.1.22`, and creates the
   isolated `plane-native` vhost and `plane_native` user with permissions on
   that vhost only.
7. Downloads and verifies Silo, starts it on loopback, creates the S3 bucket,
   and verifies CORS with a real preflight.
8. Runs migrations, registration, `configure_instance`, and a local bootstrap
   that disables telemetry before any Celery worker starts.

Useful options:

```powershell
.\dev\setup-native.ps1 -Check
.\dev\setup-native.ps1 -SkipDependencies
.\dev\setup-native.ps1 -NoStorageDownload
```

`-Check` is read-only and checks infrastructure only. `-SkipDependencies`
skips pnpm/uv install and the filtered build.

### Environment generation

On a clean checkout, setup derives the active PostgreSQL-15 and Redis
loopback listeners from OSPanel and reads RabbitMQ's configured loopback
listener. It creates all six files:

```text
.env
apps/api/.env
apps/web/.env
apps/admin/.env
apps/space/.env
apps/live/.env
```

Secrets are generated once and retained. Credential reconciliation is allowed
only for files beginning with the setup-generated header. A pre-existing
hand-edited env file is preserved and setup fails rather than rewriting it.

The effective database, Redis, AMQP, and S3 endpoints are parsed from the API
env and must resolve to loopback. Setup and start fail closed for remote
endpoints.

Redis uses dedicated database `10`. For this deployment, DB `10` was verified
empty before first use; the scripts never run `clear_cache` or a global flush.
PostgreSQL and RabbitMQ objects are scoped to the Plane database and isolated
vhost.

## Start

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev\start-native.ps1
```

The root `start-plane.bat` wrapper also runs the full readiness checker after
the launcher exits successfully, and opens `http://localhost:3059` in the
default browser only when both steps return exit code 0. Pass `--no-browser`
to suppress opening the browser for automation; `--check` only runs the
readiness check and never opens it.

Start is repeatable. It reuses healthy owned processes, restarts Silo if it is
down, verifies infrastructure protocols, starts the API on `127.0.0.1:8059`,
then starts Celery worker and beat, web, admin, space, and live. The worker uses
`--pool=solo --concurrency=1`; readiness requires a real Celery inspect ping.

The exact API command is:

```text
uv run --env-file .env manage.py runserver 127.0.0.1:8059 --settings=plane.settings.local
```

Frontend env files use `http://localhost:3059` as the browser API base. The
launcher sets `NATIVE_API_PROXY_TARGET=http://127.0.0.1:8059` for the web
process so Vite proxies `/api` and `/auth`. If starting the web frontend
manually, set that variable in the same shell first:

```powershell
$env:NATIVE_API_PROXY_TARGET = "http://127.0.0.1:8059"
corepack pnpm --filter web exec react-router dev --host 127.0.0.1 --port 3059 --strictPort
```

## Check

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev\check-native.ps1
```

Full check requires all infrastructure and all seven Plane processes: API,
worker, beat, web, admin, space, and live. It also performs the Celery ping.

For diagnostics before starting the app:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev\check-native.ps1 -InfrastructureOnly
```

## Local state

Ignored local artifacts live under `.local-native/`:

| Path     | Contents                          |
| -------- | --------------------------------- |
| `tools/` | Verified Silo executable          |
| `data/`  | Silo data                         |
| `logs/`  | Service and infrastructure logs   |
| `pids/`  | Owned-process records             |
| `temp/`  | Bounded download/extraction files |

The launcher records the PID and process start time. It will not reuse or stop
an unrelated process merely because a port is occupied.

Some native wrappers may leave their redirected log file empty. For debugging,
run the exact command from the service's working directory in a foreground
PowerShell window, for example:

```powershell
cd apps\api
uvx --from uv==0.12.21 uv run --env-file .env manage.py runserver 127.0.0.1:8059 --settings=plane.settings.local
```

```powershell
corepack pnpm --filter live dev
```

The Live health endpoint is `http://127.0.0.1:3100/live/health`.

## Email behavior

Local settings use Django's console email backend and `ENABLE_SMTP=0`. The
first administrator is created through the admin onboarding page at
`http://localhost:3001/god-mode`. Outbound invitation email is not delivered;
it is written to the API/worker console log. External SMTP and password-reset
delivery are not configured for this native local workflow.

## Personal local account

This native instance is configured as a single-user personal installation:

- The local account is `owner@plane.local`, created through the first-admin
  setup flow and registered as the instance admin.
- The workspace is `Personal` with slug `personal`. The account is its creator
  and owner, with workspace role 20 (Admin/full access).
- The current app session is stored in the user's external Opera profile.

Keep the password in a password manager; never write it into this repository
or these docs. Standard password authentication remains enabled, and the
address does not need to receive email.

The `.env` file sets `ENABLE_SIGNUP=0`. The authoritative database-backed
`InstanceConfiguration.ENABLE_SIGNUP` is also `0`; because `SKIP_ENV_VAR=1`,
the database value takes precedence. Ordinary self-service signup is therefore
disabled, with signup limited to invited addresses.

Future app logins use a 90-day sliding session, refreshed while the account is
active. The separate admin login uses a one-day inactivity session. Both use
normal HttpOnly cookies: they survive browser and service restarts under the
same browser profile, but clearing cookies, deleting the database session row,
or letting the inactivity period expire requires a normal sign-in again.

## Scope boundary

The workflow does not create a Windows logon scheduled task. It does not modify
unrelated OSPanel modules or profiles. RabbitMQ management is the only plugin
enabled, and its profile listeners are loopback-only.
