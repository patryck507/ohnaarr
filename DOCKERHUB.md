# ohnaarr

**Fixes the Sonarr and Radarr imports that need a human.**

Source, full documentation and issues: **https://github.com/patryck507/ohnaarr**

A sidecar that watches the Sonarr/Radarr queue, finds the downloads the *arr
refuses to import on its own, works out which episodes or which movie the files
actually are, and issues the `ManualImport` command — so **Activity → Queue**
stops filling up with things only you can clear.

It never touches your files: every path comes from the *arr API and the move is
performed by Sonarr/Radarr, so no media volumes are mounted.

## Run it

```bash
docker run -d --name ohnaarr \
  -p 8787:8787 \
  -v /srv/ohnaarr/config:/config \
  -e TZ=Europe/Berlin \
  --restart unless-stopped \
  patryck507/ohnaarr:latest
```

Open `http://<host>:8787` and the setup card asks for your Sonarr address and
API key. Dry run is on, so the first pass only shows you its decisions; turn it
off from the banner when they look right. Radarr is added afterwards on the
Connections page.

ohnaarr has to reach the *arrs over the network — join their Docker network or
point the URLs at the host.

## What it fixes

- **"Release was matched to series by ID"** — takes the id from the grab
  history instead of guessing from a title Sonarr could not parse, maps every
  file in the season pack to its episode, and imports.
- **Mangled German umlauts** — `MÃ¤dchen`, `M&auml;dchen`, `M%C3%A4dchen`,
  decomposed `Ma`+U+0308, and `Maedchen`/`Madchen` transliterations all match
  `Mädchen`.
- **German episode naming** — `Staffel 1 Folge 2`, `1. Staffel, 3. Folge`,
  `3x11`, `S01E02E03`, `Folge 12`, bare numbers in a pack, `104` as S01E04,
  absolute/anime numbering, air dates, and episode-title matching.
- **Movies whose German title is not the library title** — matched on German,
  original or alternate titles, with the release year deciding between remakes.
  An ambiguous title with no year is refused rather than guessed.

## Tags

| tag | contents |
| --- | --- |
| `latest` | the newest release |
| `1.3.0` | pinned release |

`linux/amd64`. Runs as uid 1000, has a `HEALTHCHECK`, and keeps its state in the
`/config` volume.

## More than a scanner

- **Push**: Sonarr calls ohnaarr the moment an import gets stuck
  (`On Manual Interaction Required`), so repairs happen in seconds.
- **Teach it**: when nothing matches, pick the right series from your library in
  the interface and the rule is written for you.
- **English and German**, switchable in the interface.

## Configuration

Everything is editable in the web interface, which stores it in
`/config/settings.json`. The common environment variables:

| variable | default | meaning |
| --- | --- | --- |
| `SONARR_URL` | `http://sonarr:8989` | base URL, as the container reaches it |
| `SONARR_API_KEY` | — | Sonarr → Settings → General |
| `SONARR_API_BASE` | `/api/v5` | `/api/v3` for Sonarr v4; auto-detected on **Test** |
| `RADARR_ENABLED` | `false` | also repair stuck movie imports |
| `RADARR_URL` / `RADARR_API_KEY` | — | the same, for Radarr |
| `DRY_RUN` | `true` | decide and log, change nothing |
| `INTERVAL_SECONDS` | `300` | pause between runs |
| `IMPORT_MODE` | `auto` | `auto` \| `move` \| `copy` |
| `WEBHOOK_URL` | — | Discord webhook → embed; any other URL → plain JSON |
| `WEB_USERNAME` / `WEB_PASSWORD` | — | put the interface behind a sign-in form |
| `HOOK_ENABLED` / `HOOK_TOKEN` | — | accept a push from Sonarr/Radarr |
| `UI_LANGUAGE` | `auto` | `auto` \| `en` \| `de` |
| `HTTP_PORT` | `8787` | web interface; `0` disables it |

The [full table](https://github.com/patryck507/ohnaarr#configuration) covers
matching thresholds, rejection handling, retry limits and notification events.

## Security

Without `WEB_USERNAME` and `WEB_PASSWORD` the interface is open to anyone who
can reach the port, and it holds your *arr API keys — set them before exposing
8787. The browser gets a sign-in form and an HttpOnly, `SameSite=Strict` session
cookie, never the basic-auth dialog; scripts can still use
`Authorization: Basic`. Failed sign-ins are throttled per client address.

## Safety

- `DRY_RUN=true` out of the box, and **Preview changes** never writes.
- Nothing is imported until the *arr's own validation reports no blocking
  rejection.
- A series or movie is never guessed when the *arr already knows it from the
  grab; an episode match outside the grabbed set is thrown away.
- Deletions, blocklisting and failed-download handling are **not** performed —
  this service only ever adds an import.
