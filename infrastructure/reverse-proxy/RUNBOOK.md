# VPS Public Dashboards — Runbook

How the SmartLoad dashboards are exposed publicly on `cie21grad.systems`, and how
to reuse the exact same setup for the next project (**cybersiren**) without redoing
any discovery. This box hosts **multiple projects behind one shared reverse proxy**.

> TL;DR of the model: every app listens on `127.0.0.1` only; one host-level Caddy
> on `:80/:443` is the single public door; a firewall + a Docker-bypass rule make
> sure nothing else is reachable from the internet. Adding a project = drop one
> file in `/etc/caddy/sites/` and reload.

---

## 1. The box at a glance

| Thing | Value |
|---|---|
| VPS public IP | `159.195.80.109` (eth0) |
| Domain | `cie21grad.systems` (registrar: **name.com**) |
| DNS | **wildcard** `*.cie21grad.systems` → `159.195.80.109` (also apex). Any subdomain already resolves here — no per-subdomain records needed. |
| Open ports (internet) | **22** (SSH), **80** (HTTP→HTTPS redirect + ACME), **443** (HTTPS). Nothing else. |
| Reverse proxy | **host-level Caddy v2** via systemd unit `caddy`, config `/etc/caddy/Caddyfile` |
| TLS | Let's Encrypt, auto-issued/renewed by Caddy (works because DNS already points here) |
| Firewall | `ufw` (host ports) **+** `DOCKER-USER` iptables rule (Docker bypasses ufw — see §5) |

### Public SmartLoad URLs

| URL | → upstream | Auth |
|---|---|---|
| https://smartload-demo.cie21grad.systems | `127.0.0.1:8091` (demo UI) | open |
| https://smartload-operator.cie21grad.systems | `127.0.0.1:8090` (operator UI) | **basic auth** (user `operator`) |
| https://smartload-grafana.cie21grad.systems | `127.0.0.1:3000` (Grafana) | Grafana login; anon read-only viewer |
| https://smartload-prometheus.cie21grad.systems | `127.0.0.1:9090` (Prometheus) | open |
| https://smartload-locust.cie21grad.systems | `127.0.0.1:8089` (Locust) | open |
| https://smartload-lb.cie21grad.systems | `127.0.0.1:8080` (NGINX LB) | open |

All six are served by the single **`smartload-aio`** container (the all-in-one image),
published on `127.0.0.1` only.

---

## 2. Architecture

```
                     internet
                        │   (only :22, :80, :443 reachable — ufw + DOCKER-USER)
                        ▼
        ┌────────────────────────────────┐
        │  host Caddy  (systemd: caddy)   │   :80 → 308 redirect → :443
        │  /etc/caddy/Caddyfile           │   terminates TLS (Let's Encrypt)
        │  imports /etc/caddy/sites/*.caddy│
        └───────────────┬────────────────┘
                        │ reverse_proxy to 127.0.0.1:<port>  (loopback, never WAN)
        ┌───────────────┼─────────────────────────────┐
        ▼               ▼                              ▼
  smartload-aio   (future) cybersiren containers   …more projects
  127.0.0.1:8090/8091/3000/9090/8089/8080          127.0.0.1:<ports>
```

Key invariant: **containers bind `127.0.0.1` only.** The internet can never reach a
container directly; the only path in is Caddy on 80/443. This is what makes "lock
everything except 22/80/443" actually true on a Docker host (see §5 for why Docker
needs special handling).

---

## 3. Files (all version-controlled in this repo)

```
infrastructure/reverse-proxy/
├── Caddyfile                     → deploy to /etc/caddy/Caddyfile (shared, global)
├── deploy.sh                     → renders + installs config, validates, reloads caddy
├── sites/
│   ├── smartload.caddy           → deploy to /etc/caddy/sites/smartload.caddy
│   │                               (operator hash is __OPERATOR_BCRYPT__ placeholder;
│   │                                deploy.sh substitutes the real hash)
│   └── _template.caddy.example   → copy this to add a new project's subdomains
└── RUNBOOK.md                    → this file

infrastructure/firewall/
├── docker-internet-lockdown.sh        → /usr/local/sbin/… (blocks internet→container)
└── docker-internet-lockdown.service   → /etc/systemd/system/… (re-applies on boot/docker restart)

docker/allinone/
├── run-live.sh                   → (re)launch smartload-aio bound to 127.0.0.1 + restart policy
└── .live.env                     → SECRETS (gitignored): Grafana pw, operator basic-auth pw + bcrypt
```

Secrets live ONLY in `docker/allinone/.live.env` (chmod 600, gitignored). The deployed
`/etc/caddy/sites/smartload.caddy` contains the real bcrypt hash; the repo copy keeps a
placeholder.

---

## 4. The reverse proxy (Caddy)

**Main config** `/etc/caddy/Caddyfile`:
```caddy
{
	email aserosama10e@gmail.com
}
import /etc/caddy/sites/*.caddy
```
That `import` is the whole trick: each project is one self-contained file under
`/etc/caddy/sites/`. Caddy obtains a cert per hostname automatically on first request.

