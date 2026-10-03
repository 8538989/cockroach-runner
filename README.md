# 蟑螂快跑

独立运行的多用户 115 自动派送服务。它直接调用 115 API 扫描源目录，并把新资源秒传到用户自己的 115 目录，提供 Telegram Bot、小程序和完整 Web 管理中心。

本项目不依赖 DIAN115、DIAN115 数据库、门户或 Emby。默认可只用 115 API，也可选配 CloudDrive2 挂载作为实时或轮询监听兜底。

## 功能

- 用户管理：账号和完整 CK、启停、删除用户、目标目录、订阅模式、搜索额度、备注。
- 派送任务：状态、尝试次数、错误、自动重试、手动重派和取消。
- 多监听目录：支持 115 API 轮询、CD2 实时监控和 CD2 定时轮询，每个目录可设置扫描间隔、文件稳定等待、启停和历史基线。
- 一次性绑定码：管理员生成限时码，用户向 Bot 发送 `/bind 绑定码` 后才能使用。
- 自动删除：可按派送成功时间延时删除，也可按文件进入监听目录的时间定时删除。仅在该资源的全部派送均已完成或取消后执行。
- 服务设置：派送并发、CK 检查周期、失败重试、最大尝试次数、默认搜索额度和日志保留。
- 运行记录：查看最近的扫描、用户、派送、绑定与清理事件。
- Telegram 小程序：用户绑定自己的 115 账号、目标目录和订阅规则。

完整管理中心位于 `/admin`，上述功能全部在独立 Docker 服务内完成。

## Docker Compose 部署

要求 Docker Engine、Docker Compose，以及一个可供 Telegram 访问的 HTTPS 域名。

```bash
cp .env.example .env
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

把管理员用户名、强密码填入 `.env` 的 `COCKROACH_ADMIN_USERNAME`、`COCKROACH_ADMIN_PASSWORD`，把生成值填入 `COCKROACH_ENCRYPTION_KEY`，然后启动：

```bash
docker compose up -d --build
docker compose ps
```

默认只监听服务器本机的 `127.0.0.1:8790`。通过反向代理打开 `https://你的域名/admin`，使用管理员用户名和密码登录，依次配置：

1. Telegram Bot Token 和小程序 HTTPS 公网地址。
2. 用于监听资源的源 115 Cookie。
3. 一个或多个监听目录 CID；使用 CD2 时同时填写容器内 `/cd2/miaochuang` 下的对应目录。
4. 绑定码、用户、派送及秒传后删除规则。
5. 确认无误后开启“自动扫描与派送”。

第一次成功扫描只建立历史基线，不会把目录里的所有旧资源批量派送。此后新出现、且达到稳定等待时间的视频资源才进入派送队列。

## 公网与 Telegram

使用 Caddy、Nginx、Traefik 等反向代理，把 HTTPS 域名转发到 `8790`。在 BotFather 中将小程序菜单地址设置为同一 HTTPS 地址。管理中心地址为 `https://你的域名/admin`。

管理员登录信息只保存在当前浏览器标签页的 `sessionStorage` 中，但管理中心仍必须使用 HTTPS。建议在反向代理中额外限制 `/admin` 的来源 IP。

同一个 Telegram Bot Token 同一时间只能由一个实例长轮询。迁移时应先停止旧服务，再启动本项目，避免两个实例抢占 Bot 更新。

## 数据、备份与迁移

SQLite 数据库保存在 Docker 命名卷 `cockroach-data` 的 `/data/cockroach-runner.db`。Cookie 与 Bot Token 使用 `COCKROACH_ENCRYPTION_KEY` 加密。备份时必须同时保存数据库和 `.env` 中的加密密钥；密钥丢失后，已有 Cookie 和 Token 无法解密。

若要直接复用旧数据目录，可在 `.env` 设置 `COCKROACH_DATA_DIR=/var/lib/cockroach-runner`，并把 `COCKROACH_UID`、`COCKROACH_GID` 设置为该目录所有者的数字 UID/GID。这样无需复制数据库，也方便快速回滚。

从旧的独立 systemd 服务迁移时，在旧服务项目目录执行：

```bash
systemctl stop cockroach-runner
docker compose up -d --build
docker compose stop
docker cp /var/lib/cockroach-runner/cockroach-runner.db cockroach-runner:/data/cockroach-runner.db
docker compose run --rm --user root --entrypoint chown cockroach-runner 10001:10001 /data/cockroach-runner.db
docker compose start
```

确认 Docker 版本健康、Bot 和派送均正常后，再决定是否移除旧 systemd 服务。不要同时运行两份服务。

## 更新、日志与健康检查

```bash
docker compose up -d --build
docker compose logs -f --tail=200
curl http://127.0.0.1:8790/healthz
```

## 本地测试

```bash
python -m unittest discover -s tests -v
node --check companion/static/admin.js
```
