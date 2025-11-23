import os
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
import csv
import io
import re
import requests

from flask import Flask, request, jsonify

# Optional BigQuery imports — only used if env vars are provided
try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    bigquery = None
    service_account = None

# --- Config -----------
# BigQuery config via environment variables
BQ_PROJECT_ID = os.getenv("BQ_PROJECT_ID")              
BQ_DATASET = os.getenv("BQ_DATASET")                   
BQ_TABLE = os.getenv("BQ_TABLE")                       
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")  
CARRIER_NAME_OVERRIDE = os.getenv("CARRIER_NAME") or os.getenv("CARRIER_NAME_RAW")
PRICING_FILE_PATH = os.getenv("PRICING_FILE_PATH", "/config/pricing.csv")
_pricing_lock = threading.Lock()
_pricing_table = {}
_pricing_loaded = False


# Flask app
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ----- In-memory store -----
_aggr = defaultdict(lambda: {
    "count": 0,
    "200": {"count": 0, "latency_sum": 0.0},
    "404": {"count": 0, "latency_sum": 0.0},
    "other": {"count": 0, "latency_sum": 0.0}
})
_lock = threading.Lock()


def _load_pricing_locked() -> None:
    import os

    global _pricing_table, _pricing_loaded

    path = PRICING_FILE_PATH
    table = {}

    if not path or not os.path.exists(path):
        logging.warning(
            path,
        )
        _pricing_table = {}
        _pricing_loaded = True
        return

    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                endpoint = (row.get("Endpoint") or "").strip()
                price_str = (row.get("Price") or "").strip()
                available = (row.get("Available") or "").strip().upper() == "TRUE"
                enabled = (row.get("Enabled") or "").strip().upper() == "TRUE"

                # Skip rows without endpoint or price, or not available/enabled
                if not endpoint or not price_str or not (available and enabled):
                    continue

                try:
                    price = float(price_str)
                except ValueError:
                    continue

                # normalize keys
                key = endpoint.split("?", 1)[0].rstrip("/") or "/"
                table[key] = price

        _pricing_table = table
        _pricing_loaded = True
        logging.info(
            "Loaded %d pricing rows from %s",
            len(_pricing_table),
            path,
        )
    except Exception as e:
        logging.error("Failed to load pricing file '%s': %s", path, e)
        _pricing_table = {}
        _pricing_loaded = True
        
def _price_for_endpoint(endpoint: str) -> float:
    """
    Look up the unit price for a given endpoint path.
    """
    global _pricing_loaded

    if not endpoint:
        return 0.0

    # Normalize: strip query and trailing slash
    key = endpoint.split("?", 1)[0].rstrip("/") or "/"

    # Lazy-load pricing if needed
    if not _pricing_loaded:
        with _pricing_lock:
            if not _pricing_loaded:
                _load_pricing_locked()

    # Get the stored value (could be float, str, or dict)
    val = _pricing_table.get(key, 0.0)

    # If newer logic ever stores dicts like {"price": 4.0026, "currency": "USD"}
    if isinstance(val, dict):
        val = (
            val.get("price")
            or val.get("Price")
            or 0.0
        )

    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0

