#!/bin/bash
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
else
  COMPOSE="docker-compose"
fi
$COMPOSE pull
$COMPOSE --env-file .env -f "docker-compose.yml" up -d --build