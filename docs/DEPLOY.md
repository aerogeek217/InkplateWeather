# Deploying and operating the server

The server runs as one container (`inkplate-weather`, service `weather-display`) on the OpenMediaVault Pi and listens
on port 8080. The image is about 0.9 GB and holds Python, Playwright 1.64.1 with the Chromium headless shell, and the
bundled fonts. It needs no internet to render, only to fetch data (weather, radar, lightning, Home Assistant).

Files on the host, next to `docker-compose.yml`:

```
docker-compose.yml
server/            build context (only needed if you build on the Pi)
config/config.yaml your config (copy of config/config.example.yaml), mounted read-only at /config
cache/             provider cache and last-good image, mounted at /cache; must be writable by uid 1000
.env               HA_TOKEN=... for the Home Assistant provider; never committed
scripts/           backup-config.sh
backups/           config backups written by scripts/backup-config.sh (they contain secrets)
DEPLOYED_COMMIT    the commit that was deployed (also baked into the image; shown on /status)
```

Compose: Docker Compose v2 is `docker compose ...`; the older standalone v1 is `docker-compose ...` with the same
arguments. Both read this file.

## 1. Build and test locally (any machine with Docker)

```sh
docker build -t inkplate-weather:latest server
cp config/config.example.yaml config/config.yaml
mkdir -p cache
docker compose up -d
curl http://localhost:8080/health                    # {"status":"ok"}
curl -o display.png http://localhost:8080/display.png  # 1200x825 PNG
docker compose down
```

## 2. Get an arm64 image onto the Pi

Pick **one** of the following.

### A. Build on the Pi (simplest)

```sh
# on the Pi, in the folder that will hold the stack (e.g. a shared folder for the OMV compose plugin)
git clone https://github.com/aerogeek217/InkplateWeather.git inkplate-weather
cd inkplate-weather
docker compose build          # native arm64 build, several minutes on a Pi 4
```

### A2. Push the source from the dev PC over SSH, build on the Pi (used for this deployment)

Use this when the Pi has no `git`. It copies exactly one commit, then builds natively. In this deployment the user
`pi` isn't in the `docker` group but has passwordless `sudo`, so Docker runs through `sudo`.

```sh
# on the dev PC, from the repo root (Git Bash). REF is the branch, tag or commit to deploy.
REF=phase-4
COMMIT=$(git rev-parse --short "$REF")
git archive --format=tar "$REF" docker-compose.yml server config/config.example.yaml scripts docs/DEPLOY.md \
  | ssh pi@192.168.1.254 "mkdir -p ~/inkplate-weather && tar -x -C ~/inkplate-weather \
      && echo $COMMIT > ~/inkplate-weather/DEPLOYED_COMMIT && cp ~/inkplate-weather/DEPLOYED_COMMIT ~/inkplate-weather/server/"

# first deployment only: create the config and the cache folder (edit config/config.yaml afterwards)
ssh pi@192.168.1.254 "cd ~/inkplate-weather && cp -n config/config.example.yaml config/config.yaml \
  && mkdir -p cache && sudo chown 1000:1000 cache"

# every deployment: back up the live config, tag the running image for rollback, build, restart
ssh pi@192.168.1.254 "cd ~/inkplate-weather && sh scripts/backup-config.sh \
  && (sudo docker tag inkplate-weather:latest inkplate-weather:previous || true) \
  && sudo docker compose build && sudo docker compose up -d"
```

The archive holds `config/config.example.yaml` only, **never `config/config.yaml`**, so the live config is not
overwritten (edit it in place on the Pi, after a backup). `DEPLOYED_COMMIT` is copied into `server/` before the build,
so it is baked into the image and `/status` shows the commit. On the very first deployment there is no `config.yaml`
yet, so run the "first deployment" command before the third one. Measured on a Pi 4: build 284 s (cold), about 80 s
warm, image 940 MB.

### B. Cross-build on a PC with buildx, then copy the image

