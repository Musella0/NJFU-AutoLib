FROM python:3.11-slim

# 境内机器构建时 apt / pip 走官方源极慢（宿迁实测 apt-get install gcc 卡 3 分钟不止，
# deb.debian.org 只有 0.37 MB/s，镜像站 1.77 MB/s）。两个 ARG 默认留空 = 官方源，
# 境外和自己部署的人什么都不用改；境内在 .env 里填镜像站，compose 会传进来。
#   APT_MIRROR=mirrors.aliyun.com
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG APT_MIRROR=
ARG PIP_INDEX_URL=

WORKDIR /app

# 系统依赖。基础镜像是 Debian trixie，源在 deb822 格式的 debian.sources 里，
# 把主机名整个换掉即可，/debian 和 /debian-security 两个路径镜像站都有。
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

# 复制项目代码
COPY . .

# 默认启动 Flask API
CMD ["python", "main.py"]
