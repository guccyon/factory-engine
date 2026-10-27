FROM python:3.12-slim
# docker CLI（ワーカーを docker run するため。ソケットはホストからマウント）
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dispatcher.py factoryctl.py stoppage.py ./
RUN ln -s /app/factoryctl.py /usr/local/bin/factoryctl && chmod +x /app/factoryctl.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "dispatcher.py"]
