<img src="icon.png" width="128" alt="">

# ohnaarr

**Fixes the Sonarr and Radarr imports that need a human.**

![Python](https://img.shields.io/badge/python-3.12-blue)
![Sonarr](https://img.shields.io/badge/Sonarr-v4%20%7C%20v5-5bc0de)
![Radarr](https://img.shields.io/badge/Radarr-v3-ffc230)
![Tests](https://img.shields.io/badge/tests-222%20passing-brightgreen)

A Docker sidecar that watches the Sonarr/Radarr queue, finds the downloads the
*arr refuses to import on its own, works out which episodes or which movie the
files actually are, and issues the `ManualImport` command — so **Activity →
Queue** stops filling up with things only you can clear.

It never touches your files. Every path comes from the *arr API and the move is
performed by Sonarr/Radarr, so no media volumes are mounted.

```
House.M.D.2004.S04E07.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX.mkv
  → Dr. House · season 4 · episode 07     matched by series:grabHistory+numbers:sxxexx
```

---

## Contents

- [What it fixes](#what-it-fixes)
- [Quick start](#quick-start)
- [Web interface](#web-interface)
- [How a run works](#how-a-run-works)
- [Discord notifications](#discord-notifications)
- [Configuration](#configuration)
- [Overrides file](#overrides-file)
- [CLI](#cli)
- [JSON API](#json-api)
- [Security](#security)
- [Safety properties](#safety-properties)
- [Development](#development)

---

## What it fixes

### 1. "Release was matched to series by ID"

```
Downloaded - Unable to Import Automatically
House.M.D.2004.S03.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX
• Found matching series via grab history, but release was matched to series by ID.
  Automatic import is not possible. See the FAQ for details.
```

The indexer matched the release by TVDB/TMDB id, so the grab is correct — but
`House.M.D.2004` does not parse back to the library title (`Dr. House`), and
Sonarr will not guess. ohnaarr detects that message, takes the id **from the grab
history** — never from title matching, because a title Sonarr could not match is
exactly the one fuzzy matching gets wrong — maps every file in the season pack to
its episode, and imports. Radarr's wording (`matched to movie by ID`) is handled
the same way.

### 2. German umlauts mangled by encoding

| arrives as | repaired to |
| --- | --- |
| `MÃ¤dchen.WG` | `Mädchen WG` |
| `MÃÂ¤dchen` (double mis-decode) | `Mädchen WG` |
| `M&auml;dchen` / `M%C3%A4dchen` | `Mädchen WG` |
| `Ma` + U+0308 (decomposed) | `Mädchen WG` |
| `Maedchen` / `Madchen` (uploader transliterated) | matches `Mädchen` |
| `GroÃŸstadtrevier` | `Großstadtrevier` |

Every title is compared in two folded forms — `ä→ae` and `ä→a` — so a release
matches the library whichever direction the uploader went.

### 3. German episode naming Sonarr does not parse

```
Staffel 1 Folge 2      1. Staffel, 3. Folge     Season 3 Episode 11
S01E02E03              S01E02-E04               3x11
Folge 12               01 - Titel.mkv           104  (= S01E04)
17.03.2024             - 123 -  (absolute/anime)
```

### 4. No numbers at all — only a German episode title

`Die.Pfefferkoerner.Der.Fluch.des.Pharaos.mkv` is matched against the episode
titles of the series (fuzzy, threshold configurable).

### 5. Movies whose German title is not the library title

`Der.Herr.der.Ringe.Die.Rueckkehr.des.Koenigs.2003…` matches on the German
title, the original title or an alternate title. When a title matches two movies
(a remake), the release year decides — and with no year, ohnaarr refuses rather
than guessing.

---

## Quick start

No image is published yet, so build it from source:

```bash
git clone https://github.com/patryck507/ohnaarr.git
cd ohnaarr
cp .env.example .env        # optional — the web interface can do all of this
docker compose up -d --build
```

Or without compose:

```bash
docker build -t ohnaarr .
docker run -d --name ohnaarr \
  -p 8787:8787 \
  -v /srv/ohnaarr/config:/config \
  -e TZ=Europe/Berlin \
  --restart unless-stopped \
  ohnaarr
```

Then open **http://&lt;host&gt;:8787**:

1. The setup card asks for the Sonarr address and API key (*Sonarr → Settings →
   General → API Key*). **Test first**, then **Connect**. Sonarr v4 and v5 both
   work; the API version is detected for you.
2. Dry run is on, so nothing is written yet. Press **Preview changes** and read
   the Decisions page:
   ```
   ~  House.M.D.2004.S04E07.German.DL.1080p.NF.WEB.H264-WAREZCX.mkv
      → Dr. House · season 4 · episode 07
        matched by series:grabHistory+numbers:sxxexx
   ```
3. Happy with them? Click **Turn dry run off** in the banner. From then on it
   imports unattended, every `INTERVAL_SECONDS`.
4. For movies: **Connections → Add Radarr**, paste the Radarr address and key,
   **Test connection**, **Save changes**.

ohnaarr has to reach the *arrs over the network — join their Docker network (see
the commented block in `docker-compose.yml`) or point the URLs at the host. It
needs **no** media volumes.

> Set a sign-in username and password before exposing port 8787 to anything you
> do not trust. See [Security](#security).

---

## Web interface

| page | what it does |
| --- | --- |
| **Dashboard** | repairs per day, totals, which strategy did the matching, recent passes |
| **Decisions** | the last run as a diff: filename, what it resolved to, and why |
| **Queue** | both queues, stuck items marked, `matched by ID` flagged, *arr messages |
| **History** | what was imported and how; unplaced files with a **Try again** button |
| **Connections** | Sonarr and Radarr: address, key, API base, health, **Test connection** |
| **Settings** | everything else, grouped, with validation and per-field provenance |
| **Overrides** | the aliases/replacements file, edited in place |
| **Name lab** | repaired name, both folded forms, parsed numbers, matched series/movie |

Every action answers with a toast, and unsaved edits raise a save bar instead of
being written as you type. Each field shows where its value came from (`set by
SONARR_URL` / `saved here`), and a saved value can be dropped so the environment
applies again. Settings are written to `/config/settings.json`; a value saved in
the UI wins over the same environment variable.

The whole interface is one self-contained HTML file: no build step, no CDN, no
webfont, no outbound requests.

---

## How a run works

1. `GET /queue` → items in `importBlocked` / `importPending` / `failedPending`
   with status `warning` or `error`. (Sonarr v4 returns one record per episode;
   those are merged, so a 16-episode pack is one decision, not 16.)
2. `GET /manualimport` for that download → candidate files, with the *arr's own
   rejections. Falls back to the output folder.
3. Skip non-video files, samples, trailers, anything under the size floor, and
   anything with a hard rejection (executables, locked files, already imported).
4. Resolve the target: the candidate's own series/movie → **grab history** →
   filename → folder → release title → `GET /parse`.
5. Sonarr only — resolve the episodes: the *arr's numbers → parsed numbers → a
   bare number inside a season pack → absolute number → air date →
   episode-title match. A result outside the episode set tied to the grab is
   discarded.
6. `POST /manualimport` re-runs the *arr's own decision engine on the proposal.
   Any rejection that is not informational aborts that file.
7. `POST /command {name: ManualImport, files: […], importMode}`, then poll until
   it completes. `downloadId` is included, so the queue item is marked imported
   and the download client is handled normally.

Failures are recorded in SQLite: `MAX_ATTEMPTS` tries per file with
`ATTEMPT_BACKOFF_SECONDS` between them, then the file is left alone (the History
page can clear that). Imported files are never reprocessed.

---

## Discord notifications

Paste a Discord webhook URL into **Settings → Notifications & interface** and
press **Send a test message**. Each run that matches `NOTIFY_EVENTS` posts one
embed:

```
ohnaarr · 4 import(s) fixed
**4** imported · **1** unresolved · **1** rejected · 1 skipped
3 queue item(s) checked in 4.2s

✅ Imported (4)
📺 Dr. House S03E01
   House.M.D.S03E01.German.DL.1080p.NF.WEB.H264-WAREZCX.mkv
🎬 Der Herr der Ringe: Die Rückkehr des Königs (2003)
   film.mkv

❓ Needs a human (1)
📺 unknown target — no series match for 'Unbekannte Serie'
   file.mkv

⛔ Rejected (1)
📺 Tatort S01E02 — sonarr rejected: notQualityUpgrade: Existing file has better quality
   alt.mkv

ohnaarr v1.2.0 · import mode: auto · sonarr + radarr
```

The colour follows the worst outcome in the run: green for a clean import,
yellow when something needs a look, red on failure, blurple for a dry run. A run
with nothing to report sends nothing, so an idle instance stays quiet. Release
names are escaped so markdown in a title cannot break the embed, every Discord
limit (6000 characters per embed, 25 fields, 1024 per field) is enforced before
sending, and a 429 is retried with the API's own `retry_after`.

Any non-Discord URL receives `{"summary": {…}, "dryRun": bool}` instead, which
suits Gotify, Apprise and n8n.

---

## Configuration

All of it is editable in the UI; the environment variables are the defaults.

### Connections

| variable | default | meaning |
| --- | --- | --- |
| `SONARR_URL` | `http://sonarr:8989` | base URL, as this container reaches it |
| `SONARR_API_KEY` | — | Sonarr → Settings → General |
| `SONARR_API_BASE` | `/api/v5` | `/api/v3` for Sonarr v4; auto-detected on **Test** |
| `SONARR_VERIFY_TLS` | `true` | off for a self-signed certificate |
| `SONARR_TIMEOUT` | `30` | request timeout, seconds |
| `RADARR_ENABLED` | `false` | also repair stuck movie imports |
| `RADARR_URL` | `http://radarr:7878` | base URL |
| `RADARR_API_KEY` | — | required when Radarr is enabled |
| `RADARR_API_BASE` | `/api/v3` | |

### Behaviour

| variable | default | meaning |
| --- | --- | --- |
| `DRY_RUN` | `true` | decide and log, change nothing |
| `INTERVAL_SECONDS` | `300` | pause between runs |
| `RUN_ONCE` | `false` | single pass, then exit |
| `IMPORT_MODE` | `auto` | `auto` \| `move` \| `copy` |
| `MIN_FILE_SIZE_MB` | `50` | smaller video files are ignored as samples |
| `VIDEO_EXTENSIONS` | `mkv,mp4,avi,…` | comma separated |
| `IGNORED_REJECTIONS` | see below | *arr complaints a manual import is meant to overrule |
| `EXTRA_FOLDER_SCAN` | — | comma-separated folders to sweep for files that never reached the queue |

### Matching

| variable | default | meaning |
| --- | --- | --- |
| `SERIES_MATCH_THRESHOLD` | `82` | fuzzy series match, 0–100 |
| `EPISODE_TITLE_THRESHOLD` | `88` | fuzzy episode-title match, 0–100 |
| `MOVIE_MATCH_THRESHOLD` | `85` | fuzzy movie-title match, 0–100 |
| `MOVIE_MIN_FILE_SIZE_MB` | `200` | keeps trailers and extras out |
| `ALLOW_NUMERIC_EPISODE_CODES` | `true` | read `104` as S01E04 |
| `ALLOW_ABSOLUTE_NUMBERING` | `true` | anime-style absolute numbers |
| `ASSUME_SEASON_FROM_CONTEXT` | `true` | a single-season series needs no season tag |
| `RADARR_IGNORED_REJECTIONS` | see below | substring patterns (Radarr rejections are free text) |

### Safety

| variable | default | meaning |
| --- | --- | --- |
| `MAX_ATTEMPTS` | `4` | tries per file before it is left alone |
| `ATTEMPT_BACKOFF_SECONDS` | `1800` | wait between tries |

### Notifications and interface

| variable | default | meaning |
| --- | --- | --- |
| `WEBHOOK_URL` | — | Discord webhook → rich embed; any other URL → plain JSON |
| `NOTIFY_EVENTS` | `imported,unresolved,errors` | `imported` \| `unresolved` \| `rejected` \| `errors` \| `dry_run` \| `every_run` |
| `NOTIFY_MAX_ITEMS` | `10` | files listed per embed section |
| `NOTIFY_USERNAME` | `ohnaarr` | webhook display name (Discord) |
| `NOTIFY_AVATAR_URL` | — | webhook avatar (Discord) |
| `WEB_USERNAME` / `WEB_PASSWORD` | — | put the interface behind a sign-in form |
| `SESSION_HOURS` | `336` | how long a sign-in lasts; a restart always ends it |
| `LOG_LEVEL` | `INFO` | `DEBUG` prints every match decision |

### Deployment (environment only, needs a restart)

| variable | default | meaning |
| --- | --- | --- |
| `HTTP_PORT` | `8787` | web interface; `0` disables it |
| `STATE_PATH` | `/config/ohnaarr.db` | attempt tracking and run history |
| `SETTINGS_PATH` | `/config/settings.json` | written by the UI |
| `OVERRIDES_PATH` | `/config/overrides.yml` | re-read every run |

Default `IGNORED_REJECTIONS` — the ones a manual import is meant to overrule:
`unknownSeries`, `unableToParse`, `invalidSeasonOrEpisode`, `noEpisodes`,
`titleMissing`, `titleTba`, `unverifiedSceneMapping`,
`missingAbsoluteEpisodeNumber`. The Radarr equivalent matches the substrings
`unknown movie`, `unable to parse`, `no movie`, `movie title`, `tba`,
`invalid movie`.

**Hard rejections are never overridable**, whatever you put in those lists.

---

## Overrides file

`/config/overrides.yml`, re-read on every run, editable in the UI — for names
matching cannot reach on its own:

```yaml
series_aliases:
  "house m d": "Dr. House"        # folded release title -> id or exact title
  "tatort muenster": 143

title_replacements:
  - ["Staffel", "Season"]
```

See [`config/overrides.example.yml`](config/overrides.example.yml).

---

## CLI

```bash
docker compose exec ohnaarr python -m ohnaarr.main check       # connectivity + queue
docker compose exec ohnaarr python -m ohnaarr.main settings    # effective config + sources
docker compose exec ohnaarr python -m ohnaarr.main run --once  # one pass, then exit
docker compose exec ohnaarr python -m ohnaarr.main explain \
  "House.M.D.2004.S03.German.DL.EAC3.1080p.NF.WEB.H264-WAREZCX"
```

`explain` (and the Name lab page) is what to reach for when a file is not picked
up: it shows the repaired spelling, both folded forms, the parsed numbers and the
series or movie it would match.

---

## JSON API

Mutating calls need the `X-Ohnaarr: 1` header, which a cross-site form cannot
set.

```
GET  /healthz /metrics            no sign-in; container checks and Prometheus
GET  /api/session                 whether sign-in is required, and who is in
POST /api/login /api/logout       exchange credentials for a session cookie
GET  /api/state                   status + settings schema + current values
GET  /api/stats?days=14           dashboard figures, daily series, run history
GET  /api/queue                   both queues, annotated with what is stuck
POST /api/run                     {"preview": true} forces a dry run
POST /api/settings /api/test      save settings, probe an *arr
POST /api/explain                 {"name": "<release name>"}
GET  /api/overrides  POST /api/overrides
GET  /api/history    POST /api/forget
POST /api/notify/test             send a sample notification
```

---

## Security

- **Sign-in.** Set `WEB_USERNAME` and `WEB_PASSWORD` (or the fields in
  *Settings → Notifications & interface*). The browser gets a form and an
  HttpOnly, `SameSite=Strict` session cookie — never the basic-auth dialog,
  because no `WWW-Authenticate` header is ever sent. Scripts may still send
  `Authorization: Basic`, so `curl -u` and existing automation keep working.
- **Sessions** live in memory only: a restart signs everyone out, and saving a
  new password signs every other browser out while keeping yours in.
- **Failed sign-ins are throttled** per client address: 8 attempts, then five
  minutes of `429`.
- **Without credentials the interface is open** to anyone who can reach the
  port, and it holds your *arr API keys. Do not publish port 8787.
- **Secrets are never echoed back.** `GET /api/state` returns `value: null` for
  every secret field; the UI can only set them.
- `/healthz` and `/metrics` stay unauthenticated for container checks and
  scrapers. `/healthz` reports liveness, so a fresh container awaiting its setup
  form is healthy; `ohnaarr_healthy` in `/metrics` tracks *arr reachability.
- Nothing from `config/` is in this repository — that is where the API keys, the
  sign-in password and the state database live.

---

## Safety properties

- `DRY_RUN=true` out of the box, and **Preview changes** never writes.
- Nothing is imported until `POST /manualimport` reports no blocking rejection.
- A series or movie is never guessed when the *arr already knows it from the grab.
- An episode match outside the grabbed episode set is thrown away.
- An ambiguous movie title with no year is refused, not guessed.
- Hard rejections (executables, locked files, already imported) are absolute.
- Samples, trailers, non-video and undersized files are skipped.
- Attempts are capped and backed off, so one pathological file cannot loop.
- Deletions, blocklisting and failed-download handling are **not** performed —
  this service only ever adds an import.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest        # 222 tests
```

The pipeline, both HTTP clients, the sign-in flow and the web API run against
mock Sonarr v4/v5, Radarr v3 and webhook servers — including the `House.M.D`
season-pack case above, and the Sonarr v4 quirks (`downloadId` is singular,
`includeSubresources` is ignored, one queue record per episode).

| module | what lives there |
| --- | --- |
| `normalize.py` | mojibake repair, umlaut folding, release-name cleaning |
| `episode_parse.py` | every season/episode pattern, absolute numbers, air dates |
| `matcher.py` | series index and episode resolution strategies |
| `movie_parse.py` | movie titles, years, ambiguity refusal |
| `sonarr.py` / `radarr.py` | the HTTP clients, split by API version |
| `fixer.py` / `movie_fixer.py` | the per-run decision pipeline |
| `service.py` | the loop, probes, settings, statistics |
| `web.py` / `auth.py` | the JSON API, sessions, throttle |
| `static/index.html` | the whole interface, in one file |

Stack: Python 3.12, `httpx`, `rapidfuzz`, `PyYAML`. The web server is
`http.server` from the standard library — no framework.
