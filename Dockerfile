# syntax=docker/dockerfile:1
#
# 国内网络默认全走镜像源；海外网络可用 --build-arg 覆盖。
#   docker build --build-arg BASE_IMAGE=python:3.12-slim .
#
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${BASE_IMAGE}

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/src \
    LANG=C.UTF-8

# Debian 12 (bookworm) 用 deb822 格式的 sources，老版本用 sources.list
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl tzdata; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY src ./src
COPY config ./config
COPY README.md ./

RUN mkdir -p /app/data

# 说明：这里以 root 运行。宿主机 ./data 是 bind mount，
# 非 root 用户（uid 1000）在多数 VPS 上会因目录属主不同而无法写入。
USER root

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/healthz || exit 1

CMD ["uvicorn", "rscbot.web.app:app", "--host", "0.0.0.0", "--port", "8001"]