```sh
# on the PC (QEMU emulation; slower than a native build)
docker buildx build --platform linux/arm64 -t inkplate-weather:arm64 --load server
docker save inkplate-weather:arm64 | gzip > inkplate-weather-arm64.tar.gz
scp inkplate-weather-arm64.tar.gz docker-compose.yml <user>@<pi-ip>:~/inkplate-weather/
scp -r config <user>@<pi-ip>:~/inkplate-weather/

# on the Pi
cd ~/inkplate-weather
gunzip -c inkplate-weather-arm64.tar.gz | docker load
docker tag inkplate-weather:arm64 inkplate-weather:latest
```

With `--load`, buildx 0.6 on Docker 20.10 puts a single-platform image into the local image store. If your buildx
rejects `--load`, write a tarball directly instead:
`docker buildx build --platform linux/arm64 -t inkplate-weather:arm64 -o type=docker,dest=inkplate-weather-arm64.tar server`
(then `docker load -i inkplate-weather-arm64.tar` on the Pi).

Don't judge the arm64 image by running it on the PC: under QEMU emulation `/health` works but Chromium segfaults
("Failed to parse the contents of /proc/self/maps"), so `/display.png` fails there. Test renders on the Pi itself.

Compose uses the existing `inkplate-weather:latest` image and only builds if that image is missing (or with `--build`).

## 3. Configure and run on the OMV Pi (command line)

```sh
cd ~/inkplate-weather                     # or the OMV compose plugin's folder for this stack
cp -n config/config.example.yaml config/config.yaml   # edit as needed; not tracked by git
mkdir -p cache && sudo chown 1000:1000 cache          # the container runs as uid 1000
echo "HA_TOKEN=<long-lived token>" > .env && chmod 600 .env   # Home Assistant provider
sudo docker compose up -d
sudo docker compose ps                     # STATUS shows (healthy) after ~1 min
sudo docker compose logs -f --tail 100 weather-display
```

(Drop `sudo` if your user is in the `docker` group.) After editing `config/config.yaml`, apply it without a restart
with `curl -X POST http://localhost:8080/admin/reload`; a config error keeps the old config and prints the problem.
Changing `.env` needs `docker compose up -d` (recreates the container with the new environment).

## 4. The OpenMediaVault compose plugin (GUI)

The OMV compose plugin (`openmediavault-compose`, from OMV-extras) is a front end for the same `docker compose`
commands. Menu names below are from OMV 7; the plugin's screens have changed between versions, so check yours. This
deployment is driven over SSH (option A2); the GUI is handy for starting, stopping and reading logs.

**One-time plugin settings** (Services → Compose → Settings):

- Shared folder for the compose files ("Compose Files"): pick or create a shared folder on a data disk, not the SD card
  (the image alone is about 1 GB). The plugin keeps each stack's compose file (and its env file) under it.
- Data folder ("Data" shared folder, if your version has the setting): a place for container data. It is not required
  here, but it is a convenient home for `config/` and `cache/`.
- Docker storage: if Docker's own data root still points at the SD card, move it to a data disk before pulling or
  building gigabytes of images (Services → Compose → Settings has a Docker storage path in recent versions).
- Save and apply the pending changes (the yellow banner in OMV).

**Add the stack** (Services → Compose → Files → Add / "+"):

1. Name it `inkplate-weather`. Paste the contents of `docker-compose.yml` into the compose editor.
2. **Where `./config` and `./cache` land.** The volume paths in the file are relative (`./config`, `./cache`). Compose
   resolves relative paths against its project directory, which in the plugin is, as far as I know, the folder the plugin
   keeps that compose file in, under the Compose Files shared folder. I haven't confirmed the exact folder name on
   OMV 7, so check: press Up, then look on the disk (SSH, or Storage → Shared Folders) for where `config` and `cache`
   appeared. To avoid any doubt, edit the two volume lines to **absolute paths** of folders you control, e.g.
   `/srv/dev-disk-by-uuid-<uuid>/appdata/inkplate-weather/config:/config:ro` and `.../cache:/cache`, and create those
   folders yourself: `mkdir -p .../config .../cache`, copy `config/config.example.yaml` to `.../config/config.yaml`,
   and `sudo chown 1000:1000 .../cache` (the container runs as uid 1000 and must be able to write the cache).
