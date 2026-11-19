# evcc tester environment

Deployment notes for the `evcc` RL environment. Source build only: the app is
built from this checkout by `tester-env`, never from a prebuilt image.

## Quick start

```bash
./tester-env deploy    # build image, start container with a fresh sqlite volume
./tester-env seed      # write sessions + energy history into that volume
./tester-env verify    # assert apiReady, site, loadpoints, sessions, energy history
./tester-env status    # container state + URL
./tester-env logs      # tail evcc logs
./tester-env stop      # stop container, keep sqlite volume
./tester-env reset     # remove container and sqlite volume
```

Reproducible state is `reset` -> `deploy` -> `seed` (the order `scripts/rl-env`
and `scripts/preflight-app` use). `deploy` already drops the volume, so a plain
`deploy` gives the unseeded config state.

URL: <http://localhost:18070/> (container port 7070; override with `--port`).
No credentials: the container runs `evcc --config /config/tester-env.yaml
--disable-auth --disable-onboarding`.

## Layout

| File | Purpose |
|------|---------|
| `Dockerfile.tester-env` | three-stage build: vite-plus UI, `golang:1.27.3-alpine` Go build, alpine runtime (+ `sqlite3` for seeding) |
| `tester-env` | CLI (`deploy`, `seed`, `verify`, `reset`, `stop`, `logs`, `status`) |
| `tester-env.yaml` | deterministic config adapted from `cmd/demo.yaml` |
| `tester-env-seed.py` | seed SQL generator and API seed check (stdlib only, needs `python3`) |
| `.dockerignore` | adds `.git` so builds are reproducible (version/commit come from build args) |
| `.gitignore` | allows `tester-env.yaml` in the fork |

## What is deterministic

- `tester-env.yaml` is `cmd/demo.yaml` with the three `Math.random()`
  expressions replaced by constants and the battery SoC / heat pump temperature
  / vehicle SoC drift loops removed. Every poll returns the same values.
- No auth, no onboarding wizard (`--disable-auth --disable-onboarding`).
- mDNS discovery off: `EVCC_DISCOVERY_HOSTS=[]`.
- sqlite only, at `/data/evcc.db` on the `evcc-data*` named volume
  (`EVCC_DATABASE_DSN`). Reset drops the volume.
- Version injected as `0.319.1-dev.tester-env`, which skips the upstream
  GitHub update check (`-dev.` builds are excluded).

Time-of-day tariffs (`demo-dynamic-grid`, `demo-solar-forecast`,
`demo-co2-forecast`, `demo-temperature-forecast`) are stable within a run but
move with the wall clock, as in other accepted energy apps.

## Seeded data

`./tester-env seed` pipes the SQL from `tester-env-seed.py` into
`sqlite3 /data/evcc.db` inside the running container (the runtime image ships
`sqlite3`). Only two things are written; everything else comes from the config.

Completed charging sessions, ids 101-114, three vehicles and two charging points:

| Month | Sessions | Content |
|-------|----------|---------|
| current | 1-6 | the month's fixed days that have passed (4/9/15/21/26) plus one session ~2.75 h ago |
| previous | 5 | white Model 3 24.6/32 kWh, blue e-Golf 15.2 kWh, green Honda e 6.4 kWh, black VanMoof 0.38 kWh |
| two months back | 5 | same fixed set |

- Sessions sit on the 4th, 9th, 15th, 21st and 26th of each month at fixed
  times, which keeps the Sessions month view, its month navigator and the
  "month without data" empty state (any month older than the window) usable.
- Fields are fully populated: charged kWh, meter start/stop, duration, SoC
  start/end, added range, odometer, solar share, cost, price/kWh, CO2/kWh.
  The VanMoof row is the edge case (0.38 kWh, no odometer).
- The freshest session is anchored to seed time (2.75 h ago, 45 min, 3.2 kWh
  Carport / blue e-Golf) so the default Sessions view is never empty; on the
  1st shortly after midnight it snaps to local month start.

Energy history for the last seven complete days plus today's completed
15 minute slots, for `home`, `grid`, `pv`, `battery`, `lp-1` (Carport) and
`lp-2` (Garage): deterministic solar curve (day-factor from the date), house
load profile, battery charge/discharge with SoC, Garage/Carport charging
windows, and one `tariffs` row per slot (grid/feed-in/CO2/temperature) so the
Energy charts show cost and CO2. Values are wh-rounded like evcc's own slots;
slots evcc writes after boot are never touched (today is seeded only up to the
last completed slot).

Stable assertions after `reset -> deploy -> seed`:

- `/api/sessions` returns 14 sessions with those vehicles, loadpoints, kWh and
  costs; the API month filter returns 5 for each of the two older months.
