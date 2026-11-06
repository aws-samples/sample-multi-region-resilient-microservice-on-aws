FROM public.ecr.aws/docker/library/node:26-alpine3.24

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

# The service only runs node. npm was needed for the two steps above, and the copy that the base image
# bundles carries its own undici and brace-expansion, which Inspector rates HIGH and CRITICAL; the
# newest node:26-alpine still ships them, so a rebuild does not clear them.
USER root
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
USER appuser

ENTRYPOINT [ "node", "dist/main.js" ]
