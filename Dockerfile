FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application (kubeconfig.yaml is NOT copied – mount it at runtime)
COPY server.py .
COPY static/ static/

ENV KUBECONFIG_PATH=/app/kubeconfig.yaml
ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
