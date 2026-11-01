FROM python:3.11-slim

WORKDIR /app

COPY requirement.txt .
RUN pip install --no-cache-dir -r requirement.txt

COPY locust_file.py .

EXPOSE 8089

CMD ["locust", "-f", "locust_file.py", "--host", "http://middleware:8000"]
