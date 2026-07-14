FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*
COPY pinger/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pinger/app ./app
COPY config/endpoints.json ./config/endpoints.json
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
