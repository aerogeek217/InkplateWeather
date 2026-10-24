# Deploying the server (phase 1)

The server runs as one container (`inkplate-weather`, service `weather-display`) on the OpenMediaVault Pi and listens
on port 8080. The image is about 0.9 GB and holds Python, Playwright 1.64.1 with the Chromium headless shell, and the
bundled fonts. It needs no internet to render, only to fetch data (from phase 2 on).

Files on the host, next to `docker-compose.yml`:

```
docker-compose.yml
server/            build context (only needed if you build on the Pi)
config/config.yaml your config (copy of config/config.example.yaml), mounted read-only at /config
cache/             provider cache and last-good image, mounted at /cache; must be writable by uid 1000
.env               optional: HA_TOKEN=... (phase 2), never committed
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

## 3. Configure and run on the OMV Pi

```sh
cd ~/inkplate-weather                     # or the OMV compose plugin's folder for this stack
cp config/config.example.yaml config/config.yaml   # edit as needed; not tracked by git
mkdir -p cache && sudo chown 1000:1000 cache       # the container runs as uid 1000
# phase 2 only: echo "HA_TOKEN=<long-lived token>" > .env && chmod 600 .env
docker compose up -d
docker compose ps                          # STATUS shows (healthy) after ~1 min
docker compose logs -f weather-display
```

In the OMV web UI (Services → Compose → Files), paste the contents of `docker-compose.yml` and keep the `config/`
and `cache/` folders next to it. The plugin's file folder must hold them (or edit the two volume paths to absolute
paths). If the plugin's folder has no `server/` directory, load the image first (option B) so compose doesn't try to
build.

## 4. Check it

```sh
curl http://<pi-ip>:8080/health
curl -o display.png "http://<pi-ip>:8080/display.png?batt=88&mv=4012&reason=timer"
docker inspect --format '{{.State.Health.Status}}' weather-display
```

Open `http://<pi-ip>:8080/preview` in a browser to see the live HTML.

## Updating

```sh
git pull && docker compose up -d --build       # option A
# option B: repeat the build/save/load steps, then: docker compose up -d
docker image prune -f                          # remove the old untagged image
```

## Notes

- Memory: Chromium uses about 300-500 MB while rendering, about 150 MB idle. `shm_size: 256m` is set because Docker's
  64 MB default `/dev/shm` can crash Chromium. On a 2 GB Pi you can uncomment `mem_limit: 1g`.
- Logs are rotated by Docker (3 × 10 MB).
- Container paths: config `/config/config.yaml` (`INKPLATE_CONFIG`), cache `/cache` (`INKPLATE_CACHE_DIR`; the
  relative default `cache_dir: cache` also lands there because `/app/cache` links to `/cache`), time zone `TZ`.
