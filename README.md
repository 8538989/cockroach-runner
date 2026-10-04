# 蟑影递送

独立运行的多用户 115 自动派送服务。它直接调用 115 API 扫描源目录，并把新资源秒传到用户自己的 115 目录，提供 Telegram Bot、小程序和完整 Web 管理中心。

本项目不依赖 DIAN115、DIAN115 数据库、门户或 Emby。默认可只用 115 API，也可选配 CloudDrive2 本地挂载或 gRPC API 作为监听兜底。

## 功能

- 用户管理：账号和完整 CK、启停、删除用户、目标目录、订阅模式、搜索额度、备注。
- 派送任务：状态、尝试次数、错误、自动重试、手动重派和取消。
- 多监听目录：支持 115 API 轮询、CD2 本地实时/轮询和 CD2 API 轮询，每个目录可设置扫描间隔、文件稳定等待、启停和历史基线。
- CD2 元数据旁路：可直接从 CD2 gRPC API 读取文件 ID、SHA1 与大小，并在本地生成 pickcode，减少源账号 115 目录 API 请求及 405 风控；CD2 异常时自动回退 115 API，目标派送仍走 115 秒传。
- 会员绑定码：管理员可生成月度（30 天）、季度（90 天）或年度（365 天）一次性会员码；用户向 Bot 发送 `/bind 绑定码` 后开通或顺延会员。
- 会员到期控制：管理端和小程序显示到期时间及剩余天数；到期后停止新建和执行自动派送，续费后恢复。
- 秒传记录：按资源实时汇总所有用户的派送状态，可删除/隐藏历史记录而不删除 115 文件。
- 自动删除：可按派送成功时间延时删除，也可按文件进入监听目录的时间定时删除。仅在该资源的全部派送均已完成或取消后执行。
- 服务设置：派送并发、CK 检查周期、失败重试、最大尝试次数、默认搜索额度和日志保留。
- 运行记录：查看最近的扫描、用户、派送、绑定与清理事件。
- Telegram 小程序：用户可使用 115 App 扫码自动获取 CK，也可手工填写 Cookie；目标目录可直接浏览 115 网盘选择并自动回填 CID。
- 自定义接收：支持全部、关键词、分类、关键词与分类同时满足、TMDB 精确订阅、排除词和关闭接收；规则变更会立即取消尚未开始且不再匹配的任务。
- 分类落盘：启用层级目录后，自动在用户选择的接收目录下建立分类子目录；Bot 成功推送和小程序记录显示实际落盘位置。
- TMDB 海报推送：派送成功后按文件名中的 TMDB ID，或片名、年份和剧集信息匹配中文资料，发送海报、评分、主演、影音规格、IMDb 与剧情简介；查询失败自动降级为普通成功通知。
- 可视化选目录：管理端可浏览源 115、用户 115 和 CD2 目录，不必手工查找 CID；文本框仍保留用于高级配置和故障兜底。

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

1. Telegram Bot Token、小程序 HTTPS 公网地址和可选的 TMDB API Key。
2. 用于监听资源的源 115 Cookie。
3. 在“服务设置”选择 CD2 本地挂载或 CD2 API。API 模式可配置每个部署自己的主机/IP、端口、API Token 和 API 根目录。
4. 如果同时启用“CD2 元数据旁路”，API 根目录应填写 CD2 内实际网盘根路径（例如 `/miaochuang`），不要填写容器挂载路径或并不存在的 `/115open`。
4. 在“监听目录”使用文件夹选择器选择源 115 目录（自动回填 CID）；使用 CD2 时再选择对应的 CD2 目录。
5. 配置绑定码、用户、派送及秒传后删除规则，确认无误后开启“自动扫描与派送”。

CD2 本地模式由 `.env` 的 `COCKROACH_CD2_PATH` 指定宿主机目录，并只读映射到容器 `/cd2/miaochuang`。不同用户可填写自己的实际挂载路径，不要求使用 `/CloudNAS/miaochuang`。CD2 API 模式不依赖该宿主机路径，默认连接 gRPC 端口 `19798`，API Token 可在 CloudDrive2 中创建。

第一次成功扫描只建立历史基线，不会把目录里的所有旧资源批量派送。此后新出现、且达到稳定等待时间的视频资源才进入派送队列。

## 公网与 Telegram

使用 Caddy、Nginx、Traefik 等反向代理，把 HTTPS 域名转发到 `8790`。在 BotFather 中将小程序菜单地址设置为同一 HTTPS 地址。管理中心地址为 `https://你的域名/admin`。

管理员登录信息只保存在当前浏览器标签页的 `sessionStorage` 中，但管理中心仍必须使用 HTTPS。建议在反向代理中额外限制 `/admin` 的来源 IP。

同一个 Telegram Bot Token 同一时间只能由一个实例长轮询。迁移时应先停止旧服务，再启动本项目，避免两个实例抢占 Bot 更新。

## 数据、备份与迁移

SQLite 数据库保存在 Docker 命名卷 `cockroach-data` 的 `/data/cockroach-runner.db`。Cookie、Bot Token 与 TMDB API Key 使用 `COCKROACH_ENCRYPTION_KEY` 加密。备份时必须同时保存数据库和 `.env` 中的加密密钥；密钥丢失后，已有密文无法解密。

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
