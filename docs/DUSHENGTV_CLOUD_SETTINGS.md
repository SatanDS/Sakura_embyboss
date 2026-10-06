# DuShengTV 账号云端设置

客户端在「设置 → 备份与同步 → TG 账号云端设置」手动上传主题、音量、字幕、弹幕及播放习惯。新客户端首次登录同一 TG 账号时自动尝试恢复一次；之后可手动恢复。已有客户端升级后不会自动覆盖本地偏好，后续登录也不会重复恢复。网络失败可从设置页重试。

备份存放在 Bot 的 MySQL 数据库 `tv_cloud_settings` 表，每个 TG 账号一条，覆盖上传会增加版本号。客户端不需要配置 WebDAV。备份包含固定白名单内的可迁移设置，不含登录凭据、服务器令牌、设备标识、显卡绑定、代理、WebDAV 账号或本机文件路径。数据库的常规备份也会包含此表。

字幕翻译的提供商、API 地址、模型、目标语言、双语／仅译文模式、原文缩放可同步。API 密钥不能同步，也不能藏在 API 地址的查询参数或账号密码部分。隐藏媒体库使用本地服务器 ID，不能作为便携云端设置同步。

## Debian / Docker 更新

在现有 Bot 服务器执行：

```bash
cd /opt/Tgbot
git pull --ff-only origin master
docker compose build embyboss
docker compose up -d --no-deps --force-recreate embyboss
docker compose logs --tail=80 embyboss
```

Bot 启动时自动运行迁移 `20261005_15` 建立新表，保留现有数据库、账号或设备。沿用当前 `.env`、TV API 8840 端口和 CDN 入口。

启动后检查：

```bash
curl --fail http://127.0.0.1:8840/api/dushengtv/v1/config
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8840/api/dushengtv/v1/settings/cloud
```

第二条应为 `401`，表示私有接口已启用且需要登录。若为 `404`，Bot 容器还未运行新版。随后安装支持云端设置的客户端，上载一次，再用新客户端登录同一账号验证自动恢复。

## API

`GET /api/dushengtv/v1/settings/cloud` 读取当前账号备份。无备份返回 `{"available":false,"schema":1,"revision":0,"updatedAt":null}`；有备份时包含 `settings`、递增 `revision` 和 UTC `updatedAt`。

`POST /api/dushengtv/v1/settings/cloud` 接收 `{"schema":1,"settings":{...}}`，原子替换当前账号备份并返回保存结果。请求不得传入 TG ID。两种操作都要求仍有效的 Bot 绑定、会话及登记设备，禁止 CDN 缓存；请求最多 32 KiB，每个令牌每分钟最多读取 60 次、上传 10 次。

设置规则在 `bot/dushengtv/cloud-settings-schema.json`，与客户端同名规范一致。任何未知字段、凭据字段或无效值都会拒绝整份更新，保留旧备份。

兼容旧客户端的已废弃项 `danmakuArea`：上传时仍按原有数值范围 0.2–1 校验，通过后忽略，不保存或返回该项。已有备份读出时仅剔除此项，不改动数据库中的原记录、版本号或更新时间。实际弹幕显示区域继续由 `danmakuRows`（1–8 行）控制；只含废弃项的空备份不会覆盖现有设置。

离线验证：`python scripts/test_dushengtv.py`。涵盖跨账号隔离、同账号跨设备、数据库保存、设备撤销、类型／尺寸／限流和迁移重复执行。`scripts/fixtures/dushengtv-portable-settings-0.0.28.json` 由该版客户端的 `portableSettings(defaults)` 生成，用于完整 75 项默认设置的 HTTP 上传、跨设备读回及新旧客户端交替上传回归。
