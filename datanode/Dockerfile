FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
ENV DFSHA_DATANODE_STORAGE_DIR=/data/blocks
ENV DFSHA_DATANODE_ADVERTISE_PORT=9001

EXPOSE 9001

# Escucha en el mismo puerto que se anuncia al ControlNode/cliente
# (DFSHA_DATANODE_ADVERTISE_PORT), para que docker-compose solo tenga que
# fijar esa variable por contenedor sin necesitar una segunda para "puerto
# de escucha interno".
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${DFSHA_DATANODE_ADVERTISE_PORT}"]
