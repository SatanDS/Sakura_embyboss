# DuShengTV Telegram 登录

TV 独立域名为 `https://tv-api.dusheng.lol`，Bot 为 `@emby_dusheng_bot`。

TV 使用独立 FastAPI 应用、监听端口、配置和令牌表；不会把接口注册到续费网站。

| 服务 | 外部入口 | Bot 本机端口 |
| --- | --- | --- |
| DuShengTV | `https://tv-api.dusheng.lol/api/dushengtv/v1/` | `127.0.0.1:8840` |
| 现有续费及其他 API | 保留已有网域与路径 | 原 `api.http_port`，通常为 `8838` |

两者仅共用现有 Bot 的 Telegram / Emby 账号绑定与会员权益数据。TV 不接收支付回调、支付令牌或支付登录请求；续费服务不提供 TV 登录端点。

## 开启服务

DNS A / AAAA 记录指向 Bot 服务器 IP，DNS 不指定端口。公网使用 HTTPS 443，再反代到本机 8840。无需公开 8840。

在现有 `config.json` 最外层新增以下字段，其余配置保留：

```json
"dushengtv": {
  "enabled": true,
  "public_url": "https://tv-api.dusheng.lol",
  "http_host": "127.0.0.1",
  "http_port": 8840,
  "bot_username": "emby_dusheng_bot",
  "max_devices": 3,
  "privacy_version": "2026-10-03",
  "server_urls": ["https://www.dusheng.xyz"]
}
```

`server_urls` 只列出当前 Bot 主 Emby 服务器的真实访问地址／线路别名。每个地址应能以用户 token 调用 `/Users/Me`，返回同一个绑定的 Emby 用户 ID。不会把 Emby 管理 API Key 发送给这些地址。不同服务器如果使用不同用户 ID，需要另建绑定后才能开放，不能把任意第三方地址加入列表。地址包含子路径时，填写完整基址。

`enabled` 默认关闭，独立于 `api.status` 和 `payments.enabled`。`http_port` 不可与 `api.http_port` 相同。`bot_username` 留空时，服务使用已启动 Bot 的官方用户名。

更新时先备份数据库和配置。Bot 启动自动运行 Alembic `20261004_14`，新增 `tv_login_challenges`、`tv_devices`、`tv_sessions`、`tv_refresh_tokens`。它接续已发布的 `20260929_13`，不依赖本机未发布的支付修改。

Docker Compose 使用项目现有 host network：

```sh
docker compose build embyboss
docker compose up -d --no-deps embyboss
curl --fail http://127.0.0.1:8840/api/dushengtv/v1/config
```

公开反代二选一：

- Caddy：将 [独立站点範本](../caddy/dushengtv.caddyfile) 加入负责公网 80/443 的 Caddy 配置，Caddy 自动申请 TLS 证书。
- Nginx：先为 `tv-api.dusheng.lol` 申请证书，再启用 [独立虚拟主机範本](../nginx/dushengtv.conf)。

现有项目的 Emby Caddy 网关可能仅监听 HTTP 18080；不要把标准 HTTPS 范本直接套进全局 `auto_https off` 的网关。若 TLS 由 CDN／上层代理终止，在该层创建独立 `tv-api.dusheng.lol` 站点，其来源路由仍指向 Bot 主机 8840，并仅开放 `/api/dushengtv/v1/*`。所有层均应关闭授权链接访问日志和缓存。

如果 DuShengCDN 节点在其他服务器上，直接回源 `http://Bot服务器IP:8840`，将 `dushengtv.http_host` 改成 `0.0.0.0`，并在 Bot 主机防火墙上只允许这些回源节点访问 8840。`127.0.0.1` 只能给同机代理访问。客户端仍填写 `https://tv-api.dusheng.lol`，不能填写 HTTP 来源地址。

检查：

```sh
curl --fail https://tv-api.dusheng.lol/api/dushengtv/v1/config
# 以下均应返回 404，确保 TV 主机没有支付或内部接口：
curl -o /dev/null -w '%{http_code}\n' https://tv-api.dusheng.lol/payments/shop
curl -o /dev/null -w '%{http_code}\n' https://tv-api.dusheng.lol/emby/line-report
```

