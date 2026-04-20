# EVE Online — Corp Hangar Fuel Block Monitor

Monitors fuel block quantities inside a specific container in a corp hangar
via the EVE ESI API. Scans every hour and **posts to Discord only when the
count changes** — either blocks added or removed — showing a clear before/after
diff. Silent otherwise.

**Requires Director role** on the authenticated character.

---

## How it works

Every hour the bot:

1. Pulls all corp assets from ESI
2. Locates the configured container by `item_id` directly (no fragile flag/location filtering)
3. Counts the fuel blocks inside it, per type and in total
4. Compares against the last saved count in `data/state.json`
5. If the count changed → posts a Discord embed showing the diff and new totals
6. If unchanged → logs to terminal only, no Discord post
7. Saves the new count to `state.json`

The first run always posts to Discord ("first reading") so you get an immediate
confirmation that everything is working.

---

## Project layout

```
eve_fuel_monitor/
├── Dockerfile
├── docker-compose.yml
├── .env                   ← WATCH_INTERVAL override (default: 60 minutes)
├── .dockerignore
├── .gitignore
├── fuel_monitor.py
├── requirements.txt
└── data/                  ← mounted into the container at /data
    ├── config.yaml        ← YOU edit this before first run
    ├── tokens.json        ← written by --auth, do not edit
    └── state.json         ← written each check, do not edit
```

---

## Quick start (Docker)

### 1. Create an ESI application

1. Go to https://developers.eveonline.com/ and sign in with your EVE account.
2. Click **Create new application**:
   - Connection type: **Authentication & API Access**
   - Scope: `esi-assets.read_corporation_assets.v1`
   - Callback URL: `http://localhost/callback`
3. Click **View Application** and copy the **Client ID**.

> The callback URL does not need to be a real server. After you log in, EVE
> redirects your browser there — the page will fail to load, and that's fine.
> You just copy the URL from the address bar and paste it into the terminal.

### 2. Create a Discord Webhook

1. Open the Discord channel you want reports posted to.
2. **Edit Channel → Integrations → Webhooks → New Webhook**.
3. Name it (e.g. "Fuel Monitor"), click **Copy Webhook URL**.

### 3. Fill in `data/config.yaml`

```yaml
client_id:    "your-esi-client-id"
callback_url: "http://localhost/callback"   # must match ESI app exactly

location_id:  1051781871633    # structure where the corp hangar is
container_id: 1052272591764    # item_id of the specific container
hangar_flag:  "CorpSAG2"       # hangar division slot

alert_threshold: 500           # @mention role if total drops below this; 0 = off

discord:
  webhook_url:     "https://discord.com/api/webhooks/..."
  mention_role_id: ""          # Discord role ID to ping on low-fuel alerts
```

### 4. Build the image

```bash
docker compose build
```

### 5. Authenticate with EVE SSO (once)

```bash
docker compose run --rm auth
```

Follow the on-screen steps:

1. Open the printed URL in your browser
2. Log in with your **Director** character and approve the scope
3. EVE redirects to `http://localhost/callback?code=...` — the page won't load, that's expected
4. Copy the full URL from your browser's address bar
5. Paste it at the terminal prompt and press Enter

`data/tokens.json` is written and the container exits. Tokens refresh automatically
forever — you only need to do this again if you explicitly revoke the app in EVE.

### 6. Verify Discord (optional but recommended)

```bash
docker compose run --rm discord-test
```

A test embed appears in your channel. If it doesn't, check `discord.webhook_url`.

### 7. Start the monitor

```bash
docker compose up -d monitor
docker compose logs -f monitor
```

The monitor runs indefinitely, checking every 60 minutes and posting to Discord
only when the fuel count changes.

---

## Discord embed format

When a change is detected, the embed shows:

```
📦  Giant Secure Container

🟢  OK — 4,800 fuel blocks

📈  +1,200 added  (3,600 → 4,800)

🟢  Caldari Fuel Block    2,400  (+800)
🟢  Amarr Fuel Block      2,400  (+400)
```

Status colours:
- 🟢 Green — fuel levels are above the alert threshold
- 🟡 Yellow — total is below `alert_threshold` (but container is not empty)
- 🔴 Red — container is empty

