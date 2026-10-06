# DuShengTV 求片服务

客户端使用现有 Telegram 登录与设备认证，热门推荐来自 MoviePilot 的 TMDB 与豆瓣接口。TMDB 卡片优先，豆瓣仅按“华语电影／国产剧”标签补充；重复卡片合并。标题、年份合并只用于热门卡片展示，不能作为订阅或入库身份。

详情按 TMDB／豆瓣原生 ID 获取，Emby 入库状态按对应用户可见内容、电影／剧集类型及 ProviderIds 核对。相同 ProviderId 有冲突时拒绝判定为同一作品；没有精确证据时不按标题猜测。已经入库时客户端显示立即播放，未入库才显示订阅。剧集订阅必须选择详情中存在的季，包括 `0` 特别篇，不能把未知季或特别篇静默换成第 1 季。

## 配置

复用 `config.json` 的 `moviepilot.url`、`username`、`password`、`access_token` 和 `lv`。地址由管理员配置，客户端不能指定 API 地址或获取该凭据。MoviePilot 点播 `status` 或豆瓣想看 `douban_status` 至少开启一个；`lv: "a"` 时仍只有白名单用户及管理员可求片。TG 账号到期、停用、未绑定、设备撤销都会拦截。

```json
"dushengtv": {
  "requests_enabled": true,
  "requests_daily_limit": 20
}
```

把这两个字段合并进现有 `dushengtv` 配置，不要覆盖现有登录域名、端口或服务器列表。一天内每个 TG 默认最多登记 20 部／季；重复查看、重复点击既有请求不额外占配额。这是 MP 自动订阅流程，不会触发旧的按下载大小积分扣费，也不修改 DoubanSync 插件想看用户列表。

## 服务器更新

```bash
cd /opt/Tgbot
git pull --ff-only origin master
docker compose up -d --build --no-deps embyboss
docker compose logs --tail=80 embyboss
```

启动会执行 `20261006_16` 迁移，新增 `tv_media_requests` 和 `tv_media_request_owners` 两张表。前者记录全局影片／季的订阅状态、MP 编号和明确错误码；后者记录 Telegram 归属。保留现有数据库持久化挂载。无需新增公网监听或把 MoviePilot 管理 API 暴露给客户端。

MP 接受订阅后，由 MP 自己的订阅搜索、下载、整理计划执行。Bot 不额外发起种子下载，也不主动发送聊天消息。测试全部使用隔离 SQLite 和本机假 MP／Emby HTTP 服务，没有向生产 MP 提交订阅。

## HTTP 契约

所有接口根路径 `/api/dushengtv/v1/requests`，请求需要已登记设备的 TG `Authorization: Bearer ...`。每次慢查询完成后重新检查会话／权限，订阅 POST 前再次检查。

| 接口 | 返回／用途 |
| --- | --- |
| `GET /catalog?type=movie&page=1` | `items, page, hasMore, warnings, enabled`；`type` 为 `movie` 或 `tv` |
| `GET /detail?key=tmdb:movie:123` | `item, library, subscription, subscriptions, canSubscribe` |
| `POST /subscribe` | 请求 `{key, season?}`；返回 `state, requestId, key, season, item, updatedAt, error, duplicate`；已入库返回 `state: "available"` 和 `library` |
| `GET /mine?page=1` | 当前 TG 的 `items, page, hasMore`，每页 30 条 |

规范媒体 key 为 `tmdb:movie:123`、`tmdb:tv:123`、`douban:movie:123`、`douban:tv:123`。

`item` 字段：`key, source, id, type, title, originalTitle, year, overview, poster, backdrop, rating, providerIds, seasons, genres`。`seasons` 项为 `{number, name, episodeCount}`。`poster/backdrop` 只允许 HTTPS 的 `image.tmdb.org` 和 `*.doubanio.com` 图片；未许可地址输出空字符串。客户端应以当前 TG／图片许可为范围转为自身的图片协议，不开放任意 URL 代理。

`library` 为 `{available, items:[{id,name,type,serverUrl}], serverUrls}`。`serverUrls` 是 Bot 配置的同一 Emby 服务别名；客户端映射已登录的本地连接后，仍需以当前 Emby 用户重新读取影片再播放。

`subscription` 是当前 TG 最近一条该作品记录，没有记录时 `{state:"none"}`；`subscriptions` 包含该作品的各季请求。状态为 `pending`（结果核对中）、`subscribed`（MP 已接受）、`failed`（明确拒绝，可重试）、`complete`（电影已在 Emby 可见）。剧集目录存在不代表整季齐全，因此不会仅凭 Series 条目自动宣布整季下载完成。`subscribed` 也不等于已经下载完成。

跨用户、跨进程以媒体 key＋季数的数据库主键防止重复请求。超时／断连可能发生在 MP 已接受之后，状态保留 `pending`；用户再次点击只核对 MP 相同 ProviderId 和季数，不能盲目重发。若 MP 始终没有该订阅，需要管理员检查 MP 任务／日志，再将确认未提交的该记录置为 `failed` 后允许重试；不能直接删除不确定状态记录。MP 明确拒绝、权限／参数错误和限流可以在修正后重试。

## 实现依据与验证

调用 MoviePilot 官方 `v2` 分支的 `/recommend/tmdb_movies`、`tmdb_tvs`、`douban_movies`、`douban_tvs`、`/media/{source:id}`、`/media/seasons` 和 `/subscribe/`。较新主分支的详情要求 `media_source + 数字 ID` 时，仅针对明确 422 的只读请求切换参数；订阅不会在未知失败后切换 API 或重复 POST。

```bash
python scripts/test_tv_requests.py -v
python scripts/test_dushengtv.py -q
```

覆盖准确 ProviderId、冲突 ID、季度与特别篇、跨用户共享去重、并发、超时不重复发起、明确失败重试、现有 MP 订阅、日限额、图片域名、热门源部分故障、真实 HTTP 登录刷新／订阅请求、Emby 用户范围以及 TG 会话／迁移回归。配置正确后，仍应在测试媒体上由用户实际点击一次订阅，验证其 MP 站点、下载器和自动整理配置；代码测试不会代替这些生产下载条件。
