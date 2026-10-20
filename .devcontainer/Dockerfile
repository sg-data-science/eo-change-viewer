ARG VARIANT=3.14-trixie
FROM python:${VARIANT}

# System libraries for the geospatial stack. fiona and pyogrio don't yet publish
# wheels for every Python version, so pip may build them from source against
# the system GDAL; libgdal-dev provides the headers and gdal-config for that.
# tk is kept for matplotlib's interactive TkAgg backend.
# chromium is driven headless by Playwright to view and screenshot the app;
# Debian's build is used rather than `playwright install`, so apt manages it
# along with its system libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        gdal-bin \
        libgdal-dev \
        tk \
        chromium \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app

# Copy only the requirements files so editing source code doesn't invalidate
# the cached dependency layer.
COPY .devcontainer/requirements.txt .devcontainer/requirements-dev.txt /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/requirements-dev.txt

EXPOSE 8050

ENTRYPOINT ["bash"]