# -------- Helpers Func------
def _parse_hour_bucket(ts_str: str | None, fallback_str: str | None) -> str:
    dt = None
    if ts_str:
        # Expecting 'YYYY-MM-DD HH:MM:SS' need to match wt
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    if dt is None and fallback_str:
        try:
            # Accepts '...Z' or offset; normalize to UTC
            # Example: '2025-11-03T10:56:16Z' ( normaliza the time in carrrier server)
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
    
    if not bigquery or not service_account:
        logging.error("BigQuery client failed: Google Cloud libraries not imported correctly.")
        return None

    config_ok = True
    if not BQ_PROJECT_ID:
        logging.error("BigQuery client failed: BQ_PROJECT_ID is not set. (Check 'GCP_PROJECT_ID' env var in your ConfigMap)")
        config_ok = False
    if not BQ_DATASET:
        logging.error("BigQuery client failed: BQ_DATASET is not set. (Check 'BIGQUERY_DATASET' env var in your ConfigMap)")
        config_ok = False
    if not BQ_TABLE:
        logging.error("BigQuery client failed: BQ_TABLE is not set. (Check 'BIGQUERY_TABLE' env var in your ConfigMap)")
        config_ok = False
    if not SERVICE_ACCOUNT_FILE:
        logging.error("BigQuery client failed: SERVICE_ACCOUNT_FILE is not set. (Check 'SERVICE_ACCOUNT_FILE_PATH' env var in your ConfigMap)")
        config_ok = False

    if not config_ok:
        logging.warning("BigQuery client not configured; aggregation will only be kept in-memory.")
        return None

    try:
        logging.info(f"Attempting to create BigQuery client for project '{BQ_PROJECT_ID}' with key '{SERVICE_ACCOUNT_FILE}'...")
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
        client = bigquery.Client(project=BQ_PROJECT_ID, credentials=creds)
        logging.info("Successfully initialized BigQuery client.")
        return client
    except Exception as e:
        logging.error(f"Failed to create BigQuery client from service account file: {e}")
        logging.error("This is likely an IAM PERMISSION error (check 'BigQuery Data Editor'/'BigQuery Job User' roles) or 'file not found' (check Secret mount).")
        return None


def _update_k8s_configmap(csv_text: str) -> None:
    """
    Update the pricing ConfigMap so new pricing survives pod restarts.
    """
    cm_name = os.getenv("PRICING_CONFIGMAP_NAME")
    namespace = os.getenv("POD_NAMESPACE", "aggregates")

    if not cm_name:
        logging.info("PRICING_CONFIGMAP_NAME not set; skipping ConfigMap update")
        return

    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            token = f.read().strip()
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")

        url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/configmaps/{cm_name}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
        }

        payload = {
            "data": {
                "pricing.csv": csv_text
            }
        }

        resp = requests.patch(url, headers=headers, json=payload, verify=ca_path)
        if resp.status_code not in (200, 201):
            logging.error(
                "Failed to patch ConfigMap %s (%s): %s",
                cm_name, resp.status_code, resp.text
            )
        else:
            logging.info("Successfully updated ConfigMap %s", cm_name)
    except Exception as e:
        logging.error("Error updating ConfigMap %s: %s", cm_name, e)


def _sanitize_bq_field(name: str) -> str:
    """
    Convert CSV header to a BigQuery-safe column name:
    """
    if name is None:
        name = ""
    name = name.strip()
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if not name:
        name = "field"
    # Column name cannot start with digit
    if name[0].isdigit():
        name = "_" + name
    return name


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
            bucket["count"] += 1
            b = bucket[status_bucket]
            b["count"] += 1
            b["latency_sum"] += latency

    return jsonify({"ingested": added}), 200


@app.route("/debug/buffer", methods=["GET"])
def debug_buffer():
    """Return current in-memory aggregate state."""
    with _lock:
        dump = []
        for (hour_iso, client, api_path, carrier_name, customer_name), v in _aggr.items():
            final_carrier_name = CARRIER_NAME_OVERRIDE if CARRIER_NAME_OVERRIDE else carrier_name
            # totals per status
            total_200 = int(v["200"]["count"])
            total_404 = int(v["404"]["count"])
            total_other = int(v["other"]["count"])

            # per-status avg latencies
            avg_200 = (v["200"]["latency_sum"] / total_200) if total_200 else 0.0
            avg_404 = (v["404"]["latency_sum"] / total_404) if total_404 else 0.0
            avg_other = (v["other"]["latency_sum"] / total_other) if total_other else 0.0

            # unit price for each  endpoint (per successful transaction)
            unit_price = _price_for_endpoint(api_path)
        
            # emit one row per non-zero segment
            segments = [
                ("Successful", total_200, avg_200),
                ("Unsuccessful Transactions", total_404, avg_404),
                ("Other", total_other, avg_other),
            ]
        
            for tx_type, tx_count, tx_avg in segments:
                if tx_count <= 0:
                    continue
                # Revenue calculation based on tx_type
                if tx_type == "Successful":
                    # Full price
                    est_revenue = tx_count * unit_price
                elif tx_type == "Unsuccessful Transactions":
                    # Half price
                    est_revenue = tx_count * (unit_price / 2)
                else:
                    # Other = no revenue
                    est_revenue = 0.0
                    
                dump.append({
                    "datatime": hour_iso,
                    "client": client,
                    "api_path": api_path,
                    "carrier_name": final_carrier_name,
                    "customer_name": customer_name,

                    # counts per status
                    "total_full_rate_billable_transaction": total_200,
                    "total_lower_rate_billable_transaction": total_404,
                    "total_no_billable_transaction": total_other,

                    # averages per status
                    "avg_latency_full_rate": avg_200,
                    "avg_latency_lower_rate": avg_404,
                    "avg_latency_no_billable": avg_other,

                    #  est_revenue is calculated based on segments
                    "est_revenue": est_revenue
            })
    return jsonify({"buffer_content": dump}), 200



