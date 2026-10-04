# Docker 安装

## 前置条件

- Docker Engine 与 Docker Compose 插件
- 可供 Telegram 访问的 HTTPS 域名
- Telegram Bot Token
- Evan 提供的项目激活码

## 安装

```bash
git clone https://github.com/8538989/cockroach-runner.git
cd cockroach-runner
cp .env.example .env
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

编辑 `.env`：

```dotenv
COCKROACH_ADMIN_USERNAME=admin
COCKROACH_ADMIN_PASSWORD=请改成强密码
COCKROACH_ENCRYPTION_KEY=上一步生成的Fernet密钥
COCKROACH_ACTIVATION_CODE=向Evan获取
COCKROACH_BIND_IP=127.0.0.1
COCKROACH_PORT=8790
COCKROACH_CD2_PATH=/你的/CD2/网盘挂载路径
```

启动并检查：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100
curl http://127.0.0.1:8790/healthz
```

缺少或填错激活码时，容器日志会显示激活失败并停止启动。

## HTTPS

使用 Caddy、Nginx 或 Traefik 将 HTTPS 域名反向代理到 `127.0.0.1:8790`。管理中心为 `/admin`，小程序为站点根路径 `/`。
