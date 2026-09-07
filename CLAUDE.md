# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Linux Router turns a Debian/Armbian device into a router with a Flask web console for system status, wired/Wi-Fi networking, hotspot, connected clients, and maintenance tools. All network management goes through **NetworkManager** (`nmcli`), `netplan`, `dnsmasq`, and `iptables`; the console itself only shells out via the root Agent (never directly).

## Core Architecture: two processes over a Unix socket

The single most important concept. The app is split so that the unprivileged web tier never runs privileged code directly:

- **Web tier** — Flask app (`app.py` → `router_panel/web.py`), runs as unprivileged `router-panel` user under Gunicorn (`router-panel.service`). Handles pages, login, CSRF, and submitting requests to the Agent.
- **Root Agent** (`agent.py` → `router_panel/agent_server.py`), runs as root (`router-panel-agent.service`). Executes allowlisted system queries and all network mutations.

They communicate over a **Unix socket** (`/run/linux-router/agent.sock`; env `LINUX_ROUTER_AGENT_SOCKET`) using newline-delimited JSON. The wire client is `router_panel/agent_client.py`; the server is `agent_server.py`. Three request shapes:

- `{"method":"query","name":...,"params":...}` → immediate read. Dispatch lives in `execute_query()` in `agent_server.py` (~line 646). Returns a `result` directly.
- `{"method":"submit","action":...,"params":...,"scope":...,"context":...}` → enqueues a long-running mutation and returns an `operation` with an `operation_id`. **Network-changing operations are executed serially** by a single worker thread.
- `{"method":"operation","operation_id":...}` → poll status/result of a submitted operation.

Long operations: the web tier gets an `operation_id`, then the browser polls `/operations/<operation_id>`. When done, that endpoint (in `web.py`) re-queries fresh status via `query_agent(...)` and returns re-rendered HTML fragments keyed by `scope` (`network`/`hotspot`) — see the `partials/` templates.

`router-panel-agent.service` is declared `Requires=`/`After=` by `router-panel.service`, so the Agent must be up before the web tier.

## Adding features (how layers connect)

There is a fixed pipeline. To add a new capability, follow it:

1. **Data contracts** live as `TypedDict`s in `router_panel/contracts.py` (`WirelessStatus`, `HotspotStatus`, `WiredStatus`, `SystemInfo`, …).
2. **Web route** → in `web_general.py`, `web_network.py`, or `web_tools.py`, inside `register_*_routes(app, login_required, is_async_request)`. Use `query_agent("name")` for reads or `submit_operation(action, params, scope=..., context=...)` for mutations. A submitting route usually returns a `queued_response(...)` containing `operation_id`.
3. **Agent-side read** → add a branch to `execute_query()` in `agent_server.py`, backed by a `gather_*` function in the relevant module (`network.py`, `system.py`, `dependencies.py`, `tailscale.py`, `service_monitor.py`).
4. **Agent-side mutation** → write an `_execute_*` function in `agent_server.py` and register it in the `OPERATIONS` dict (user-triggered) or `INTERNAL_OPERATIONS` dict (internal-only, e.g. hotspot keepalive recovery). These build on apply-layer functions in `network_operations.py`.
5. **Template** → a Jinja page in `templates/` extending `base.html`, with async refresh fragments in `templates/partials/`.
6. Some existing routes/tests assert the route table, so keep `EXPECTED_ROUTES` in `tests/test_application.py` in sync.

## router_panel module responsibilities

- `web.py` — assembles the app: CSRF (`before_request`, POST-only), `login_required`, the async `/operations/<id>` poll endpoint, context-processor globals, then calls `register_general_routes` / `register_network_routes` / `register_tools_routes`.
- `web_network.py` — the most involved router; adds a `with_fragments()`/`submit()` helper used by all `/wifi` and `/hotspot` routes.
- `agent_client.py` — Unix socket client (`query_agent`, `submit_operation`, `get_operation`, `AgentError`).
- `agent_server.py` — socket server, `OperationRegistry`, serial worker queue (`AgentRuntime`), hotspot-keepalive supervisor thread, and the query/operation dispatch tables. Mocked/extended heavily in tests.
- `core.py` — shared plumbing: `run_command`, `atomic_write_text`, timed caches, config load/save under `DATA_DIR`, auth/password hashing, path constants. Imports of environment variables (e.g. `DATA_DIR`) are resolved **at import time**.
- `network.py` — *reads*/gathers network state (nmcli parsing, hotspot/wired/wireless status) plus a small `network_parsers.py` for raw `nmcli`/`iw` text parsing (used with fixture files in tests).
- `network_operations.py` — *applies* changes (wifi connect/disconnect/forget, wired apply, hotspot start/stop). The privileged mutation layer.
- `dependencies.py` — dependency check + guided repair. `system.py` — system overview. `tailscale.py`, `service_monitor.py`, `hotspot_keepalive.py` — the three tools subsystems.

