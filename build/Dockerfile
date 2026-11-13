# AVADO Lightning image: LND plus the AVADO page.
#
# Build context is the repository root (docker-compose.yml: context ".",
# dockerfile "build/Dockerfile"), so the image can read the package version
# from dappnode_package.json. See /.dockerignore for what is sent.

# Base image pinned by digest (multi-arch index digest), the same as the Bitcoin
# Node's. Update tag and digest together: `docker buildx imagetools inspect
# debian:<tag>` ("Digest:" line).
ARG DEBIAN_IMAGE=debian:bookworm-20261027-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

########## LND, verified
FROM --platform=linux/amd64 ${DEBIAN_IMAGE} AS lnd
# The official release from github.com/lightningnetwork/lnd. The SHA-256 below is
# the line for this file in the release's manifest-v0.21.8-beta.txt, whose
# signatures were checked with gpg against the keys in the lnd repository
# (scripts/keys at the release tag): 6 good signatures, among them roasbeef,
# ViktorT-11 and ziggie1984. A different download fails the build. To update:
# change both lines together.
ARG LND_VERSION=v0.21.8-beta
ARG LND_SHA256=4910a14801e10164ca2ea67f5935e2486874331d56bbd0638a68c6158a8e82c9
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    file="lnd-linux-amd64-${LND_VERSION}.tar.gz"; \
    curl -fsSL --retry 5 "https://github.com/lightningnetwork/lnd/releases/download/${LND_VERSION}/${file}" -o /tmp/lnd.tar.gz; \
    echo "${LND_SHA256}  /tmp/lnd.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/lnd; \
    tar -xzf /tmp/lnd.tar.gz -C /opt/lnd --strip-components=1; \
    /opt/lnd/lnd --version | grep -q "${LND_VERSION#v}"

########## Runtime
FROM --platform=linux/amd64 ${DEBIAN_IMAGE}
# python3: the page and supervisor (standard library only). qrencode: the QR code
# for the phone. libcap2-bin only to give python the right to open port 80
# without root (file capability, like the Bitcoin Node), then removed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 qrencode libcap2-bin tini \
    && setcap cap_net_bind_service=+ep "$(readlink -f /usr/bin/python3)" \
    && getcap "$(readlink -f /usr/bin/python3)" | grep -q cap_net_bind_service \
    && apt-get purge -y libcap2-bin && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=lnd /opt/lnd/lnd /opt/lnd/lncli /usr/local/bin/
COPY build/app /app
COPY dappnode_package.json /tmp/dappnode_package.json
RUN python3 -c 'import json,re; v=json.load(open("/tmp/dappnode_package.json"))["version"]; \
assert re.fullmatch(r"[0-9A-Za-z.+-]+", v), "bad package version"; open("/app/package-version","w").write(v+"\n")' \
    && rm /tmp/dappnode_package.json \
    && cd /app && python3 -m unittest discover -s tests -q \
    && rm -rf /app/tests /app/__pycache__ \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin lnd \
    && mkdir -p /data && chown lnd:lnd /data \
    && ln -s /data/lnd /home/lnd/.lnd

# Not root: the page binds port 80 through the file capability above, and a new
# volume starts out owned by `lnd` because /data is. ~/.lnd points at LND's
# folder, so `lncli` works as is for support (docker exec ... lncli getinfo).
USER lnd
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
VOLUME /data
EXPOSE 80 8080 9735
# tini reaps processes and passes the stop signal on; main.py then stops LND cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/main.py"]
