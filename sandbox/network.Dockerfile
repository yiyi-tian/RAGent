FROM ragent-command-sandbox:v2
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    squid-openssl openssl socat && rm -rf /var/lib/apt/lists/*
COPY --chmod=0755 ragent-sni-check /usr/local/libexec/ragent-sni-check
CMD ["/usr/sbin/squid", "-N", "-f", "/etc/squid/squid.conf"]
