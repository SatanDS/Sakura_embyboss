# DuShengTV Telegram 登录

已有部署新增 Telegram 头像接口的拉取、重建、验证与回退步骤见 [头像更新说明](DUSHENGTV_AVATARS.md)。

可选弹幕服务的配置、鉴权和更新步骤见 [弹幕接入说明](DUSHENGTV_DANMAKU.md)。

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
  "privacy_version": "2026-10-03",
  "server_urls": ["https://www.dusheng.xyz", "https://www.dusheng.lol"]
}
```

`server_urls` 是 TV 可连接的 Emby 地址白名单，未列入的地址会被拒绝。这里只列出当前 Bot 主 Emby 服务器的真实访问地址／线路别名，须共用同一套用户和登录凭据。Bot 从只读 Emby 认证数据库取得用户 token 的真实持有者，再使用现有顶层 `emby_url` 检查该账号及 token 是否仍可用，用户 ID 必须与 Telegram 绑定相符。这样服务端验证不会依赖公网线路的区域 DNS／CDN。请求只携带用户 token，不使用或下发 Emby 管理 API Key。验证成功后客户端继续使用原公网线路播放，不会收到来源地址。不同服务器如果使用不同用户 ID，需要另建绑定后才能开放。地址包含子路径时，填写完整基址。

升级代码不会覆盖现有 `config.json` 的白名单；已有部署需在 `dushengtv.server_urls` 中补上 `https://www.dusheng.lol` 并重启 Bot。

设备数量默认不限，可省略 `max_devices`，也可以填 `0`。旧配置中的 `"max_devices": 3` 仍会限制为 3 台；先更新并重建 Bot，再删除该配置或改为 `0` 并重启。Bot 启动保存配置时可能会补回 `"max_devices": 0`，仍表示不限。设备登记、签名验证和撤销功能继续生效。正整数配置保留原有上限语义。

`enabled` 默认关闭，独立于 `api.status` 和 `payments.enabled`。`http_port` 不可与 `api.http_port` 相同。`bot_username` 留空时，服务使用已启动 Bot 的官方用户名。

更新时先备份数据库和配置。Bot 启动自动运行 Alembic。`20261004_14` 新增 `tv_login_challenges`、`tv_devices`、`tv_sessions`、`tv_refresh_tokens`，`20261122_18` 为 `tv_sessions` 增加登录方式字段，并用 `tv_emby_owners` 永久保存独立 Emby 账号的内部编号。

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

## Emby 令牌身份验证

TV 验证与 Bot 现有线路验证共用只读认证数据库查询。Emby 4.9 不支持 `/Users/Me`；仅用客户端提交的 ID 请求 `/Users/{Id}` 也不能证明 token 属于该账号。因此先按 token 查询 `Tokens`／`Tokens_2` 的有效记录，必要时经 `users.db` 将内部数字 ID 转成公开 GUID，再请求来源端 `/Users/{已验证的 GUID}` 检查账号状态。

如果已为线路验证配置 `emby_auth_db_path` 并挂载目录，可直接复用，无需增加 TV 专用数据库。否则将 Emby 的完整配置目录只读挂载至 Bot 容器，例如：

```yaml
services:
  embyboss:
    volumes:
      - /opt/embyserver/config:/emby-auth:ro
```

主机路径须对应实际 Emby 配置目录。在 `config.json` **最外层**设置，而非 `dushengtv` 内：

```json
"emby_auth_db_path": "/emby-auth/data/authentication.db"
```

若挂载的已是 `data` 目录，则使用 `/emby-auth/authentication.db`。需要整个目录内的实时 SQLite 数据、WAL 文件及 `users.db`，不能使用过期副本。也支持既有环境变量 `EMBY_AUTH_DB_PATH`。修改挂载后需重新创建容器。

未配置返回 `EMBY_AUTH_NOT_CONFIGURED`；认证数据库缺失、不可读或不支持其结构时返回 `EMBY_AUTH_UNAVAILABLE`。无效、已撤销、归属不唯一或与 TG 绑定不符的 token 均拒绝授权，不会退回信任客户端自报身份。Bot 来源端访问失败返回 `EMBY_UNAVAILABLE`。

