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

## CDN 弹幕源站复用 Bot 节点清单

同一台 Debian 12 主机上的弹幕 TLS 源站可复用 Bot「CDN 真实 IP」面板已经维护的节点，无需手动同步 Nginx allow 文件。现有数据是最外层 `config.trusted_proxy_cidrs`：Bot 将其保存在 `config.json`，不是单独的 SQL 表。仅所有者和管理员可在 Bot 私聊中修改；成功保存后，下一次请求立即使用最新清单。保存失败时恢复原清单。

Bot 新增只供同机代理使用的 `GET /emby/cdn_origin`。Nginx 的内部 `auth_request` 向 `http://127.0.0.1:8838/emby/cdn_origin` 请求，携带：

- `X-DuSheng-Line-Token`：与现有 `config.api.line_report_token` 相同的内部密钥。
- `X-Proxy-Peer-IP`：Nginx 的实际 TCP 连接来源 `$realip_remote_addr`，由 Nginx覆盖，不能采用客户端请求中的同名头或 `X-Forwarded-For`。

接口同时要求真实连接来自 loopback 和正确的内部密钥。命中当前节点 IP/CIDR 返回 204；未命中、空清单或无效清单返回 403；来源头缺失、重复或无效返回 400。IPv4、IPv6 和 IPv4-mapped IPv6 均支持。响应禁止缓存，不返回整个清单，也不向弹幕容器下发数据库凭据。原 `/emby/real_ip` 只解析真实用户 IP，不执行白名单拒绝，不能代替这个新接口。

确认现有 Bot 配置中的以下字段；编辑时保留 `api` 的其他设置及已有内部密钥，不要新建或替换整个配置文件：

```json
"api": {
  "status": true,
  "http_url": "127.0.0.1",
  "http_port": 8838,
  "line_report_token": "保留现有的至少32字符内部密钥"
}
```

此监听器的开关是 `api.status`，不是 `dushengtv.enabled`；端口是 `api.http_port`。首次启用或修改这些字段后重建/替换 Bot 容器。日后仅通过 Bot 面板添加、删除节点不需要重启 Bot、Nginx 或弹幕容器。手工编辑磁盘上的 `config.json` 不会自动更新运行中的 Bot，必须重启后才生效。

弹幕 TLS 源站的 Nginx 使用 host 网络，以 loopback 调用 Bot。不要把 8838 或认证子路径公开到 CDN，不缓存认证子请求；Bot 不可用时应拒绝回源。TLS 源站仍只开放弹幕业务接口并检查独立的弹幕凭据，节点白名单不能代替接口认证。Nginx 模板与配置命令见 danmu_api 的 CDN 源站部署说明。

清空 Bot 节点清单会立即拒绝所有弹幕 CDN 回源；Emby 现有的真实 IP 解析行为保持不变。Bot 调用同机弹幕服务仍使用 `http://127.0.0.1:9321`，不依赖公开 CDN 域名、TLS 源站或这个额外白名单。

## 离线验证与回退

```bash
python scripts/test_dushengtv_danmaku.py -v
python scripts/test_dushengtv.py -v
python -m unittest scripts.test_proxy_ip scripts.test_api_security scripts.test_real_ip_panel -v
```

测试不访问真实 Telegram、Emby、弹幕源或生产数据库。上线后使用已有 TG 登录和已登记设备的客户端选一部电影及一集明确季数的剧集，检查加载弹幕、暂停、快进及本地导入。修改 `.env` 将两项配置清空，再运行 `docker compose up -d --no-deps embyboss` 即可关闭远程弹幕；TG 登录和播放继续使用原有接口。
