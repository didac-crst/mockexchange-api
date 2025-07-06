FROM python:3.11-slim

WORKDIR /app

# copy project
COPY pyproject.toml README.md .
COPY src ./src

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

# install project so 'mockexchange' is on site-packages
RUN python -m pip install --no-cache-dir --editable .

EXPOSE 8000
CMD ["uvicorn", "scripts.server:app", "--host", "0.0.0.0", "--port", "8000"]
