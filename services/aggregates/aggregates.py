import os
import csv
import logging
from flask import Flask, request, jsonify
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import datetime, timedelta
from collections import defaultdict
import threading
import os, json, time


# --- Flask App Initialization ---
app = Flask(__name__)

# --- In-memory storage and lock ---
LOG_BUFFER = []
data_lock = threading.Lock()


# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- Configuration Loading ---
def get_config(env_var, required=True, default=None):
    value = os.getenv(env_var, default)
    if required and value is None:
        logging.error(f"FATAL: Environment variable {env_var} is not set.")
        raise ValueError(f"Missing required environment variable: {env_var}")
    return value
# Global variable for the BQ client
bq_client = None

try:
    GCP_PROJECT_ID = get_config("GCP_PROJECT_ID")
    SERVICE_ACCOUNT_FILE = get_config("SERVICE_ACCOUNT_FILE_PATH")
    PRICING_FILE_PATH = os.getenv("PRICING_FILE_PATH", "/config/pricing.csv")
    BIGQUERY_DATASET = get_config("BIGQUERY_DATASET")
    BIGQUERY_TABLE = get_config("BIGQUERY_TABLE")
    BIGQUERY_TABLE_REF = f"`{GCP_PROJECT_ID}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}`"
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
    bq_client = bigquery.Client(project=GCP_PROJECT_ID, credentials=credentials)
    logging.info("Successfully initialized BigQuery client.")
except Exception as e:
    logging.error(f"Failed to initialize configuration or BigQuery client: {e}")


# --- Pricing Data Loading ---
def load_pricing_data(pricing_file_path):
    pricing_map = {}
    try:
        with open(pricing_file_path, 'r', encoding='utf-8-sig') as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                endpoint = row.get('Endpoint', '').strip()
                price_str = row.get('Price', '0').strip()
                api = row.get('API', '').strip()
                if not endpoint: continue
                try:
                    price = float(price_str)
                except (ValueError, TypeError):
                    price = 0.0
                pricing_map[endpoint] = {'price': price, 'api': api}
    except FileNotFoundError:
        logging.error(f"Pricing file not found at {pricing_file_path}")
    except Exception as e:
        logging.error(f"Error loading pricing data: {e}")
    return pricing_map

pricing_map = load_pricing_data(PRICING_FILE_PATH)

