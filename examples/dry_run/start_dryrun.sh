#!/bin/bash

cd on_docker
docker compose build --no-cache
docker compose up -d