FROM docker.io/library/python:3.12-slim
WORKDIR /app
COPY hermes-cost-exporter.py /app/hermes-cost-exporter.py
EXPOSE 9101
CMD ["python", "hermes-cost-exporter.py"]