客户端 `app-config.json` 的 `gatewayUrl` 为 `https://tv-api.dusheng.lol`，`botUsername` 为 `emby_dusheng_bot`。更改后需要重新打包客户端。

## 登录和设备管理

1. TV 用户同意设备信息用途后发起授权，客户端生成 state 与 PKCE S256。
2. 系统浏览器打开独立 TV 授权页，展示与客户端一致的六位确认码。
3. 打开 `@emby_dusheng_bot`；`/start tvlogin_…` 只准备请求，用户核对确认码并点击「确认登录」后才批准。不会要求 Telegram 密码或短信验证码，也无需 BotFather 的网页 Login Widget 域名。
4. 后端按 Telegram 官方 Bot 更新中的数字用户 ID 检查 Emby 绑定、有效权益及禁用状态。客户端以 state 和 PKCE 消费一次性请求。
5. 客户端用本机 Ed25519 私钥签署 60 秒单次 nonce，登记设备后才能连接 Emby。未绑定、到期、超过设备上限均拒绝。
6. 每次续期／心跳重新检查绑定及设备。访问令牌 15 分钟、会话最长 30 天，刷新令牌单次轮换，重放撤销该会话。

Bot `/tvdevices` 可查看及撤销本人的 TV 设备，即使新电脑因设备数量上限无法登录，也能先在 Bot 释放名额。客户端设备管理也可撤销。撤销后旧会话失效；重新使用必须再次通过 Telegram 确认。

TV 登出／撤销时立即清理画面中的服务器、媒体卡片、详情及观看记录；未登录不会展示示范片库。已存的本机数据按 Telegram 账号隔离，只有该账号重新登录后可见。

## 接口与验证

共同前缀 `/api/dushengtv/v1`，JSON 请求最大 32 KiB。除配置和授权 start/poll/cancel/authorize、session/refresh 外，使用 `Authorization: Bearer …`。错误格式 `{ "code": "…", "message": "…" }`，响应禁止缓存。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | `/config` | 公开 Bot 用户名、隐私版本 |
| POST | `/auth/telegram/start` | state、PKCE、installationId → 请求 ID、确认码、授权页 |
| GET | `/auth/telegram/authorize?request=…` | 短期授权页，仅跳转 Bot |
| POST | `/auth/telegram/poll` | challengeId、state、codeVerifier → pending / denied / unbound / authorized |
| POST | `/auth/telegram/cancel` | 同上，撤销尚未消费的请求 |
| GET | `/session` | TG 身份、绑定、设备状态 |
| POST | `/session/refresh` | refreshToken、installationId → 旋转凭据 |
| POST | `/session/logout` | 撤销会话 |
| POST | `/devices/challenge` | 申请签名 nonce |
| POST | `/devices/register` | 硬件哈希、公钥、签名与用途同意 |
| POST | `/devices/heartbeat` | 验证登记公钥和 nonce |
| GET / DELETE | `/devices` / `/devices/{id}` | 查看／撤销当前用户设备 |
| POST | `/servers/authorize` | 白名单地址、Emby 用户 ID 与用户 token 对应校验 |

`authProvider: telegram` 表示身份来自受信任的 Telegram Bot 更新，不是客户端自报的 Telegram ID，也不声称使用 Telegram OIDC。身份确认与支付网页登录各用自己的协议和数据表。

```sh
python -m pip install -r requirements-test.txt
python scripts/test_dushengtv.py -v
```

测试使用隔离 SQLite 与伪造 Telegram 传输，覆盖真实授权服务、HTTP 路由、签名、状态／PKCE、消费／过期、设备上限、刷新重放、绑定变化、白名单与端口路由隔离。正式上线仍需在独立域名完成一次真实 Bot 点击授权。MySQL 的每用户行锁负责串行设备登记；SQLite 测试不证明 MySQL 并发行为。
