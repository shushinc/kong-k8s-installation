Kong API Analytics: New Client Onboarding Guide

1. Overview

This document outlines the standard procedure for onboarding a new client onto the Kong API analytics platform. The core principle of this process is data isolation and security.

For each new client, we will create:

A dedicated BigQuery Dataset to house all their specific analytics data.

A unique Google Cloud Service Account (SA) that has permission to write data only to that client's dataset.

This ensures that one client's service account cannot access another client's data. This guide provides a complete script to automate the entire setup process.

2. Prerequisites

Before you begin, ensure you have the following:

The gcloud command-line tool installed and authenticated.

The bq command-line tool installed (gcloud components install bq).

Permissions in your Google Cloud project to:

Create BigQuery datasets (bigquery.datasets.create).

Create Service Accounts (iam.serviceAccounts.create).

Set IAM policies on the project and on BigQuery datasets.

3. Onboarding Process & Script

The following script automates all the necessary steps. It is designed to be run for each new client.

Instructions

Copy the script below and save it as onboard_client.sh.

Make the script executable: chmod +x onboard_client.sh.

Run the script with your Project ID and the new Client's Name as arguments:

./onboard_client.sh "sherlock-005" "new-client-name"


Client Name Convention: Use a short, lowercase, URL-friendly name (e.g., shush-demandpartner, shush-enterprise). This name will be used for the dataset and service account.

The Onboarding Script (clientaggregates_onboard.sh)


4. Next Steps

After running the script, you will have a ./gcp/[client-name]-sa-key.json file.

This key is what you will use to configure the Kong aggregator for this specific client. For example, in a Kubernetes environment, you would:

Create a new Kubernetes Secret from this JSON file.
kubectl create secret generic [client-name]-gcp-sa-key \
  --from-file=key.json=.gcp/[client-name]-sa-key.json \
  -n default


  Manually update below values in the config.yaml:
  1. GCP_PROJECT_ID
  2. GCP_SERVICE_ACCOUNT_EMAIL
  3. BIGQUERY_DATASET
  4. SERVICE_ACCOUNT_FILE_PATH


Aftet t

Deploy a new instance of the aggregator, configured to use this new secret.
```bash
kubectl create secret generic elangotest-gcp-sa-key --from-file=key.json=./elangotest-sa-key.json
  ```
