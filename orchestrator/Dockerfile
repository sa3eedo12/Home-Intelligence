FROM python:3.12-slim
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY shared /shared
RUN pip install --no-cache-dir /shared
COPY orchestrator /src/orchestrator
# Migration files live at /src/infra/postgres/init/*.sql so the boot-time
# migrations runner (orchestrator/migrations.py) can apply any new SQL on
# every startup, not just the first volume init.
COPY infra /src/infra
RUN pip install --no-cache-dir -e /src/orchestrator
ENV PYTHONPATH=/src
EXPOSE 8000
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]
