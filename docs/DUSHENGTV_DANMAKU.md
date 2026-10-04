# DuShengTV 弹幕服务

Bot 的 `POST /api/dushengtv/v1/providers/danmaku` 可代理 [SatanDS/danmu_api](https://github.com/SatanDS/danmu_api) 的专用接口。客户端仍只连接现有 TV 登录域名，弹幕服务地址和 token 只保存在 Bot 环境变量中，不需要新增公网域名或数据库表。

## 配置与更新

先部署包含 `POST /api/v1/dushengtv/danmaku` 的 danmu_api 版本；普通上游镜像只有弹弹play接口，不能替代这个版本。Docker 构建与私有监听示例见该仓库的 DuShengTV 部署说明。保留其缓存目录挂载，避免重建容器后丢失匹配缓存。

在 Bot 的 `.env` 中设置：

```dotenv
TGBOT_DANMU_API_URL=http://127.0.0.1:9321
TGBOT_DANMU_API_TOKEN=与弹幕服务TOKEN一致的独立随机令牌
```

Token 必须是 32–256 位的英文字母、数字、下划线或连字符，不可使用默认值 `87654321`。可执行 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成；将同一值分别写入 danmu_api 的 `TOKEN` 与 Bot 的 `TGBOT_DANMU_API_TOKEN`。

两项都留空时保留原来的“未配置弹幕供应商”提示。Bot 的 Compose 使用 host 网络；同机 danmu_api 可将容器端口发布到宿主机 `127.0.0.1:9321`。两个服务都使用同一 Docker 网络时，地址可改为 `http://danmu-api:9321`。远程服务使用 HTTPS；HTTP 只允许 loopback、RFC1918、IPv6 ULA 地址或单段 Docker 服务名。地址必须是服务根地址，不含 token、路径、查询参数或账号密码。

在服务器的 Bot 仓库目录、已部署的 `master` 分支中拉取代码，然后重建并替换 Bot 容器：

```bash
git pull --ff-only origin master
docker compose build embyboss
docker compose up -d --no-deps --force-recreate embyboss
```

仅重启旧容器不会更新代码或环境变量。TV 域名的 Nginx、CDN 等所有代理层需允许至少 75 秒来源响应时间；本仓库 Nginx TV 範本已设置为 75 秒。Bot 对单次弹幕请求最多等待 70 秒，弹幕服务应在 60 秒内结束，客户端使用 75 秒超时。Caddy 默认无响应头超时，无需修改本仓库範本。

如果使用 Nginx，在现有 `tv-api.dusheng.lol` 站点的 `/api/dushengtv/v1/` location 中将 `proxy_read_timeout` 改为 `75s`，保留实际证书路径和来源地址。仓库範本不会自动覆盖已安装的站点文件。检查通过后重载：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

若 Nginx 运行在容器中，将上述命令换成对该代理容器执行 `nginx -t`，成功后执行 `nginx -s reload`；若站点由 DuShengCDN 面板管理，应在其站点配置中调整来源超时并应用配置。

升级支持季数与影片类型的新 DuShengTV 客户端；旧客户端缺少季数时会提示更新，不会默认选第一季。没有匹配结果或弹幕服务暂时不可用时，仍可在播放器导入本地 XML / JSON 弹幕。

## 接口与鉴权

客户端请求继续使用 Telegram 会话 Bearer token。Bot 在请求弹幕服务前后均验证账号权益、TG/Emby 绑定、会话和已登记设备；每个 TG 账号每分钟最多请求 12 次。客户端提供的服务器地址和媒体 URL 不用于出站请求。

Bot 只向固定的 `/api/v1/dushengtv/danmaku` 发送 `Authorization: Bearer <TGBOT_DANMU_API_TOKEN>`。请求为 `{title,type,season,episode,year,providerIds}`，不携带 TG token、Emby token、TG ID 或设备信息。`title` 最多 256 字符，`type` 支持 `Movie`、`Episode`、`local`；剧集必须有季数和集数。季数允许 0–999，集数允许 0–99999，年份允许 1800–2200。0 值原样传递，由弹幕服务返回特殊集暂不支持自动匹配的提示。Provider ID 最多 16 项，值最多 128 字符。

供应商返回 `{available,comments,match?,message?}`；每条弹幕包含秒数 `time`、`mode`（1/4/5）、`color`（`#RRGGBB`）和最多 300 字符的 `text`。Bot 只返回这些已验证字段及有限的匹配信息，最多 50,000 条、12 MiB 的弹幕正文；来源响应上限 20 MiB。无匹配时返回 `available:false` 和可见提示。错误不会透传来源响应内容、URL 或 token；供应商的 401/403 转为 502，不会被客户端当成 Telegram 登录过期。超时为 504，供应商限流为 429。

Bot 不跟随重定向，也不使用进程的代理环境变量。若弹幕源需要代理，应在 danmu_api 的源配置中设置。专用 danmu_api 接口须避免使用共享 Bot 来源 IP 来记录用户匹配偏好；部署时关闭 `REMEMBER_LAST_SELECT`，防止服务端公共偏好影响用户匹配。

## 离线验证与回退

```bash
python scripts/test_dushengtv_danmaku.py -v
python scripts/test_dushengtv.py -v
```

测试不访问真实 Telegram、Emby、弹幕源或生产数据库。上线后使用已有 TG 登录和已登记设备的客户端选一部电影及一集明确季数的剧集，检查加载弹幕、暂停、快进及本地导入。修改 `.env` 将两项配置清空，再运行 `docker compose up -d --no-deps embyboss` 即可关闭远程弹幕；TG 登录和播放继续使用原有接口。
