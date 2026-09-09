ARG RUNTIME_IMAGE=cr.l5d.io/linkerd/proxy:edge-26.8.2
FROM ${RUNTIME_IMAGE}
COPY linkerd2-proxy /usr/lib/linkerd/linkerd2-proxy
