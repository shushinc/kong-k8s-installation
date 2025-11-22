#!/bin/bash

# ==============================================================================
#  Kong Analytics - New Client Onboarding Script (Interactive)
# ==============================================================================
#
#  This script interactively prompts for the necessary information to create
#  a dedicated BigQuery dataset, a BigQuery table, and a scoped
#  Service Account for a new client.
#
#  Usage:
#  ./onboard_client.sh
#  (No arguments needed)
#
# ==============================================================================

# --- 1. Get GCP Project ID Interactively ---
# Attempt to get the current project from gcloud config as a default
CURRENT_PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

echo "Please enter the GCP Project ID."
read -p "Project ID [default: ${CURRENT_PROJECT_ID}]: " GCP_PROJECT_ID
# If the user just presses enter, use the detected default
GCP_PROJECT_ID=${GCP_PROJECT_ID:-$CURRENT_PROJECT_ID}

if [ -z "$GCP_PROJECT_ID" ]; then
    echo "error: GCP Project ID cannot be empty."
    exit 1
fi

# --- 2. Get Carrier Name Interactively ---
echo ""
echo "Please enter a name for the new client."
echo "Use a short, lowercase, URL-friendly name (e.g., 'shush-demandpartner', 'shush-entriprise')."
read -p "Carrier Name: " CLIENT_NAME_INPUT

if [ -z "$CLIENT_NAME_INPUT" ]; then
    echo "error: Carrier Name cannot be empty."
    exit 1
fi

# --- 2b. Get Kubernetes Namespace ---
echo ""
echo "Please enter the Kubernetes namespace for this client."
read -p "K8s Namespace [default: aggregates]: " K8S_NAMESPACE
K8S_NAMESPACE=${K8S_NAMESPACE:-aggregates}

# --- 2c. Set Pricing File Path (Hardcoded) ---
LOCAL_PRICING_FILE_PATH="config/api_pricing.csv"

# Check if the pricing file actually exists before proceeding
if [ ! -f "$LOCAL_PRICING_FILE_PATH" ]; then
    echo "error: Pricing file not found at $LOCAL_PRICING_FILE_PATH"
    echo "Please make sure the file exists before running this script."
    exit 1
fi