## Runtime state / data directory

Persisted state lives under `DATA_DIR`: the Flask secret key, `auth.json` (password hashes), `network.json` (LAN config), `initial_password.txt`, plus per-feature `*.json`. `DATA_DIR` defaults to the repo-local `data/` in development but `/var/lib/linux-router` in production (`LINUX_ROUTER_DATA_DIR` env). Never commit real secrets; `data/*` is gitignored except `.gitkeep`. Distinguish prod secrets from the checked-in source cleanly — tests point `DATA_DIR` at temp dirs.

Environment variables that matter: `LINUX_ROUTER_DATA_DIR`, `LINUX_ROUTER_AGENT_SOCKET`, `LINUX_ROUTER_AGENT_GROUP`.

## Development commands

Run the root Agent **first**, then the web app (see README "Development"):

```bash
# Terminal 1 — privileged agent
sudo env \
  LINUX_ROUTER_DATA_DIR=/var/lib/linux-router \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock \
  python3 agent.py

# Terminal 2 — web app (no sudo)
python3 app.py           # serves http://127.0.0.1:80
```

No requirements.txt — Python deps (`flask`, `gunicorn`) and system tools are installed by `install.sh` as Debian/apt packages. `app.py` is the Gunicorn WSGI entry point (`app:app`).

Run tests (single `unittest` file; the whole suite is one big file):

```bash
python3 -m unittest tests.test_application

# one test method
python3 -m unittest tests.test_application.ApplicationStructureTests.test_csrf_rejects_missing_token_and_accepts_valid_token
```

Restart rules after a code change:
- Web code or templates → restart `router-panel.service`
- Agent / system queries / network ops → restart `router-panel-agent.service`
- `static/style.css` change → also bump the `v=` cache-busting param on the CSS link in `templates/base.html`

## Tests

`tests/test_application.py` is a single, very large `unittest` suite (the only test module). It imports `app`, `router_panel.*`, and `install.sh`. Patterns to know:
- Commands are mocked at the source: `patch.object(core.subprocess, "run", ...)` since `run_command` shells out.
- Module-level path constants are patched directly (e.g. `patch.object(core, "THERMAL_ROOT", tmp)`), not env vars.
- Config-write/file behaviors use real `tempfile.TemporaryDirectory()` dirs.
- Raw tool output for parser tests lives in `tests/fixtures/` (`nmcli_devices.txt`, `iw_phy.txt`, `iw_station_dump.txt`).
- In-process integration exercises the app via `application.app.test_client()` and boots an `agent_server.AgentRuntime()` (often with a long `monitor_initial_delay` to avoid the keepalive thread interfering).
- `EXPECTED_ROUTES` (top of file) is asserted against the real route table; add new routes there.

## Deployment / installer

`install.sh` is a single self-contained installer supporting `install`, `upgrade`, `uninstall` (with `--no-network-config`, `--defer-network-restart`, `--apply-network-now`, `--purge-data` flags). Network changes during install/uninstall/repair can drop connectivity — README warns to run these from a local console. The systemd units use `@INSTALL_DIR@`/`@DATA_DIR@` placeholders substituted by the installer. See `DEPLOYMENT.md` for manual deploy + troubleshooting; `README.md` documents flags and the two service model.

Interface/constants shared across modules and pinned in `core.py`: `HOTSPOT_CONNECTION_NAME="DebianRouterHotspot"`, `HOTSPOT_DEFAULT_SSID`, `HOTSPOT_VIRTUAL_INTERFACE_PREFIX="ap-"` (virtual interfaces matching `ap-*` are cleaned up on uninstall).

UI text is largely Simplified Chinese (inline in templates and code). Keep new strings consistent with the surrounding language of the file you are editing.