- November (current month) initially shows 4 sessions, 49.4 kWh, 8.16 €.
- `/api/history/energy?aggregate=day` returns 7 day buckets for home/grid/pv/
  battery: pv 203.454 kWh, home 123.896 kWh, grid import 41.501 kWh, battery
  charge 120.303 kWh, with grid cost > 0 from the tariff join.
- `verify` recomputes those numbers from the same generator, so a schema or
  format drift fails the CLI instead of silently producing empty charts.

`verify` assumes it runs in the same local (Europe/Berlin) day as `seed`:
session days and the history window are anchored to that date. `rl-env` and
`preflight-app` run seed and verify back to back; re-run `seed` if the date
rolled over.

## State-changing surfaces

The dashboard's mutating controls go through the config's custom JS plugins, so
UI actions change what later reads show:

- loadpoint mode selector (Off / PV / Min+PV / Now) and enable/disable,
- max current and 1p/3p phase switching (Garage),
- heat pump mode (sg-ready) and temperature limits,
- config view: device CRUD and the YAML editor (session settings persist in
  sqlite, so they reset when the volume is dropped).

`reset` removes the volume, so a mode change such as Garage `off -> now` is
reverted by `reset` (or by `deploy`, which also drops the volume); the seeded
sessions and history are then recreated by `seed`.

## Sponsor-only surfaces

Telemetry, the hosted optimizer (Optimize view) and some hardware drivers need
a sponsor token. The demo/custom-plugin path needs none; avoid the Optimize
view when authoring scenarios.

## Rebuild costs

Measured 2026-11-19 on this host (28 cores, 62 GB RAM):

| Change | Wall time | Dominant stages |
|--------|-----------|-----------------|
| cold build | 192 s | npm ci, vp build, go mod download, go install tool, go generate, go build |
| Go-only mutation | 33 s | `make assets` 26.6 s, `make build` 3.6 s |
| UI mutation under `assets/` | 33 s | `make ui` 23.2 s (vp build 21.4 s), `make assets`, `make build` |
| no-op rebuild | 1.7 s | all cached |

BuildKit cache mounts (`evcc-npm`, `evcc-gomod`, `evcc-gocache`) keep
`npm ci`, `go mod download`/`go install tool` and the Go compile cache warm
across rebuilds. `make patch-asn1` runs in its own layer, so it is only
re-executed when the Go base image changes.

## Caveats

- The Go builder must stay on `golang:1.27.3-alpine`: `make patch-asn1` patches
  `$(GOROOT)/src/vendor/.../cryptobyte/asn1.go`.
- The UI stage needs `ghcr.io/voidzero-dev/vite-plus:1.4.0` (Vite+).
- `.dockerignore` excludes `*.yaml`, so `tester-env.yaml` is mounted into the
  container instead of being baked into the image.
- The runtime image installs `sqlite3` (`apk add --no-cache sqlite`) so `seed`
  can write into the volume; the layer is cached, so rebuilds stay fast.
- `seed` and `verify` need `python3` on the host (stdlib only, same as the
  Solectrus seed script).
- OCPP/EEBus listeners still bind inside the container; they are not published.

## Fork baseline and verification

- Canonical fork: <https://github.com/sma-rl-dev/evcc>, created with
  `gh repo fork https://github.com/evcc-io/evcc --org sma-rl-dev --remote=false`
  (isFork, parent `evcc-io/evcc`; no personal-namespace fork exists or is used).
- Branch `tester-env/baseline` = upstream tag `0.319.1`
  (`29ef1eb4d0fff3186f6d9574496691b09cd6ae43`) + tooling commit
  `5e488316acb9efb857ef2297b66027e5c98fd286` (Dockerfile.tester-env, tester-env,
  tester-env.yaml, tester-env-seed.py, DEPLOY-SMA.md, .dockerignore/.gitignore).
- Corpus submodule `apps/evcc` tracks this fork and pins an immutable commit;
  `app_cli = "tester-env"` is submodule-relative and all deployment tooling lives
  inside the fork.

Fresh-checkout gate (2026-11-19, from `apps/evcc`):

```bash
IMAGE_TAG=tester-env-evcc:integrator-check ./tester-env deploy --run-id integrator --port 18071
./tester-env seed   --run-id integrator --port 18071   # 14 sessions, 7 days history
./tester-env verify --run-id integrator --port 18071   # PASS, seed re-checked
./tester-env reset  --run-id integrator                # only its container + volume
```

`deploy` rebuilt from editable source (warm cache), became healthy, and
`seed`/`verify` passed. `reset` removed only `tester-env-evcc-integrator` and
`evcc-data-integrator` and kept the image (content-addressed `IMAGE_TAG`).

Browser evidence (browser-checker 2026-11-19, recorded here as supplied by the
orchestrator; the fork integrator ran no browser): dashboard, Sessions
November/September/Total periods and the Energy day view for 18 Nov are visible
after `reset -> deploy -> seed`.
