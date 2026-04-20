#!/usr/bin/env python3
"""
EVE Online Corp Hangar Fuel Block Monitor
==========================================
Monitors fuel block quantities inside a specific container in a corp hangar
and posts to Discord only when the count changes (added or removed).

Requires Director role on your character.

Environment variables (override defaults, useful in Docker):
  CONFIG_PATH   path to config.yaml  (default: ./config.yaml)
  TOKEN_PATH    path to tokens.json  (default: ./tokens.json)
  STATE_PATH    path to state.json   (default: ./state.json)

Usage:
  python fuel_monitor.py --auth          # interactive EVE SSO login (run once)
  python fuel_monitor.py                 # single check
  python fuel_monitor.py --watch 60      # poll every 60 minutes (default)
  python fuel_monitor.py --discord-test  # send a test embed to Discord
  python fuel_monitor.py --debug         # dump raw ESI data for the target
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
STATE_FILE   = Path(os.environ.get("STATE_PATH",  "state.json"))

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
    if not cfg.get("callback_url"):
        print("[ERROR] callback_url is missing from config.yaml\n"
              "        It must exactly match the Callback URL set in your ESI app.\n"
              "        Example:  callback_url: \"http://localhost/callback\"")
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


# ── State persistence (last-known fuel counts) ────────────────────────────────

def load_state() -> dict:
    """
    Return the persisted state dict.
    Schema:
      {
        "counts": {                     # keyed by container_id (as str)
          "1052272591764": {
            "total": 4800,
            "by_type": { "4051": 2400, "4312": 2400 }   # type_id keys as str
          }
        },
        "last_checked": "2026-04-19T12:00:00+00:00"
      }
    """
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass   # treat a corrupt state file as empty
    return {"counts": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── OAuth2 PKCE ───────────────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier  = secrets.token_urlsafe(32)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def exchange_code(client_id: str, code: str, verifier: str, callback_url: str) -> dict:
    resp = requests.post(SSO_TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     client_id,
        "redirect_uri":  callback_url,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not resp.ok:
        print(f"\n[ERROR] EVE SSO token exchange failed: {resp.status_code}")
        print(f"        Response: {resp.text[:500]}")
        print(f"\n  Common causes:")
        print(f"  • callback_url in config.yaml doesn't exactly match your ESI app registration")
        print(f"  • The authorization code was already used or has expired (codes are single-use)")
        print(f"  • The code was copied incorrectly — make sure there are no trailing spaces")
        print(f"\n  Your configured callback_url: {callback_url}")
        sys.exit(1)
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
      2. User logs in; EVE redirects to callback_url which won't load —
         that's fine. The user copies the full redirect URL (or just the
         code= value) from the browser address bar and pastes it here.
      3. We extract the code, exchange it for tokens, and save tokens.json.

    IMPORTANT: cfg["callback_url"] must exactly match the Callback URL
    registered in your ESI application at developers.eveonline.com.
    """
    callback_url        = cfg["callback_url"]
    verifier, challenge = generate_pkce()
    state               = secrets.token_hex(8)

    params = {
        "response_type":         "code",
        "redirect_uri":          callback_url,
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
    print("  Step 3 — Your browser will redirect to a page that won't load.")
    print(f"           That's expected. Copy the full URL from your browser's")
    print(f"           address bar and paste it at the prompt below.")
    print(f"           It looks like:  {callback_url}?code=...&state=...")
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
        qs        = parse_qs(urlparse(raw).query)
        code      = qs.get("code", [None])[0]
        got_state = qs.get("state", [None])[0]
    else:
        # User pasted the raw code string (or "code=xxxxx")
        if raw.startswith("code="):
            raw = raw[len("code="):]
        code      = raw
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
    tokens = exchange_code(cfg["client_id"], code, verifier, callback_url)

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

        total_pages = int(resp.headers.get("X-Pages", 1))
        page_data   = resp.json()
        print(f"[~] Asset page {page}/{total_pages}  ({len(page_data)} items)…")
        all_assets.extend(page_data)

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

def find_fuel_in_containers(assets: list[dict], cfg: dict, debug: bool = False) -> list[dict]:
    """
    Walk the ESI asset tree and return fuel block data for matched containers.

    ESI asset tree for a corp hangar inside a player-owned structure:

        Structure 1051781871633          ← root; NOT an item_id
          └─ OfficeFolder item           ← location_id = structure_id
               └─ CorpSAG1 item         ← location_id = OfficeFolder item_id
                    └─ Container         ← location_id = CorpSAG1 item_id
                         └─ Fuel stacks  ← location_id = container item_id, quantity = N

    Key facts that inform this code:
      • location_id in the flat ESI response is EITHER a real location (station/
        structure ID) OR the item_id of the parent asset. You tell them apart by
        checking whether location_id exists as an item_id in the asset list.
      • Fuel blocks are non-singleton stacks. ESI always returns a quantity field
        for non-singleton items. quantity is NEVER missing for stacked items.
      • Containers (singletons) have is_singleton=True and quantity=1 (implied).
      • Nothing except the OfficeFolder level sits directly at the structure ID.
        The container is 3 levels deep, so a flat location_id == structure_id
        filter will never find it.

    Strategy:
      • Fast path (container_id known): look up the container directly by item_id.
        No location or flag filters — just a dict lookup. Verify it traces back
        to target_location via iterative parent walk.
      • General path (no container_id): BFS from target_location through the
        children map to find all reachable singletons in corp hangar flags.
    """
    target_location     = cfg.get("location_id")       # int or None
    target_container_id = cfg.get("container_id")       # int or None

    # ── Build lookup structures ───────────────────────────────────────────────
    # by_id: item_id → asset dict  (for O(1) parent-walk and direct lookup)
    by_id: dict[int, dict] = {a["item_id"]: a for a in assets}

    # children: location_id → [asset, ...]
    # Only index entries where location_id is present; skip missing/None.
    children: dict[int, list[dict]] = {}
    for a in assets:
        loc = a.get("location_id")
        if loc is not None:
            children.setdefault(loc, []).append(a)

    # ── Helper: iterative parent walk to find the root location ──────────────
    def root_location_of(item: dict) -> int | None:
        """
        Walk up the asset tree from item until location_id is NOT an item_id
        in our corpus — that value is the structure/station ID.
        Returns None if we exceed 20 hops (cycle guard).
        """
        seen: set[int] = set()
        current = item
        for _ in range(20):
            loc = current.get("location_id")
            if loc is None:
                return None
            if loc not in by_id:
                return loc          # this is the real location (structure/station)
            if loc in seen:
                return None         # cycle — should never happen in valid ESI data
            seen.add(loc)
            current = by_id[loc]
        return None                 # exceeded hop limit

    # ── Debug output ──────────────────────────────────────────────────────────
    if debug:
        direct = children.get(target_location, [])
        print(f"\n[DEBUG] Direct children of structure {target_location}: {len(direct)}")
        for a in direct[:30]:
            print(f"  item_id={a['item_id']}  type_id={a.get('type_id')}  "
                  f"flag={a.get('location_flag')}  singleton={a.get('is_singleton')}  "
                  f"qty={a.get('quantity', '(singleton)')}")
        if target_container_id:
            c = by_id.get(target_container_id)
            if c:
                root = root_location_of(c)
                print(f"\n[DEBUG] Container {target_container_id}:")
                print(f"  type_id={c.get('type_id')}  flag={c.get('location_flag')}  "
                      f"singleton={c.get('is_singleton')}  location_id={c.get('location_id')}")
                print(f"  Root location traces to: {root}")
                inside = children.get(target_container_id, [])
                print(f"  Items inside container ({len(inside)}):")
                for a in inside[:50]:
                    print(f"    item_id={a['item_id']}  type_id={a.get('type_id')}  "
                          f"flag={a.get('location_flag')}  qty={a.get('quantity', '(singleton)')}")
            else:
                print(f"\n[DEBUG] container_id {target_container_id} NOT FOUND in assets.")
        print()

    # ── Fast path: container_id is known ─────────────────────────────────────
    if target_container_id:
        container = by_id.get(target_container_id)

        if container is None:
            print(f"[!] container_id {target_container_id} not found in corp assets.")
            print(f"    Verify the item_id is correct and belongs to this corporation.")
            return []

        # Confirm the container traces back to the expected structure
        if target_location is not None:
            actual_root = root_location_of(container)
            if actual_root != target_location:
                print(f"[!] Container {target_container_id} traces to location "
                      f"{actual_root}, not {target_location}.")
                print(f"    Check location_id in config.yaml.")
                return []

        # Confirm the container sits in the expected hangar slot (if configured)
        target_hangar_flag = cfg.get("hangar_flag", "").strip() or None
        actual_flag        = container.get("location_flag")
        if target_hangar_flag and actual_flag != target_hangar_flag:
            print(f"[!] Container {target_container_id} is in slot '{actual_flag}', "
                  f"not '{target_hangar_flag}'.")
            print(f"    Check hangar_flag in config.yaml.")
            return []

        # Find fuel blocks whose location_id == container's item_id.
        # Fuel blocks are non-singleton stacks: quantity is always present.
        # We do NOT default quantity — if it's missing, something is wrong with
        # the ESI response and we want to surface that rather than count 0.
        fuel_inside = []
        for item in children.get(target_container_id, []):
            if item.get("type_id") not in FUEL_BLOCK_TYPE_IDS:
                continue
            if "quantity" not in item:
                print(f"[!] Fuel block item_id={item['item_id']} type_id={item['type_id']} "
                      f"is missing 'quantity' in ESI response — skipping.")
                continue
            fuel_inside.append(item)

        return [{
            "container_id":   target_container_id,
            "container_type": container["type_id"],
            "location_flag":  actual_flag,
            "fuel":           fuel_inside,
        }]

    # ── General path: BFS from target_location to find all containers ─────────
    # These are the flags that indicate an item is sitting inside a corp hangar
    # division, not inside another item (ship, container, etc.).
    CORP_HANGAR_FLAGS = {
        "CorpSAG1", "CorpSAG2", "CorpSAG3", "CorpSAG4",
        "CorpSAG5", "CorpSAG6", "CorpSAG7",
        "Hangar", "HangarAll",
    }

    if target_location is None:
        print("[!] location_id is not set in config.yaml — cannot scan for containers.")
        return []

    # BFS: collect every item reachable from the structure ID
    visited: set[int] = set()
    queue = list(children.get(target_location, []))
    while queue:
        item = queue.pop()
        iid  = item["item_id"]
        if iid in visited:
            continue
        visited.add(iid)
        queue.extend(children.get(iid, []))

    results = []
    for iid in visited:
        item = by_id[iid]
        # A container in a corp hangar is a singleton with a CorpSAG/Hangar flag
        if not item.get("is_singleton"):
            continue
        if item.get("location_flag") not in CORP_HANGAR_FLAGS:
            continue
        fuel_inside = []
        for child in children.get(iid, []):
            if child.get("type_id") not in FUEL_BLOCK_TYPE_IDS:
                continue
            if "quantity" not in child:
                print(f"[!] Fuel block item_id={child['item_id']} missing 'quantity' — skipping.")
                continue
            fuel_inside.append(child)
        results.append({
            "container_id":   iid,
            "container_type": item["type_id"],
            "location_flag":  item["location_flag"],
            "fuel":           fuel_inside,
        })

    return results


# ── Terminal Report ───────────────────────────────────────────────────────────

def print_report(results: list[dict], type_names: dict[int, str], cfg: dict) -> bool:
    """Pretty-print to stdout. Returns True if the total is below alert_threshold."""
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
        cname = type_names.get(r["container_type"], f"Type {r['container_type']}")
        print(f"\n  📦  {cname}  (id: {r['container_id']})")
        print(f"      Hangar slot: {r['location_flag']}")
        if not r["fuel"]:
            print("      — No fuel blocks found —")
            if threshold:
                print(f"\n      ⚠  ALERT: Container is empty (threshold: {threshold:,})")
                any_alert = True
        else:
            total = 0
            for f in r["fuel"]:
                fname  = FUEL_BLOCK_TYPE_IDS.get(f["type_id"], f"type {f['type_id']}")
                qty    = f["quantity"]        # quantity is always present for non-singleton stacks
                total += qty
                print(f"      • {fname:<28} {qty:>8,}")
            print(f"      {'─'*40}")
            print(f"      {'Total fuel blocks':<28} {total:>8,}")
            if threshold and total < threshold:
                print(f"\n      ⚠  ALERT: Total {total:,} is below threshold of {threshold:,}")
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
    prev_counts: dict,      # {container_id_str: {"total": int, "by_type": {type_id_str: int}}}
) -> list[dict]:
    """
    Build Discord embed dicts, one per container, showing the change in fuel count.
    prev_counts is the state from before this check.
    """
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
        cid_str    = str(r["container_id"])

        # Current totals
        current_by_type: dict[str, int] = {}
        for f in fuel_items:
            current_by_type[str(f["type_id"])] = f["quantity"]
        current_total = sum(current_by_type.values())

        # Previous totals (may be absent on first run)
        prev          = prev_counts.get(cid_str, {})
        prev_by_type  = prev.get("by_type", {})
        prev_total    = prev.get("total", None)   # None = no prior reading

        # Colour and status
        is_empty = current_total == 0
        is_low   = bool(threshold and current_total < threshold and not is_empty)
        if is_empty:
            color, status = COLOR_EMPTY, "🔴  **EMPTY** — No fuel blocks found!"
        elif is_low:
            color, status = COLOR_LOW,   f"🟡  **LOW** — {current_total:,} blocks (threshold: {threshold:,})"
        else:
            color, status = COLOR_OK,    f"🟢  **OK** — {current_total:,} fuel blocks"

        # Change summary line
        if prev_total is None:
            change_line = "_First reading — no previous count to compare._"
        else:
            delta = current_total - prev_total
            if delta > 0:
                change_line = f"📈  **+{delta:,}** added  ({prev_total:,} → {current_total:,})"
            elif delta < 0:
                change_line = f"📉  **{delta:,}** removed  ({prev_total:,} → {current_total:,})"
            else:
                change_line = f"↔️  No change  ({current_total:,})"   # shouldn't reach Discord normally

        # Per-type breakdown with per-type delta
        lines = []
        all_type_ids = set(current_by_type) | set(prev_by_type)
        for tid_str in sorted(all_type_ids, key=lambda t: FUEL_BLOCK_TYPE_IDS.get(int(t), t)):
            fname   = FUEL_BLOCK_TYPE_IDS.get(int(tid_str), f"Type {tid_str}")
            curr_q  = current_by_type.get(tid_str, 0)
            prev_q  = prev_by_type.get(tid_str, 0)
            type_delta = curr_q - prev_q
            delta_str  = f"  `({type_delta:+,})`" if prev_total is not None and type_delta != 0 else ""
            emoji      = _status_emoji(curr_q, threshold)
            lines.append(f"{emoji}  **{fname}**  `{curr_q:,}`{delta_str}")

        breakdown = "\n".join(lines) if lines else "_No fuel blocks._"

        embeds.append({
            "title":       f"📦  {cname}",
            "description": f"{status}\n\n{change_line}\n\n{breakdown}",
            "color":       color,
            "fields": [
                {"name": "Hangar Slot",     "value": r["location_flag"],       "inline": True},
                {"name": "Container ID",    "value": f"`{r['container_id']}`", "inline": True},
                {"name": "Alert Threshold", "value": f"`{threshold:,}`" if threshold else "_not set_", "inline": True},
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

def run_check(cfg: dict, debug: bool = False) -> bool:
    """
    Run one ESI check. Compares current fuel counts against persisted state.
    Posts to Discord only if the count changed. Returns True if alerted.
    """
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

    results    = find_fuel_in_containers(assets, cfg, debug=debug)
    type_ids   = list({r["container_type"] for r in results})
    type_names = resolve_names(type_ids)

    # ── Build current counts snapshot ────────────────────────────────────────
    # {container_id_str: {"total": int, "by_type": {type_id_str: qty}}}
    current_counts: dict[str, dict] = {}
    for r in results:
        by_type = {str(f["type_id"]): f["quantity"] for f in r["fuel"]}
        current_counts[str(r["container_id"])] = {
            "total":   sum(by_type.values()),
            "by_type": by_type,
        }

    # ── Load previous state and detect changes ───────────────────────────────
    state      = load_state()
    prev_counts: dict = state.get("counts", {})

    changed = False
    for cid_str, curr in current_counts.items():
        prev = prev_counts.get(cid_str)
        if prev is None or prev["total"] != curr["total"]:
            changed = True
            break

    # Also flag as changed if a container that existed before has disappeared
    for cid_str in prev_counts:
        if cid_str not in current_counts:
            changed = True
            break

    # ── Terminal report (always) ──────────────────────────────────────────────
    alerted = print_report(results, type_names, cfg)

    if changed:
        print("[~] Count changed — will post to Discord.")
    else:
        print("[~] No change in fuel count since last check — skipping Discord.")

    # ── Persist new state ─────────────────────────────────────────────────────
    state["counts"] = current_counts
    save_state(state)

    # ── Discord (only on change) ──────────────────────────────────────────────
    discord_cfg = cfg.get("discord") or {}
    webhook_url = discord_cfg.get("webhook_url", "")

    if webhook_url and changed:
        embeds  = build_discord_embeds(results, type_names, cfg, tokens, prev_counts)
        role_id = discord_cfg.get("mention_role_id", "").strip() or None
        content = f"<@&{role_id}> ⚠️ Fuel alert!" if (alerted and role_id) else None
        print("[→] Posting to Discord…")
        post_to_discord(webhook_url, embeds, content=content)
    elif not webhook_url:
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
    parser.add_argument("--debug",        action="store_true",
                        help="Print raw ESI asset data for the target location/container")
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
                run_check(cfg, debug=args.debug)
                print(f"[…] Next check in {args.watch} minute(s)…\n")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[✓] Monitor stopped.")
    else:
        run_check(cfg, debug=args.debug)


if __name__ == "__main__":
    main()
