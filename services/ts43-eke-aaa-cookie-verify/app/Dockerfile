# Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# (Optional) a couple of base packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy the code and rename the entry file so uvicorn can import it
COPY ts43-issue-auth-code.py /app/main.py

# If you later add more modules/templates/static, switch to:
# COPY app/ /app/

EXPOSE 8080

# Start FastAPI
# Your file defines `app = FastAPI(...)`, so this import path is correct.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
