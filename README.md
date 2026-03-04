# Automated Music Pipeline: VPS → NAS → Beets → Plex

A fully automated pipeline that ingests a torrent, downloads it on a remote VPS, syncs it to a home NAS, auto-imports it with [beets](https://beets.io), and surfaces it in Plex — without any manual intervention.

---

## 🗂️ Background: The Library Consolidation (Phase 0)

Before any automation could be built, there was a more immediate problem to solve: two separate music libraries accumulated across two PCs, totalling roughly 1.25TB, needed to be merged, deduplicated, and properly organised into a single collection on the NAS before beets could take over ongoing imports.

### The starting point

One library sat at around 500GB, the other closer to 750GB. Both were reasonably well-tagged — artist names, album titles, years — but folder structures were inconsistent across the two. More importantly, there was substantial duplication: files duplicated within each individual library, and then a significant overlap between the two collections where both machines had acquired the same albums independently over the years.

My wife's library had an additional structural problem: many files were nested several levels deeper than beets expects, which caused beets to either skip them entirely or misidentify the album structure. A preprocessing step was needed just to make the library ingestable.

### The approach

**Step 1 — Flatten deep folder structures.** A Python script was written to traverse my wife's library and restructure directories where tracks were buried too deep (e.g. `Artist/Album/CD1/Disc 1/track.flac` → `Artist/Album/track.flac`). This was a prerequisite for beets being able to process anything reliably.

**Step 2 — Stage both libraries on the NAS.** Rather than importing directly from each PC, both libraries were copied to a staging area on the Unraid NAS so all processing could happen in one place.

**Step 3 — Duplicate analysis.** Before running beets, a pass was made to categorise duplicates rather than blindly discard them. Files were classified into three buckets:

- **True duplicates** — identical files (matched by audio fingerprint or bitrate/duration/tags), safe to discard one copy
- **Needs review** — same album/track metadata but different file properties (different rips, different bitrates, lossy vs lossless) requiring a human call on which to keep
- **Different versions** — genuinely distinct releases: remasters, deluxe editions, live versions, alternate mixes that should both be retained

This categorisation meant nothing got silently dropped — the "needs review" pile could be worked through manually without holding up the rest of the import.

**Step 4 — beets import.** Once the folder structure was normalised and duplicates resolved, beets was run against the full staged collection. beets handled the final tagging (pulling from MusicBrainz), canonical renaming, and moving files into the unified library structure.

---

## 📐 Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│  Tracker                                             │
└─────────────────────┬────────────────────────────────┘
                      │  torrent
                      ▼
┌──────────────────────────────────────────────────────┐
│  VPS                                                 │
│                                                      │
│  ┌─────────────────┐  AutoLabel  ┌────────────────┐  │
│  │  ruTorrent      │ ──────────► │ "NewMusic"     │  │
│  │  + AutoTools    │             └───────┬────────┘  │
│  └─────────────────┘             AutoMove│           │
│                                          ▼           │
│                              ~/files/NewMusic/       │
│                                          │           │
│                              ┌───────────┴────────┐  │
│                              │ Syncthing          │  │
│                              │ (Send Only)        │  │
│                              └───────────┬────────┘  │
└──────────────────────────────────────────┼───────────┘
                                           │  sync
                                           ▼
┌──────────────────────────────────────────────────────┐
│  Home NAS (Unraid)                                   │
│                                                      │
│  ┌──────────────────────────────┐                    │
│  │ Syncthing (Receive Only)     │ ◄── receives files │
│  │ /mnt/user/Media/Music-inbox/ │                    │
│  └──────────────┬───────────────┘                    │
│                 │  StateChanged: idle + fully synced │
│                 ▼                                    │
│  ┌──────────────────────────────┐                    │
│  │ syncthing-beets-watcher.py   │ ← daemon           │
│  │ (watches Syncthing REST API) │                    │
│  └──────────────┬───────────────┘                    │
│                 │  20s debounce, then:               │
│                 ▼                                    │
│  ┌──────────────────────────────┐                    │
│  │ beets import                 │ ← User Script      │
│  │ (auto-tag, rename, move)     │                    │
│  └──────────────┬───────────────┘                    │
│                 │  on success:                       │
│                 ▼                                    │
│  ┌──────────────────────────────┐                    │
│  │ Plex                         │                    │
│  │ (REST API library scan)      │                    │
│  └──────────────────────────────┘                    │
│                                                      │
│  Music appears in Plex automatically                 │
└──────────────────────────────────────────────────────┘
```

---

## 🧱 Components

| Layer | Tool | Role |
|---|---|---|
| VPS | ruTorrent + AutoTools plugin | Download, auto-label, auto-move torrents |
| Sync | Syncthing | Replicate finished downloads from VPS to NAS |
| NAS OS | Unraid | Host for all local services |
| Import | beets | Auto-tag, rename, and organise music library |
| Media server | Plex | Serve the library |
| Glue | `syncthing-beets-watcher.py` | Trigger beets + Plex scan on sync completion |

---

## ⚙️ Setup Guide

### 1. ruTorrent (VPS side)

#### AutoLabel
Use the **AutoTools** plugin to automatically label torrents from your source:

- **Settings → Autotools → AutoLabel**
- Set a label filter that matches your source's announce URL
- Label: `NewMusic`

#### AutoMove
Move completed, labeled downloads to the Syncthing watch folder:

- **Settings → Autotools → AutoMove**
- Label filter: `/NewMusic/`  ← **important**: don't use `/.*/` or every torrent (including movies etc.) will get moved
- Destination: `~/files/NewMusic`
- Operation: **Move**
- When: **On Finish**

> ⚠️ **Gotcha**: If your label filter is `/.*/` (the regex default), *all* torrents get moved — not just music. Scope it to `/NewMusic/`.

---

### 2. Syncthing

#### VPS side
- Folder path: `~/files/NewMusic`
- Folder type: **Send Only**
- Share with your NAS device

#### NAS side (Unraid + Syncthing Docker)
- Accept the share from the VPS
- Folder path: `/mnt/user/Media/Music-inbox/` (or wherever you want the staging area)
- Folder type: **Receive Only**

> ⚠️ **Gotcha**: The Syncthing Docker container needs an explicit path mapping for `/mnt/user/Media/Music-inbox/` to be accessible. If you see errors like `"Failed to create folder root directory (mkdir /mnt/user: permission denied)"`, the path isn't mapped into the container — fix it in the Syncthing container's volume settings.

> ⚠️ **Gotcha**: Syncthing folder IDs must match *exactly* on both sides to pair correctly. When in doubt, have one side send a share offer and the other side accept it via the UI — don't try to manually type matching IDs.

---

### 3. beets (Unraid User Script)

Create a User Script called `beets-inbox-import` that imports from your staging folder into your library:

```bash
#!/bin/bash
docker exec beets beet import /music-inbox -q
```

Adjust the container name and paths to match your setup. Schedule this as a nightly fallback (e.g. `0 3 * * *`) in addition to the real-time trigger below.

---

### 4. syncthing-beets-watcher (the glue)

This is the key piece: **[`syncthing-beets-watcher.py`](./syncthing-beets-watcher.py)** — a Python daemon that watches Syncthing's event stream and fires beets (then triggers a Plex scan) as soon as a sync completes.

The configuration block at the top of the script is the only thing you need to edit:

```python
SYNCTHING_URL        = "http://localhost:8384"
SYNCTHING_CONFIG     = "/mnt/user/appdata/syncthing/config.xml"
MUSIC_FOLDER_LABEL   = "NewMusic"
BEETS_SCRIPT         = "/boot/config/plugins/user.scripts/scripts/beets-inbox-import/script"
PLEX_URL             = "http://localhost:32400"
PLEX_PREFS           = "/mnt/user/appdata/PlexMediaServer/Preferences.xml"
PLEX_LIBRARY_TYPE    = "artist"
DEBOUNCE_SECS        = 20
```

**Deployment on Unraid:**
1. In the User Scripts plugin, create a new script called `syncthing-beets-watcher`
2. Paste the contents of `syncthing-beets-watcher.py` as the script body
3. Set the schedule to **At Startup of Array**
4. Click **Run in Background**

Plex integration is optional and non-fatal — if the token or library can't be found, the watcher still runs and triggers beets normally.

---

## 🐛 Gotchas & Lessons Learned

### Syncthing

**Docker path mapping**
If Syncthing is running in a Docker container (as it typically does on Unraid), the container won't be able to access host paths unless they're explicitly mapped as volumes. Errors like `"Failed to create folder root directory (mkdir /mnt/user: permission denied)"` or `"folder path missing"` both point to this. Fix the path mapping in the container settings, not in Syncthing itself.

**Send Only / Receive Only pairing**
If both sides are set to "Send & Receive", Syncthing may attempt to delete files on the VPS after they're moved locally. Set VPS to **Send Only** and NAS to **Receive Only** (or **Receive Encrypted**) to prevent this.

---

### ruTorrent AutoTools

**AutoLabel regex**
The default filter `/.*/` matches everything — including non-music torrents. Always scope your AutoMove filter to the specific label, e.g. `/NewMusic/`.

**AutoMove timing**
Set AutoMove to trigger **On Finish** (not on add) so files are only moved once fully downloaded and checked.

---

### Syncthing REST API (for the watcher script)

- Syncthing exposes a long-polling events API: `GET /rest/events?events=StateChanged&since={id}&timeout=60`
- The `timeout` parameter causes the request to block until an event arrives or the timeout elapses — this is ideal for a daemon; no polling loop needed
- Always read `since` from the last event ID you processed to avoid replaying old events on reconnect
- After a `StateChanged` → `idle` event, double-check with `/rest/db/completion?folder={id}` before triggering import — the folder can go idle briefly between chunks

---

### Plex

**Token location**
The Plex auth token lives in `Preferences.xml` as the `PlexOnlineToken` attribute. Reading it from disk avoids hard-coding it.

**Library section discovery**
Rather than hard-coding the section ID (which can change), query `/library/sections` and filter by `type="artist"` to find the music library dynamically.

---

## 📋 Monitoring

**Watch the watcher logs in real time:**
```bash
tail -f /tmp/syncthing-beets-watcher.log
```

**Check if the daemon is running:**
```bash
pgrep -a -f syncthing-beets-watcher
```

**Manually trigger a beets import (bypasses the watcher):**
Run the `beets-inbox-import` User Script from the Unraid UI, or:
```bash
bash /boot/config/plugins/user.scripts/scripts/beets-inbox-import/script
```

---

## 🔄 End-to-End Flow (happy path)

1. Torrent is added to ruTorrent (manually or via RSS/autodownloader)
2. AutoLabel assigns the `NewMusic` label based on announce URL
3. AutoMove moves finished files to `~/files/NewMusic`
4. Syncthing syncs the files to `/mnt/user/Media/Music-inbox/` on the NAS
5. `syncthing-beets-watcher` detects the `StateChanged → idle` event
6. After a 20-second debounce (to let any remaining small files finish), beets runs
7. beets tags, renames, and moves files into the main music library
8. The watcher calls the Plex `/library/sections/{id}/refresh` API
9. The album appears in Plex within seconds ✓

Total hands-on time after initial setup: **zero**.

---

## 🛠️ Requirements

- A VPS running ruTorrent with the AutoTools plugin
- Syncthing on both the VPS and NAS
- Unraid NAS (User Scripts plugin for running the daemon and beets script)
- [beets](https://beets.io) (running in Docker or natively)
- Plex Media Server

---

## 📄 License

MIT — do whatever you like with it.
