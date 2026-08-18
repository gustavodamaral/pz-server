# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1 AS rcon-cli

ARG TARGETARCH
ARG RCON_CLI_VERSION=1.7.7

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
        amd64) archive_arch="amd64"; checksum="a6faf3d8b8259e88fd0a662dd6baff74a4226bafd96a9f578fcc3f4f534cadf2" ;; \
        arm64) archive_arch="arm64"; checksum="05648eb1b2f6bd7b331776baee9e791fb3a938b343fa35e89b663b5527eabe27" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --show-error --silent \
        --output /tmp/rcon-cli.tar.gz \
        "https://github.com/itzg/rcon-cli/releases/download/${RCON_CLI_VERSION}/rcon-cli_${RCON_CLI_VERSION}_linux_${archive_arch}.tar.gz" \
    && printf '%s  %s\n' "${checksum}" /tmp/rcon-cli.tar.gz > /tmp/rcon-cli.sha256 \
    && sha256sum --check --strict /tmp/rcon-cli.sha256 \
    && mkdir /out \
    && tar --extract --gzip --file /tmp/rcon-cli.tar.gz --directory /out rcon-cli \
    && chmod 0755 /out/rcon-cli

FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 65532 watchdog \
    && useradd --system --uid 65532 --gid 65532 --home-dir /nonexistent --shell /usr/sbin/nologin watchdog

COPY --from=rcon-cli /out/rcon-cli /usr/local/bin/rcon-cli
COPY pyproject.toml README.md /app/
COPY src /app/src

WORKDIR /app
RUN pip install --no-cache-dir .

USER watchdog:watchdog
ENTRYPOINT ["pz-watchdog"]
