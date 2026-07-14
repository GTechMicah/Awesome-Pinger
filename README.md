# Pinger

Continuously measures HTTP(S) endpoint availability and end-to-end request latency, stores each sample in PostgreSQL, and visualizes the results in Grafana.

## Start

1. Copy `.env.example` to `.env` and set strong passwords.
2. Edit `config/endpoints.json` to seed endpoints on startup, if desired. Each entry has `name`, `url`, and optional `enabled` fields.
3. Run `docker compose up --build -d`.
4. Add or change endpoints through the API (or use the built-in docs at `http://localhost:8080/docs`):

```powershell
Invoke-RestMethod http://localhost:8080/endpoints -Method Post -ContentType 'application/json' -Body '{"name":"Cloudflare","url":"https://1.1.1.1/cdn-cgi/trace"}'
```

5. Open Grafana at `http://localhost:3000` and use the **Endpoint latency** dashboard. Login credentials come from `.env`.

The dashboard uses continuous straight latency lines and includes a color-coded latest-probe table for every endpoint. Failed transport checks are shown as red points below zero, within a reserved red threshold band; HTTP status errors remain visible as latency measurements because the host did respond. The **Manage endpoints** link lets you add endpoints, edit URLs, move endpoints up or down, disable them, or remove them; changes save immediately to PostgreSQL and survive container restarts.

## Endpoint API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/endpoints` | List active endpoints |
| `POST` | `/endpoints` | Add endpoint: `{ "name": "...", "url": "https://..." }` |
| `PATCH` | `/endpoints/{id}` | Update name, URL, or enabled state |
| `DELETE` | `/endpoints/{id}` | Stop probing (historical samples remain) |
| `GET` | `/health` | Service/database health |

Only `http://` and `https://` URLs are accepted. Latency is measured with Python's monotonic high-resolution clock around the whole HTTP request, including DNS lookup, TCP/TLS connection, request, and response headers/body. HTTP errors still record their measured latency; transport and timeout failures record an error with no artificial latency value.

## Operational notes

- Grafana’s database data source uses the same PostgreSQL instance and is provisioned on first start.
- `config/endpoints.json` seeds missing endpoints whenever the pinger container starts. API/dashboard changes are persisted immediately in PostgreSQL and take precedence over entries with the same name.
- Probe intervals are scheduled from the completion of the previous sweep; set `PING_INTERVAL_SECONDS` according to endpoint count and timeout budget.
- `STATUS_REFRESH_SECONDS` controls how often the management page refreshes its displayed endpoint health (default: 5 seconds).
- This reports latency from the Docker host/network where it runs—not user-device latency.
- Data retention is intentionally not automatic. Add a retention policy appropriate to your storage requirements.
