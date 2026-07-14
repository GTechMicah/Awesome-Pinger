# Pinger

Pinger continuously measures HTTP(S) endpoint reachability and end-to-end request latency. It stores samples in PostgreSQL and visualizes live and historical results in Grafana.

## What it measures

This is an HTTP(S) probe, not an ICMP ping. Each sample measures the full request from the Docker host: DNS resolution, TCP/TLS connection, request, and response. A responding endpoint with an HTTP 404 is reachable, but is labeled **HTTP response**; a transport failure or timeout is labeled **Down**.

## Quick start

1. Install Docker Desktop.
2. Copy `.env.example` to `.env` and set strong passwords.
3. Optionally edit `config/endpoints.json` to choose first-run defaults.
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

The config file **seeds missing endpoint names** on service startup. It never overwrites an existing endpoint with the same name, so edits made in the endpoint manager persist. Removing an entry from the config does not delete historical/user-managed endpoints from PostgreSQL.

## Managing endpoints

Use the endpoint manager to:

- Add endpoints.
- Edit endpoint names and URLs.
- Enable or disable probes.
- Move endpoints up or down; this also controls legend ordering.
- Remove an endpoint from active probing while retaining its historical samples.

The manager refreshes displayed health and latency every `STATUS_REFRESH_SECONDS` without overwriting in-progress name or URL edits. It includes a local clock and a link to the Grafana dashboard.

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
| `PATCH` | `/endpoints/{id}` | Update name, URL, or enabled state |
| `POST` | `/endpoints/{id}/move` | Reorder with `{ "direction": "up" }` or `down` |
| `DELETE` | `/endpoints/{id}` | Stop active probing while retaining history |
| `GET` | `/health` | Service and database health |

Only absolute `http://` and `https://` URLs are accepted.

## Persistence and startup

PostgreSQL and Grafana data are stored in named Docker volumes. Normal restarts, rebuilds, and `docker compose down` retain history. Do **not** run `docker compose down -v` unless you intentionally want to erase stored data.

All services use `restart: unless-stopped`. To run automatically on another Windows computer, enable Docker Desktop’s “Start Docker Desktop when you log in,” then start the stack once with `docker compose up --build -d`. For an unattended machine, create a Windows Task Scheduler task that runs the same command at system startup.

Grafana provisioning is built into the local Grafana image rather than mounted from the host. This avoids the Windows `Access is denied` error that can occur when Docker bind-mounts the `grafana/provisioning` directory.

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

## Notes

- Results represent latency from the computer and network running Docker, not latency from every client viewing Grafana.
- Probe sweeps run concurrently across enabled endpoints; the next sweep waits until the previous sweep is complete, then waits `PING_INTERVAL_SECONDS`.
- There is no automatic retention policy. Plan disk capacity or add a retention process for long-running deployments.
