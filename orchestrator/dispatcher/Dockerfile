FROM python:3.12-slim
# docker CLI（ワーカーを docker run するため。ソケットはホストからマウント）
# git と docker compose は shipper（ADR 0012）が使う
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli docker-compose docker-buildx git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dispatcher.py factoryctl.py stoppage.py supervisor.py judgment.py ops.py console.py ship.py learn.py ./
RUN ln -s /app/factoryctl.py /usr/local/bin/factoryctl && chmod +x /app/factoryctl.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "dispatcher.py"]