def calculate_hourly_key(timestamp_str):
    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        next_hour = (timestamp + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return next_hour.isoformat()
    except (ValueError, TypeError):
        return None
    
    
def process_and_aggregate_logs():
    global LOG_BUFFER
    if not LOG_BUFFER:
        logging.info("No logs in buffer to process.")
        return

    aggregates = defaultdict(lambda: {
        '200': 0, '4xx': 0, '5xx': 0, 'other': 0,
        'latency_sum': 0, 'request_count': 0, 'est_revenue': 0
    })
    
    with data_lock:
        logs_to_process = list(LOG_BUFFER)
        LOG_BUFFER.clear()

    for log_data in logs_to_process:
        # The log_data from Fluent Bit is the full record, the part we need is inside the 'json' key
        json_part = log_data.get('json', {})
        if not json_part:
            # Handle cases where the 'json' key might be missing from the parser's output
            json_part = log_data
            
        json_part = log_data.get('json', {})
        endpoint = json_part.get('uri')
        status_code = int(json_part.get('status_code', 0))
        timestamp = json_part.get('timestamp')
        latency = float(json_part.get('latency_ms', 0))
        customer_name = json_part.get('customer_name', 'Unknown')
        client = json_part.get('client', 'Unknown')
        carrier_name = json_part.get('carrier_name', 'Unknown')
        method = json_part.get('method', 'Unknown')

        hourly_key = calculate_hourly_key(timestamp)
        pricing_info = pricing_map.get(f"/{endpoint}") or pricing_map.get(endpoint)

        if not hourly_key or not pricing_info:
            continue

        api = pricing_info.get('api', 'Unknown')
        price = pricing_info.get('price', 0.0)
        key_endpoint = f"/{endpoint}" if not endpoint.startswith('/') else endpoint

        key = (hourly_key, customer_name, client, carrier_name, key_endpoint, api, method)
        
        agg = aggregates[key]
        if 200 <= status_code < 300: agg['200'] += 1
        elif 400 <= status_code < 500: agg['4xx'] += 1
        elif 500 <= status_code < 600: agg['5xx'] += 1
        else: agg['other'] += 1
        
        agg['latency_sum'] += latency
        agg['request_count'] += 1
        if 200 <= status_code < 300:
             agg['est_revenue'] += price

    final_aggregates_dict = {str(k): v for k, v in aggregates.items()}
    logging.info(f"PREVIEW of data to be sent to BigQuery:\n{json.dumps(final_aggregates_dict, indent=2)}")
    
    push_to_bigquery(aggregates)
    
    
def push_to_bigquery(aggregates):
    if not bq_client:
        logging.error("BigQuery client is not initialized. Cannot push data.")
        return
    if not aggregates:
        logging.info("No aggregated data to push to BigQuery.")
        return

    # This MERGE statement is the core of the BigQuery operation.
    # It updates a row if it finds a match based on the key columns,
    # otherwise, it inserts a new row.
    merge_query_template = f"""
    MERGE INTO {BIGQUERY_TABLE_REF} AS target
    USING (
        SELECT
            CAST(@datatime AS TIMESTAMP) as datatime,
            @carrier_name AS carrier_name,
            @client AS client,
            @customer_name AS customer_name,
            @endpoint AS endpoint,
            @attribute AS attribute,
            @transaction_type AS transaction_type,
            @transaction_type_count AS transaction_type_count,
            @total_full_rate_billable_transaction AS total_full_rate_billable_transaction,
            @total_lower_rate_billable_transaction AS total_lower_rate_billable_transaction,
            @total_no_billable_transaction AS total_no_billable_transaction,
            @avg_latency AS avg_latency,
            @est_revenue AS est_revenue
    ) AS source
    ON  target.datatime = source.datatime
        AND target.carrier_name = source.carrier_name
        AND target.client = source.client
        AND target.customer_name = source.customer_name
        AND target.endpoint = source.endpoint
        AND target.attribute = source.attribute
        AND target.transaction_type = source.transaction_type
    WHEN MATCHED THEN
        UPDATE SET
            transaction_type_count = target.transaction_type_count + source.transaction_type_count,
            total_full_rate_billable_transaction = target.total_full_rate_billable_transaction + source.total_full_rate_billable_transaction,
            total_lower_rate_billable_transaction = target.total_lower_rate_billable_transaction + source.total_lower_rate_billable_transaction,
            total_no_billable_transaction = target.total_no_billable_transaction + source.total_no_billable_transaction,
            est_revenue = target.est_revenue + source.est_revenue
            -- Note: avg_latency is not updated on match to keep the first recorded average
    WHEN NOT MATCHED THEN
        INSERT (datatime, carrier_name, client, customer_name, endpoint, attribute, transaction_type, transaction_type_count, total_full_rate_billable_transaction, total_lower_rate_billable_transaction, total_no_billable_transaction, avg_latency, est_revenue)
        VALUES (datatime, carrier_name, client, customer_name, endpoint, attribute, transaction_type, transaction_type_count, total_full_rate_billable_transaction, total_lower_rate_billable_transaction, total_no_billable_transaction, avg_latency, est_revenue);
    """

    rows_to_insert = []
    # The key is a tuple: (hourly_key, customer_name, client, carrier_name, endpoint, api, method)
    for key, counts in aggregates.items():
        (hourly_key, customer_name, client_name, carrier_name, endpoint, api, method) = key
        
        avg_latency = counts['latency_sum'] / counts['request_count'] if counts['request_count'] > 0 else 0

        # Create separate records for each transaction type (200s, 4xx, etc.)
        transaction_types = [
            ("Successful", counts['200']),
            ("Client Error", counts['4xx']),
            ("Server Error", counts['5xx']),
            ("Other", counts['other'])
        ]

        for tx_type, tx_count in transaction_types:
            if tx_count > 0:
                # Set billable transaction counts based on the type
                full_rate = tx_count if tx_type == "Successful" else 0
                lower_rate = 0 # Adjust if you have other billable types
                no_bill = tx_count if tx_type != "Successful" else 0

                # Calculate revenue only for successful transactions
                est_revenue = counts['est_revenue'] if tx_type == "Successful" else 0

                query_params = [
                    bigquery.ScalarQueryParameter("datatime", "TIMESTAMP", hourly_key),
                    bigquery.ScalarQueryParameter("carrier_name", "STRING", carrier_name),
                    bigquery.ScalarQueryParameter("client", "STRING", client_name),
                    bigquery.ScalarQueryParameter("customer_name", "STRING", customer_name or None),
                    bigquery.ScalarQueryParameter("endpoint", "STRING", endpoint),
                    bigquery.ScalarQueryParameter("attribute", "STRING", api),
                    bigquery.ScalarQueryParameter("transaction_type", "STRING", tx_type),
                    bigquery.ScalarQueryParameter("transaction_type_count", "INT64", tx_count),
                    bigquery.ScalarQueryParameter("total_full_rate_billable_transaction", "INT64", full_rate),
                    bigquery.ScalarQueryParameter("total_lower_rate_billable_transaction", "INT64", lower_rate),
                    bigquery.ScalarQueryParameter("total_no_billable_transaction", "INT64", no_bill),
                    bigquery.ScalarQueryParameter("avg_latency", "FLOAT64", avg_latency),
                    bigquery.ScalarQueryParameter("est_revenue", "FLOAT64", est_revenue),
                ]

                job_config = bigquery.QueryJobConfig(query_parameters=query_params)
                try:
                    query_job = bq_client.query(merge_query_template, job_config=job_config)
                    query_job.result()  # Wait for the job to complete
                except Exception as e:
                    logging.error(f"BigQuery merge failed for key {key} and type {tx_type}: {e}")
    
    logging.info(f"Successfully processed and attempted to push {len(aggregates)} aggregated records to BigQuery.")

# --- BigQuery Client Initialization ---
def get_bigquery_client():
    if not SERVICE_ACCOUNT_FILE: return None
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/bigquery"]
        )
        return bigquery.Client(project=GCP_PROJECT_ID, credentials=credentials)

    except FileNotFoundError:
        logging.error(f"Service account file not found at: {SERVICE_ACCOUNT_FILE}")
    except Exception as e:
        logging.error(f"Failed to create BigQuery client: {e}")
    return None

