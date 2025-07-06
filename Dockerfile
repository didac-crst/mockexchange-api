FROM python:3.11-slim

WORKDIR /app

# copy project
COPY pyproject.toml README.md .
COPY src/mockexchange ./src
COPY src/scripts ./scripts

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "scripts.server:app", "--host", "0.0.0.0", "--port", "8000"]
