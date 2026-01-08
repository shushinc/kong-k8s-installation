#!/bin/bash

# ==============================================================================
#  Kong Analytics - New Client Onboarding Script (Interactive)
# ==============================================================================
#
#  This script interactively prompts for the necessary information to create
#  a dedicated BigQuery dataset, a BigQuery table, and a scoped
#  Service Account for a new client. It also generates a single Kubernetes
#  install YAML: install-<client>.yaml
#
#  Adds Moriarty defaults + prompts for Moriarty hostname to build(it support only https):
#    DRUPAL_ANALYTICS_URL="https://<hostname>/analytics/node/add"
#    DRUPAL_BASIC_USER="admin"
#    DRUPAL_BASIC_PASS="pass"
#    DRUPAL_TIMEOUT_SECONDS="10"
#
#  Usage:
#    ./clientaggregates_onboard.sh
#
# ==============================================================================

set -euo pipefail

# --- 1. Get GCP Project ID Interactively ---
CURRENT_PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"

echo "Please enter the GCP Project ID."
read -r -p "Project ID [default: ${CURRENT_PROJECT_ID}]: " GCP_PROJECT_ID
GCP_PROJECT_ID="${GCP_PROJECT_ID:-$CURRENT_PROJECT_ID}"

if [ -z "${GCP_PROJECT_ID}" ]; then
  echo "error: GCP Project ID cannot be empty."
  exit 1
fi

# --- 2. Get Carrier/Client Name Interactively ---
echo ""
echo "Please enter a name for the new client."
echo "Use a short, lowercase, URL-friendly name (e.g., 'shush-demandpartner', 'shush-enterprise')."
read -r -p "Carrier Name: " CLIENT_NAME_INPUT

if [ -z "${CLIENT_NAME_INPUT}" ]; then
  echo "error: Carrier Name cannot be empty."
  exit 1
fi

# --- 2b. Get Kubernetes Namespace ---
echo ""
echo "Please enter the Kubernetes namespace for this client."
read -r -p "K8s Namespace [default: aggregates]: " K8S_NAMESPACE
K8S_NAMESPACE="${K8S_NAMESPACE:-aggregates}"

# --- 2c. Get Moriarty/Drupal Hostname (Interactive) ---
echo ""
echo "Please enter the Moriarty / Drupal hostname (NO protocol, NO path)."
echo "Example: 34.171.237.201  or  moriarty.example.com"
read -r -p "Drupal Hostname: " DRUPAL_HOSTNAME

if [ -z "${DRUPAL_HOSTNAME}" ]; then
  echo "error: Drupal hostname cannot be empty."
  exit 1
fi

# --- Moriarty defaults (as requested) ---
DRUPAL_ANALYTICS_URL="https://${DRUPAL_HOSTNAME}/analytics/node/add"
DRUPAL_BASIC_USER="admin"
DRUPAL_BASIC_PASS="pass"
DRUPAL_TIMEOUT_SECONDS="10"

# --- 2d. Set Pricing File Path (Hardcoded) ---
LOCAL_PRICING_FILE_PATH="config/api_pricing.csv"

if [ ! -f "${LOCAL_PRICING_FILE_PATH}" ]; then
  echo "error: Pricing file not found at ${LOCAL_PRICING_FILE_PATH}"
  echo "Please make sure the file exists before running this script."
  exit 1
fi

# --- 3. Define Resource Names ---
CLIENT_NAME="$(echo "${CLIENT_NAME_INPUT}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g')"
BQ_DATASET_ID="${CLIENT_NAME}_kong_analytics"
BQ_TABLE_ID="kong_aggregate"

SA_NAME="${CLIENT_NAME}-kong-aggregator"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# Filenames
CONFIG_FILE="aggregator-config-${CLIENT_NAME}.yaml"
DEPLOY_FILE="aggregator-deployment-${CLIENT_NAME}.yaml"
SECRET_FILE="secret-${CLIENT_NAME}.yaml"
PRICING_MAP_FILE="configmap-pricing-${CLIENT_NAME}.yaml"
FINAL_INSTALL_FILE="install-${CLIENT_NAME}.yaml"

