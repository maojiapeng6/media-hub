# MediaHub 总控台容器
# 纯标准库 http.server，无第三方依赖（部署极简）
FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PORT=5678

WORKDIR /app
COPY app.py index.html ./

# /data 持久化：webcfg.json（业务配置）、.kfcsec/（主密钥/install.lock/授权库）
VOLUME ["/data"]

EXPOSE 5678
CMD ["python", "app.py"]
