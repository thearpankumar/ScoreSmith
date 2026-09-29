# Dev-mode Dockerfile for the Quality Scorecard frontend.
# Matches infra/docker-compose.yml's `frontend` service: build context is
# ../frontend, the container listens on port 3000, NEXT_PUBLIC_API_BASE_URL
# is injected via env, and the compose file bind-mounts the source tree for
# live reload (with node_modules/.next kept as anonymous volumes so the
# image's installed deps and build cache aren't shadowed by the mount).
FROM node:20-slim

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

# Install dependencies first so `docker compose build` can cache this layer
# independently of source changes.
COPY package.json package-lock.json* ./
RUN npm install

# Copy the rest of the source. In dev this is immediately overlaid by the
# compose bind mount (../frontend:/app); the COPY keeps `docker build` alone
# (e.g. CI) usable without the mount.
COPY . .

EXPOSE 3000

# Reachable from the host / other containers, matching the compose port map.
CMD ["npm", "run", "dev"]