Arrows on each type line show the per-type change since the last reading.
The first post after startup says "First reading" with no diff.

---

## Managing the monitor

```bash
# View live logs
docker compose logs -f monitor

# Stop
docker compose stop monitor

# Restart (picks up config changes without rebuild)
docker compose restart monitor

# Stop and remove containers
docker compose down

# Rebuild after editing fuel_monitor.py or requirements.txt
docker compose build && docker compose up -d monitor
```

### Changing the poll interval

Edit `.env`:
```
WATCH_INTERVAL=30   # check every 30 minutes instead of 60
```
Then `docker compose restart monitor`.

---

## Configuration reference (`data/config.yaml`)

| Key | Required | Description |
|-----|----------|-------------|
| `client_id` | ✓ | ESI application Client ID |
| `callback_url` | ✓ | Callback URL — must match ESI app registration exactly |
| `location_id` | ✓ | Station or structure ID where the corp hangar is |
| `container_id` | ✓ | `item_id` of the container to monitor |
| `hangar_flag` | ✗ | Expected hangar slot (`CorpSAG1`–`7`, `Hangar`, `HangarAll`) — used to verify the container is in the right division |
| `alert_threshold` | ✗ | @mention the role when total blocks fall below this number; `0` = disabled |
| `discord.webhook_url` | ✓ | Full Discord webhook URL |
| `discord.mention_role_id` | ✗ | Discord role ID to @mention when `alert_threshold` is breached |

### Finding `location_id`

- NPC stations: 8-digit IDs (e.g. `60003760` = Jita 4-4). Search at https://everef.net/search
- Player structures: 13-digit IDs. Search the structure name at everef.net, or
  open Show Info in-game and copy the structure ID from the URL or info panel.

### Finding `container_id` and `hangar_flag`

Run the bot with `--debug` to dump everything ESI returns for your structure:

```bash
docker compose run --rm monitor python fuel_monitor.py --debug
```

This prints all items at your `location_id` and — if `container_id` is set —
everything inside the container, including the `location_flag` it reports.

### Getting a Discord Role ID (for @mentions)

1. Enable **Developer Mode**: Discord → User Settings → Advanced → Developer Mode.
2. Server Settings → Roles → right-click the role → **Copy Role ID**.
3. Paste into `discord.mention_role_id`.

---

## State file (`data/state.json`)

The bot writes `data/state.json` after every check to remember the last known
fuel counts. It survives container restarts because it lives in the `./data/`
volume mount. Do not edit it manually. To force a fresh "first reading" post
to Discord (e.g. after changing containers), delete `state.json` and restart.

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
| `TOKEN_PATH` | `./tokens.json` | Path for OAuth token storage |
| `STATE_PATH` | `./state.json` | Path for fuel count state |
| `WATCH_INTERVAL` | `60` | Minutes between checks (used by Docker CMD) |

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
  a password. Listed in `.gitignore` and `.dockerignore`.
- `data/config.yaml` contains your Discord webhook URL — also keep it out of
  version control (in `.gitignore`).
- The bot uses **PKCE OAuth2** — no client secret is ever stored anywhere.
- ESI access tokens expire after 20 minutes and are refreshed automatically.
- Your EVE account password is never seen by this application.
- The container runs as a non-root user (`appuser`).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `403 Forbidden` from ESI | Character lacks Director role, or wrong ESI scope was granted when authenticating |
| Container not found | Verify `container_id` is correct; run `--debug` to inspect what ESI returns |
| Wrong hangar slot error | Update `hangar_flag` in config to match what `--debug` reports |
| `500 Internal Server Error` from EVE SSO | `callback_url` in config doesn't match the Callback URL in your ESI app — they must be identical |
| "Could not find a code value" | Copy the full address-bar URL after the redirect, not just part of it |
| Discord `400 Bad Request` | Webhook URL is malformed |
| Discord `404 Not Found` | Webhook was deleted in Discord — recreate it and update config |
| No Discord post despite change | Check that `discord.webhook_url` is set; check logs for `[!]` errors |
| Want to reset the diff baseline | Delete `data/state.json` and restart — next check posts as a "first reading" |
| Token expired / revoked | Re-run `docker compose run --rm auth` |
