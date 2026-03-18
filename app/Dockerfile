FROM python:3.12-slim

WORKDIR /app

COPY app/src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/src/ ./src/
COPY reference/ ./reference/

ENTRYPOINT ["python"]
CMD ["src/download_trip_data.py"]
