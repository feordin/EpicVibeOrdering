# Container image for the EpicVibe CDS Hooks service.
#
# Optional: the primary dev loop runs the service on the host
# (`uvicorn epicvibe.cds.app:create_app --factory --port 8000`).  This image
# exists so `demo/reference-sandbox/docker-compose.yml --profile cds` can run
# the service inside the compose network as well.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Runtime data the service reads at startup (catalog) and the fixtures the
# demo provider/evals refer to.
COPY fixtures ./fixtures
COPY evals ./evals

EXPOSE 8000

CMD ["uvicorn", "epicvibe.cds.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
