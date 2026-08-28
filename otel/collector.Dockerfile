# otelcol-contrib ships FROM SCRATCH — no shell, no curl/wget, nothing but the binary and CA
# certs (verified: `docker export` on the base image lists exactly those two things). There is
# no image-native way to exec a healthcheck inside it. This layers in a statically-linked
# busybox (musl build, no shared-library deps) so `docker compose`'s healthcheck can exec it
# directly — COPY doesn't need a shell in either stage, so the base image's bare rootfs is fine.
FROM busybox:1.37-musl AS tools

FROM otel/opentelemetry-collector-contrib:0.159.0
COPY --from=tools /bin/busybox /tools/busybox
