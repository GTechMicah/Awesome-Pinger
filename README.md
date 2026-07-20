# Pinger

Pinger continuously measures HTTP(S) endpoint reachability and end-to-end request latency. It stores samples in PostgreSQL and visualizes live and historical results in Grafana.

## What it measures

Each endpoint can use one of two probe types:

- **HTTP(S) request** measures the full request from the Docker host: DNS resolution, TCP/TLS connection, request, and response. A responding endpoint with an HTTP 404 is reachable, but is labeled **HTTP response**.
- **ICMP ping** sends one network ping to a hostname or IP address and records the packet round-trip time reported by `ping` itself (not container/process startup time). A hostname or IP is recommended; if an existing HTTP(S) URL is retained when switching probe types, Pinger extracts its hostname internally for the ping.

A transport failure, ICMP failure, or timeout is labeled **Down**.

## Quick start

1. Install Docker Desktop.
2. Copy `.env.example` to `.env` and set strong passwords.
3. Optionally edit `config/endpoints.json` to choose first-run defaults. The file is bundled into the pinger image, so edits require a rebuild.
4. Start the stack:

   ```powershell
   docker compose up --build -d
   ```

5. Open:

   | Service | URL |
   | --- | --- |
   | Grafana dashboard | `http://localhost:3000/d/endpoint-latency/endpoint-latency` |
   | Endpoint manager | `http://localhost:8080/manage` |
   | API documentation | `http://localhost:8080/docs` |

Grafana credentials come from `.env`.

## Default endpoints

`config/endpoints.json` includes lightweight public connectivity endpoints:

- Google connectivity: `https://www.google.com/generate_204`
- Cloudflare trace: `https://www.cloudflare.com/cdn-cgi/trace`
- Mozilla connectivity: `https://detectportal.firefox.com/success.txt`
- Microsoft connectivity: `http://www.msftconnecttest.com/connecttest.txt`

The config file **seeds missing endpoint names** on service startup. It never overwrites an existing endpoint with the same name, so edits made in the endpoint manager persist. Removing an entry from the config does not delete historical/user-managed endpoints from PostgreSQL. After editing this file, apply it with `docker compose up --build -d pinger`.

For an ICMP default, use `"type": "icmp"` with a hostname or IP address:

```json
{
  "name": "Local router",
  "url": "192.168.1.1",
  "type": "icmp",
  "enabled": true
}
```

## Managing endpoints

Use the endpoint manager to:

- Add endpoints; **ICMP ping** is the default type for new endpoints.
- Choose **HTTP(S) request** or **ICMP ping** for each endpoint.
- Edit endpoint names and URLs/hosts.
- Enable or disable probes.
- Move endpoints up or down; this also controls legend ordering.
- Remove an endpoint from active probing while retaining its historical samples.

The manager uses one **Save all changes** button, so name, target, type, and enabled-state edits across multiple rows are saved together without row-level refreshes clearing other pending edits. Moving or removing an endpoint saves pending edits first. Switching probe types never rewrites the target field, so `http://` and `https://` are preserved when switching back to an HTTP(S) probe.

The manager validates names and targets before saving. Invalid fields receive a red outline and a warning icon with a hoverable explanation; attempting to save also shows the same message at the top. Validation covers required fields, duplicate names, valid HTTP(S) URL schemes, and valid ICMP hosts/URLs. Clearing both fields in the Add endpoint form clears its pending validation warnings.

The manager refreshes displayed health and latency every `STATUS_REFRESH_SECONDS` without overwriting in-progress edits. Its status, local-clock, and **Stats range** controls are grouped in a toolbar above the endpoint table. The range selector supports 15 minutes, 1 hour, 24 hours, 7 days, and **All time**, with separate per-endpoint **Min**, **Avg**, **Max**, and **Failures** columns for the chosen range. The all-time calculation uses an indexed aggregate query and is suitable for the normal monitoring history retained by this stack. It also includes an **Open dashboard** link that follows the configured `GRAFANA_PORT`.

## Dashboard behavior

- The main graph defaults to the last 15 minutes and refreshes every 5 seconds.
- Each endpoint displays as one continuous latency line.
- Transport failures are plotted at `-50 ms` in the reserved negative region and connected to the surrounding successful points for that same endpoint; the negative value is a visual indicator, not measured latency.
- Hovering the graph shows endpoint values at that time, sorted from highest latency to lowest.
- The right-side legend follows the saved endpoint-manager order and shows endpoint names only.
- The **Endpoint overview: latest probe, latency, and failures** table combines the newest probe (health, latency, status code, timestamp, and error) with minimum, average, maximum, successful-sample count, and failure count for every endpoint in the current dashboard time range. Transport/ICMP failures and HTTP 5xx responses count as failures; HTTP 4xx responses remain reachable measurements.

Health colors:

| Status | Meaning |
| --- | --- |
| Green — Healthy | Successful HTTP response below 400 |
| Orange — HTTP response | Endpoint was reached but returned HTTP 4xx |
| Red — Server error / Down | HTTP 5xx response, or no HTTP response due to a transport failure/timeout |
| Gray — No data | No probe has been saved yet |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | required | PostgreSQL password |
| `GRAFANA_ADMIN_USER` | `admin` | Grafana administrator user |
| `GRAFANA_ADMIN_PASSWORD` | required | Grafana administrator password |
| `PING_INTERVAL_SECONDS` | `5` | Delay after each completed probe sweep |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Per-request HTTP timeout |
| `STATUS_REFRESH_SECONDS` | `5` | Endpoint-manager health refresh interval |
| `DATA_RETENTION_DAYS` | `30` | Keep raw probe samples for this many days; set `0` to disable automatic cleanup |
| `PINGER_PORT` | `8080` | Host port for the API and manager |
| `GRAFANA_PORT` | `3000` | Host port for Grafana |

