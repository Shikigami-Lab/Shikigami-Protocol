FROM python:3.12-slim
COPY requirements.txt /tmp/contract-requirements.txt
RUN pip install --no-cache-dir -r /tmp/contract-requirements.txt pillow==11.3.0 matplotlib==3.10.8 \
    && useradd --create-home --uid 1000 tester
USER tester
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
