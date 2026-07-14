# Pinger

Pinger continuously measures HTTP(S) endpoint reachability and end-to-end request latency. It stores samples in PostgreSQL and visualizes live and historical results in Grafana.

## What it measures

Each endpoint can use one of two probe types:

- **HTTP(S) request** measures the full request from the Docker host: DNS resolution, TCP/TLS connection, request, and response. A responding endpoint with an HTTP 404 is reachable, but is labeled **HTTP response**.
- **ICMP ping** sends one network ping to a hostname or IP address and records the round-trip time. Use an IP address or hostname only—do not include `http://` or a path.

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

The manager uses one **Save all changes** button, so name, target, type, and enabled-state edits across multiple rows are saved together without row-level refreshes clearing other pending edits. Moving or removing an endpoint saves pending edits first. It refreshes displayed health and latency every `STATUS_REFRESH_SECONDS` without overwriting in-progress edits, and includes a local clock plus an **Open dashboard** link that follows the configured `GRAFANA_PORT`.

## Dashboard behavior

- The main graph defaults to the last 15 minutes and refreshes every 5 seconds.
- Successful probes display as continuous latency lines.
- Transport failures appear as red markers in a reserved negative region; the negative values are visual indicators, not measured latency.
- Hovering the graph shows latency, reachability, and HTTP status at that time, sorted from highest latency to lowest.
- The right-side legend follows the saved endpoint-manager order.
- The **Latest probe from every endpoint** table shows the newest probe, latency, status code, timestamp, and error for each endpoint.

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
| `POST` | `/endpoints` | Add `{ "name": "...", "url": "https://..." }` |
| `PATCH` | `/endpoints/{id}` | Update name, URL/host, `probe_type` (`http` or `icmp`), or enabled state |
| `POST` | `/endpoints/{id}/move` | Reorder with `{ "direction": "up" }` or `down` |
| `DELETE` | `/endpoints/{id}` | Stop active probing while retaining history |
| `GET` | `/health` | Service and database health |

HTTP probes accept only absolute `http://` and `https://` URLs. ICMP probes accept hostnames or IP addresses.

## Persistence and startup

PostgreSQL and Grafana data are stored in named Docker volumes. Normal restarts, rebuilds, and `docker compose down` retain history. Do **not** run `docker compose down -v` unless you intentionally want to erase stored data.

All services use `restart: unless-stopped`. To run automatically on another Windows computer, enable Docker Desktop’s “Start Docker Desktop when you log in,” then start the stack once with `docker compose up --build -d`. For an unattended machine, create a Windows Task Scheduler task that runs the same command at system startup.

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
