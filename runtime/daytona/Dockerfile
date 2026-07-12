# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM node:22.19.0-bookworm-slim@sha256:4a4884e8a44826194dff92ba316264f392056cbe243dcc9fd3551e71cea02b90 AS node-runtime

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

LABEL org.opencontainers.image.source="https://github.com/mogilventures/mogil-bench" \
      org.opencontainers.image.description="Credential-free Mogil Bench runtime for Harbor 0.18.0 on Daytona" \
      io.mogil.harbor.version="0.18.0" \
      io.mogil.pi.version="0.80.6"

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /opt/mogil-pi
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund \
    && ln -s /opt/mogil-pi/node_modules/.bin/pi /usr/local/bin/pi \
    && test "$(pi --version)" = '0.80.6' \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

WORKDIR /workspace
CMD ["/bin/sh"]
