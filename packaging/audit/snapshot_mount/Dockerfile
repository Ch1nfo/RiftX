# syntax=docker/dockerfile:1.7

ARG SOURCE_DATE_EPOCH=1785801600

FROM docker.io/library/python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS python-runtime

RUN set -eu; \
    test -x /usr/local/bin/python3; \
    test ! -e /usr/bin/python3; \
    ln -s /usr/local/bin/python3 /usr/bin/python3; \
    rm -rf \
        /root/.cache \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.12 \
        /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.12/site-packages/setuptools \
        /usr/local/lib/python3.12/site-packages/setuptools-*.dist-info \
        /usr/local/lib/python3.12/site-packages/wheel \
        /usr/local/lib/python3.12/site-packages/wheel-*.dist-info

FROM scratch

COPY --from=python-runtime / /

LABEL org.opencontainers.image.title="RiftX Code Audit Snapshot Mount Runtime" \
      org.opencontainers.image.description="Pinned standard-library-only runtime for private Snapshot materialization" \
      org.opencontainers.image.version="3.12.13-riftx1" \
      io.riftx.audit.snapshot-mount.image-schema="riftx.audit-snapshot-mount-image/v1" \
      io.riftx.audit.snapshot-mount.base-index-digest="sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"

USER 65532:65532
WORKDIR /
ENTRYPOINT ["/usr/bin/python3"]
CMD ["-I", "-B", "-c", "import sys; sys.exit(0)"]
