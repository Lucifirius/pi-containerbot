# EVE Online — Corp Hangar Fuel Block Monitor

Monitors fuel block quantities inside a container in a corp hangar via the
EVE ESI API and posts rich Discord embed reports to a channel of your choice.

**Requires Director role** on the authenticated character.

---

## Project layout

```
eve_fuel_monitor/
├── Dockerfile
├── docker-compose.yml
├── .env                   ← poll interval and other overrides
├── .dockerignore
├── .gitignore
├── fuel_monitor.py
├── requirements.txt
└── data/                  ← mounted into the container at /data
    ├── config.yaml        ← YOU edit this
    └── tokens.json        ← written automatically after auth
```

---

## Quick start (Docker — recommended)

### 1. Fill in `data/config.yaml`

Open `data/config.yaml` and set at minimum:

```yaml
client_id:   "your-esi-client-id"
location_id: 60003760

discord:
  webhook_url: "https://discord.com/api/webhooks/..."
```

See **Configuration reference** below for all options.

### 2. Create an ESI application

1. Go to https://developers.eveonline.com/ and sign in.
2. **Create new application**:
   - Connection type: **Authentication & API Access**
   - Scope: `esi-assets.read_corporation_assets.v1`
   - Callback URL: `http://localhost/callback`
3. **View Application** → copy the **Client ID** into `data/config.yaml`.

> The callback URL `http://localhost/callback` does not need to be reachable.
> After login EVE redirects your browser there; you just copy the URL out of
> the address bar and paste it into the terminal.

### 3. Build the image

```bash
docker compose build
```

### 4. Authenticate with EVE SSO (once)

```bash
docker compose run --rm auth
```

The container prints a login URL and a prompt. Follow these steps:

1. **Open the URL** in your browser (it's printed in the terminal).
2. **Log in** with your Director character and approve the scope.
3. EVE redirects your browser to `http://localhost/callback?code=...` — this
   page won't load, and that's expected.
4. **Copy the full URL** from your browser's address bar.
5. **Paste it** into the terminal at the prompt and press Enter.

The container exchanges the code for tokens, writes `data/tokens.json`,
and exits. You won't need to do this again unless you revoke the app's access.

### 5. Verify Discord (optional but recommended)

```bash
docker compose run --rm discord-test
```

A test embed appears in your channel. If it doesn't, check `discord.webhook_url`.

### 6. Start the monitor

```bash
docker compose up -d monitor
docker compose logs -f monitor
```

---

## Managing the monitor

```bash
# View live logs
docker compose logs -f monitor

# Stop
docker compose stop monitor

# Restart after a config change (config is re-read on every poll, no rebuild needed)
docker compose restart monitor

# Stop and remove containers
docker compose down

# Rebuild after editing fuel_monitor.py or requirements.txt
docker compose build && docker compose up -d monitor
```

### Changing the poll interval

Edit `.env`:
```
WATCH_INTERVAL=30   # check every 30 minutes
```
Then `docker compose restart monitor`.

---

## Configuration reference (`data/config.yaml`)

| Key | Required | Description |
|-----|----------|-------------|
| `client_id` | ✓ | ESI application Client ID |
| `location_id` | ✓ | Station or structure ID of the corp hangar |
| `container_id` | ✗ | `item_id` of a specific container (most precise) |
| `alert_threshold` | ✗ | Warn when total blocks fall below this; `0` = off |
| `discord.webhook_url` | ✓* | Full Discord webhook URL (*required for Discord posts) |
| `discord.post_on_alert_only` | ✗ | `true` = only post when fuel is low (default: `false`) |
| `discord.mention_role_id` | ✗ | Discord role ID to @mention on alerts |

### Finding `location_id`

- NPC stations: 8-digit IDs. Search the name at https://everef.net/search
- Player structures: 13-digit IDs. Search the structure name at everef.net, or
  use the in-game Show Info window.

### Finding `container_id`

Leave it blank on the first run. The bot prints (and posts to Discord) every
container it finds at `location_id`. Copy the `Container ID` field of your
target and set it in `config.yaml`. Subsequent runs will filter to that
container only.

### Creating a Discord Webhook

1. Open the target channel → **Edit Channel → Integrations → Webhooks → New Webhook**.
2. Name it, click **Copy Webhook URL**, paste into `discord.webhook_url`.

### Getting a Discord Role ID (for @mentions on alerts)

1. Enable **Developer Mode**: User Settings → Advanced → Developer Mode.
2. Server Settings → Roles → right-click the role → **Copy Role ID**.
3. Paste into `discord.mention_role_id`.

---

## Running without Docker (bare Python)

```bash
pip install -r requirements.txt
cp data/config.yaml .
python fuel_monitor.py --auth
python fuel_monitor.py --discord-test
python fuel_monitor.py --watch 60
```

Environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CONFIG_PATH` | `./config.yaml` | Path to config file |
| `TOKEN_PATH` | `./tokens.json` | Path for token storage |
| `WATCH_INTERVAL` | `60` | Minutes between checks (Docker CMD default) |

---

## Running as a systemd service (Linux, non-Docker)

Create `/etc/systemd/system/fuel-monitor.service`:

```ini
[Unit]
Description=EVE Fuel Block Monitor
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/eve_fuel_monitor
ExecStart=/usr/bin/python3 fuel_monitor.py --watch 60
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now fuel-monitor
sudo journalctl -u fuel-monitor -f
```

---

## Security notes

- `data/tokens.json` contains a long-lived OAuth refresh token — treat it like
  a password. It is listed in `.gitignore` and `.dockerignore`.
- `data/config.yaml` contains your Discord webhook URL — keep it out of
  version control too (also in `.gitignore`).
- The bot uses **PKCE OAuth2** — no client secret is ever stored.
- Access tokens expire after 20 minutes and refresh automatically.
- Your EVE account password is never seen by this application.
- The container runs as a non-root user (`appuser`).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `403 Forbidden` from ESI | Character lacks Director role, or wrong ESI scope granted |
| No containers found | Check `location_id`; clear `container_id` to list all containers |
| `tokens.json` not found | Re-run `docker compose run --rm auth` |
| "Could not find a code value" | Make sure you copy the full address-bar URL, not just the page title |
| Discord `400 Bad Request` | Webhook URL is malformed |
| Discord `404 Not Found` | Webhook was deleted — recreate it in Discord |
| Token expired / invalid | Re-run `docker compose run --rm auth` |
