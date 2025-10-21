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

After running the script, you will have a ./gcp/key.json file.

Share this key.json file with customer, during deployment