BQ_CLIENT = get_bigquery_client()

@app.route('/ingest', methods=['POST'])
def ingest_log():
    # This endpoint should be very fast - just add to buffer and return.
    with data_lock:
        log_data = request.json
        if not isinstance(log_data, list):
            log_data = [log_data]
        LOG_BUFFER.extend(log_data)
    return jsonify(success=True), 200

@app.route('/trigger_aggregation', methods=['POST'])
def trigger_aggregation():
    try:
        process_and_aggregate_logs()
        return jsonify(message="Aggregation pushed to BigQuery."), 200
    except Exception as e:
        logging.error(f"Error during triggered aggregation: {e}", exc_info=True)
        return jsonify(message=f"Error: {e}"), 500
    
# def ingest_log():
#     log_data = request.json
#     if not isinstance(log_data, list):
#         log_data = [log_data]
#     with data_lock:
#         for record in log_data:
#             parsed_data = record.get('json', {})
#             if not parsed_data: continue
#             attribute = parsed_data.get('uri', 'Unknown')
#             client = parsed_data.get('client', 'Unknown')
#             customer_name = parsed_data.get('customer_name', 'Unknown')
#             carrier_name = parsed_data.get('carrier_name', 'Unknown')
#             method = parsed_data.get('method', 'Unknown')
#             status = str(parsed_data.get('status_code', 'other'))
#             latency = float(parsed_data.get('latency_ms', 0))
#             timestamp_str = parsed_data.get('timestamp', '')
#             if not timestamp_str: continue
#             timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
#             next_hour = (timestamp + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
#             hourly_key_str = next_hour.isoformat()
#             pricing_info = PRICING_MAP.get(attribute)
#             if not pricing_info: continue
#             api = pricing_info['api']
#             key = (hourly_key_str, customer_name, client, carrier_name, attribute, api, method)
#             agg_store = LOG_AGGREGATES[key]
#             if status == '200': agg_store['200'] += 1
#             elif status == '404': agg_store['404'] += 1
#             else: agg_store['other'] += 1
#             agg_store['latency_sum'] += latency
#             agg_store['request_count'] += 1
#             agg_store['est_revenue'] += pricing_info['price']
#     return jsonify(success=True), 200

# @app.route('/trigger_aggregation', methods=['POST'])
# def trigger_aggregation():
#     global LOG_AGGREGATES
#     with data_lock:
#         aggregates_to_push, LOG_AGGREGATES = LOG_AGGREGATES, defaultdict(lambda: {'200': 0, '404': 0, 'other': 0, 'latency_sum': 0, 'request_count': 0, 'est_revenue': 0})
#     if not aggregates_to_push:
#         return jsonify(message="No data to aggregate."), 200
#     if not BQ_CLIENT:
#         return jsonify(message="BigQuery client not configured."), 500
#     push_to_bigquery(aggregates_to_push, BQ_CLIENT)
#     return jsonify(message="Aggregation pushed to BigQuery."), 200