# --- 3. Define and Confirm Resource Names ---
# Format client name to be safe for resource naming
CLIENT_NAME=$(echo "$CLIENT_NAME_INPUT" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g')
BQ_DATASET_ID="${CLIENT_NAME}_kong_analytics"
BQ_TABLE_ID="kong_aggregate"
SA_NAME="${CLIENT_NAME}-kong-aggregator"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
KEY_FILE_PATH="./gcp/key.json"
# Define all our filenames
CONFIG_FILE="aggregator-config-${CLIENT_NAME}.yaml"
DEPLOY_FILE="aggregator-deployment-${CLIENT_NAME}.yaml"
SECRET_FILE="secret-${CLIENT_NAME}.yaml"
PRICING_MAP_FILE="configmap-pricing-${CLIENT_NAME}.yaml"
FINAL_INSTALL_FILE="install-${CLIENT_NAME}.yaml"

# --- Define resource names (these must match the deployment.yaml) ---
SECRET_NAME="${CLIENT_NAME}-gcp-sa-key"
PRICING_CONFIGMAP_NAME="pricing-config-${CLIENT_NAME}"
AGGREGATOR_CONFIGMAP_NAME="aggregator-config-${CLIENT_NAME}"

# Define key paths
KEY_FILE_PATH="./gcp/key.json"
IMAGE_PATH="us-central1-docker.pkg.dev/sherlock-004/ts43/aggregates:v1.0.14-2"

echo "--------------------------------------------------"
echo "The following resources will be created/generated:"
echo "--------------------------------------------------"
echo "Project ID:          $GCP_PROJECT_ID"
echo "K8s Namespace:       $K8S_NAMESPACE"
echo "BigQuery Dataset:    $BQ_DATASET_ID"
echo "BigQuery Table:      $BQ_TABLE_ID"
echo "Service Account:     $SA_NAME"
echo "Docker Image Used:   $IMAGE_PATH"
echo "Key File Name:       $KEY_FILE_PATH"
echo "K8s Config File:     $CONFIG_FILE_PATH"
echo "K8s Deployment File: $DEPLOYMENT_FILE_PATH"
echo "--------------------------------------------------"
read -p "Do you want to proceed? (y/n): " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Operation cancelled."
    exit 1
fi

# --- 4. Configure gcloud CLI ---
gcloud config set project "$GCP_PROJECT_ID"

# --- 5. Create the BigQuery Dataset ---
echo -e "\n[STEP 1/6] Creating BigQuery dataset: $BQ_DATASET_ID..."
if bq mk --dataset --description="Analytics data for client ${CLIENT_NAME}" "${GCP_PROJECT_ID}:${BQ_DATASET_ID}"; then
    echo "Dataset created successfully."
else
    echo "Warning: Dataset might already exist. Continuing..."
fi

# --- 6. Create the BigQuery Table --- 
TABLE_ID="kong_aggregate"
echo -e "\n[STEP 2/6] Creating BigQuery table: ${BQ_DATASET_ID}.${TABLE_ID}..."

# Define the schema from your image
SCHEMA="datatime:TIMESTAMP,carrier_name:STRING,client:STRING,customer_name:STRING,endpoint:STRING,transaction_type:STRING,transaction_type_count:INT64,total_full_rate_billable_transaction:INT64,total_lower_rate_billable_transaction:INT64,total_no_billable_transaction:INT64,avg_latency_full_rate:FLOAT64,avg_latency_lower_rate:FLOAT64,avg_latency_no_billable:FLOAT64,est_revenue:FLOAT64"

if bq mk --table --description="Aggregated Kong analytics data" "${GCP_PROJECT_ID}:${BQ_DATASET_ID}.${TABLE_ID}" $SCHEMA; then
    echo "Table created successfully."
else
    echo "Warning: Table might already exist. Continuing..."
fi

# --- 7. Create the Dedicated Service Account ---  
echo -e "\n[STEP 3/6] Creating Service Account: $SA_NAME..."
if gcloud iam service-accounts create "$SA_NAME" --display-name="Service Account for ${CLIENT_NAME} Kong Analytics"; then
    echo "Service Account created successfully."
else
    echo "Warning: Service Account might already exist. Continuing..."
fi

# ADD THIS BLOCK
echo "Waiting 10 seconds for IAM to propagate..."
sleep 10
# END OF NEW BLOCK

# --- 8. Grant Scoped IAM Permissions ---  
echo -e "\n[STEP 4/6] Granting IAM permissions..."

echo " -> Granting 'BigQuery Job User' role at the project level..."
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/bigquery.jobUser" --condition=None > /dev/null

echo " -> Granting 'BigQuery Data Editor' role on dataset '$BQ_DATASET_ID'..."
bq show --format=prettyjson "${GCP_PROJECT_ID}:${BQ_DATASET_ID}" > dataset_policy.json
if command -v jq &> /dev/null; then
    jq ".access += [{\"role\": \"WRITER\", \"userByEmail\": \"${SA_EMAIL}\"}]" dataset_policy.json > updated_policy.json
else
    # Fallback for systems without jq
    sed -i '$ d' dataset_policy.json
    echo " ,{\"role\": \"WRITER\", \"userByEmail\": \"${SA_EMAIL}\"}]}" >> dataset_policy.json
    mv dataset_policy.json updated_policy.json
fi
bq update --source updated_policy.json "${GCP_PROJECT_ID}:${BQ_DATASET_ID}"
rm dataset_policy.json updated_policy.json
echo "IAM permissions granted successfully."

# --- 9. Create and Download the Service Account Key ---
echo -e "\n[STEP 5/7] Creating and downloading JSON key..."
# Ensure the gcp directory exists
mkdir -p ./gcp
gcloud iam service-accounts keys create "$KEY_FILE_PATH" \
    --iam-account="${SA_EMAIL}"


# --- 10. Generate Kubernetes Configuration ---  
echo -e "\n[STEP 6/7] Generating Kubernetes config file: $CONFIG_FILE"
cat <<EOF > "$CONFIG_FILE"
# This file was auto-generated by the onboarding script
apiVersion: v1
kind: ConfigMap
metadata:
  name: aggregator-config-${CLIENT_NAME}
  namespace: aggregates
data:
  BQ_PROJECT_ID: "${GCP_PROJECT_ID}"
  BQ_DATASET: "${BQ_DATASET_ID}"
  BQ_TABLE: "kong_aggregate"
  GOOGLE_APPLICATION_CREDENTIALS: "/gcp/key.json"
  PRICING_FILE_PATH: "/config/pricing.csv"
  GCP_SERVICE_ACCOUNT_EMAIL: "${SA_EMAIL}"
  CARRIER_NAME: "${CLIENT_NAME}"   
  CARRIER_NAME_RAW: "${CLIENT_NAME_INPUT}"
EOF

echo -e "\n[STEP 6/6] Generating Kubernetes deployment file: deployment-${CLIENT_NAME}.yaml"



# --- 11. Generate Kubernetes Deployment File ---
echo -e "\n[STEP 7/7] Generating Kubernetes deployment file: $DEPLOYMENT_FILE_PATH"
cat <<EOF > "$DEPLOY_FILE"
# Auto-generated for client: ${CLIENT_NAME}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: aggregator-sa-${CLIENT_NAME}
  namespace: aggregates
  annotations:
    iam.gke.io/gcp-service-account: "${SA_EMAIL}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aggregator-${CLIENT_NAME}
  namespace: aggregates
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
            name: aggregator-config-${CLIENT_NAME}
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
          secretName: ${CLIENT_NAME}-gcp-sa-key
      - name: pricing-volume
        configMap:
          name: pricing-config-${CLIENT_NAME}
---
apiVersion: v1
kind: Service
metadata:
  name: log-sink-svc
  namespace: aggregates
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
  namespace: aggregates
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
EOF

# --- 12. Generate Secret YAML from Key File ---
echo -e "\n[STEP 8/10] Generating Secret YAML for ${SECRET_NAME}..."
kubectl create secret generic "${SECRET_NAME}" \
    --from-file=key.json="${KEY_FILE_PATH}" \
    -n "${K8S_NAMESPACE}" \
    --dry-run=client -o yaml > "${SECRET_FILE}"
if [ $? -ne 0 ]; then
    echo "Error: Failed to generate Secret YAML."
    exit 1
fi
echo "Generated ${SECRET_FILE}"

# --- 13. Generate ConfigMap YAML from Pricing File ---
echo -e "\n[STEP 9/10] Generating ConfigMap YAML for ${PRICING_CONFIGMAP_NAME}..."
kubectl create configmap "${PRICING_CONFIGMAP_NAME}" \
    --from-file=pricing.csv="${LOCAL_PRICING_FILE_PATH}" \
    -n "${K8S_NAMESPACE}" \
    --dry-run=client -o yaml > "${PRICING_MAP_FILE}"
if [ $? -ne 0 ]; then
    echo "Error: Failed to generate ConfigMap YAML."
    exit 1
fi
echo "Generated ${PRICING_MAP_FILE}"

# --- 14. Final Packaging ---
echo -e "\n[STEP 10/10] Combining all YAMLs into single install file: ${FINAL_INSTALL_FILE}"

# Use '---' to separate Kubernetes resources in a single file
cat "${CONFIG_FILE}" > "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${DEPLOY_FILE}" >> "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${SECRET_FILE}" >> "${FINAL_INSTALL_FILE}"
echo -e "\n---" >> "${FINAL_INSTALL_FILE}"
cat "${PRICING_MAP_FILE}" >> "${FINAL_INSTALL_FILE}"

# Clean up the temporary individual files
echo "Cleaning up temporary files..."
rm "${CONFIG_FILE}" "${DEPLOY_FILE}" "${SECRET_FILE}" "${PRICING_MAP_FILE}"

# --- Final Output ---
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
