FROM python:3.12-slim

WORKDIR /app

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENTRYPOINT ["python", "src/download_trip_data.py"]
