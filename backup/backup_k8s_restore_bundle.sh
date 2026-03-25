#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./backup_k8s_restore_bundle.sh
# Optional:
#   NAMESPACES="hudson redis" ./backup_k8s_restore_bundle.sh
#   OUTDIR_BASE=/backup ./backup_k8s_restore_bundle.sh

NAMESPACES=(${NAMESPACES:-hudson kong redis})
OUTDIR_BASE="${OUTDIR_BASE:-$PWD}"
TS="$(date +%F_%H%M%S)"
OUTDIR="${OUTDIR_BASE}/k8s-backup-${TS}"

mkdir -p "${OUTDIR}"/{cluster,namespaces,reports,runtime}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

need_cmd kubectl
need_cmd sed
need_cmd awk
need_cmd sort
need_cmd uniq
need_cmd tar

# Kong Postgres dump configuration
KONG_PG_NS="${KONG_PG_NS:-kong}"
KONG_PG_HOST="${KONG_PG_HOST:-postgres-kong.kong.svc.cluster.local}"
KONG_PG_DB="${KONG_PG_DB:-kong}"
KONG_PG_USER="${KONG_PG_USER:-kong}"
KONG_PG_PASSWORD="${KONG_PG_PASSWORD:-supersecret-kong}"
KONG_PG_PORT="${KONG_PG_PORT:-5432}"
KONG_PG_IMAGE="${KONG_PG_IMAGE:-postgres:17}"

clean_yaml() {
  sed \
    -e '/^[[:space:]]*uid:/d' \
    -e '/^[[:space:]]*resourceVersion:/d' \
    -e '/^[[:space:]]*generation:/d' \
    -e '/^[[:space:]]*creationTimestamp:/d' \
    -e '/^[[:space:]]*deletionTimestamp:/d' \
    -e '/^[[:space:]]*deletionGracePeriodSeconds:/d' \
    -e '/^[[:space:]]*managedFields:/,/^[^[:space:]]/d' \
    -e '/^[[:space:]]*status:/,$d'
}

backup_cluster_info() {
  kubectl version -o yaml > "${OUTDIR}/cluster/kubectl-version.yaml" || true
  kubectl cluster-info dump > "${OUTDIR}/cluster/cluster-info-dump.txt" || true
  kubectl get nodes -o wide > "${OUTDIR}/cluster/nodes.txt"
  kubectl get nodes -o yaml | clean_yaml > "${OUTDIR}/cluster/nodes.yaml"
  kubectl get ns -o yaml | clean_yaml > "${OUTDIR}/cluster/namespaces.yaml"
  kubectl get storageclass -o yaml | clean_yaml > "${OUTDIR}/cluster/storageclasses.yaml" || true
  kubectl get pv -o yaml | clean_yaml > "${OUTDIR}/cluster/persistentvolumes.yaml" || true
  kubectl get crd -o yaml | clean_yaml > "${OUTDIR}/cluster/crds.yaml" || true
  kubectl get clusterrole -o yaml | clean_yaml > "${OUTDIR}/cluster/clusterroles.yaml" || true
  kubectl get clusterrolebinding -o yaml | clean_yaml > "${OUTDIR}/cluster/clusterrolebindings.yaml" || true
  kubectl get priorityclass -o yaml | clean_yaml > "${OUTDIR}/cluster/priorityclasses.yaml" || true
}

backup_namespace_restore_objects() {
  local ns="$1"
  local nsdir="${OUTDIR}/namespaces/${ns}"
  mkdir -p "${nsdir}"/{restore,raw}

  kubectl get namespace "${ns}" -o yaml | clean_yaml > "${nsdir}/restore/00-namespace.yaml"

  # Restore-relevant namespaced resources
  local kinds=(
    serviceaccount
    secret
    configmap
    role
    rolebinding
    deployment
    statefulset
    daemonset
    service
    ingress
    job
    cronjob
    pvc
    networkpolicy
    poddisruptionbudget
    horizontalpodautoscaler
    resourcequota
    limitrange
  )

  for kind in "${kinds[@]}"; do
    if kubectl get "${kind}" -n "${ns}" >/dev/null 2>&1; then
      kubectl get "${kind}" -n "${ns}" -o yaml | clean_yaml > "${nsdir}/restore/${kind}.yaml" || true
    fi
  done

  # Capture any additional custom namespaced resources too
  while IFS= read -r res; do
    case "${res}" in
      pods|pods.metrics.k8s.io|replicasets.apps|replicationcontrollers|controllerrevisions.apps|events|events.events.k8s.io|endpoints|endpointslices.discovery.k8s.io|leases.coordination.k8s.io)
        continue
        ;;
    esac

    safe_name="$(echo "${res}" | tr '/.' '__')"

    if kubectl get "${res}" -n "${ns}" >/dev/null 2>&1; then
      kubectl get "${res}" -n "${ns}" -o yaml | clean_yaml > "${nsdir}/raw/${safe_name}.yaml" || true
    fi
  done < <(kubectl api-resources --verbs=list --namespaced -o name | sort -u)
}