# K8s resource names
SECRET_NAME="${CLIENT_NAME}-gcp-sa-key"
PRICING_CONFIGMAP_NAME="pricing-config-${CLIENT_NAME}"
AGGREGATOR_CONFIGMAP_NAME="aggregator-config-${CLIENT_NAME}"

# Key paths
KEY_FILE_PATH="./gcp/key.json"

# Image
IMAGE_PATH="us-central1-docker.pkg.dev/sherlock-004/ts43/aggregates:v1.0.16.1"

echo ""
echo "--------------------------------------------------"
echo "The following resources will be created/generated:"
echo "--------------------------------------------------"
echo "Project ID:             ${GCP_PROJECT_ID}"
echo "Client Name (raw):      ${CLIENT_NAME_INPUT}"
echo "Client Name (safe):     ${CLIENT_NAME}"
echo "K8s Namespace:          ${K8S_NAMESPACE}"
echo "BigQuery Dataset:       ${BQ_DATASET_ID}"
echo "BigQuery Table:         ${BQ_TABLE_ID}"
echo "Service Account:        ${SA_NAME}"
echo "Service Account Email:  ${SA_EMAIL}"
echo "Docker Image Used:      ${IMAGE_PATH}"
echo "Key File Path:          ${KEY_FILE_PATH}"
echo "Final Install File:     ${FINAL_INSTALL_FILE}"
echo ""
echo "Moriarty Analytics URL:   ${DRUPAL_ANALYTICS_URL}"
echo "Moriarty Basic User:      ${DRUPAL_BASIC_USER}"
echo "Moriarty Timeout Seconds: ${DRUPAL_TIMEOUT_SECONDS}"
echo "--------------------------------------------------"
read -r -p "Do you want to proceed? (y/n): " CONFIRM
if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
  echo "Operation cancelled."
  exit 0
fi

# --- 4. Configure gcloud CLI ---
gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

# --- 5. Create the BigQuery Dataset ---
echo -e "\n[STEP 1/7] Creating BigQuery dataset: ${BQ_DATASET_ID}..."
if bq mk --dataset --description="Analytics data for client ${CLIENT_NAME}" "${GCP_PROJECT_ID}:${BQ_DATASET_ID}"; then
  echo "Dataset created successfully."
else
  echo "Warning: Dataset might already exist. Continuing..."
fi

# --- 6. Create the BigQuery Table ---
echo -e "\n[STEP 2/7] Creating BigQuery table: ${BQ_DATASET_ID}.${BQ_TABLE_ID}..."

SCHEMA="datatime:TIMESTAMP,carrier_name:STRING,client:STRING,customer_name:STRING,endpoint:STRING,pricing_type:STRING,transaction_type:STRING,transaction_type_count:INT64,total_full_rate_billable_transaction:INT64,total_lower_rate_billable_transaction:INT64,total_no_billable_transaction:INT64,avg_latency_full_rate:FLOAT64,avg_latency_lower_rate:FLOAT64,avg_latency_no_billable:FLOAT64,est_revenue:FLOAT64"

if bq mk --table --description="Aggregated Kong analytics data" "${GCP_PROJECT_ID}:${BQ_DATASET_ID}.${BQ_TABLE_ID}" ${SCHEMA}; then
  echo "Table created successfully."
else
  echo "Warning: Table might already exist. Continuing..."
fi

# --- 7. Create the Dedicated Service Account ---
echo -e "\n[STEP 3/7] Creating Service Account: ${SA_NAME}..."
if gcloud iam service-accounts create "${SA_NAME}" --display-name="Service Account for ${CLIENT_NAME} Kong Analytics"; then
  echo "Service Account created successfully."
else
  echo "Warning: Service Account might already exist. Continuing..."
fi

echo "Waiting 10 seconds for IAM to propagate..."
sleep 10

# --- 8. Grant Scoped IAM Permissions ---
echo -e "\n[STEP 4/7] Granting IAM permissions..."