**Deploy / change config:**
```bash
# after editing infrastructure/reverse-proxy/*:
bash infrastructure/reverse-proxy/deploy.sh      # validates + zero-downtime reload
# or by hand:
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**Basic auth hash:**
```bash
caddy hash-password --plaintext 'your-password'   # paste output into the site file
```

---

## 5. Firewall — and the Docker gotcha (IMPORTANT)

Two layers, because **`ufw` alone is not enough on a Docker host**:

1. **`ufw`** — host-level INPUT filtering. Allows only 22/80/443:
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
   sudo ufw --force enable
   ```
   This governs host processes (sshd, Caddy). It does **NOT** govern Docker-published
   ports: Docker inserts its own DNAT rules that are evaluated before ufw's INPUT
   chain, so a container published on `0.0.0.0:9092` stays world-reachable even with
   `ufw deny`. (This is a well-known Docker behavior, not a misconfig.)

2. **`DOCKER-USER` chain** — the supported hook to filter traffic forwarded to
   containers. We drop all *new* internet→container connections on the WAN interface,
   while leaving replies and host/inter-container traffic alone:
   ```
   DOCKER-USER:
     -i eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN   # replies OK
     -i eth0 -j DROP                                                # block internet→container
     (default) -j RETURN                                            # host/Caddy→container OK
   ```
   Applied + persisted by `docker-internet-lockdown.service` (re-runs at boot and on
   every `docker` restart, because Docker recreates DOCKER-USER empty each time).

   > This is why container ports may still show as `0.0.0.0:PORT` in `docker ps`
   > (e.g. cybersiren's) yet are NOT reachable from the internet — the DROP rule
   > catches them. The clean habit is still to **bind to `127.0.0.1`** in compose so
   > defense is twofold.

**Verify from your laptop (NOT from the VPS — local hairpin bypasses the WAN rule):**
```bash
nc -vz cie21grad.systems 443     # should CONNECT
nc -vz cie21grad.systems 22      # should CONNECT
nc -vz cie21grad.systems 9092    # should TIME OUT / refuse (a cybersiren container port)
```

---

## 6. The SmartLoad container (`smartload-aio`)

Single all-in-one image running the full stack via supervisord. Launched for live use by:
```bash
bash docker/allinone/run-live.sh
```
which: removes any old container, then `docker run -d --restart unless-stopped` with
**every dashboard port bound to `127.0.0.1`** (8090/8091/3000/9090/8089/8080) and the
Grafana admin password + public root URL injected from `.live.env`.

- Ephemeral by design (no data volumes) — demo data repopulates on its own. To persist
  Postgres/Grafana/Prometheus across recreputs, add `-v` mounts for
  `/var/lib/postgresql/data`, `/var/lib/grafana/data`, `/var/lib/prometheus/data`.
- `--restart unless-stopped` ⇒ survives reboot.

**Credentials** (full values in `docker/allinone/.live.env`):
- Operator dashboard basic auth: user `operator`, password in `.live.env`
  (`OPERATOR_BASICAUTH_PASSWORD`).
- Grafana admin: user `admin`, password = `GRAFANA_PASSWORD` in `.live.env`.

---

## 7. ➤ Onboarding the NEXT project (cybersiren) — the reusable recipe

DNS is already done for you (wildcard). You only need 3 things:

1. **Make cybersiren's dashboards listen on `127.0.0.1`.** In its compose file, change
   published ports from `"8080:80"` to `"127.0.0.1:8080:80"`, etc. (Even if you forget,
   the DOCKER-USER rule blocks the internet — but do it anyway.) Note which localhost
   port each dashboard ends up on.

2. **Add a Caddy site file:**
   ```bash
   cp infrastructure/reverse-proxy/sites/_template.caddy.example \
      /etc/caddy/sites/cybersiren.caddy        # then edit hostnames + 127.0.0.1:ports
   # gate any control/admin UI with basic_auth (caddy hash-password ...)
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```
   Example block:
   ```caddy
   cybersiren-portal.cie21grad.systems   { reverse_proxy 127.0.0.1:18090 }
   cybersiren-jaeger.cie21grad.systems   { reverse_proxy 127.0.0.1:16686 }
   ```

3. **Visit the URL.** Caddy fetches a Let's Encrypt cert on the first hit. Done.

Firewall? Already handles it — nothing to change (22/80/443 stay the only open ports;
the lockdown service already blocks direct access to all of cybersiren's container
ports). Keep cybersiren's site file in the cybersiren repo for version control, mirroring
how `sites/smartload.caddy` lives here.

---

## 8. Operations / troubleshooting

| Task | Command |
|---|---|
| Reload proxy after editing a site file | `sudo systemctl reload caddy` |
| See proxy logs | `sudo journalctl -u caddy -f` |
| Check a cert | `echo \| openssl s_client -connect 127.0.0.1:443 -servername smartload-demo.cie21grad.systems 2>/dev/null \| openssl x509 -noout -dates -issuer` |
| Cert won't issue | confirm host resolves here (`dig +short <host> @8.8.8.8` → `159.195.80.109`) and 80/443 reachable; `sudo journalctl -u caddy` for ACME errors |
| Restart SmartLoad stack | `bash docker/allinone/run-live.sh` |
| Re-apply Docker firewall (after `systemctl restart docker`) | `sudo systemctl start docker-internet-lockdown.service` (auto on boot/docker restart) |
| ufw state | `sudo ufw status verbose` |
| DOCKER-USER state | `sudo iptables -L DOCKER-USER -n -v --line-numbers` |
| 502 from a subdomain | the upstream container/port is down → `docker ps`, check `127.0.0.1:<port>` answers |

Certs renew automatically (~30 days before expiry) as long as DNS keeps pointing here
and 80/443 stay open. No cron needed.
