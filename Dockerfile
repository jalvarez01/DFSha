FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Volumen para persistir la BD SQLite fuera del contenedor.
VOLUME ["/data"]
ENV DFSHA_DATABASE_URL=sqlite:////data/dfsha.db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
