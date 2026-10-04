# 更新 DuShengTV 的 Telegram 头像接口

此更新新增 `GET /api/dushengtv/v1/profile/avatar`。接口只返回当前已登录、已绑定且设备仍获准使用的 Telegram 用户头像，不接受其他用户 ID。Bot 使用现有 Pyrogram 连接读取头像，客户端和接口响应不会拿到 Bot token、Telegram file ID 或带凭据的文件地址。

无需修改 `config.json`、数据库或安装新依赖。TV 的 8840 监听器与 Bot 在同一个进程中；本仓库 Dockerfile 用 `COPY . .` 将代码放进镜像，因此拉取代码后要重新构建镜像，仅 `docker restart` 不会载入新源代码。

## 在现有服务器更新

进入服务器上已有的 Bot 仓库目录（包含 `docker-compose.yml`，实际目录以你的部署为准），执行：

```sh
git status --short
TV_AVATAR_BACKUP="../tgbot-avatar-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$TV_AVATAR_BACKUP"
git rev-parse HEAD > "$TV_AVATAR_BACKUP/previous-commit.txt"
cp -p config.json docker-compose.yml "$TV_AVATAR_BACKUP/"
docker image tag "$(docker inspect --format '{{.Image}}' embyboss)" sakura_embyboss:before-tv-avatar
git pull --ff-only origin master
docker compose build embyboss
docker compose up -d --no-deps --force-recreate embyboss
```

如果 `git status` 显示你改过的源代码，先保留这些改动，再合并更新；不要使用强制重置覆盖它们。以上命令只重建、替换 `embyboss`，现有 MySQL 容器、数据库目录与配置挂载继续使用原数据。Bot 会有一次短暂重启。

确认接口：

```sh
curl --fail http://127.0.0.1:8840/api/dushengtv/v1/config
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8840/api/dushengtv/v1/profile/avatar
curl -sS -o /dev/null -w '%{http_code}\n' https://tv-api.dusheng.lol/api/dushengtv/v1/profile/avatar
```

配置接口应成功；后两个头像请求没有携带用户登录凭据，应返回 **401**，这表示新接口已生效；若仍为 **404**，检查是否构建并替换了正确的 Bot 容器，以及反向代理是否指向该容器的 8840 端口。

然后重新打开新版 DuShengTV。已登录的账号也会自动取头像，无需重新授权；已运行的客户端会在后续会话检查中自动重试。头像不可见、用户未设头像或 Telegram 暂时不可用时，界面回退到默认图标，登录和播放不受影响。成功头像缓存约 10 分钟，之后自动检查更新。

## 回退

如果新容器启动异常，可恢复刚才保留的镜像，配置和数据库仍使用原挂载：

```sh
docker image tag sakura_embyboss:before-tv-avatar sakura_embyboss:local
docker compose up -d --no-deps --force-recreate --no-build embyboss
```

旧提交号保存在 `$TV_AVATAR_BACKUP/previous-commit.txt`。镜像回退后，先排查再重新构建，避免立即用新源码覆盖回退镜像。

## 离线验证

```sh
python scripts/test_dushengtv.py
python scripts/test_dushengtv_avatars.py
```

测试覆盖头像账户隔离、下载后撤销会话、无头像、并发请求合并、缓存更新、短暂故障回退及非图片数据拒绝，不会连接 Telegram 或发送消息。
