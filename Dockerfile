# syntax=docker/dockerfile:1.7

FROM debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241 AS rcon-cli

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

FROM debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241

ARG PZ_UID=1000
ARG PZ_GID=1000

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    HOME=/home/pz \
    PATH=/opt/steamcmd:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/pz-updater \
    STEAMCMD_DIR=/opt/steamcmd \
    PZ_SERVER_DIR=/opt/pzserver \
    ZOMBOID_DIR=/home/pz/Zomboid

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install --no-install-recommends --yes \
        bash \
        ca-certificates \
        coreutils \
        curl \
        gettext-base \
        jq \
        lib32gcc-s1 \
        lib32stdc++6 \
        libatomic1 \
        libgcc-s1 \
        libstdc++6 \
        locales \
        passwd \
        procps \
        python3 \
        tar \
        tini \
        util-linux \
    && sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${PZ_GID}" pz \
    && useradd --create-home --home-dir /home/pz --uid "${PZ_UID}" --gid "${PZ_GID}" --shell /bin/bash pz \
    && mkdir --parents /backups /opt/pz-updater /opt/steamcmd /opt/pzserver /home/pz/Zomboid \
    && curl --fail --location --show-error --silent \
        --output /tmp/steamcmd_linux.tar.gz \
        https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
    && tar --extract --gzip --file /tmp/steamcmd_linux.tar.gz --directory /opt/steamcmd \
    && rm /tmp/steamcmd_linux.tar.gz \
    && chown --recursive pz:pz /backups /opt/steamcmd /opt/pzserver /home/pz

COPY --from=rcon-cli /out/rcon-cli /usr/local/bin/rcon-cli
COPY --chown=root:root src/pz_updater /opt/pz-updater/pz_updater
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/pz-entrypoint
COPY --chmod=0755 docker/healthcheck.sh /usr/local/bin/pz-healthcheck
COPY --chmod=0755 docker/console.sh /usr/local/bin/pz-console
COPY --chmod=0755 docker/updater.sh /usr/local/bin/pz-updater

USER pz:pz
WORKDIR /opt/pzserver

EXPOSE 16261/udp 16262/udp 27015/tcp

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=2h --retries=5 CMD ["/usr/local/bin/pz-healthcheck"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/pz-entrypoint"]
