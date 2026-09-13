# CDS Hooks Sandbox (https://github.com/cds-hooks/sandbox) has no published
# image and no Dockerfile upstream, so we build one here.
#
# Stage 1 clones a pinned commit and runs the production webpack build; stage 2
# serves the resulting static bundle with nginx.

FROM node:22-bookworm AS build

ARG SANDBOX_REPO=https://github.com/cds-hooks/sandbox.git
# Pinned: "Merge pull request #222 ... long-running-extension-indicator", 2026-06-05.
ARG SANDBOX_COMMIT=b52fc1b54ba36138e7846b690cd39a18eab54903

# Registry to install from.  On a corporate network that blocks or TLS-inspects
# registry.npmjs.org, point this at your mirror (see docker-compose.yml /
# demo/reference-sandbox/.env).  npm rewrites the default-registry `resolved`
# URLs in package-lock.json to this host, so `npm ci` stays lockfile-exact.
ARG NPM_REGISTRY=https://registry.npmjs.org/

WORKDIR /src
RUN git init . \
 && git remote add origin "$SANDBOX_REPO" \
 && git fetch --depth 1 origin "$SANDBOX_COMMIT" \
 && git checkout FETCH_HEAD

# Install and build in one layer with node_modules on tmpfs: only ./build is
# needed by the runtime stage, so the ~1.3GB dependency tree never lands in an
# image layer.  `test -x` guards against npm exiting 0 with a half-installed
# tree (it does that when registry fetches fail: the tell-tale symptom is
# "npm error Exit handler never called!" preceded by
# ERR_SSL_SSL/TLS_ALERT_HANDSHAKE_FAILURE lines in the npm debug log, which
# means the registry above is not reachable from inside Docker).
RUN --mount=type=tmpfs,target=/src/node_modules \
    npm ci --registry="$NPM_REGISTRY" --legacy-peer-deps --no-audit --no-fund \
 && test -x node_modules/.bin/webpack \
 && npm run build

FROM nginx:1.27-alpine AS runtime
COPY --from=build /src/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