@app.route("/trigger_aggregation", methods=["POST"])
def trigger_aggregation():

    with _lock:
        # snapshot current state and then clear so we don't double-insert
        snapshot = dict(_aggr)
        _aggr.clear()

    rows = []
    for (hour_iso, client, api_path, carrier_name, customer_name), v in snapshot.items():
        final_carrier_name = CARRIER_NAME_OVERRIDE if CARRIER_NAME_OVERRIDE else carrier_name
        # counts per status
        total_200 = int(v["200"]["count"])
        total_404 = int(v["404"]["count"])
        total_other = int(v["other"]["count"])

        # unit price for each endpoint (per successful transaction) #check with lee on all endpoint matches
        unit_price = _price_for_endpoint(api_path)
    
        # per-status avg latencies
        avg_200 = (v["200"]["latency_sum"] / total_200) if total_200 else 0.0
        avg_404 = (v["404"]["latency_sum"] / total_404) if total_404 else 0.0
        avg_other = (v["other"]["latency_sum"] / total_other) if total_other else 0.0

        # emit one row per non-zero segment
        segments = [
            ("Successful", total_200, avg_200),
            ("Unsuccessful Transactions", total_404, avg_404),
            ("Other", total_other, avg_other),
        ]

        for tx_type, tx_count, tx_avg in segments:
            if tx_count <= 0:
                continue
            # Revenue calculation based on tx_type
            if tx_type == "Successful":
                # Full price
                est_revenue = tx_count * unit_price
            elif tx_type == "Unsuccessful Transactions":
                # Half price
                est_revenue = tx_count * (unit_price / 2)
            else:
                # Other = no revenue
                est_revenue = 0.0
            
            rows.append({
                "datatime": hour_iso,                   
                "carrier_name": final_carrier_name,
                "client": client,
                "customer_name": customer_name,
                "endpoint": api_path,

                # row segment
                "transaction_type": tx_type,
                "transaction_type_count": tx_count,

                # totals for the whole (client,endpoint,carrier,customer,hour) combo
                "total_full_rate_billable_transaction": total_200,
                "total_lower_rate_billable_transaction": total_404,
                "total_no_billable_transaction": total_other,

                # per-status averages exposed as separate columns
                "avg_latency_full_rate": avg_200,
                "avg_latency_lower_rate": avg_404,
                "avg_latency_no_billable": avg_other,

                # as discussed: est_revenue = average latency of the *segment*
                "est_revenue": est_revenue
            })

    # Insert into BigQuery if configured; otherwise return what would be inserted.
    client_bq = _get_bq_client()
    if client_bq is None or not rows:
        return jsonify({"to_insert": rows, "inserted": 0}), 200

    table_id = f"{BQ_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
    try:
        errors = client_bq.insert_rows_json(table_id, rows)
        if errors:
            logging.error(f"BigQuery insert errors: {errors}")
            return jsonify({"inserted": 0, "errors": errors}), 500
        return jsonify({"inserted": len(rows)}), 200
    except Exception as e:
        logging.error(f"BigQuery insertion failed: {e}")
        return jsonify({"inserted": 0, "error": str(e)}), 500

