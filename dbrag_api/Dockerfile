FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 ACCEPT_EULA=Y

# Microsoft ODBC Driver 18 for SQL Server
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc-dev \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql.list \
 && apt-get update && apt-get install -y --no-install-recommends msodbcsql18 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model (ONNX all-MiniLM-L6-v2) into the image: no download at runtime/cold start
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as E; E()(['warmup'])"

COPY app ./app
COPY scripts ./scripts
COPY data ./data
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh && useradd -r -u 10001 appuser && chown -R appuser /srv /root/.cache 2>/dev/null || true
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s CMD curl -fs http://localhost:8000/healthz || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
