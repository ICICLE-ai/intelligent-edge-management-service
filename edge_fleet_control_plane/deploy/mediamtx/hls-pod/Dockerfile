# Custom MediaMTX image for the HLS playback pod.
# Same pattern as your mosquitto image: a stock base + a baked-in config file.
# (The regex proxy-pull path can't be expressed as MTX_* env vars, so it must
# live in mediamtx.yml.)
#
# We also copy in a CA certificate bundle: the stock bluenviron/mediamtx image
# is built FROM scratch and has NO CA certs, so its rtsps:// source pull can't
# validate the Tapis TLS certificate and the pull times out. The CA bundle lets
# MediaMTX validate edgemediaingest.pods.icicleai.tapis.io and survives cert
# rotation (unlike pinning a fingerprint).
#
# Build & push to a registry Tapis can pull from, then allowlist the image:
#   docker buildx build --platform linux/amd64 \
#     -t docker.io/<youruser>/edge-mediamtx-hls:latest --push .

FROM alpine:3 AS certs
RUN apk add --no-cache ca-certificates

FROM bluenviron/mediamtx:latest
COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY mediamtx.yml /mediamtx.yml
