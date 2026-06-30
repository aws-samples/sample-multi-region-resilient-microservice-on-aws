FROM node:25-alpine3.21

RUN apk upgrade --no-cache

ENV APPUSER=appuser
ENV APPUID=1001
ENV APPGID=1000

RUN adduser \
    --home "/app" \
    --uid "$APPUID" \
    --disabled-password \
    "$APPUSER"

WORKDIR /app
USER appuser

COPY --chown=appuser:appuser package.json .
COPY --chown=appuser:appuser package-lock.json .

RUN npm ci
HEALTHCHECK CMD curl --fail http://localhost:8080 || exit 1
COPY . /app
RUN npm run build

ENTRYPOINT [ "node", "dist/main.js" ]
