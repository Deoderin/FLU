FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY arbiter.py policy.py panel.html ./
ENTRYPOINT ["python", "-u", "arbiter.py"]
CMD ["--config", "/config/arbiter.yaml", "--state", "/state"]
