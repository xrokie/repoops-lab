FROM python:3.12-slim
WORKDIR /app
COPY requirements-dev.txt /app/
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pyproject.toml /app/
COPY src /app/src
COPY tests /app/tests
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "pytest", "-q", "-m", "not docker", "-p", "no:cacheprovider"]
