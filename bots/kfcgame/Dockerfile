# 不带任何 apt 安装步骤 —— 飞牛访问 Debian 官方源很慢，会卡死构建
# 全部依赖走清华 pypi 镜像，纯 wheel 安装，秒级完成
FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

CMD ["python", "-m", "app.main"]
