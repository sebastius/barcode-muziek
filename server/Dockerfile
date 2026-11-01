FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY barcoder.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "barcoder.py"]