此修正保持 TV 接口协议不变，现有客户端无需重新安装。

## 登录和设备管理

### Emby 用户名密码登录

DuShengTV 也支持 `POST /api/dushengtv/v1/auth/emby/login`，请求体包含
`username`、`password` 和 `installationId`。网关会向配置的 Emby 服务验证凭据，
不会保存或记录密码。账号必须存在于 Bot 的 `emby` 表或无 Telegram 账号使用的
`emby2` 表，并且仍有有效权益。Telegram 绑定账号沿用原有账号键；`emby2` 账号
使用稳定的负数内部键，因此设备、云端设置、一起看、求片和媒体归属仍沿用现有
TV 会话协议。登录返回正常的访问令牌和刷新令牌，`/session` 返回
`authProvider: "emby"`，同时保留数字格式的 `user.telegramId` 兼容字段。

该接口按来源地址和用户名限流。客户端只能通过配置的 HTTPS 网关发送凭据，并应在
请求结束后立即丢弃密码。

1. TV 用户同意设备信息用途后发起授权，客户端生成 state 与 PKCE S256。
2. 系统浏览器打开独立 TV 授权页，展示与客户端一致的六位确认码。
3. 授权页提供 Telegram 应用直开链接和网页链接。进入 `@emby_dusheng_bot` 后，若出现「开始 / Start」需点击发送新的登录请求；如果只打开聊天而没有确认卡，使用授权页的「复制本次登录指令」，将完整指令粘贴至 Bot 私聊并发送。退出后重登必须使用新授权页的指令。`/start tvlogin_…` 只准备请求，用户核对确认码并点击「确认登录」后才批准。不会要求 Telegram 密码或短信验证码，也无需 BotFather 的网页 Login Widget 域名。
4. 后端按 Telegram 官方 Bot 更新中的数字用户 ID 检查 Emby 绑定、有效权益及禁用状态。客户端以 state 和 PKCE 消费一次性请求。
5. 客户端用本机 Ed25519 私钥签署 60 秒单次 nonce，登记设备后才能连接 Emby。未绑定、到期均拒绝；仅在配置正整数设备上限时检查数量。
6. 每次续期／心跳重新检查绑定及设备。访问令牌 15 分钟、会话最长 30 天，刷新令牌单次轮换，重放撤销该会话。

Bot `/tvdevices` 可查看及撤销本人的 TV 设备，即使新电脑因设备数量上限无法登录，也能先在 Bot 释放名额。客户端设备管理也可撤销。撤销后旧会话失效；重新使用必须再次通过 Telegram 确认。

TV 登出／撤销时立即清理画面中的服务器、媒体卡片、详情及观看记录；未登录不会展示示范片库。已存的本机数据按 Telegram 账号隔离，只有该账号重新登录后可见。

## 接口与验证

共同前缀 `/api/dushengtv/v1`，JSON 请求最大 32 KiB。除配置和授权 start/poll/cancel/authorize、session/refresh 外，使用 `Authorization: Bearer …`。错误格式 `{ "code": "…", "message": "…" }`，响应禁止缓存。

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| GET | `/config` | 公开 Bot 用户名、隐私版本 |
| POST | `/auth/telegram/start` | state、PKCE、installationId → 请求 ID、确认码、授权页 |
| POST | `/auth/emby/login` | Emby username、password、installationId → TV 访问／刷新令牌 |
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
| GET / POST | `/settings/cloud` | 读取／保存当前 TG 账号的可迁移设置备份 |
| POST | `/servers/authorize` | 白名单地址、Emby 用户 ID 与用户 token 对应校验 |
| POST | `/watch/rooms` | 以当前影片、版本和位置创建一起看房间 |
| POST | `/watch/rooms/{code}/join`、`/watch/rooms/{code}/leave` | 加入、退出或由房主解散 |
| WebSocket | `/watch/rooms/{code}/socket` | 用 TV Bearer 连接，收发状态快照、成员就绪状态、播放操作和时钟消息 |

