FROM python:3.12-slim

ARG HTTP_PROXY="http://proxy-dmz.intel.com:912"
ARG HTTPS_PROXY="http://proxy-dmz.intel.com:912"
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app

RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5001

CMD ["sh", "-c", "uvicorn chat.app:app --host 0.0.0.0 --port 5001 --workers ${UVICORN_WORKERS:-2}"]
