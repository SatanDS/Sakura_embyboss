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

现有配置缺失这两个字段时自动使用以上默认值，无须手动添加；要调整时仅合并对应字段，不要覆盖现有登录域名、端口或服务器列表。一天内每个 TG 默认最多登记 20 部／季；重复查看、重复点击既有请求不额外占配额。这是 MP 自动订阅流程，不会触发旧的按下载大小积分扣费，也不修改 DoubanSync 插件想看用户列表。

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
| `GET /catalog?type=movie&page=1&query=影片名称` | `items, page, hasMore, warnings, enabled`；`type` 为 `movie` 或 `tv`；省略 `query` 返回热门推荐 |
| `GET /detail?key=tmdb:movie:123` | `item, library, subscription, subscriptions, canSubscribe` |
| `POST /subscribe` | 请求 `{key, season?}`；返回 `state, requestId, key, season, item, updatedAt, error, duplicate`；已入库返回 `state: "available"` 和 `library` |
| `GET /mine?page=1&query=影片名称` | 当前 TG 的 `items, page, hasMore`，每页 30 条；可选 `query` 筛选标题或原名；已完整入库返回 `state: "complete"` |
| `GET /image?url=...` | 受认证的 TMDB／豆瓣图片字节；拒绝任意主机、重定向、HTML 和超过 10 MiB 的图片 |

规范媒体 key 为 `tmdb:movie:123`、`tmdb:tv:123`、`douban:movie:123`、`douban:tv:123`。

搜索词最多 128 字，拒绝控制字符；连续空白合并，空搜索返回原来的推荐／订阅列表。电影和电视剧搜索调用 MoviePilot `/media/search` 的 `type=media` 元数据搜索，再按作品类型筛选；不会发起资源下载搜索或创建订阅。结果按类型、关键词和页码缓存五分钟。MoviePilot 混合返回电影与电视剧，筛选后某页可能为空，但 `hasMore` 为真时客户端会继续加载。

“我的订阅”在数据库中按当前 Telegram 用户、标题或原名筛选后分页；`%`、`_` 当作普通文字。搜索不会改变订阅状态，也不能读取其他用户的请求。客户端支持输入防抖、回车、清除与切换分类保留关键词，并丢弃过时查询的响应。

`item` 字段：`key, source, id, type, title, originalTitle, year, overview, poster, backdrop, rating, providerIds, seasons, genres`。`seasons` 项为 `{number, name, episodeCount}`。`poster/backdrop` 只允许 HTTPS 的 `image.tmdb.org` 和 `*.doubanio.com` 图片；未许可地址输出空字符串。客户端应以当前 TG／图片许可为范围转为自身的图片协议，不开放任意 URL 代理。

图片链路参考 MoviePilot 前端 `src/utils/imageUtils.ts`、`MediaCard.vue` 和 `PersonCard.vue`：识别豆瓣演员 `avatar.normal`，TMDB 原图按海报 w500、背景 w1280、头像 w185 缩放；自定义 TMDB 图片域名只提取符合 `/t/p/...` 的不可变图片路径，统一映射到标准域名，不授予任意镜像 URL 的访问权限。Bot 经 MP `/system/img/0`（豆瓣）或 `/system/img/1`（TMDB）及其缓存获取图片，保留 MP 独立资源 Cookie；Cookie 过期通过只读 `/user/current` 刷新，MP 凭据不会下发客户端。

图片代理在下载前后检查 TG 账户和设备权限，同 URL 合并下载、最多六路并发，服务器图片缓存有字节上限。客户端海报、背景、演员头像按 TG 与标准 URL 持久缓存并建立索引，成功下载起保留 30 天；回到页面或重启不重复下载有效图片。过期重新获取成功后原子替换，临时失败保留旧图；手动清图片缓存也会清索引。旧 Bot 没有图片端点时客户端每分钟最多一次兼容探测，然后使用原有公开图片源。

更新后可在服务器只读检查实际 MP 图片认证与下载，不会创建订阅，也不会输出 Token：

```bash
cd /opt/Tgbot
docker compose exec -T embyboss python - < scripts/diagnose_request_images.py
```

