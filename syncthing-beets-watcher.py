#!/usr/bin/env python3
"""
syncthing-beets-watcher.py

Watches Syncthing for Music Inbox sync completion,
automatically triggers beets import, then triggers a Plex music library scan.

LOGS: tail -f /tmp/syncthing-beets-watcher.log

Deploy as an Unraid User Script set to:
  Schedule: At Startup of Array
  Run mode: Run in Background
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# ── Configuration ─────────────────────────────────────────────────────────────

# Syncthing GUI address (as seen from within Unraid host, not a container)
SYNCTHING_URL = "http://localhost:8384"

# Path to Syncthing's config.xml inside its appdata folder
SYNCTHING_CONFIG = "/mnt/user/appdata/syncthing/config.xml"

# Label of the Syncthing folder to watch (as shown in the Syncthing UI)
MUSIC_FOLDER_LABEL = "NewMusic"

# The beets import User Script — reuses your existing script so nothing is duplicated
BEETS_SCRIPT = "/boot/config/plugins/user.scripts/scripts/beets-inbox-import/script"

# Plex server address (from Unraid host perspective)
PLEX_URL = "http://localhost:32400"

# Path to Plex's Preferences.xml — token is read from here automatically
# Adjust if your Plex appdata lives elsewhere
PLEX_PREFS = "/mnt/user/appdata/PlexMediaServer/Preferences.xml"

# Type of Plex library to scan ("artist" = Music). Used to auto-find the section.
PLEX_LIBRARY_TYPE = "artist"

# Seconds to wait after sync completes before firing beets
DEBOUNCE_SECS = 20

# Log file location
LOG_FILE = "/tmp/syncthing-beets-watcher.log"

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Syncthing ─────────────────────────────────────────────────────────────────

def get_syncthing_api_key():
    """Read Syncthing API key from config.xml."""
    try:
        tree = ET.parse(SYNCTHING_CONFIG)
        key = tree.getroot().find(".//gui/apikey")
        if key is not None and key.text:
            return key.text.strip()
    except Exception as e:
        log.error(f"Failed to read Syncthing API key from {SYNCTHING_CONFIG}: {e}")
    return None


def st_get(api_key, path, timeout=70):
    """HTTP GET against the Syncthing REST API."""
    url = f"{SYNCTHING_URL}{path}"
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_folder_id(api_key):
    """Find the Syncthing folder ID matching MUSIC_FOLDER_LABEL."""
    folders = st_get(api_key, "/rest/config/folders", timeout=15)
    for f in folders:
        if f.get("label") == MUSIC_FOLDER_LABEL:
            return f["id"]
    return None


def folder_is_synced(api_key, folder_id):
    """Return True if the folder has nothing left to receive."""
    try:
        c = st_get(api_key, f"/rest/db/completion?folder={folder_id}", timeout=15)
        need = c.get("needFiles", 1) + c.get("needDirectories", 0) + c.get("needSymlinks", 0)
        return need == 0
    except Exception as e:
        log.warning(f"Could not check folder completion: {e}")
        return False


# ── Plex ──────────────────────────────────────────────────────────────────────

def get_plex_token():
    """Read Plex auth token from Preferences.xml."""
    try:
        tree = ET.parse(PLEX_PREFS)
        token = tree.getroot().get("PlexOnlineToken")
        if token:
            return token.strip()
    except Exception as e:
        log.warning(f"Could not read Plex token from {PLEX_PREFS}: {e}")
    return None


def get_plex_music_section(token):
    """Find the Plex music library section key by scanning /library/sections."""
    try:
        url = f"{PLEX_URL}/library/sections?X-Plex-Token={token}"
        with urllib.request.urlopen(url, timeout=10) as r:
            tree = ET.fromstring(r.read())
        for directory in tree.findall("Directory"):
            if directory.get("type") == PLEX_LIBRARY_TYPE:
                key = directory.get("key")
                title = directory.get("title", "?")
                log.info(f"Found Plex music library: '{title}' (section {key})")
                return key
    except Exception as e:
        log.warning(f"Could not query Plex library sections: {e}")
    return None


def trigger_plex_scan(token, section_key):
    """Tell Plex to refresh the music library section."""
    try:
        url = f"{PLEX_URL}/library/sections/{section_key}/refresh?X-Plex-Token={token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        log.info(f"Plex library scan triggered (section {section_key}) ✓")
    except Exception as e:
        log.error(f"Failed to trigger Plex scan: {e}")


# ── Beets ─────────────────────────────────────────────────────────────────────

def run_beets():
    """Fire the beets import User Script."""
    if not os.path.isfile(BEETS_SCRIPT):
        log.error(f"Beets script not found at {BEETS_SCRIPT} — update BEETS_SCRIPT in config")
        return False

    log.info(f"Triggering beets import: {BEETS_SCRIPT}")
    try:
        result = subprocess.run(
            ["bash", BEETS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.stdout.strip():
            log.info(f"beets output:\n{result.stdout.strip()}")
        if result.stderr.strip():
            log.warning(f"beets stderr:\n{result.stderr.strip()}")
        if result.returncode == 0:
            log.info("beets import finished successfully ✓")
            return True
        else:
            log.error(f"beets exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        log.error("beets import timed out after 10 minutes")
    except Exception as e:
        log.error(f"Failed to run beets: {e}")
    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def watch(st_key, folder_id, plex_token, plex_section):
    """
    Long-poll Syncthing's StateChanged event stream.
    When the music folder transitions to 'idle' and is fully synced,
    run beets then trigger a Plex library scan.
    """
    log.info(f"Watching Syncthing folder '{MUSIC_FOLDER_LABEL}' (id={folder_id})")
    last_event_id = 0
    debounce_until = None

    while True:
        try:
            events = st_get(
                st_key,
                f"/rest/events?events=StateChanged&since={last_event_id}&timeout=60",
                timeout=70,
            )

            for event in events:
                last_event_id = max(last_event_id, event["id"])
                data = event.get("data", {})

                if data.get("folder") != folder_id:
                    continue

                from_state = data.get("from", "")
                to_state   = data.get("to", "")
                log.debug(f"StateChanged: {from_state} → {to_state}")

                if to_state == "idle" and from_state in ("syncing", "sync-preparing", "scan-waiting", "scanning"):
                    if folder_is_synced(st_key, folder_id):
                        log.info(f"Sync complete — beets will run in {DEBOUNCE_SECS}s")
                        debounce_until = time.time() + DEBOUNCE_SECS
                    else:
                        log.debug("Folder idle but still has pending items — skipping")

            # Fire beets then Plex scan once debounce elapses
            if debounce_until and time.time() >= debounce_until:
                debounce_until = None
                success = run_beets()
                if success and plex_token and plex_section:
                    trigger_plex_scan(plex_token, plex_section)
                elif not plex_token:
                    log.warning("No Plex token available — skipping Plex scan")

        except urllib.error.URLError as e:
            log.warning(f"Syncthing unreachable ({e}) — retrying in 15s")
            time.sleep(15)
        except Exception as e:
            log.error(f"Unexpected error: {e} — retrying in 15s")
            time.sleep(15)


def main():
    log.info("=" * 60)
    log.info("syncthing-beets-watcher starting")
    log.info("=" * 60)

    # Wait for Syncthing to be ready
    st_key = None
    while not st_key:
        st_key = get_syncthing_api_key()
        if not st_key:
            log.warning("Syncthing API key not available yet — retrying in 30s")
            time.sleep(30)
    log.info("Syncthing API key loaded")

    folder_id = None
    while not folder_id:
        try:
            folder_id = get_folder_id(st_key)
        except Exception:
            pass
        if not folder_id:
            log.warning(f"Folder '{MUSIC_FOLDER_LABEL}' not found in Syncthing — retrying in 30s")
            time.sleep(30)
    log.info(f"Syncthing folder found: {folder_id}")

    # Set up Plex (non-fatal if unavailable)
    plex_token = get_plex_token()
    plex_section = None
    if plex_token:
        log.info("Plex token loaded")
        plex_section = get_plex_music_section(plex_token)
        if not plex_section:
            log.warning("Could not find Plex music library — Plex scans will be skipped")
    else:
        log.warning(f"No Plex token found at {PLEX_PREFS} — Plex scans will be skipped")

    watch(st_key, folder_id, plex_token, plex_section)


if __name__ == "__main__":
    main()
