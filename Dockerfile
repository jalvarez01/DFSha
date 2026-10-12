FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
ENV DFSHA_DATABASE_URL=sqlite:////data/dfsha.db
ENV DFSHA_STORAGE_DIR=/data/blocks

EXPOSE 8000

# Con DFSHA_TLS_CERT/DFSHA_TLS_KEY definidos sirve HTTPS (Hito 3, cifrado en
# tránsito); sin ellos, HTTP (desarrollo y tests).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 ${DFSHA_TLS_CERT:+--ssl-certfile $DFSHA_TLS_CERT --ssl-keyfile $DFSHA_TLS_KEY}"]
