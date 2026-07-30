# Kibana Notifier and Log Chat

This project contains:
1. Elasticsearch log polling and notification workflow.
2. FastAPI-based chat API and web UI for multi-source analysis:
   - Elasticsearch indices
   - Jenkins console output (paste or URL)
   - Uploaded documents with RAG retrieval

## Local Run (Without Docker)

1. Install dependencies.

```bash
pip install -r requirements.txt
```

2. Update settings in [config/config.yaml](config/config.yaml).

3. Run chat API + UI.

```bash
python -m chat.app
```

4. Access:
1. Chat window: http://localhost:5001
2. API docs: http://localhost:5001/docs

## Docker Containerization

### Build and run with Docker

```bash
docker build -t log-chat:latest .
docker run -d --name log-chat \
  -p 5001:5001 \
  -e FLASK_SECRET_KEY="replace-with-strong-secret" \
  -e UVICORN_WORKERS=2 \
  -v ${PWD}/config/config.yaml:/app/config/config.yaml:ro \
  -v ${PWD}/chat/data:/app/chat/data \
  --restart unless-stopped \
  log-chat:latest
```

### Run with Docker Compose

```bash
docker compose up -d --build
```

Compose file: [docker-compose.yml](docker-compose.yml)

Docker image definition: [Dockerfile](Dockerfile)

## Hosting on Remote Server

### Option 1: Direct port exposure (quick start)

1. Open inbound port 5001 in security group/firewall.
2. Run container and publish port 5001.
3. Access chat UI at:
   - http://<server-ip>:5001

### Option 2: Reverse proxy with TLS (recommended)

1. Keep container internal (publish to localhost or private network).
2. Put Nginx/Traefik in front.
3. Attach a domain and TLS certificate.
4. Route:
1. https://chat.example.com -> http://log-chat:5001

### Option 3: Kubernetes

1. Build and push image to registry.
2. Deploy with Deployment + Service.
3. Expose via Ingress for TLS/domain access.

## Access Patterns for API and Chat Window

1. Browser chat UI:
   - GET /
2. OpenAPI docs:
   - GET /docs
3. Programmatic API usage:
1. POST /api/chat
2. POST /api/documents/upload
3. GET /api/documents
4. POST /api/jenkins/console/analyze

## Production Notes

1. Set strong session secret via FLASK_SECRET_KEY environment variable.
2. Persist [chat/data](chat/data) volume for uploaded documents and vector DB.
3. Ensure [config/config.yaml](config/config.yaml) uses reachable endpoints from inside container:
1. Elasticsearch host
2. Ollama URL (if not colocated with container)
3. Jenkins URL and auth settings
4. Scale workers with UVICORN_WORKERS based on CPU and workload profile.
5. Use reverse proxy and TLS for internet-facing deployments.