`authProvider: telegram` 表示身份来自受信任的 Telegram Bot 更新，`authProvider: emby`
表示身份来自 Emby 用户名密码验证。两者都由服务端按绑定和权益校验，客户端不能自报
Telegram ID，也不声称使用 Telegram OIDC。身份确认与支付网页登录各用自己的协议和数据表。

账号云端设置的更新步骤与恢复行为见 [DUSHENGTV_CLOUD_SETTINGS.md](DUSHENGTV_CLOUD_SETTINGS.md)。

### 部署一起看

更新 Bot 和 `requirements.txt` 的依赖后重启现有 DuShengTV 独立监听器。房间保存在进程内，使用单个 TV 进程，无需数据库迁移。重启后正在观看的房间失效，房主可重新建房。客户端仅在本机使用各自的 Emby 凭据和线路取流。房间服务接收影片和版本 ID、播放位置和成员状态。

公网部署在既有 TV HTTPS 域名上使用同源 WSS。使用 Nginx 时，将 [TV 站点配置](../nginx/dushengtv.conf) 的 `map` 放在 `http` 上下文，并在 TV 接口 location 转发 WebSocket Upgrade，执行 `nginx -t` 后重载。既有 [Caddy 配置](../caddy/dushengtv.caddyfile) 的 `reverse_proxy` 支持 WebSocket。如有 CDN，启用 WebSocket 转发，并确保链路允许 `Authorization` 请求头、禁用房间路径缓存和访问日志。

部署后用两个已登记的 DuShengTV 账号连接同一服同一影片。房主建房，另一人输入八位房间码，验证两端成员就绪、房主开播、客人暂停与恢复，然后房主解散，双方应收到结束事件。断开房主连接会暂停房间，60 秒内可以重连，超时房间自动解散。

```sh
python -m pip install -r requirements-test.txt
python scripts/test_dushengtv.py -v
python scripts/test_vip_identity.py -v
```

#### 部署房间语音

房间语音使用单独的 LiveKit SFU。现有 `tv-api.dusheng.lol` 继续负责 TV 登录、播放同步和文字聊天，语音使用 `voice-api.dusheng.lol` 的 WSS 信令入口，媒体端口直接到 LiveKit 主机。

先把 `voice-api.dusheng.lol` 和 `turn-voice-api.dusheng.lol` 的 DNS A / AAAA 记录指向 LiveKit 主机。若前面使用 CDN，这两个记录应使用 DNS 直连，普通 HTTP CDN 不会转发 WebRTC UDP。

```sh
cp livekit.yaml.example livekit.yaml
# 编辑 livekit.yaml，替换 keys 下的 API key 和 secret，并将相同值写入 .env
# TGBOT_LIVEKIT_API_KEY / TGBOT_LIVEKIT_API_SECRET
docker compose config --quiet
docker compose --profile voice up -d livekit
docker compose logs --tail=100 livekit
```

LiveKit 证书目录由 `LIVEKIT_TURN_CERT_DIR` 指定，目录内需要有 `fullchain.pem` 和 `privkey.pem`。防火墙需要允许 `7881/tcp`、`3478/udp`、`5349/tcp` 和 `50000-50100/udp`。使用 Caddy 时导入 [LiveKit 站点模板](../caddy/dushengtv.caddyfile)；使用 Nginx 时执行 `nginx -t` 后重载 [TV 站点配置](../nginx/dushengtv.conf)。

桌面客户端不需要手动修改本地网络。UDP 被限制的网络会尝试 TCP 和 TURN/TLS 回退，服务器侧必须保留对应端口和有效证书。

测试使用隔离 SQLite 与伪造 Telegram 传输，覆盖真实授权服务、HTTP 路由、签名、状态／PKCE、消费／过期、设备上限、刷新重放、绑定变化、白名单及端口路由隔离。Emby 4.9 回归使用真实临时 `Tokens_2`、`users.db` 与本机 HTTP 来源，验证令牌归属、禁用／撤销、错误归属和部署错误；不再将 `/Users/Me` 当作可用接口。正式上线仍需在独立域名完成一次真实 Bot 点击授权和 Emby 连接。MySQL 的每用户行锁负责串行设备登记；SQLite 测试不证明 MySQL 并发行为。
