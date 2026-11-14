# CirrhosisRegistry — application image (web + worker share this image).
#
# Python 3.13-slim matches the version used for native development.
# ghostscript is required by camelot-py's lattice table extraction.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so source edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==22.0.0

COPY . .

EXPOSE 5000

# Default command = web. docker-compose overrides this for the worker.
# gunicorn (not `python run.py`): binds 0.0.0.0 and is production-grade.
# --timeout 600 matches the long-running upload/preflight requests.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "600", "run:app"]
