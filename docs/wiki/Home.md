# 蟑影递送 Wiki

蟑影递送是一个独立的多用户 115 自动派送项目，不依赖 DIAN115。仓库同时包含后端、Web 管理中心和 Telegram 小程序框架，使用 Docker Compose 即可整体部署。

## 导航

- [Docker 安装](Installation.md)
- [首次配置](Configuration.md)
- [小程序与接收规则](Mini-App.md)
- [更新、备份与迁移](Maintenance.md)
- [故障排查](Troubleshooting.md)

## 运行组成

- `companion/service.py`：监听、规则匹配、115 秒传、接力、清理、Bot 和 HTTP API。
- `companion/static/`：管理中心与 Telegram 小程序完整静态框架。
- `compose.yaml`：后端、小程序、管理中心和 CD2 挂载的一体化容器部署。
- `/data/cockroach-runner.db`：SQLite 数据库；CK、Bot Token、TMDB Key 和 CD2 Token 均加密保存。

## 激活要求

原版项目启动时必须提供 `COCKROACH_ACTIVATION_CODE`。请向 Evan 获取激活码，不要把激活码提交到公开仓库或截图公开。

> 这是公开源码项目，激活校验用于原版部署管理，并不能阻止第三方修改源代码移除校验。