echo " -> Granting 'BigQuery Job User' role at the project level..."
gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser" --condition=None >/dev/null

echo " -> Granting 'BigQuery Data Editor' role on dataset '${BQ_DATASET_ID}'..."
bq show --format=prettyjson "${GCP_PROJECT_ID}:${BQ_DATASET_ID}" > dataset_policy.json
if command -v jq >/dev/null 2>&1; then
  jq ".access += [{\"role\": \"WRITER\", \"userByEmail\": \"${SA_EMAIL}\"}]" dataset_policy.json > updated_policy.json
else
  # fallback without jq
  sed -i '$ d' dataset_policy.json
  echo " ,{\"role\": \"WRITER\", \"userByEmail\": \"${SA_EMAIL}\"}]}" >> dataset_policy.json
  mv dataset_policy.json updated_policy.json
fi
bq update --source updated_policy.json "${GCP_PROJECT_ID}:${BQ_DATASET_ID}"
rm -f dataset_policy.json updated_policy.json
echo "IAM permissions granted successfully."

# --- 9. Create and Download the Service Account Key ---
echo -e "\n[STEP 5/7] Creating and downloading JSON key..."
mkdir -p ./gcp
gcloud iam service-accounts keys create "${KEY_FILE_PATH}" \
  --iam-account="${SA_EMAIL}"

# --- 10. Generate Kubernetes ConfigMap (aggregator-config-<client>) ---
echo -e "\n[STEP 6/7] Generating Kubernetes config file: ${CONFIG_FILE}"
cat <<EOF > "${CONFIG_FILE}"
# This file was auto-generated by the onboarding script
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${AGGREGATOR_CONFIGMAP_NAME}
  namespace: ${K8S_NAMESPACE}
data:
  BQ_PROJECT_ID: "${GCP_PROJECT_ID}"
  BQ_DATASET: "${BQ_DATASET_ID}"
  BQ_TABLE: "${BQ_TABLE_ID}"
  GOOGLE_APPLICATION_CREDENTIALS: "/gcp/key.json"
  PRICING_FILE_PATH: "/config/pricing.csv"
  GCP_SERVICE_ACCOUNT_EMAIL: "${SA_EMAIL}"
  CARRIER_NAME: "${CLIENT_NAME}"
  CARRIER_NAME_RAW: "${CLIENT_NAME_INPUT}"

  # --- moriarty client lookup for pricing-type ---
  KONG_ADMIN_URL: "http://kong-kong-admin.kong.svc.cluster.local:8001"
  DEFAULT_PRICING_TYPE: "international"
  CLIENT_PRICING_CACHE_TTL: "300"
  REDIS_HOST: "ts43-redis.kong.svc.cluster.local"
  REDIS_PORT: "6379"

  # --- Moriarty push settings ---
  DRUPAL_ANALYTICS_URL: "${DRUPAL_ANALYTICS_URL}"
  DRUPAL_BASIC_USER: "${DRUPAL_BASIC_USER}"
  DRUPAL_BASIC_PASS: "${DRUPAL_BASIC_PASS}"
  DRUPAL_TIMEOUT_SECONDS: "${DRUPAL_TIMEOUT_SECONDS}"
EOF

# --- 11. Generate Kubernetes Deployment/CronJob/RBAC YAML ---
echo -e "\n[STEP 7/7] Generating Kubernetes deployment file: ${DEPLOY_FILE}"
cat <<EOF > "${DEPLOY_FILE}"
# Auto-generated for client: ${CLIENT_NAME}

