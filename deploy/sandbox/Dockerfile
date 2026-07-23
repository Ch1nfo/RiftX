FROM golang:1.25.1-bookworm@sha256:c423747fbd96fd8f0b1102d947f51f9b266060217478e5f9bf86f145969562ee AS security-tools

ARG HTTPX_VERSION=v1.9.0
ARG NUCLEI_VERSION=v3.8.0
ARG FFUF_VERSION=v2.1.0

RUN GOBIN=/out CGO_ENABLED=0 go install github.com/projectdiscovery/httpx/cmd/httpx@${HTTPX_VERSION} \
    && GOBIN=/out CGO_ENABLED=0 go install github.com/projectdiscovery/nuclei/v3/cmd/nuclei@${NUCLEI_VERSION} \
    && GOBIN=/out CGO_ENABLED=0 go install github.com/ffuf/ffuf/v2@${FFUF_VERSION}

FROM rust:1.95.0-bookworm@sha256:6258907abe69656e41cd992e0b705cdcfabcbbe3db374f92ed2d47121282d4a1 AS codex-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends clang cmake libssl-dev make perl pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src/codex-rs
COPY codex-rs/ ./
RUN cargo build --locked --release -p codex-cli --bin codex

FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 nmap tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin riftx \
    && mkdir -p /opt/riftx/wordlists /opt/riftx/nuclei-templates /run/riftx /workspace \
    && chown -R riftx:riftx /workspace

COPY --from=codex-builder /src/codex-rs/target/release/codex /usr/local/bin/codex
COPY --from=security-tools /out/httpx /out/nuclei /out/ffuf /usr/local/bin/
COPY deploy/sandbox/wordlists/ /opt/riftx/wordlists/
COPY deploy/sandbox/nuclei-templates/ /opt/riftx/nuclei-templates/

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["codex", "exec-server", "--listen", "ws://0.0.0.0:9800", "--riftx-auth-file", "/run/riftx/auth.json"]
