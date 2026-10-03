FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY synth/deploy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && useradd --uid 10001 --create-home synth
COPY synth/config.py synth/deployment.json /app/synth/
COPY synth/server /app/synth/server
COPY synth/worker /app/synth/worker
COPY synth/contracts /app/synth/contracts
COPY synth/speakers /app/synth/speakers
COPY synth/deploy/supabase-ca.crt /app/synth/deploy/supabase-ca.crt
USER synth
CMD ["python", "-m", "uvicorn", "synth.server.app:app", "--host", "0.0.0.0", "--port", "18280", "--no-access-log"]
