
echo "The following resources will be created/generated:"

echo "--------------------------------------------------"

echo "Project ID:          $GCP_PROJECT_ID"

echo "K8s Namespace:       $K8S_NAMESPACE"

echo "BigQuery Dataset:    $BQ_DATASET_ID"

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



# --- 3. Execute GCP Commands ---

gcloud config set project "$GCP_PROJECT_ID"



echo -e "\n[STEP 1/6] Creating BigQuery dataset..."

bq mk --dataset --description="Analytics data for client ${CLIENT_NAME}" "${GCP_PROJECT_ID}:${BQ_DATASET_ID}" &> /dev/null || echo "⚠️  Warning: Dataset might already exist."



echo -e "\n[STEP 2/6] Creating Service Account..."

gcloud iam service-accounts create "$SA_NAME" --display-name="Service Account for ${CLIENT_NAME} Kong Analytics" &> /dev/null || echo "⚠️  Warning: Service Account might already exist."



echo -e "\n[STEP 3/6] Granting IAM permissions..."

gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser" --condition=None &> /dev/null

bq show --format=prettyjson "${GCP_PROJECT_ID}:${BQ_DATASET_ID}" > ds.json && jq ".access += [{\"role\": \"WRITER\", \"userByEmail\": \"${SA_EMAIL}\"}]" ds.json > updated_ds.json && bq update --source updated_ds.json "${GCP_PROJECT_ID}:${BQ_DATASET_ID}" &> /dev/null && rm ds.json updated_ds.json

echo "IAM permissions granted."



echo -e "\n[STEP 4/6] Creating and downloading JSON key..."

gcloud iam service-accounts keys create "$KEY_FILE_PATH" --iam-account="${SA_EMAIL}"



# --- 4. Generate Kubernetes Config File ---

echo -e "\n[STEP 5/6] Generating Kubernetes config file: $CONFIG_FILE_PATH"

PRICING_DATA=$(sed 's/^/    /' "$PRICING_FILE_PATH")

cat <<EOF > "$CONFIG_FILE_PATH"

# Auto-generated for client: ${CLIENT_NAME}

apiVersion: v1

kind: ConfigMap

metadata:

  name: aggregator-config-${CLIENT_NAME}

  namespace: ${K8S_NAMESPACE}

data:

  GCP_PROJECT_ID: "${GCP_PROJECT_ID}"

  GCP_SERVICE_ACCOUNT_EMAIL: "${SA_EMAIL}"

  BIGQUERY_DATASET: "${BQ_DATASET_ID}"

  BIGQUERY_TABLE: "kong_aggregate"

  SERVICE_ACCOUNT_FILE_PATH: "/gcp/key.json"

  PRICING_FILE_PATH: "/config/pricing.csv"

---

apiVersion: v1

kind: ConfigMap

metadata:

  name: pricing-config-${CLIENT_NAME}

  namespace: ${K8S_NAMESPACE}

data:

  pricing.csv: |

${PRICING_DATA}

EOF



# --- 5. Generate Kubernetes Deployment File ---

echo -e "\n[STEP 6/6] Generating Kubernetes deployment file: $DEPLOYMENT_FILE_PATH"

cat <<EOF > "$DEPLOYMENT_FILE_PATH"

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

          secretName: key

      - name: pricing-volume

        configMap:

          name: pricing-config-${CLIENT_NAME}

---

apiVersion: v1

kind: Service

metadata:

  name: log-sink-${CLIENT_NAME}

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

            - "http://log-sink-${CLIENT_NAME}.${K8S_NAMESPACE}.svc.cluster.local:8080/trigger_aggregation"

          restartPolicy: OnFailure

EOF



# --- 6. Final Output ---

echo "--------------------------------------------------"

echo "Onboarding Complete for Client: $CLIENT_NAME"

echo "--------------------------------------------------"

echo "Service account key saved to: $KEY_FILE_PATH"

echo "Kubernetes config file generated: $CONFIG_FILE_PATH"

echo "Kubernetes deployment file generated: $DEPLOYMENT_FILE_PATH"

echo ""

echo "Next Steps: folow ## Deployment Of aggragtes:"

echo "1.Create the Kubernetes namespace if it doesn't exist: kubectl create ns $K8S_NAMESPACE"

echo "2.Create the Kubernetes secret: kubectl create secret generic key --from-file=key.json=${KEY_FILE_PATH} -n ${K8S_NAMESPACE}"

echo "3.Apply boths generated YAML files: kubectl apply -f ${CONFIG_FILE_PATH} -f ${DEPLOYMENT_FILE_PATH}"

echo "--------------------------------------------------"