3. **The image.** The compose file has both `image:` and `build: ./server`. The plugin's folder won't contain `server/`
   unless you put it there, so make sure the image `inkplate-weather:latest` already exists on the Pi, otherwise Up
   will try to build and fail. Two ways: build it over SSH with option A2 (`sudo docker compose build` in
   `~/inkplate-weather`; the image lands in the same Docker daemon, so the plugin finds it), or cross-build on a PC and
   `docker load` it (option B). Either way, use the **Up** button afterwards; it starts the container from the
   existing image.
4. **The Home Assistant token.** The compose file reads `HA_TOKEN` with `${HA_TOKEN:-}`. In the stack's edit page
   look for an environment-file section ("Environment" in recent plugin versions) and enter `HA_TOKEN=<token>` there;
   the plugin stores it as an `.env` file next to the compose file. If your version has no such field, create the
   `.env` file by hand in the folder that holds the compose file (`chmod 600`), or replace
   `${HA_TOKEN:-}` in the compose editor with the token (it then sits in the plugin's config, so prefer the env file).
5. Save. The stack appears in the Files list.

**Day-to-day buttons** (select the stack in the Files list; in OMV 7 they are in the toolbar above the list or a row
menu, labelled Up, Down, Restart, Logs, Pull, Build, Check; names vary by version):

- **Up** runs `docker compose up -d`: starts the stack, and recreates the container if the image or the compose file
  changed. Use it after loading or building a new image.
- **Down** stops and removes the container (`cache/` and `config/` are untouched on disk).
- **Restart** restarts the running container without re-reading the compose file.
- **Logs** shows the container's output (the same as `docker compose logs`; lines start with a timestamp).
- **Pull** / **Update** try to download `inkplate-weather:latest` from a registry, which doesn't exist for this
  local image: don't use them for this stack. Update by loading or building the image, then Up (see Updating).

Check it in Services → Compose → Containers (or the Files list) for a `healthy` status, then run the checks in the
next section.

## 5. Check it

```sh
curl http://<pi-ip>:8080/health
curl -o display.png "http://<pi-ip>:8080/display.png?batt=88&mv=4012&reason=timer"
sudo docker inspect --format '{{.State.Health.Status}}' weather-display
curl -s http://<pi-ip>:8080/status.json | head -c 400     # includes deployed_commit
```

Open `http://<pi-ip>:8080/preview` in a browser to see the live HTML, and `http://<pi-ip>:8080/status` for the
provider health page.

## Updating

```sh
git pull && docker compose up -d --build       # option A
# option A2: rerun its deployment commands (the "every deployment" command backs up the config first)
# option B: repeat the build/save/load steps, then: docker compose up -d
docker image prune -f                          # remove the old untagged image
```

With the OMV plugin: get the new image onto the Pi first (A2 or B), then press **Up** on the stack.
Back up the config before every deploy: `sh scripts/backup-config.sh` (see Config backups).

## Rollback

The code and the image are the unit of rollback; the config is separate and backed up.

**Previous image (fast, no rebuild).** The A2 "every deployment" command tags the image that was running as
`inkplate-weather:previous` before building. To go back:

```sh
ssh pi@192.168.1.254 "cd ~/inkplate-weather && sudo docker tag inkplate-weather:previous inkplate-weather:latest \
  && sudo docker compose up -d"
```

(Compose doesn't build when the image exists, so this just recreates the container.) `:previous` is a tagged image,
so a plain `docker image prune -f` keeps it. Each deployment moves the tag to whatever was running, so after a bad
deploy roll back before you deploy again, or the bad image becomes `:previous`.

**An older commit (rebuild).** Check what is deployed (`cat ~/inkplate-weather/DEPLOYED_COMMIT`, or `deployed_commit` on
`/status.json`), then run the A2 commands again with `REF=<the good commit>`. This rebuilds the image from that commit
and bakes its hash in. `config/config.yaml` is not touched.

**An older config.** Backups are in `~/inkplate-weather/backups/`. Restore one over the live files and reload:

```sh
ssh pi@192.168.1.254
cd ~/inkplate-weather
ls backups/                                       # config-YYYYmmdd-HHMMSS.tar.gz, newest last
tar -tzf backups/config-20260101-033000.tar.gz    # check what is inside
tar -xzf backups/config-20260101-033000.tar.gz    # overwrites config/config.yaml (and .env if it was saved)
curl -X POST http://localhost:8080/admin/reload   # picks up config.yaml; after restoring .env run: sudo docker compose up -d
```

## Config backups

`config/config.yaml` and `.env` are the only state worth saving (they hold your location, source choices and secrets);
`cache/` is rebuilt from the sources. `scripts/backup-config.sh` copies the two files into
`backups/config-YYYYmmdd-HHMMSS.tar.gz`, with mode 600, and keeps the newest 14 archives. It prints what it did and
exits non-zero if anything failed. It uses only POSIX `sh`, `tar`, `date` and `ls`.

```sh
# manually, on the Pi
cd ~/inkplate-weather && sh scripts/backup-config.sh
sh ~/inkplate-weather/scripts/backup-config.sh ~/inkplate-weather      # from anywhere: pass the stack folder
sh scripts/backup-config.sh . 30                                       # keep 30 instead of 14 (or BACKUP_KEEP=30)
```

- **Before every deploy:** the A2 "every deployment" command runs it first. Run it by hand before editing
  `config/config.yaml` too.
- **Nightly:** on the Pi, `crontab -e` as the user that owns the stack folder and add
  (03:30 every day; adjust the home folder):

  ```
  30 3 * * * /bin/sh /home/pi/inkplate-weather/scripts/backup-config.sh /home/pi/inkplate-weather 14 2>&1 | logger -t inkplate-backup
  ```

  Check the result with `ls -l ~/inkplate-weather/backups` or `journalctl -t inkplate-backup` (or `grep inkplate-backup
  /var/log/syslog`, depending on the system's logging). A nightly run on an unchanged config just adds an identical
  archive; 14 of them are a few KB each.
- The archives hold secrets (the HA token). They stay on the Pi under `backups/` (directory mode 700); copy them
  somewhere safe yourself if you want an off-device copy. `backups/` is gitignored.

## Notes

- Memory: Chromium uses about 300-500 MB while rendering, about 150 MB idle. `shm_size: 256m` is set because Docker's
  64 MB default `/dev/shm` can crash Chromium. On a 2 GB Pi you can uncomment `mem_limit: 1g`.
- Logs: the app logs to stdout/stderr, one line per event, each starting with a timestamp in the container's `TZ`
  (`2026-10-25 08:15:02,123 INFO    inkplate: config reloaded from ...`; uvicorn's access lines have it too). Docker
  rotates them (json-file, 3 × 10 MB, so at most 30 MB per container). Read them with
  `sudo docker compose logs --tail 200 weather-display` (add `-f` to follow, `-t` for Docker's own UTC timestamps).
  The app writes no log files of its own.
- Disk use under `cache/` (container path `/cache`) stays small and does not grow over time, because every file is
  rewritten in place and the time series are trimmed. It holds:
  - `last-good.png`: the most recent rendered display (the fallback when a render fails), roughly 50-200 KB;
  - `providers/<name>.json`: one file per provider with the last good readings (forecast, observations, radar
    frames as small PNG masks, ...), typically tens of KB each, a few hundred KB at most for the radar;
  - `history.json`: the station temperature and pressure history, trimmed to the last 48 hours;
  - `lightning.json`: lightning flashes in the radar area, trimmed to the last 30 minutes;
  - `nws_points.json`: the cached NWS grid lookup for your location (one entry per location, a few KB).

  Expect well under 10 MB in total (`du -sh cache` to check). It is all safe to delete: stop the stack, remove the
  files (or `rm -rf cache/*`) and start it again. The next requests refetch everything; the past-24 h part of the
  trend chart says "No history yet" (and the pressure trend is missing) until about a day of readings has been
  recorded again, and `last-good.png` reappears after the first successful render.
  A provider you rename or remove from the config can leave its old `providers/<name>.json` behind (a few KB);
  delete it if you like. Keep the folder owned by uid 1000.
- Container paths: config `/config/config.yaml` (`INKPLATE_CONFIG`), cache `/cache` (`INKPLATE_CACHE_DIR`; the
  relative default `cache_dir: cache` also lands there because `/app/cache` links to `/cache`), time zone `TZ`.
