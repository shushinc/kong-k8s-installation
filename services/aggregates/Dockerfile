# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the content of the local src directory to the working directory
COPY aggregator_app.py .
COPY pricing.csv .
# You will need to place your service account key here
COPY your-service-account-key.json ./service-account.json

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Run aggregator_app.py when the container launches
CMD ["python", "aggregator_app.py"]
