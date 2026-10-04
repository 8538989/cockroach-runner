# 更新、备份与迁移

## 更新

```bash
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps
```

更新前建议暂停服务并备份数据库。

## 备份

必须同时保存：

- `/data/cockroach-runner.db`
- `.env` 中的 `COCKROACH_ENCRYPTION_KEY`
- 反向代理配置

数据库密文无法在丢失 Fernet 密钥后恢复。

## 回滚

1. 暂停服务。
2. 恢复对应版本代码或镜像。
3. 恢复部署前数据库备份。
4. 保留原 Fernet 密钥和激活码。
5. 启动后检查 `/healthz`、监控心跳和最新事件。
