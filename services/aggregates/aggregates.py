
import os
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone

from flask import Flask, request, jsonify

# Optional BigQuery imports — only used if env vars are provided
try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    bigquery = None
    service_account = None

# -------------- Config --------------
# BigQuery config via environment variables
BQ_PROJECT_ID = os.getenv("BQ_PROJECT_ID")              # e.g. "sherlock-004"
BQ_DATASET = os.getenv("BQ_DATASET")                    # e.g. "elangotest_kong_analytics"
BQ_TABLE = os.getenv("BQ_TABLE")                        # e.g. "kong_hourly_aggregates"
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")  # path to SA key JSON

# Flask app
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------- In-memory store --------------
# Keyed by (hour_ts_iso, client, api_path, carrier_name, customer_name)
# Values contain counters/sums to compute aggregates on demand.
_aggr = defaultdict(lambda: {
    "sum_latency": 0.0,
    "count": 0,
    "200": 0,
    "404": 0,
    "other": 0
})
_lock = threading.Lock()


# -------------- Helpers --------------
def _parse_hour_bucket(ts_str: str | None, fallback_str: str | None) -> str:
    """
    Parse the hour bucket as ISO 8601 string (UTC) like '2025-11-03T10:00:00Z'.
    Priority:
      1) 'datatime' coming as 'YYYY-MM-DD HH:00:00' (assumed UTC)
      2) 'timestamp' ISO (e.g., '2025-11-03T10:56:16Z') floored to hour
    """
    dt = None
    if ts_str:
        # Expecting 'YYYY-MM-DD HH:MM:SS'
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    if dt is None and fallback_str:
        try:
            # Accepts '...Z' or offset; normalize to UTC
            # Example: '2025-11-03T10:56:16Z'
            ts = fallback_str.rstrip("Z")
            if "+" in ts or "-" in ts[10:]:
                dt = datetime.fromisoformat(fallback_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.utcnow().replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    dt_floor = dt.replace(minute=0, second=0, microsecond=0)
    return dt_floor.isoformat().replace("+00:00", "Z")


def _status_bucket(code: int | str) -> str:
    try:
        c = int(code)
    except Exception:
        return "other"
    if c == 200:
        return "200"
    if c == 404:
        return "404"
    return "other"


def _get_bq_client():
    """Return a BigQuery client if credentials and config are present; else None."""
    if not (BQ_PROJECT_ID and BQ_DATASET and BQ_TABLE and SERVICE_ACCOUNT_FILE and bigquery and service_account):
        logging.warning("BigQuery client not configured; aggregation will only be kept in-memory.")
        return None
    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
        return bigquery.Client(project=BQ_PROJECT_ID, credentials=creds)
    except Exception as e:
        logging.error(f"Failed to create BigQuery client: {e}")
        return None


# -------------- API --------------
@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Accepts either a single record or a list of records.
    Each record is expected to have a 'json' object like:
      {
        "client": "...",
        "api_path": "/camara/authorizer",
        "carrier_name": "Unknown",
        "customer_name": "Unknown",
        "status_code": 200,
        "latency_ms": 83.0,
        "datatime": "2025-11-03 10:00:00",
        "timestamp": "2025-11-03T10:56:16Z"
      }
    If 'api_path' is missing, we fallback to 'api_path_with_query' (without query part) or derive from 'uri'.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "invalid JSON"}), 400
    records = payload if isinstance(payload, list) else [payload]

    added = 0
    with _lock:
        for rec in records:
            data = rec.get("json") if isinstance(rec, dict) else None
            if not isinstance(data, dict):
                # support direct JSON without wrapper
                data = rec if isinstance(rec, dict) else None
            if not data:
                continue

            client = (data.get("client") or "Unknown").strip() or "Unknown"
            carrier_name = (data.get("carrier_name") or "Unknown").strip() or "Unknown"
            customer_name = (data.get("customer_name") or "Unknown").strip() or "Unknown"

            # api_path detection
            api_path = data.get("api_path")
            if not api_path:
                api_path = data.get("api_path_with_query")
                if api_path and "?" in api_path:
                    api_path = api_path.split("?", 1)[0]
            if not api_path:
                # fall back to "uri" (less ideal, but present in your log)
                api_path = data.get("uri") or "/unknown"

            # time bucketing
            hour_iso = _parse_hour_bucket(data.get("datatime"), data.get("timestamp"))

            # status + latency
            status_bucket = _status_bucket(data.get("status_code"))
            try:
                latency = float(data.get("latency_ms", 0.0))  # milliseconds
            except Exception:
                latency = 0.0

            key = (hour_iso, client, api_path, carrier_name, customer_name)
            bucket = _aggr[key]
            bucket["sum_latency"] += latency
            bucket["count"] += 1
            bucket[status_bucket] += 1
            added += 1

    return jsonify({"ingested": added}), 200


