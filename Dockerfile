# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV TERM=xterm-256color

# Install system dependencies
# libpq-dev is needed for psycopg2
# libgeos-dev is needed for shapely/geopandas
# gcc/g++ might be needed for building some extensions
# p7zip-full is needed for extracting raw_data.7z
# osmium-tool is needed for OSM data processing
# gdal-bin (ogr2ogr) is needed for geospatial conversions
# default-jre is needed for osm2po (Java)
# git is needed to install occupancy from its release tag
RUN apt-get update && apt-get install -y \
    git \
    gcc \
    g++ \
    libpq-dev \
    libgeos-dev \
    p7zip-full \
    osmium-tool \
    gdal-bin \
    default-jre \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Copy .env.docker as .env for container use
COPY .env.docker .env

# Extract raw_data.7z if it exists and raw_data directory is empty or doesn't exist
RUN if [ -f "raw_data.7z" ] && [ -s "raw_data.7z" ]; then \
        echo "Extracting raw_data.7z..." && \
        7z x -o. -y raw_data.7z && \
        rm -f raw_data.7z && \
        echo "Extraction complete"; \
    fi

# Expose the port the app runs on
EXPOSE 8086

# Run the command to start the application
# Using uvicorn directly - scaling handled by running multiple containers
# Nginx load balances across containers
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8086", "--timeout-keep-alive", "300"]