# def push_to_bigquery(aggregates, client):
#     merge_query_template = f"""
#     MERGE INTO {BIGQUERY_TABLE_REF} AS target
#     USING (
#         SELECT @datatime AS datatime, @carrier_name AS carrier_name, @client AS client, @customer_name AS customer_name, @endpoint AS endpoint, @attribute AS attribute, @transaction_type AS transaction_type, @transaction_type_count AS transaction_type_count, @total_full_rate_billable_transaction AS total_full_rate_billable_transaction, @total_lower_rate_billable_transaction AS total_lower_rate_billable_transaction, @total_no_billable_transaction AS total_no_billable_transaction, @avg_latency AS avg_latency, @est_revenue AS est_revenue
#     ) AS source ON target.datatime = source.datatime AND target.carrier_name = source.carrier_name AND target.client = source.client AND target.customer_name = source.customer_name AND target.endpoint = source.endpoint AND target.attribute = source.attribute AND target.transaction_type = source.transaction_type
#     WHEN MATCHED THEN UPDATE SET transaction_type_count = source.transaction_type_count, total_full_rate_billable_transaction = source.total_full_rate_billable_transaction, total_lower_rate_billable_transaction = source.total_lower_rate_billable_transaction, total_no_billable_transaction = source.total_no_billable_transaction, avg_latency = source.avg_latency, est_revenue = source.est_revenue
#     WHEN NOT MATCHED THEN INSERT (datatime, carrier_name, client, customer_name, endpoint, attribute, transaction_type, transaction_type_count, total_full_rate_billable_transaction, total_lower_rate_billable_transaction, total_no_billable_transaction, avg_latency, est_revenue) VALUES (source.datatime, source.carrier_name, source.client, source.customer_name, source.endpoint, source.attribute, source.transaction_type, source.transaction_type_count, source.total_full_rate_billable_transaction, source.total_lower_rate_billable_transaction, source.total_no_billable_transaction, source.avg_latency, source.est_revenue);
#     """
#     for (hourly_key, customer_name, client_name, carrier_name, endpoint, api, method), counts in aggregates.items():
#         avg_latency = counts['latency_sum'] / counts['request_count'] if counts['request_count'] > 0 else 0
#         transaction_types = [{"type": "Successful", "count": counts['200']}, {"type": "Unsuccessful Transactions", "count": counts['404']}, {"type": "Other", "count": counts['other']}]
#         for tx_type in transaction_types:
#             if tx_type["count"] > 0:
#                 query_params = [
#                     bigquery.ScalarQueryParameter("datatime", "TIMESTAMP", hourly_key),
#                     bigquery.ScalarQueryParameter("carrier_name", "STRING", carrier_name),
#                     bigquery.ScalarQueryParameter("client", "STRING", client_name),
#                     bigquery.ScalarQueryParameter("customer_name", "STRING", customer_name or None),
#                     bigquery.ScalarQueryParameter("endpoint", "STRING", endpoint),
#                     bigquery.ScalarQueryParameter("attribute", "STRING", api),
#                     bigquery.ScalarQueryParameter("transaction_type", "STRING", tx_type["type"]),
#                     bigquery.ScalarQueryParameter("transaction_type_count", "INT64", tx_type["count"]),
#                     bigquery.ScalarQueryParameter("total_full_rate_billable_transaction", "INT64", counts['200']),
#                     bigquery.ScalarQueryParameter("total_lower_rate_billable_transaction", "INT64", counts['404']),
#                     bigquery.ScalarQueryParameter("total_no_billable_transaction", "INT64", counts['other']),
#                     bigquery.ScalarQueryParameter("avg_latency", "FLOAT64", avg_latency),
#                     bigquery.ScalarQueryParameter("est_revenue", "FLOAT64", counts['est_revenue']),
#                 ]
#                 job_config = bigquery.QueryJobConfig(query_parameters=query_params)
#                 try:
#                     query_job = client.query(merge_query_template, job_config=job_config)
#                     query_job.result()
#                 except Exception as e:
#                     logging.error(f"BigQuery merge failed: {e}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    # Check for a 'FLASK_DEBUG' environment variable. Default to 'False' if not set.
    is_debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')

    # Run the app with debug mode enabled or disabled based on the environment variable.
    app.run(host='0.0.0.0', port=8080, debug=is_debug_mode)