@app.route("/debug/buffer", methods=["GET"])
def debug_buffer():
    """Return current in-memory aggregate state."""
    with _lock:
        dump = []
        for (hour_iso, client, api_path, carrier_name, customer_name), v in _aggr.items():
            avg_latency = (v["sum_latency"] / v["count"]) if v["count"] else 0.0
            dump.append({
                "datatime": hour_iso,
                "client": client,
                "api_path": api_path,
                "carrier_name": carrier_name,
                "customer_name": customer_name,
                "total_full_rate_billable_transaction": v["200"],
                "total_lower_rate_billable_transaction": v["404"],
                "total_no_billable_transaction": v["other"],
                "avg_latency": avg_latency,
                # As requested: est_revenue = average latency of the segment
                "est_revenue": avg_latency
            })
    return jsonify({"buffer_content": dump}), 200


@app.route("/trigger_aggregation", methods=["POST"])
def trigger_aggregation():
    """
    Computes aggregates and (if configured) inserts them into BigQuery.
    For each unique (hour, client, api_path, carrier_name, customer_name) we emit
    THREE rows (transaction_type = Successful/Unsuccessful Transactions/Other).
    """
    with _lock:
        # snapshot current state and then clear so we don't double-insert
        snapshot = dict(_aggr)
        _aggr.clear()

    rows = []
    for (hour_iso, client, api_path, carrier_name, customer_name), v in snapshot.items():
        count = max(v["count"], 1)  # guard against div-by-zero
        avg_latency = v["sum_latency"] / count
        total_200 = v["200"]
        total_404 = v["404"]
        total_other = v["other"]

        triplets = [
            ("Successful", total_200),
            ("Unsuccessful Transactions", total_404),
            ("Other", total_other)
        ]

        for tx_type, tx_count in triplets:
            if tx_count <= 0:
                continue
            rows.append({
                "datatime": hour_iso,  # TIMESTAMP
                "carrier_name": carrier_name,
                "client": client,
                "customer_name": customer_name,
                "endpoint": api_path,
                "transaction_type": tx_type,
                "transaction_type_count": tx_count,
                "total_full_rate_billable_transaction": total_200,
                "total_lower_rate_billable_transaction": total_404,
                "total_no_billable_transaction": total_other,
                "avg_latency": avg_latency,
                # As requested: est_revenue = average latency of the segment
                "est_revenue": avg_latency
            })

    # If BQ is not configured, just return what we'd insert.
    client_bq = _get_bq_client()
    if client_bq is None or not rows:
        return jsonify({"to_insert": rows, "inserted": 0}), 200

    # Insert into BigQuery
    table_id = f"{BQ_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
    try:
        # Convert datatime string to RFC3339 for BQ insert if needed
        for r in rows:
            # BigQuery accepts RFC3339; our hour_iso is already '...Z'
            pass
        errors = client_bq.insert_rows_json(table_id, rows)
        if errors:
            logging.error(f"BigQuery insert errors: {errors}")
            return jsonify({"inserted": 0, "errors": errors}), 500
        return jsonify({"inserted": len(rows)}), 200
    except Exception as e:
        logging.error(f"BigQuery insertion failed: {e}")
        return jsonify({"inserted": 0, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Run Flask app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

