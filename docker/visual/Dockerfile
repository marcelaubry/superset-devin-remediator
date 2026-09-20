# Pinned browser + OS so screenshots are byte-for-byte reproducible on any host (macOS,
# Linux, CI). Never run the capture script on the host: font rendering differs.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
RUN pip install --no-cache-dir playwright==1.47.0 httpx==0.27.2
WORKDIR /app
