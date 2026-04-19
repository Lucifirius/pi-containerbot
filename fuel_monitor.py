#!/usr/bin/env python3
"""
EVE Online Corp Hangar Fuel Block Monitor
==========================================
Monitors fuel block quantities inside a specific container in a corp hangar
and posts rich embed reports to a Discord channel via webhook.

Requires Director role on your character.

Environment variables (override defaults, useful in Docker):
  CONFIG_PATH   path to config.yaml  (default: ./config.yaml)
  TOKEN_PATH    path to tokens.json  (default: ./tokens.json)

Usage:
  python fuel_monitor.py --auth          # interactive EVE SSO login (run once)
  python fuel_monitor.py                 # single check
  python fuel_monitor.py --watch 60      # poll every 60 minutes
  python fuel_monitor.py --discord-test  # send a test embed to Discord
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import yaml

# ── Runtime paths (overridable via env for Docker) ───────────────────────────

CONFIG_FILE  = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
TOKEN_FILE   = Path(os.environ.get("TOKEN_PATH",  "tokens.json"))

# The redirect URI registered in your ESI app.
# EVE SSO redirects here after login; we extract the code from the terminal
# instead of running a local server, so the URI just needs to match your app
# registration — it does not need to be reachable.
CALLBACK_URL = "http://localhost/callback"

# ── ESI / SSO constants ───────────────────────────────────────────────────────

ESI_BASE       = "https://esi.evetech.net/latest"
SSO_AUTH_URL   = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL  = "https://login.eveonline.com/v2/oauth/token"
REQUIRED_SCOPE = "esi-assets.read_corporation_assets.v1"

# Fuel block type IDs (all four racial variants)
FUEL_BLOCK_TYPE_IDS = {
    4051: "Caldari Fuel Block",
    4246: "Gallente Fuel Block",
    4247: "Minmatar Fuel Block",
    4312: "Amarr Fuel Block",
}

# Discord embed colours (decimal int)
COLOR_OK    = 0x57F287   # green
COLOR_LOW   = 0xFEE75C   # yellow
COLOR_EMPTY = 0xED4245   # red
COLOR_INFO  = 0x5865F2   # blurple


# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[ERROR] Config not found at {CONFIG_FILE}.\n"
              f"        Mount config.yaml into the container at that path.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    if not cfg.get("client_id"):
        print("[ERROR] client_id is missing from config.yaml")
        sys.exit(1)
    return cfg


def load_tokens() -> dict | None:
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


def save_tokens(tokens: dict):
    # Ensure parent dir exists (important inside containers with mounted volumes)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"[✓] Tokens saved to {TOKEN_FILE}")


# ── OAuth2 PKCE ───────────────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier  = secrets.token_urlsafe(32)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def exchange_code(client_id: str, code: str, verifier: str) -> dict:
    resp = requests.post(SSO_TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     client_id,
        "redirect_uri":  CALLBACK_URL,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(client_id: str, refresh_token: str) -> dict:
    resp = requests.post(SSO_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     client_id,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp.raise_for_status()
    return resp.json()


def get_valid_token(cfg: dict) -> str:
    """Return a valid access token, auto-refreshing if it has expired."""
    tokens = load_tokens()
    if not tokens:
        print("[ERROR] No tokens found.\n"
              "        Run:  docker compose run --rm auth")
        sys.exit(1)
    expires_at = tokens.get("expires_at", 0)
    if time.time() >= expires_at - 60:
        print("[~] Access token expired — refreshing…")
        new = refresh_access_token(cfg["client_id"], tokens["refresh_token"])
        tokens["access_token"]  = new["access_token"]
        tokens["refresh_token"] = new.get("refresh_token", tokens["refresh_token"])
        tokens["expires_at"]    = time.time() + new["expires_in"]
        save_tokens(tokens)
    return tokens["access_token"]


# ── Auth Flow ─────────────────────────────────────────────────────────────────

def do_auth(cfg: dict):
    """
    Interactive PKCE auth flow — no local HTTP server required.

    Workflow:
      1. Prints the EVE SSO login URL (and tries to open it in a browser).
      2. User logs in; EVE redirects to CALLBACK_URL which won't load —
         that's fine. The user copies the full redirect URL (or just the
         code= value) from the browser address bar and pastes it here.
      3. We extract the code, exchange it for tokens, and save tokens.json.
    """
    verifier, challenge = generate_pkce()
    state = secrets.token_hex(8)

    params = {
        "response_type":         "code",
        "redirect_uri":          CALLBACK_URL,
        "client_id":             cfg["client_id"],
        "scope":                 REQUIRED_SCOPE,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{SSO_AUTH_URL}?{urlencode(params)}"

    print("\n" + "═" * 65)
    print("  EVE SSO Authentication")
    print("═" * 65)
    print("\n  Step 1 — Open this URL in your browser:\n")
    print(f"  {auth_url}\n")
    print("  Step 2 — Log in with your Director character and approve the scope.")
    print()
    print("  Step 3 — Your browser will be redirected to a page that")
    print(f"           won't load (that's expected). Copy the full URL")
    print(f"           from your browser's address bar and paste it below.")
    print("           It looks like:  http://localhost/callback?code=...&state=...")
    print("           You can also paste just the code= value if you prefer.")
    print("═" * 65 + "\n")

    # Try to open the browser; silently ignore failures (e.g. headless Docker)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Prompt until we get a non-empty response
    while True:
        raw = input("  Paste the redirect URL (or code value) here: ").strip()
        if raw:
            break
        print("  [!] Nothing entered — please try again.")

    # Accept either the full redirect URL or a bare code value
    if raw.startswith("http"):
        qs   = parse_qs(urlparse(raw).query)
        code = qs.get("code", [None])[0]
        got_state = qs.get("state", [None])[0]
    else:
        # User pasted the raw code string (or "code=xxxxx")
        if raw.startswith("code="):
            raw = raw[len("code="):]
        code = raw
        got_state = None   # can't verify state from a bare code

    if not code:
        print("\n[ERROR] Could not find a 'code' value in what you pasted.")
        print("        Make sure you copy the full address-bar URL after the redirect.")
        sys.exit(1)

    # Verify state only when we have it (full URL was pasted)
    if got_state is not None and got_state != state:
        print("\n[ERROR] State mismatch — the URL may have been tampered with.")
        sys.exit(1)

    print("\n[→] Exchanging code for tokens…")
    tokens = exchange_code(cfg["client_id"], code, verifier)

    # Decode character info from the JWT payload (no signature check needed)
    payload_b64    = tokens["access_token"].split(".")[1]
    padding        = 4 - len(payload_b64) % 4
    payload        = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
    character_id   = int(payload["sub"].split(":")[-1])
    character_name = payload.get("name", "Unknown")

    corp_resp = requests.get(
        f"{ESI_BASE}/characters/{character_id}/",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    corp_resp.raise_for_status()
    corporation_id = corp_resp.json()["corporation_id"]

    tokens["expires_at"]     = time.time() + tokens["expires_in"]
    tokens["character_id"]   = character_id
    tokens["character_name"] = character_name
    tokens["corporation_id"] = corporation_id
    save_tokens(tokens)

    print(f"\n[✓] Authenticated as: {character_name}  (corp ID: {corporation_id})")
    print("[✓] tokens.json written to the data volume.")
    print("[✓] You can now start the monitor:  docker compose up -d monitor")


# ── ESI Asset Fetching ────────────────────────────────────────────────────────

def fetch_all_corp_assets(access_token: str, corporation_id: int) -> list[dict]:
    """Fetch every page of corporation assets from ESI."""
    all_assets = []
    page = 1
    while True:
        resp = requests.get(
            f"{ESI_BASE}/corporations/{corporation_id}/assets/",
            params={"page": page},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept":        "application/json",
            },
        )
        if resp.status_code == 403:
            print("[ERROR] 403 Forbidden — verify Director role and ESI scope.")
            sys.exit(1)
        resp.raise_for_status()
        page_data = resp.json()
        if not page_data:
            break
        all_assets.extend(page_data)
        total_pages = int(resp.headers.get("X-Pages", 1))
        print(f"[~] Asset page {page}/{total_pages}  ({len(page_data)} items)…")
        if page >= total_pages:
            break
        page += 1
    return all_assets


def resolve_names(ids: list[int]) -> dict[int, str]:
    """Batch-resolve type IDs → human-readable names via ESI."""
    if not ids:
        return {}
    resp = requests.post(
        f"{ESI_BASE}/universe/names/",
        json=list(set(ids)),
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return {item["id"]: item["name"] for item in resp.json()}


# ── Core Logic ────────────────────────────────────────────────────────────────

def find_fuel_in_containers(assets: list[dict], cfg: dict) -> list[dict]:
    """
    Walk the ESI asset tree and return fuel block data for matched containers.

    Matching rules (all applied together):
      location_id   — station/structure the container sits in (required in cfg)
      container_id  — specific item_id (optional; most precise)
      Containers must be singletons sitting in a corp hangar flag.
    """
    target_location     = cfg.get("location_id")
    target_container_id = cfg.get("container_id")

    HANGAR_FLAGS = {
        "CorpSAG1", "CorpSAG2", "CorpSAG3", "CorpSAG4",
        "CorpSAG5", "CorpSAG6", "CorpSAG7", "Hangar", "HangarAll",
    }

    # Build parent_id → [children] map for O(1) child lookup
    children: dict[int, list[dict]] = {}
    for a in assets:
        children.setdefault(a.get("location_id", 0), []).append(a)

    results = []
    for item in assets:
        if target_location and item.get("location_id") != target_location:
            continue
        if item.get("location_flag") not in HANGAR_FLAGS:
            continue
        if not item.get("is_singleton"):          # containers are always singletons
            continue
        if target_container_id and item["item_id"] != target_container_id:
            continue

        cid         = item["item_id"]
        fuel_inside = [
            c for c in children.get(cid, [])
            if c.get("type_id") in FUEL_BLOCK_TYPE_IDS
        ]
        if fuel_inside or not target_container_id:
            results.append({
                "container_id":   cid,
                "container_type": item["type_id"],
                "location_flag":  item["location_flag"],
                "fuel":           fuel_inside,
            })

    return results


# ── Terminal Report ───────────────────────────────────────────────────────────

def print_report(results: list[dict], type_names: dict[int, str], cfg: dict) -> bool:
    """Pretty-print to stdout. Returns True if any alert threshold was breached."""
    threshold = cfg.get("alert_threshold", 0)
    now       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'═'*55}")
    print(f"  Fuel Block Monitor  ·  {now}")
    print(f"{'═'*55}")

    if not results:
        print("  ⚠  No matching containers found.")
        print("  Check location_id / container_id in config.yaml")
        print(f"{'═'*55}\n")
        return False

    any_alert = False
    for r in results:
        cname = type_names.get(r["container_type"], f"Container {r['container_type']}")
        print(f"\n  📦  {cname}  (id: {r['container_id']})")
        print(f"      Hangar slot: {r['location_flag']}")
        if not r["fuel"]:
            print("      — No fuel blocks found —")
        else:
            total = 0
            for f in r["fuel"]:
                fname = FUEL_BLOCK_TYPE_IDS.get(f["type_id"], f"type {f['type_id']}")
                qty   = f.get("quantity", 1)
                total += qty
                flag  = " ⚠  LOW" if threshold and qty < threshold else ""
                print(f"      • {fname:<28} {qty:>8,}{flag}")
                if flag:
                    any_alert = True
            print(f"      {'─'*40}")
            print(f"      {'Total fuel blocks':<28} {total:>8,}")
            if threshold and total < threshold:
                print(f"\n      ⚠  ALERT: Total below threshold of {threshold:,}")
                any_alert = True

    print(f"\n{'═'*55}\n")
    return any_alert


# ── Discord ───────────────────────────────────────────────────────────────────

def _status_emoji(qty: int, threshold: int) -> str:
    if qty == 0:
        return "🔴"
    if threshold and qty < threshold:
        return "🟡"
    return "🟢"


def build_discord_embeds(
    results: list[dict],
    type_names: dict[int, str],
    cfg: dict,
    tokens: dict,
) -> list[dict]:
    """Build Discord embed dicts (one per container) for the webhook payload."""
    threshold = cfg.get("alert_threshold", 0)
    corp_id   = tokens.get("corporation_id", 0)
    char_id   = tokens.get("character_id", 0)
    char_name = tokens.get("character_name", "Unknown")
    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_iso   = datetime.now(timezone.utc).isoformat()

    if not results:
        return [{
            "title":       "⚠️  No Containers Found",
            "description": (
                "No matching containers were found at the specified location.\n"
                "Check `location_id` / `container_id` in `config.yaml`."
            ),
            "color":     COLOR_EMPTY,
            "footer":    {"text": f"EVE Fuel Monitor • {now_str}"},
            "timestamp": now_iso,
        }]

    embeds = []
    for r in results:
        cname      = type_names.get(r["container_type"], f"Type {r['container_type']}")
        fuel_items = r["fuel"]
        total      = sum(f.get("quantity", 1) for f in fuel_items)
        is_empty   = total == 0
        is_low     = bool(threshold and total < threshold and not is_empty)

        if is_empty:
            color, status_line = COLOR_EMPTY, "🔴  **EMPTY** — No fuel blocks found!"
        elif is_low:
            color, status_line = COLOR_LOW,   f"🟡  **LOW** — Below threshold of {threshold:,}"
        else:
            color, status_line = COLOR_OK,    "🟢  **OK** — Fuel levels nominal"

        if fuel_items:
            lines = []
            for f in fuel_items:
                fname = FUEL_BLOCK_TYPE_IDS.get(f["type_id"], f"Type {f['type_id']}")
                qty   = f.get("quantity", 1)
                lines.append(f"{_status_emoji(qty, threshold)}  **{fname}**  `{qty:,}`")
            breakdown  = "\n".join(lines)
            breakdown += f"\n\n**Total  ›  `{total:,}` fuel blocks**"
        else:
            breakdown = "_No fuel blocks inside this container._"

        embeds.append({
            "title":       f"📦  {cname}",
            "description": f"{status_line}\n\n{breakdown}",
            "color":       color,
            "fields": [
                {"name": "Hangar Slot",      "value": r["location_flag"],         "inline": True},
                {"name": "Container ID",     "value": f"`{r['container_id']}`",   "inline": True},
                {"name": "Alert Threshold",  "value": f"`{threshold:,}`" if threshold else "_not set_", "inline": True},
            ],
            "thumbnail": {"url": f"https://images.evetech.net/corporations/{corp_id}/logo"},
            "footer": {
                "text":     f"EVE Fuel Monitor • {char_name} • {now_str}",
                "icon_url": f"https://images.evetech.net/characters/{char_id}/portrait",
            },
            "timestamp": now_iso,
        })

    return embeds


def post_to_discord(webhook_url: str, embeds: list[dict], content: str | None = None):
    """POST embeds to Discord. Chunks into groups of 10 (Discord's limit)."""
    CHUNK = 10
    for i in range(0, max(1, len(embeds)), CHUNK):
        payload: dict = {"embeds": embeds[i : i + CHUNK]}
        if content and i == 0:
            payload["content"] = content
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 204:
            print(f"[✓] Discord chunk {i // CHUNK + 1} posted.")
        else:
            print(f"[!] Discord {resp.status_code}: {resp.text[:300]}")


def send_discord_test(cfg: dict, tokens: dict):
    webhook_url = (cfg.get("discord") or {}).get("webhook_url", "")
    if not webhook_url:
        print("[ERROR] discord.webhook_url is not set in config.yaml")
        sys.exit(1)
    embed = {
        "title":       "🛰️  EVE Fuel Monitor — Test Message",
        "description": (
            "Connection successful!\n\n"
            "The bot is configured correctly and will post fuel reports here."
        ),
        "color":  COLOR_INFO,
        "fields": [
            {"name": "Character", "value": tokens.get("character_name", "?"), "inline": True},
            {"name": "Corp ID",   "value": str(tokens.get("corporation_id", "?")), "inline": True},
        ],
        "footer":    {"text": "EVE Fuel Monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    print("[→] Sending test message to Discord…")
    post_to_discord(webhook_url, [embed])


# ── Main Check ────────────────────────────────────────────────────────────────

def run_check(cfg: dict) -> bool:
    tokens = load_tokens()
    if not tokens:
        print("[ERROR] Not authenticated.\n"
              "        Run:  docker compose run --rm auth")
        sys.exit(1)

    access_token   = get_valid_token(cfg)
    corporation_id = tokens["corporation_id"]

    print(f"[→] Fetching corp assets for corporation {corporation_id}…")
    assets = fetch_all_corp_assets(access_token, corporation_id)
    print(f"[✓] Total assets fetched: {len(assets)}")

    results    = find_fuel_in_containers(assets, cfg)
    type_ids   = list({r["container_type"] for r in results})
    type_names = resolve_names(type_ids)
    alerted    = print_report(results, type_names, cfg)

    discord_cfg = cfg.get("discord") or {}
    webhook_url = discord_cfg.get("webhook_url", "")

    if webhook_url:
        post_on_alert_only = discord_cfg.get("post_on_alert_only", False)
        should_post        = (not post_on_alert_only) or alerted
        if should_post:
            embeds  = build_discord_embeds(results, type_names, cfg, tokens)
            role_id = discord_cfg.get("mention_role_id")
            content = f"<@&{role_id}> ⚠️ Fuel alert!" if (alerted and role_id) else None
            print("[→] Posting to Discord…")
            post_to_discord(webhook_url, embeds, content=content)
        else:
            print("[~] Discord: no alert — skipping post (post_on_alert_only=true).")
    else:
        print("[~] No Discord webhook configured — skipping.")

    return alerted


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="EVE Online Corp Hangar Fuel Block Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (bare Python):\n"
            "  python fuel_monitor.py --auth\n"
            "  python fuel_monitor.py --watch 60\n"
            "  python fuel_monitor.py --discord-test\n\n"
            "Examples (Docker):\n"
            "  docker compose run --rm auth\n"
            "  docker compose up -d monitor\n"
            "  docker compose run --rm discord-test\n"
        ),
    )
    parser.add_argument("--auth",         action="store_true",
                        help="Run interactive OAuth2 PKCE auth flow")
    parser.add_argument("--watch",        type=int, metavar="MINUTES",
                        help="Poll every N minutes (runs forever)")
    parser.add_argument("--discord-test", action="store_true",
                        help="Send a test embed to the Discord webhook")
    args = parser.parse_args()

    cfg = load_config()

    if args.auth:
        do_auth(cfg)
        return

    if args.discord_test:
        tokens = load_tokens()
        if not tokens:
            print("[ERROR] Authenticate first.")
            sys.exit(1)
        send_discord_test(cfg, tokens)
        return

    if args.watch:
        interval = args.watch * 60
        print(f"[★] Watch mode — every {args.watch} min. Ctrl+C to stop.\n")
        try:
            while True:
                run_check(cfg)
                print(f"[…] Next check in {args.watch} minute(s)…\n")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[✓] Monitor stopped.")
    else:
        run_check(cfg)


if __name__ == "__main__":
    main()