@app.route("/upload_pricing", methods=["POST"])
def upload_pricing():
    """
    Upload a new pricing.csv, reload it in memory, and push it to BigQuery
    into a dated table pricing_YYYYMMDD.

    Supports:
      - multipart/form-data: file field named 'file'
      - JSON: { "csv": "<full csv text>" }
    """
    global _pricing_loaded, _pricing_table

    # 1) Get CSV text from multipart or JSON
    csv_text = None

    # multipart/form-data
    uploaded_file = request.files.get("file")
    if uploaded_file:
        data = uploaded_file.read()
        if not data:
            return jsonify({"error": "uploaded file is empty"}), 400
        try:
            csv_text = data.decode("utf-8")
        except UnicodeDecodeError:
            csv_text = data.decode("latin-1")

    # JSON { "csv": "..." }
    if csv_text is None:
        payload = request.get_json(silent=True) or {}
        csv_text = payload.get("csv")
        if not csv_text:
            return jsonify({
                "error": "no pricing data provided; upload as multipart 'file' or JSON field 'csv'"
            }), 400

    # 2) Parse CSV and rebuild in-memory pricing table (no file write needed)
    table = {}
    reader = csv.DictReader(io.StringIO(csv_text))

    for row in reader:
        endpoint = (row.get("Endpoint") or "").strip()
        price_str = (row.get("Price") or "").strip()
        available = (row.get("Available") or "").strip().upper() == "TRUE"
        enabled = (row.get("Enabled") or "").strip().upper() == "TRUE"

        # Skip rows without endpoint or price, or not available/enabled
        if not endpoint or not price_str or not (available and enabled):
            continue

        try:
            price = float(price_str)
        except Exception:
            logging.warning(f"Invalid price '{price_str}' for endpoint '{endpoint}', skipping")
            continue

        table[endpoint] = {
            "price": price,
            "available": available,
            "enabled": enabled,
        }

    with _pricing_lock:
        _pricing_table = table
        _pricing_loaded = True
        rows_in_memory = len(_pricing_table)

    logging.info("Loaded %d pricing rows from upload_pricing", rows_in_memory)

    # 3) Push CSV to BigQuery into pricing_YYYYMMDD (with sanitized column names)
    client_bq = _get_bq_client()
    bq_result = {"uploaded": False}

    if client_bq:
        try:
            from google.cloud import bigquery as bq_module

            # Re-read CSV and write out a cleaned version with BQ-safe headers
            input_buf = io.StringIO(csv_text)
            reader = csv.DictReader(input_buf)

            orig_fieldnames = reader.fieldnames or []
            sanitized_fieldnames = [_sanitize_bq_field(fn) for fn in orig_fieldnames]


            out_buf = io.StringIO()
            writer = csv.DictWriter(out_buf, fieldnames=sanitized_fieldnames)
            writer.writeheader()

            for row in reader:
                out_row = {}
                for orig, san in zip(orig_fieldnames, sanitized_fieldnames):
                    out_row[san] = row.get(orig)
                writer.writerow(out_row)

            cleaned_csv = out_buf.getvalue()

            now_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            table_name = f"pricing_{now_str}"
            table_id = f"{BQ_PROJECT_ID}.{BQ_DATASET}.{table_name}"      

            job_config = bq_module.LoadJobConfig(
                source_format=bq_module.SourceFormat.CSV,
                skip_leading_rows=1,
                autodetect=True,
                write_disposition=bq_module.WriteDisposition.WRITE_TRUNCATE,
            )

            load_job = client_bq.load_table_from_file(
                io.StringIO(cleaned_csv),
                table_id,
                job_config=job_config,
            )
            load_job.result()  # wait for completion

            dest_table = client_bq.get_table(table_id)
            bq_result = {
                "uploaded": True,
                "table": table_name,
                "row_count": dest_table.num_rows,
            }
        except Exception as e:
            logging.error("Failed to load pricing CSV into BigQuery: %s", e)
            bq_result = {"uploaded": False, "error": str(e)}

    # 4) Update Kubernetes ConfigMap so the new pricing is persisted
    _update_k8s_configmap(csv_text)


    return jsonify({
        "status": "ok",
        "pricing_rows_in_memory": rows_in_memory,
        "bigquery": bq_result,
    }), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Run Flask app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))