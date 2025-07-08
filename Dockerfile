FROM python:3.11-slim

WORKDIR /app

# --- install Poetry -------------------------------------------------
RUN pip install --no-cache-dir poetry==1.8.2           # or any pinned version

# --- copy dependency spec first (better layer-caching) -------------
# COPY pyproject.toml poetry.lock ./
COPY pyproject.toml ./

# install runtime deps only
RUN poetry lock --no-update 
RUN poetry install --no-root --only main

# --- copy the actual source code -----------------------------------
COPY src ./src
RUN ls -l ./src

# make the package importable for uvicorn
RUN python -m pip install --no-cache-dir --editable .

EXPOSE 8000
CMD ["uvicorn", "mockexchange_api.server:app", "--host", "0.0.0.0", "--port", "8000"]