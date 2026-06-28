FROM python:3.11-slim

WORKDIR /app

# Install build dependencies required for compiling crypto libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Run pre-execution check to ensure no secrets are hardcoded in the built image
RUN python3 verify_no_secrets.py

# Run main application
CMD ["python3", "main.py"]
