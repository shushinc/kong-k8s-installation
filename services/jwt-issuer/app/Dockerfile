# Use a lightweight and secure official Python base image
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy the application source code into the container
# Copy the code and rename the entry file so uvicorn can import it
COPY jwt-issuer.py /app/main.py

# Inform Docker that the container listens on port 8000 at runtime
EXPOSE 8000

# Start FastAPI
# Your file defines `app = FastAPI(...)`, so this import path is correct.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]