apiVersion: v1
kind: ServiceAccount
metadata:
  name: aggregator-sa-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
  annotations:
    iam.gke.io/gcp-service-account: "${SA_EMAIL}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aggregator-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aggregator-${CLIENT_NAME}
  template:
    metadata:
      labels:
        app: aggregator-${CLIENT_NAME}
    spec:
      serviceAccountName: aggregator-sa-${CLIENT_NAME}
      containers:
      - name: aggregator
        image: "${IMAGE_PATH}"
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef:
            name: ${AGGREGATOR_CONFIGMAP_NAME}
        env:
        - name: PRICING_CONFIGMAP_NAME
          value: "${PRICING_CONFIGMAP_NAME}"
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: REDIS_HOST
          valueFrom:
            configMapKeyRef:
              name: ${AGGREGATOR_CONFIGMAP_NAME}
              key: REDIS_HOST
        - name: REDIS_PORT
          valueFrom:
            configMapKeyRef:
              name: ${AGGREGATOR_CONFIGMAP_NAME}
              key: REDIS_PORT
        - name: REDIS_PASSWORD
          valueFrom:
            secretKeyRef:
              name: redis-auth
              key: password
        volumeMounts:
        - name: gcp-key-volume
          mountPath: "/gcp"
          readOnly: true
        - name: pricing-volume
          mountPath: "/config"
          readOnly: true
      volumes:
      - name: gcp-key-volume
        secret:
          secretName: ${SECRET_NAME}
      - name: pricing-volume
        configMap:
          name: ${PRICING_CONFIGMAP_NAME}
---
apiVersion: v1
kind: Service
metadata:
  name: log-sink-svc
  namespace: ${K8S_NAMESPACE}
spec:
  selector:
    app: aggregator-${CLIENT_NAME}
  ports:
  - protocol: TCP
    port: 8080
    targetPort: 8080
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: bq-aggregator-trigger-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
spec:
  schedule: "0 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: trigger-curl
            image: curlimages/curl:7.82.0
            args:
            - "-X"
            - "POST"
            - "http://log-sink-svc.${K8S_NAMESPACE}.svc.cluster.local:8080/trigger_aggregation"
          restartPolicy: OnFailure
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: aggregator-configmap-editor-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  resourceNames: ["${PRICING_CONFIGMAP_NAME}"]
  verbs: ["get", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: aggregator-configmap-editor-binding-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
subjects:
- kind: ServiceAccount
  name: aggregator-sa-${CLIENT_NAME}
  namespace: ${K8S_NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: aggregator-configmap-editor-${CLIENT_NAME}
EOF

# --- 12. Generate Secret YAML from Key File ---
echo -e "\n[STEP 8/10] Generating Secret YAML for ${SECRET_NAME}..."
kubectl create secret generic "${SECRET_NAME}" \
  --from-file=key.json="${KEY_FILE_PATH}" \
  -n "${K8S_NAMESPACE}" \
  --dry-run=client -o yaml > "${SECRET_FILE}"
echo "Generated ${SECRET_FILE}"

# --- 13. Generate ConfigMap YAML from Pricing File ---
echo -e "\n[STEP 9/10] Generating ConfigMap YAML for ${PRICING_CONFIGMAP_NAME}..."
kubectl create configmap "${PRICING_CONFIGMAP_NAME}" \
  --from-file=pricing.csv="${LOCAL_PRICING_FILE_PATH}" \
  -n "${K8S_NAMESPACE}" \
  --dry-run=client -o yaml > "${PRICING_MAP_FILE}"
echo "Generated ${PRICING_MAP_FILE}"

# --- 14. Final Packaging ---
echo -e "\n[STEP 10/10] Combining all YAMLs into single install file: ${FINAL_INSTALL_FILE}"

cat "${CONFIG_FILE}" > "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${DEPLOY_FILE}" >> "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${SECRET_FILE}" >> "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${PRICING_MAP_FILE}" >> "${FINAL_INSTALL_FILE}"

echo "Cleaning up temporary files..."
rm -f "${CONFIG_FILE}" "${DEPLOY_FILE}" "${SECRET_FILE}" "${PRICING_MAP_FILE}"

echo ""
echo "=================================================="
echo "Onboarding Complete for: ${CLIENT_NAME}"
echo "=================================================="
echo "A single installation file has been created:"
echo " -> ${FINAL_INSTALL_FILE}"
echo ""
echo "Send this file to your client."
echo "The client's only instruction is to run:"
echo "   kubectl apply -f ${FINAL_INSTALL_FILE}"
echo ""
