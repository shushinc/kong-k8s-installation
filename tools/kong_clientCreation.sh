#!/usr/bin/env bash

# This script automates the creation of a Kong consumer, OAuth2 credentials,
# and a JWT secret based on user inputs.
#
# It requires 'curl' and 'jq' to be installed.

# --- Safety Settings ---
set -euo pipefail

# --- Static Configuration ---
# These values are fixed for this workflow.
REDIRECT_URI="https://oauth.pstmn.io/v1/callback"
TAG="Q1"
JWT_ALGORITHM="HS256"

# !! WARNING: Hardcoding secrets is a security risk.
#    For production, consider generating this secret dynamically
#    or passing it via a secure environment variable.
JWT_SECRET="strongpassword"

# Common curl options
# -s : Silent mode (don't show progress)
# -L : Follow redirects
# -k : Allow insecure/self-signed SSL certs.
#      Remove -k if you have a valid, trusted certificate.
CURL_OPTS=("-s" "-L" "-k")

# --- Dependency Check ---
if ! command -v curl &> /dev/null; then
    echo "ERROR: 'curl' is not installed. Please install it to continue." >&2
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "ERROR: 'jq' is not installed. Please install it to continue." >&2
    echo "       (e.g., 'sudo apt-get install jq' or 'brew install jq')" >&2
    exit 1
fi

# --- Get Dynamic User Input ---

# 1. Get Kong Admin URL details
read -p "Enter the Kong Admin IP or Hostname (e.g., 34.61.21.100): " KONG_ADMIN_IP_OR_HOST
read -p "Enter the Kong Admin Port (e.g., 32441): " KONG_ADMIN_PORT

if [[ -z "$KONG_ADMIN_IP_OR_HOST" || -z "$KONG_ADMIN_PORT" ]]; then
    echo "ERROR: Kong Admin IP/Host and Port cannot be empty." >&2
    exit 1
fi

# Construct the base URL
KONG_ADMIN_URL="https://${KONG_ADMIN_IP_OR_HOST}:${KONG_ADMIN_PORT}"

# 2. Get Customer Name
read -p "Enter the customer name: " CUSTOMER_NAME

if [[ -z "$CUSTOMER_NAME" ]]; then
    echo "ERROR: Customer name cannot be empty." >&2
    exit 1
fi

echo "--------------------------------------------------"
echo "Processing onboarding for: $CUSTOMER_NAME"
echo "Targeting Kong Admin API at: $KONG_ADMIN_URL"
echo "--------------------------------------------------"


# --- Step 1: Create Consumer ---
echo "Step 1: Creating consumer..."
consumer_response=$(curl "${CURL_OPTS[@]}" \
    --location "$KONG_ADMIN_URL/consumers" \
    --header 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "username=$CUSTOMER_NAME" \
    --data-urlencode "custom_id=$CUSTOMER_NAME")

# Validate response and extract ID
if ! echo "$consumer_response" | jq -e '.id' > /dev/null; then
    echo "ERROR: Failed to create consumer." >&2
    echo "Response: $consumer_response" >&2
    exit 1
fi
CONSUMER_ID=$(echo "$consumer_response" | jq -r '.id')
echo "Consumer created successfully (ID: $CONSUMER_ID)."


# --- Step 2: Create ClientID and Secret ---
echo "Step 2: Creating OAuth2 credentials..."
oauth_response=$(curl "${CURL_OPTS[@]}" \
    --location "$KONG_ADMIN_URL/consumers/$CUSTOMER_NAME/oauth2" \
    --header 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "name=$CUSTOMER_NAME" \
    --data-urlencode "redirect_uris[]=$REDIRECT_URI" \
    --data-urlencode "tags[]=$TAG")

# Validate response and extract credentials
if ! echo "$oauth_response" | jq -e '.client_id' > /dev/null; then
    echo "ERROR: Failed to create OAuth2 credentials." >&2
    echo "Response: $oauth_response" >&2
    exit 1
fi

CLIENT_ID=$(echo "$oauth_response" | jq -r '.client_id')
CLIENT_SECRET=$(echo "$oauth_response" | jq -r '.client_secret')
echo "OAuth2 credentials created."


# --- Step 3: Create the JWT secret (Background Job) ---
echo "Step 3: Creating JWT secret..."
jwt_response=$(curl "${CURL_OPTS[@]}" \
    --location "$KONG_ADMIN_URL/consumers/$CUSTOMER_NAME/jwt" \
    --header 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "key=$CUSTOMER_NAME" \
    --data-urlencode "algorithm=$JWT_ALGORITHM" \
    --data-urlencode "secret=$JWT_SECRET" \
    --data-urlencode "tags[]=$TAG")

# Validate response
if ! echo "$jwt_response" | jq -e '.id' > /dev/null; then
    echo "ERROR: Failed to create JWT secret." >&2
    echo "Response: $jwt_response" >&2
    exit 1
fi
echo "JWT secret created successfully."


# --- Final Output ---
printf "\n%s\n" "--------------------------------------------------"
printf "   Client Onboarding Complete for: %s\n" "$CUSTOMER_NAME"
printf "%s\n" "--------------------------------------------------"
printf "   Client ID:     %s\n" "$CLIENT_ID"
printf "   Client Secret: %s\n" "$CLIENT_SECRET"
printf "%s\n" "--------------------------------------------------"

exit 0