backup_runtime_state() {
  local ns="$1"
  local rtdir="${OUTDIR}/runtime/${ns}"
  mkdir -p "${rtdir}"/{pods,describes,logs}

  kubectl get all -n "${ns}" -o wide > "${rtdir}/get-all.txt" || true
  kubectl get pod -n "${ns}" -o wide > "${rtdir}/pods.txt" || true
  kubectl get pod -n "${ns}" -o yaml > "${rtdir}/pods.yaml" || true
  kubectl get events -n "${ns}" --sort-by=.metadata.creationTimestamp > "${rtdir}/events.txt" || true

  kubectl describe deploy -n "${ns}" > "${rtdir}/describes/deployments.txt" 2>/dev/null || true
  kubectl describe sts -n "${ns}" > "${rtdir}/describes/statefulsets.txt" 2>/dev/null || true
  kubectl describe ds -n "${ns}" > "${rtdir}/describes/daemonsets.txt" 2>/dev/null || true
  kubectl describe svc -n "${ns}" > "${rtdir}/describes/services.txt" 2>/dev/null || true
  kubectl describe ingress -n "${ns}" > "${rtdir}/describes/ingress.txt" 2>/dev/null || true
  kubectl describe pvc -n "${ns}" > "${rtdir}/describes/pvc.txt" 2>/dev/null || true
  kubectl describe pod -n "${ns}" > "${rtdir}/describes/pods.txt" 2>/dev/null || true

  # Per-pod logs and describe
  while IFS= read -r pod; do
    [[ -z "${pod}" ]] && continue
    kubectl describe pod "${pod}" -n "${ns}" > "${rtdir}/pods/${pod}.describe.txt" 2>/dev/null || true

    while IFS= read -r c; do
      [[ -z "${c}" ]] && continue
      kubectl logs "${pod}" -n "${ns}" -c "${c}" --tail=-1 > "${rtdir}/logs/${pod}__${c}.log" 2>/dev/null || true
      kubectl logs "${pod}" -n "${ns}" -c "${c}" --previous > "${rtdir}/logs/${pod}__${c}.previous.log" 2>/dev/null || true
    done < <(kubectl get pod "${pod}" -n "${ns}" -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}' 2>/dev/null || true)
  done < <(kubectl get pod -n "${ns}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
}

backup_kong_postgres_db() {
  local nsdir="${OUTDIR}/namespaces/${KONG_PG_NS}"
  local dbdir="${OUTDIR}/runtime/${KONG_PG_NS}/db"
  mkdir -p "${dbdir}"

  if ! kubectl get ns "${KONG_PG_NS}" >/dev/null 2>&1; then
    echo "WARNING: namespace ${KONG_PG_NS} not found, skipping Kong Postgres dump"
    return 0
  fi

  local dump_file="${dbdir}/kong.backup.${TS}.dump"
  local meta_file="${dbdir}/kong-postgres-backup-meta.txt"

  cat > "${meta_file}" <<EOF
namespace=${KONG_PG_NS}
host=${KONG_PG_HOST}
database=${KONG_PG_DB}
user=${KONG_PG_USER}
port=${KONG_PG_PORT}
image=${KONG_PG_IMAGE}
dump_file=$(basename "${dump_file}")
created_at=${TS}
format=custom
options=--no-owner --no-acl
EOF

  echo "Backing up Kong Postgres DB to ${dump_file}"

  if kubectl run pg-dump-once -n "${KONG_PG_NS}" --rm -i \
      --image="${KONG_PG_IMAGE}" \
      --restart=Never \
      --env="PGHOST=${KONG_PG_HOST}" \
      --env="PGDATABASE=${KONG_PG_DB}" \
      --env="PGUSER=${KONG_PG_USER}" \
      --env="PGPASSWORD=${KONG_PG_PASSWORD}" \
      --env="PGPORT=${KONG_PG_PORT}" \
      -- /bin/bash -lc 'pg_dump --format=custom --no-owner --no-acl' \
      > "${dump_file}"; then
    echo "Kong Postgres backup completed: ${dump_file}"
  else
    echo "WARNING: Kong Postgres backup failed" | tee -a "${meta_file}"
    rm -f "${dump_file}"
  fi
}

backup_reports() {
  local ns="$1"
  local rpt="${OUTDIR}/reports/${ns}"
  mkdir -p "${rpt}"

  kubectl get deploy,sts,ds,job,cronjob -n "${ns}" \
    -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"="}{.image}{" "}{end}{"\n"}{end}' \
    | sed 's/[[:space:]]*$//' \
    > "${rpt}/images.tsv" || true

  kubectl get deploy,sts,ds,job,cronjob -n "${ns}" \
    -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\tENV\t"}{range .env[*]}{.name}{"="}{.value}{";"}{end}{"\n"}{end}{end}' \
    > "${rpt}/env-inline.tsv" || true

  kubectl get deploy,sts,ds,job,cronjob -n "${ns}" \
    -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\tENVFROM\t"}{range .envFrom[*]}{.configMapRef.name}{" "}{.secretRef.name}{";"}{end}{"\n"}{end}{end}' \
    > "${rpt}/envfrom.tsv" || true

  kubectl get deploy,sts,ds,job,cronjob -n "${ns}" \
    -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\tMOUNTS\t"}{range .volumeMounts[*]}{.name}{":"}{.mountPath}{";"}{end}{"\n"}{end}{end}' \
    > "${rpt}/volume-mounts.tsv" || true

  kubectl get pvc -n "${ns}" -o wide > "${rpt}/pvc.txt" || true
  kubectl get secret -n "${ns}" > "${rpt}/secrets.txt" || true
  kubectl get configmap -n "${ns}" > "${rpt}/configmaps.txt" || true
  kubectl get svc -n "${ns}" -o wide > "${rpt}/services.txt" || true
}