After changing `.env`, run:

```powershell
docker compose up -d
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/endpoints` | List endpoints, latest status, and ordering |
| `GET` | `/endpoint-stats?window=15m` | Per-endpoint min/avg/max and failure counts; supported windows: `15m`, `1h`, `24h`, `7d`, `all` |
| `POST` | `/endpoints` | Add `{ "name": "...", "url": "https://..." }` |
| `PATCH` | `/endpoints/{id}` | Update name, URL/host, `probe_type` (`http` or `icmp`), or enabled state |
| `POST` | `/endpoints/{id}/move` | Reorder with `{ "direction": "up" }` or `down` |
| `DELETE` | `/endpoints/{id}` | Stop active probing while retaining history |
| `GET` | `/health` | Service and database health |

HTTP probes accept only absolute `http://` and `https://` URLs. ICMP probes accept hostnames or IP addresses; stored HTTP(S) URLs are also accepted and ping their hostname.

## Persistence and startup

PostgreSQL and Grafana data are stored in named Docker volumes. Normal restarts, rebuilds, and `docker compose down` retain history. Do **not** run `docker compose down -v` unless you intentionally want to erase stored data.

### Raw-sample retention

To keep the database from growing without limit, Pinger keeps raw probe samples for **30 days** by default. A background cleanup runs once per day and deletes older samples in small batches, allowing PostgreSQL to reuse the storage without interrupting normal probing. Endpoint definitions, Grafana configuration, and samples within the selected retention period are unaffected.

Set `DATA_RETENTION_DAYS` in `.env` to a longer or shorter period, then apply it with `docker compose up -d`. Set it to `0` only if you intentionally want unlimited raw-history retention. The dashboard's **All time** range means all retained data, not data that has already expired.

All services use `restart: unless-stopped`. Once the stack has been started successfully, Docker restarts it whenever the Docker engine starts (unless the containers were explicitly stopped).

### Run at startup on a Docker host

1. Start the stack once:

   ```bash
   docker compose up --build -d
   ```

2. Configure the host's Docker engine to start at boot. On a standard Linux Docker host:

   ```bash
   sudo systemctl enable --now docker
   ```

No startup script is needed after that: the Compose restart policies bring PostgreSQL, Pinger, and Grafana back automatically.

### Windows and Windows Server

- **Windows 10/11:** use Docker Desktop with Linux containers and enable its **Start Docker Desktop when you log in** setting. Start the stack once with the command above.
- **Windows Server:** Docker Desktop is not supported on Windows Server, and the native Windows Server Docker engine runs Windows containers only. This stack uses Linux images (PostgreSQL, Grafana, and the Pinger image), so run it in a Linux VM on the server—for example, an Ubuntu VM configured to auto-start in Hyper-V—or deploy it to another Linux Docker host. Enable the Docker service inside that Linux host as shown above; the stack then starts automatically whenever the VM/host boots.

This project therefore works on Docker hosts that can run Linux containers, including Linux servers, Linux VMs, Docker Desktop on Windows/macOS, and managed Linux container hosts. It cannot run unchanged against a Windows-containers-only Docker engine.

Grafana provisioning and the default endpoint config are built into local images rather than mounted from the host. This avoids Windows `Access is denied` errors that can occur when Docker bind-mounts `grafana/provisioning` or `config/endpoints.json`.

### Upgrading an existing installation

Pull or copy the updated project files, then rebuild and recreate the stack:

```powershell
docker compose up --build -d
```

The pinger automatically adds the `probe_type` database column and preserves all existing endpoints and history. Existing endpoints are assigned the HTTP probe type. The Docker service is granted the required `NET_RAW` capability and includes the standard `ping` tool for ICMP probes.

## Useful commands

```powershell
# View running services
docker compose ps

# Follow probe-service logs
docker compose logs -f pinger

# Stop services, keeping all data
docker compose down

# Rebuild after source or dashboard changes
docker compose up --build -d
```

## Troubleshooting

### Grafana port 3000 is already allocated

If Docker reports `Bind for 0.0.0.0:3000 failed: port is already allocated`, another application or container already owns port 3000. Either stop the old service if it is no longer needed, or choose a different Grafana host port.

To inspect Docker containers and their port mappings:

```powershell
docker ps --format "table {{.Names}}\t{{.Ports}}"
```

To use port 3001 instead, set this in `.env`:

```env
GRAFANA_PORT=3001
```

Then apply it:

```powershell
docker compose up -d
```

The dashboard will then be at `http://localhost:3001/d/endpoint-latency/endpoint-latency`. The same approach works for an occupied API/manager port by changing `PINGER_PORT`.

## Notes

- Results represent latency from the computer and network running Docker, not latency from every client viewing Grafana.
- Probe sweeps run concurrently across enabled endpoints; the next sweep waits until the previous sweep is complete, then waits `PING_INTERVAL_SECONDS`.
- There is no automatic retention policy. Plan disk capacity or add a retention process for long-running deployments.
