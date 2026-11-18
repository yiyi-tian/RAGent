FROM ragent-ubuntu-base:24.04.5
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash coreutils grep findutils git curl wget ca-certificates openssh-client \
    python3 python3-venv python3-pip nodejs npm build-essential cmake pkg-config \
    libjson-c-dev libcurl4-openssl-dev procps clang-format \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/tmp/home
ENV PYTHONUSERBASE=/workspace/.ragent-packages/python
ENV PIP_USER=1
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ENV GIT_TERMINAL_PROMPT=0
WORKDIR /workspace
CMD ["/bin/bash", "--noprofile", "--norc"]
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    socat && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 ragent-launcher /usr/local/libexec/ragent-launcher
RUN ln -s ragent-launcher /usr/local/libexec/ragent-askpass
