# Kong -Postgres Backup & Restore (Rollback)

This guide shows how to **backup** and **restore (rollback)** Kong’s Postgres database running in the `kong` namespace.

## Prerequisites

- You have `kubectl` access to the cluster.
- Postgres service is reachable at:

  `postgres-kong.kong.svc.cluster.local:5432`

- DB credentials (example):

  - `PGDATABASE=kong`
  - `PGUSER=kong`
  - `PGPASSWORD=supersecret-kong`


---

## Backup 

Create a **kong postgres** dump file:

```bash
kubectl run pg-dump-once -n kong --rm -i \
  --image=postgres:17 \
  --restart=Never \
  --env="PGHOST=postgres-kong.kong.svc.cluster.local" \
  --env="PGDATABASE=kong" \
  --env="PGUSER=kong" \
  --env="PGPASSWORD=supersecret-kong" \
  --env="PGPORT=5432" \
  -- /bin/bash -lc 'pg_dump --format=custom --no-owner --no-acl' \
  > kong.backup.$(date +%F).dump
```

### Verify dump file exists
```bash
ls -lh kong.backup.*.dump
```

---

## Restore / Rollback 

### 1. Scale Kong down 
```bash
kubectl -n kong scale deployment/kong-kong --replicas=0
kubectl -n kong rollout status deployment/kong-kong
```

### 2. Restore from dump file

Set  dump filename:

```bash
DUMP_FILE="kong.backup.2026-02-26.dump"
```

Restore command:

```bash
kubectl run pg-restore-once -n kong --rm -i \
  --image=postgres:17 \
  --restart=Never \
  --env="PGHOST=postgres-kong.kong.svc.cluster.local" \
  --env="PGDATABASE=kong" \
  --env="PGUSER=kong" \
  --env="PGPASSWORD=supersecret-kong" \
  --env="PGPORT=5432" \
  -- /bin/bash -lc 'set -euo pipefail
      TMP="$(mktemp /tmp/kong.dump.XXXXXX)"
      cat > "$TMP"
      pg_restore --verbose --clean --if-exists --no-owner --no-acl --dbname="$PGDATABASE" "$TMP"
      rm -f "$TMP"
    ' < "$DUMP_FILE"
```


### 3. Scale Kong back up
```bash
kubectl -n kong scale deployment/kong-kong --replicas=1
kubectl -n kong rollout status deployment/kong-kong
```

---

## Quick Validation

### Check Kong pods
```bash
kubectl -n kong get pods
```

### Check DB tables exist
```bash
kubectl run pg-check -n kong --rm -i --image=postgres:17 --restart=Never \
  --env="PGHOST=postgres-kong.kong.svc.cluster.local" \
  --env="PGDATABASE=kong" \
  --env="PGUSER=kong" \
  --env="PGPASSWORD=supersecret-kong" \
  --env="PGPORT=5432" \
  -- /bin/bash -lc 'psql -d "$PGDATABASE" -c "\\dt" | head -60'
```

---