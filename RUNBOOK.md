# GitOps host migration runbook

End-to-end procedure to take a host running raw `docker compose` stacks and put it under
[`doco-cd`](https://github.com/kimdre/doco-cd) with secrets in Vaultwarden, semver-pinned
images, and Dependabot. Tested on `eva01`; reuse for `eva00` etc.

Conventions throughout:
- `<host>` — short host name used as gitops subfolder (e.g. `eva01`, `eva00`)
- `<host_ip>` — host LAN IP (used for cross-stack networking)
- Repo: https://github.com/liamniou/gitops (one repo per fleet, one subfolder per host)
- Bind mounts live under `/home/lestar/_volumes/<app>/`

---

## 1. Bootstrap layer (manual, NOT under gitops)

Two stacks are kept outside gitops to avoid a chicken-and-egg on host boot:

### 1a. Vaultwarden — `/home/lestar/containers/vaultwarden/compose.yaml`
Standard `vaultwarden/server:latest` with persistent volume at
`/home/lestar/_volumes/vaultwarden/`. Bring up first; create an admin account and
an API key. This is the secret store backing `doco-cd`.

### 1b. doco-cd + bitwarden-rest-api — `/home/lestar/doco-cd/compose.yml`
- `ghcr.io/kimdre/doco-cd:<pinned>` polls the gitops repo every 60s
- `ghcr.io/kimdre/bitwarden-rest-api-server` sidecar (a.k.a.
  [bitwarden-rest-api-server](https://github.com/kimdre/bitwarden-rest-api-server))
  exposes Vaultwarden items as REST on `bitwarden-api:8087`
- `POLL_CONFIG` is **inline** in `compose.yml`, not in a per-stack `.doco-cd.yml`
- Reload after editing `POLL_CONFIG`:
  ```
  cd /home/lestar/doco-cd && docker compose up -d
  ```
- Network created: `doco-cd_default` (used by curl bootstrap commands below)
- Gitops checkout lives at:
  `/var/lib/docker/volumes/doco-cd_data/_data/github.com/liamniou/gitops/<host>/`

`POLL_CONFIG` skeleton (one entry per deployment):
```yaml
POLL_CONFIG: |
  deployments:
    - name: <stack>
      working_dir: <host>/<stack>
      reference: refs/heads/main
      external_secrets:
        VAR_NAME:
          store_ref: bitwarden-fields
          remote_ref:
            key: <vaultwarden-item-uuid>
            property: <field-name-on-that-item>
```

---

## 2. Per-stack migration loop

For each container in `/home/lestar/containers/<stack>/`:

### 2.1. Analyse
- `cat compose.yaml` and the matching `.env` if present
- Note every env var, bind mount, port, network, healthcheck
- Decide: keep as one stack, split into multiple stacks, consolidate with another, or delete

### 2.2. Secrets → Vaultwarden
For each stack with secrets, create ONE Vaultwarden item named `<stack>-<host>`
with one custom field per env var:

```bash
docker run --rm --network doco-cd_default curlimages/curl:latest -s \
  -X POST -H "Content-Type: application/json" \
  -d '{
        "type": 1,
        "name": "<stack>-<host>",
        "fields": [
          {"name": "VAR_ONE", "value": "val1", "type": 1},
          {"name": "VAR_TWO", "value": "val2", "type": 1}
        ],
        "login": {"uris": [{"uri": "https://example.tld"}]}
      }' \
  http://bitwarden-api:8087/object/item
```

Response contains `data.id` — that UUID is the `remote_ref.key` you reference
from `POLL_CONFIG`.

### 2.3. Write the gitops compose

```
mkdir -p /tmp/gitops/<host>/<stack>
$EDITOR /tmp/gitops/<host>/<stack>/compose.yaml
```

Rules:
- **No secrets in the file** — only `${VAR}` placeholders; doco-cd injects them
- Bind mounts use absolute paths under `/home/lestar/_volumes/<app>/`
- Cross-stack containers cannot resolve each other by service name — connect via
  `<host_ip>:<published_port>`, OR `network_mode: host`, OR an explicit external network
- Project name (which prefixes named volumes) = deployment name in `POLL_CONFIG`
- Pin every image to a semver tag (see §4)

### 2.4. Migrate stateful data
For DBs, do a clean shutdown before moving the data dir:
```bash
docker compose exec <db_service> mysqladmin -uroot -p shutdown   # mysql
docker compose exec <db_service> pg_ctl stop                     # postgres (or just down)
sudo mv /old/data/path /home/lestar/_volumes/<app>/db
```

### 2.5. Stop old, register new, validate
```bash
# Stop OLD stack (never use -v unless you mean to lose volumes)
cd /home/lestar/containers/<stack> && docker compose down

# Add deployment block to /home/lestar/doco-cd/compose.yml POLL_CONFIG
$EDITOR /home/lestar/doco-cd/compose.yml
cd /home/lestar/doco-cd && docker compose up -d

# Commit + push
cd /tmp/gitops && git add <host>/<stack> && git commit -m "Add <stack>" && git push

# Wait up to 60s for next doco-cd poll, then check
docker logs --since=2m doco-cd 2>&1 | grep -v "deployment in progress"
docker ps --filter "label=com.docker.compose.project=<stack>"
```

### 2.6. Validate, then clean up
Once the user confirms the new stack works:
```bash
rm -rf /home/lestar/containers/<stack>
docker image prune -f
```

---

## 3. Cross-stack networking gotchas

- Each gitops deployment gets its own docker bridge → service-name DNS does NOT
  cross stacks. Examples encountered:
  - `zigbee2mqtt` → `mqtt`: had to change `server: mqtt://mqtt:1883` to
    `server: mqtt://<host_ip>:1883`
  - `alloy` scrape targets in other stacks: use `<host_ip>:<port>`
- `network_mode: host` works for things like Home Assistant that need mDNS/discovery
- For shared infra (rare), declare an `external: true` network and `docker network create` it manually

---

## 4. Image tag policy

- **Third-party images**: pin to specific semver, e.g. `postgres:14.23`,
  `cloudflare/cloudflared:2026.5.2`, `koenkk/zigbee2mqtt:2.10.1`
- **Own GHCR images** that only publish `:latest`: pin with immutable digest
  `image:latest@sha256:<digest>` — Dependabot updates the digest
- **Never** use `:latest`, `:stable`, `:7-alpine` (major-only) or no tag

Resolve current latest semver for a Docker Hub image:
```bash
curl -s "https://hub.docker.com/v2/repositories/<repo>/tags?page_size=100" | \
  python3 -c "import sys,json,re; d=json.load(sys.stdin); \
    print('\n'.join(sorted([t['name'] for t in d['results'] \
    if re.match(r'^v?\d+\.\d+\.\d+$', t['name'])])[-5:]))"
```

GHCR (anonymous):
```bash
tok=$(curl -s "https://ghcr.io/token?scope=repository:<owner>/<image>:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $tok" \
  "https://ghcr.io/v2/<owner>/<image>/tags/list?n=2000" | jq -r '.tags[]'
```

Get the current digest of an image already pulled:
```bash
docker pull -q <image>:latest
docker image inspect <image>:latest --format '{{index .RepoDigests 0}}'
```

---

## 5. Dependabot

`.github/dependabot.yml` at the gitops repo root (one entry per deployment):
```yaml
version: 2
updates:
  - package-ecosystem: "docker-compose"
    directory: "/<host>/<stack>/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    commit-message:
      prefix: "deps(<stack>)"
    labels:
      - "dependencies"
      - "docker"
```
Generator one-liner once you have the directory layout:
```bash
for d in <host>/*/; do
  name=$(basename "$d")
  cat <<EOF
  - package-ecosystem: "docker-compose"
    directory: "/$d"
    schedule: {interval: "weekly", day: "monday"}
    open-pull-requests-limit: 5
    commit-message: {prefix: "deps($name)"}
    labels: ["dependencies", "docker"]
EOF
done
```

Merging a Dependabot PR → next `doco-cd` poll redeploys the stack.

---

## 6. Useful checks

```bash
# All running containers and the images they're on
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | sort | column -t -s$'\t'

# Any container still on a floating tag (excluding bootstrap)
docker ps --format '{{.Image}}' | grep -E ':(latest|stable)$|^[^:/]+$' | grep -v '@sha256'

# Find which compose project a container came from
docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}'

# doco-cd activity
docker logs --since=5m doco-cd 2>&1 | grep -vE "deployment in progress|polling repository"
```

---

## 7. Decision log from eva01 (reuse / override per host)

| Container          | Decision                                                                 |
|--------------------|--------------------------------------------------------------------------|
| `conbee`           | Split into 3 stacks: `mqtt`, `homeassistant`, `zigbee2mqtt`              |
| `prometheus-agent` | Consolidated into `alloy` (scrape jobs added)                            |
| `dockhand`         | Removed (unused)                                                         |
| `grafana-screenshot` | Removed (not running)                                                  |
| `hawser`           | `/stacks` repointed to `doco-cd_data` volume so it sees gitops checkout  |
| `vaultwarden`      | Stays as bootstrap (chicken-and-egg with doco-cd)                        |
| `socials-to-telegram` | `cookies.txt` removed from image, mounted at runtime from `_volumes`  |
| `missing-container-metrics` | Forked, bumped Docker SDK to v27, built via GHCR Actions        |