报告只包含图片字段类型、公开图片域名、结果码、格式和字节数。本地没有生产 MP 配置时，离线回归不能替代这一步实际部署验证。

`library` 为 `{available, items:[{id,name,type,serverUrl}], serverUrls}`。`serverUrls` 是 Bot 配置的同一 Emby 服务别名；客户端映射已登录的本地连接后，仍需以当前 Emby 用户重新读取影片再播放。

`subscription` 是当前 TG 最近一条该作品记录，没有记录时 `{state:"none"}`；`subscriptions` 包含该作品的各季请求。状态为 `pending`（结果核对中）、`subscribed`（MP 已接受）、`failed`（明确拒绝，可重试）、`complete`（电影或订阅的完整季已在当前用户的 Emby 中入库）。0.0.26 客户端已经支持把 `complete` 显示为卡片右上角“已入库”，本次只需更新 Bot。

打开“我的订阅”或详情时自动核对：电影要求类型与所有共享 ProviderIds 一致，且 Emby 返回实际 `MediaSources.Path`；剧集要求 MP 的准确季总集数大于零、`/tmdb/{tmdbid}/{season}` 的完整去重分集清单与总数一致，再逐集验证当前用户的 Emby 可播放文件。不会把 Series 目录、缺集、虚拟待播项、重复版本或另一季当作整季入库；合并集使用 `IndexNumberEnd` 展开，特别篇季 0 按真实编号集合匹配。MP 的 `season_info[].episode_count` 表示季总集数，不以 Emby 的文件数量或已播集数猜测；订阅时已有较大的总集数也不会被后来较小的清单覆盖。

核对先合并当前页的 Emby 电影／剧集身份查询，最多三路源站请求。每页总预算六秒，慢源超时返回已确认的部分，列表仍可使用；同一 TG＋Emby 用户＋请求及影片元数据的核对结果短缓存 30 秒，MP 季元数据缓存五分钟。离开再进入页面会重新获取状态，短缓存到期后重查。Emby 明确删除、隐藏或变成缺集时当前账户显示“已订阅”；源站暂时故障保留该账户最后明确状态。共享数据库记录中的历史 `complete` 本身不证明另一个账户现在可见；没有该账户的入库证据时保守显示“已订阅”，也不会让一个用户的权限变化全局降级其他人的已完成任务。

无可验证 TMDB 映射、季总集数未知、分集资料不全或相互矛盾时不能可靠宣布整季完成，保持待核对状态。MP 仅显示下载完成也不等于已入库，必须通过上述 Emby 文件核对。详情的 Series 播放入口仍表示该剧已存在，不代表所有季完成；每条订阅按所选季独立显示状态。

跨用户、跨进程以媒体 key＋季数的数据库主键防止重复请求。超时／断连可能发生在 MP 已接受之后，状态保留 `pending`；用户再次点击只核对 MP 相同 ProviderId 和季数，不能盲目重发。若 MP 始终没有该订阅，需要管理员检查 MP 任务／日志，再将确认未提交的该记录置为 `failed` 后允许重试；不能直接删除不确定状态记录。MP 明确拒绝、权限／参数错误和限流可以在修正后重试。

## 实现依据与验证

调用 MoviePilot 官方 `v2` 分支的 `/recommend/tmdb_movies`、`tmdb_tvs`、`douban_movies`、`douban_tvs`、`/media/{source:id}`、`/media/seasons` 和 `/subscribe/`。较新主分支的详情要求 `media_source + 数字 ID` 时，仅针对明确 422 的只读请求切换参数；订阅不会在未知失败后切换 API 或重复 POST。

```bash
python scripts/test_tv_requests.py -v
python scripts/test_dushengtv.py -q
```

覆盖准确 ProviderId、冲突 ID、季度与特别篇、跨用户共享去重、并发、超时不重复发起、明确失败重试、现有 MP 订阅、日限额、图片域名、热门源部分故障、真实 HTTP 登录刷新／订阅请求、Emby 用户范围以及 TG 会话／迁移回归。配置正确后，仍应在测试媒体上由用户实际点击一次订阅，验证其 MP 站点、下载器和自动整理配置；代码测试不会代替这些生产下载条件。