write_restore_script() {
  cat > "${OUTDIR}/restore.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:-$(pwd)}"

for nsdir in "${BACKUP_DIR}"/namespaces/*; do
  [ -d "${nsdir}" ] || continue
  ns="$(basename "${nsdir}")"

  echo "Restoring namespace: ${ns}"

  if [ -f "${nsdir}/restore/00-namespace.yaml" ]; then
    kubectl apply -f "${nsdir}/restore/00-namespace.yaml"
  fi

  for f in \
    secret.yaml \
    configmap.yaml \
    serviceaccount.yaml \
    role.yaml \
    rolebinding.yaml \
    pvc.yaml \
    service.yaml \
    deployment.yaml \
    statefulset.yaml \
    daemonset.yaml \
    ingress.yaml \
    job.yaml \
    cronjob.yaml \
    networkpolicy.yaml \
    poddisruptionbudget.yaml \
    horizontalpodautoscaler.yaml \
    resourcequota.yaml \
    limitrange.yaml
  do
    if [ -f "${nsdir}/restore/${f}" ]; then
      kubectl apply -f "${nsdir}/restore/${f}"
    fi
  done

  # Apply any extra custom resources captured under raw/
  if [ -d "${nsdir}/raw" ]; then
    find "${nsdir}/raw" -type f -name '*.yaml' -print0 | sort -z | xargs -0 -r -n1 kubectl apply -f
  fi
done

echo "Restore apply completed."
EOF
  chmod +x "${OUTDIR}/restore.sh"
}

create_archive() {
  tar -czf "${OUTDIR}.tar.gz" -C "$(dirname "${OUTDIR}")" "$(basename "${OUTDIR}")"
}

main() {
  echo "Creating backup in: ${OUTDIR}"
  backup_cluster_info

  for ns in "${NAMESPACES[@]}"; do
    echo "Backing up namespace: ${ns}"
    kubectl get ns "${ns}" >/dev/null 2>&1 || {
      echo "WARNING: namespace ${ns} not found, skipping"
      continue
    }

    backup_namespace_restore_objects "${ns}"
    backup_runtime_state "${ns}"
    backup_reports "${ns}"
  done

  if printf '%s\n' "${NAMESPACES[@]}" | grep -qx "${KONG_PG_NS}"; then
    backup_kong_postgres_db
  fi

  write_restore_script
  create_archive

  cat <<EOF

Backup completed.

Directory:
  ${OUTDIR}

Archive:
  ${OUTDIR}.tar.gz

Restore:
  cd ${OUTDIR}
  ./restore.sh ${OUTDIR}

IMPORTANT:
- This backup includes manifests, secrets, configmaps, env references, images, services, PVC objects, runtime reports, and a Kong Postgres dump when the kong namespace is included.
- This does NOT include actual PersistentVolume data for other workloads.
EOF
}

main "$@"