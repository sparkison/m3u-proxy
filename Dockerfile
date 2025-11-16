FROM alpine:3.21.3

# Add Alpine edge repository for FFmpeg 8.0
RUN echo "@edge https://dl-cdn.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories

# Install FFmpeg 8.0 and system dependencies
RUN apk update && apk --no-cache add \
    # FFmpeg 8.0 from Alpine edge
    ffmpeg@edge \
    # Common utilities
    pciutils \
    wget \
    nano \
    curl \
    # Python dependencies
    python3 \
    py3-pip \
    py3-virtualenv

# Create symlink for python command
RUN ln -s /usr/bin/python3 /usr/bin/python

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY main.py .
COPY .env.example .env

# Copy Docker scripts
COPY docker/ ./docker/

# Create directories
RUN mkdir -p /tmp/m3u-proxy-streams

# Make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/docker/check-hwaccel.sh /app/docker/verify-hwaccel.sh

# Environment variables
ENV PYTHONPATH=/app

# Override the default entrypoint and run the application
ENTRYPOINT ["/app/docker/entrypoint.sh"]
