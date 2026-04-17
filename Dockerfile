FROM python:3.12-slim

WORKDIR /app

# No third-party deps – only stdlib is used
COPY hamalert.py .

CMD ["python", "-u", "hamalert.py"]
