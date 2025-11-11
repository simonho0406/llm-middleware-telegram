# Use an official Python runtime as a parent image
FROM python:3.11-buster

# Set environment variables
# Prevents Python from writing pyc files to disc (equivalent to python -B)
ENV PYTHONDONTWRITEBYTECODE 1
# Ensures Python output is sent straight to terminal without being buffered
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies if needed (e.g., for libraries that require C extensions)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
ENV PYTHONPATH=/usr/local/lib/python3.11/site-packages:$PYTHONPATH
ENV PYTHONPATH=/usr/local/lib/python3.11/site-packages:$PYTHONPATH

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# Copy the rest of the application code into the container
COPY . .

# Command to run the application
CMD ["python", "main.py"]
