# Deterministic build for Penny (Railway uses this over Railpack/nixpacks).
# One image, two roles: the Slack listener (default) and the review dashboard
# (ROLE=dashboard) — selected at runtime so both Railway services share it.
FROM python:3.12-slim

WORKDIR /app
COPY . .
# Editable install: deps installed, and the package stays rooted at /app so
# config.py resolves /app/clients (the per-client YAML lives at repo root).
RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1
CMD ["sh","-c","if [ \"$ROLE\" = dashboard ]; then exec gunicorn cfo_agent.adapters.dashboard:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 60; else exec python -m cfo_agent.cli penny listen --client august --month ${CLOSE_MONTH}; fi"]
