# DuSheng Emby 管理 Bot 部署指南

本文件整理本專案在 Debian 12 上的完整部署流程：Telegram Bot/API、Emby、MySQL、Docker、Caddy 線路檢測，以及 DuShengCDN/NPM 前置代理。

> 將 <...> 換成自己的值。Token、API hash、Emby API key、資料庫密碼不要提交 Git 或貼到公開聊天。

## 1. 架構

~~~text
Telegram 使用者 -> Telegram -> embyboss

Emby 客戶端 -> DuShengCDN/NPM (HTTPS，保留 Host/認證標頭)
             -> Caddy :18080
                -> Bot API :8838/emby/line_report
                -> Emby :8096
~~~

本專案的 Docker Compose 使用 host network：

| 服務 | 預設位址 | 用途 |
| --- | --- | --- |
| Emby | 127.0.0.1:8096 | Emby 源站 |
| Bot API | 127.0.0.1:8838 | 內部 API、線路檢測 |
| Caddy | *:18080 | VIP/普通線路入口 |
| MySQL | 127.0.0.1:3306 | Bot 資料庫 |

8838 不應直接對公網開放；Caddy 必須使用 host network，才能以 127.0.0.1 呼叫 Bot 的內部端點。啟用付款網站時，只透過獨立支付網域反代 `/payments/*`，不要公開整個 Bot API，詳見第 17 節。

## 2. 申請 Telegram Bot 與 API

### 2.1 BotFather Token

1. Telegram 搜尋官方 @BotFather，發送 /newbot。
2. 輸入顯示名稱，再輸入以 bot 結尾的 username，例如 `my_emby_bot`。
3. 將 BotFather 回傳的 Token（類似 123456789:AA...）填入 config.json 的 bot_token。
4. bot_name 填 username，不要加 @。
5. 需要 Bot 讀取群組非命令訊息時，才在 BotFather 使用 /setprivacy → Disable。

### 2.2 Telegram API ID/API hash

這不是 BotFather Token，而是 Pyrogram 啟動所需的 MTProto 應用資料。

1. 開啟 https://my.telegram.org，以自己的 Telegram 帳號登入。
2. 進入 API development tools，建立應用程式。
3. 取得數字 api_id 和字串 api_hash。
4. 分別填入 owner_api 和 owner_hash。

不要把 api_hash 或 Bot Token 發到 GitHub、Issue 或群組。

### 2.3 取得 Telegram ID

- 個人 ID：私聊 @userinfobot 或可信的 ID 查詢 Bot。
- 群組 ID：將本 Bot 加入目標群並設為管理員，再用 ID 查詢工具，或把群組訊息轉發給查詢 Bot。
- 私密群組不需要把 @RawDataBot 拉進群；若禁止轉發，可暫時加入 @getidsbot 後移除。
- 超級群 ID 通常是 -100...，例如 -1001234567890。

把自己的數字 ID 填到 owner，群組 ID 填到 group 陣列。main_group、chanel 可填公開群/頻道 username（不要加 @），也可填完整 Telegram 邀請網址（例如 https://t.me/+AbCd...）；私密群不要把 -100... 當 username，-100... 只填到 group 陣列。

## 3. Emby API key

在 Emby Dashboard → Advanced → API Keys 建立 key，填入 emby_api。

emby_url 填 Bot 可直接連線的管理源站，例如 http://127.0.0.1:8096。它不是使用者公開網址；公開普通線路和 VIP 線路分別填 emby_line、emby_whitelist_line。

## 4. 安裝 Debian 12 與 Docker

確認環境：

~~~bash
cat /etc/os-release
uname -m
docker --version
docker compose version
~~~

未安裝 Docker 時：

~~~bash
apt-get update
apt-get install -y git docker.io docker-compose-plugin
systemctl enable --now docker
docker --version
docker compose version
~~~

## 5. 下載程式

~~~bash
mkdir -p /opt
git clone https://github.com/SatanDS/Sakura_embyboss.git /opt/Tgbot
cd /opt/Tgbot
git remote -v
~~~

如果使用自己的 fork：

~~~bash
git remote set-url origin https://github.com/<your-owner>/<your-repo>.git
~~~

auto_update.git_repo 只填 OWNER/REPO，例如 SatanDS/Sakura_embyboss，不要填完整 URL。

## 6. 設定 MySQL

~~~bash
cd /opt/Tgbot
cp .env.example .env
nano .env
~~~

至少填入：

~~~dotenv
MYSQL_IMAGE=mysql:5.7
MYSQL_ROOT_PASSWORD=<長隨機 root 密碼>
MYSQL_USER=dusheng
MYSQL_DATABASE=embyboss
MYSQL_PASSWORD=<長隨機資料庫密碼>
~~~

MYSQL_USER、MYSQL_DATABASE、MYSQL_PASSWORD 必須與 config.json 的 db_user、db_name、db_pwd 相同：

~~~bash
chmod 600 .env
~~~

## 7. 設定 config.json

~~~bash
cd /opt/Tgbot
cp config_example.json config.json
nano config.json
~~~

不要用以下片段覆蓋整份檔案；它只列出要修改的主要值，其他欄位保留 config_example.json：

~~~json
{
  "bot_name": "<Bot username，不含@>",
  "bot_token": "<BotFather token>",
  "owner_api": 12345678,
  "owner_hash": "<my.telegram.org 的 api_hash>",
  "owner": 123456789,
  "group": [-1001234567890],
  "main_group": "<群 username，不含@>",
  "chanel": "<頻道 username，不含@>",
  "emby_api": "<Emby API key>",
  "emby_url": "http://127.0.0.1:8096",
  "emby_line": "https://normal.example.com",
  "emby_whitelist_line": "https://vip.example.com",
  "db_host": "127.0.0.1",
  "db_user": "dusheng",
  "db_pwd": "<與 .env 的 MYSQL_PASSWORD 相同>",
  "db_name": "embyboss",
  "db_port": 3306,
  "db_is_docker": true,
  "db_docker_name": "mysql"
}
~~~

db_port: 3306 在 config.json 根層級，和 db_host、db_user、db_name 同一層，不是在 .env 裡。

確認 open 區塊：

~~~json
"open": {
  "stat": false,
  "checkin": true,
  "exchange": true,
  "whitelist": true,
  "use_whitelist_code": true,
  "invite": true,
  "invite_lv": "admin"
}
~~~

invite_lv：

- admin：只有 owner 和 admins 可兌換邀請。
- a、b、c、d：依既有 Emby 帳戶等級判斷。

線路和 API 建議：

~~~json
"line_filter_terminate_session": true,
"line_filter_block_user": false,
"api": {
  "status": true,
  "http_url": "127.0.0.1",
  "http_port": 8838,
  "allow_origins": ["*"],
  "line_report_token": "<與 Caddy/CDN 相同的 64 位 hex 隨機密鑰>",
  "api_key": "<另一組獨立的 64 位 hex 隨機密鑰>",
  "allow_legacy_bot_token": false
},
"ranks": {
  "logo": "DuSheng",
  "backdrop": false
}
~~~

ranks.logo 會決定新深連結、註冊碼、續期碼、白名單碼的前綴。設成 DuSheng 後新碼會以 DuSheng- 開頭；舊 Sakura- 碼仍可兌換。

新用户注册成功后的「用户须知」可以随时修改：管理员私聊 Bot 发送 `/config`，点击「注册须知 → 修改内容」，回复输入提示并发送完整的新文字。发送后会先预览，预览成功并保存后立即对后续注册生效，无需重启；「查看当前内容」可预览，「恢复默认」需再次确认。支持换行和 Bot 的 Markdown 格式，最多 4000 个字符（部分表情按两个字符计）；发送 `/cancel` 或超时不会保存。

自定义内容保存在 `config.json` 顶层的 `registration_notice`，重启和更新代码后仍会保留。旧配置缺少此字段，或设置为 `null` 时，继续发送默认须知。须知与「加入群组」按钮独立，修改文字不会修改群组地址，也不会批量修改已发送给老用户的消息。

白名單是隨訂閱期限的 VIP 權限，不是永久權限：必須 lv=a 且 ex 尚未到期。續期會延長 VIP 有效期，舊資料中沒有 ex 的永久白名單會被到期檢查降級。

~~~bash
chmod 600 config.json
python3 -m json.tool config.json >/dev/null && echo 'config.json JSON OK'
~~~

## 8. 啟動 MySQL 與 Bot

~~~bash
cd /opt/Tgbot
mkdir -p db db_backup log
docker compose config >/dev/null && echo 'Compose config OK'
docker compose up -d mysql
docker compose ps
~~~

首次初始化可能需幾十秒，確認：

~~~bash
docker exec -it mysql mysqladmin ping -h127.0.0.1 -uroot -p
~~~

看到 mysqld is alive 後：

~~~bash
docker compose build embyboss
docker compose up -d --force-recreate embyboss
docker compose ps
docker compose logs --tail=200 embyboss
~~~

正常日誌會有資料庫遷移完成、排程建立和 Uvicorn 8838 啟動。不要用 GitHub 範例檔覆蓋正式 config.json。

### 8.1 API 與播放列表保護升級

一般整合 API（`/user/*`、`/auth/login`、`/emby/webhook/*`）現在要求 `X-API-Key` 請求頭。請另行產生 32-byte 隨機密鑰填入 `api.api_key`，更新所有呼叫方與 Emby Webhook 的自訂請求頭；不可重用 Telegram Bot Token 或 `api.line_report_token`。未設定新密鑰時，一般 API 不接受存取；只使用線路檢查的部署可以保留 `api_key: null`。

舊版 `?token=<Bot Token>` 預設停用。無法立即更新自訂標頭的整合，可暫時明確設定 `api.allow_legacy_bot_token: true`，遷移完成後關閉；錯誤的 `X-API-Key` 不會降級改用 URL Token。Uvicorn 存取日誌與 Caddy 請求/debug 日誌已停用，Nginx 範例存取日誌省略查詢參數。啟用暫時相容模式時，前置代理也不得記錄帶 Token 的完整 URL。原有日誌可能仍含舊 Bot Token，應限制存取並在完成遷移後輪換 Bot Token。

`/auth/login` 驗證 Emby 帳號密碼，只回傳帳號 ID 與名稱，不簽發使用者 Session Token；它是受 API key 保護的整合介面。失敗現在使用正確的 HTTP 400/401/404/500 狀態。

播放列表保護必須同時升級 Bot 與代理模板。Caddy 現在於伺服器內部呼叫 `/emby/ban_playlist`，不再把用戶重新導向 localhost。Caddy/Nginx 會傳遞共享密鑰、原始 URI/方法與 Emby 用戶憑據；Bot 透過 Emby 驗證身份，任何偽造的 userId 都不能指定封禁對象。有效違規操作封禁後仍回傳 HTTP 403，表示原始操作已被阻擋；通知失敗不會撤銷已保存的封禁狀態。

Nginx 使用者需在 `/etc/nginx/emby-line-secret.conf` 設定 `set $dusheng_line_token "<同一個 line_report_token>";`、將檔案權限設為 600，並取消模板內對應 `include` 的註解。未設定密鑰時，播放列表與線路上報均拒絕處理。Nginx 的 mirror 線路上報仍是非同步，若需要在媒體傳輸前阻擋 VIP 線路請求，使用下方 Caddy `forward_auth` 模板。

### 8.2 凍結期與分區授權升級

啟動時的 Alembic 遷移 `20260909_04` 會新增可為空的 `emby.disabled_at`，不刪除既有資料。上線前先備份資料庫；新封禁帳戶從實際禁用時間起計算 `freeze_days`。舊封禁帳戶若沒有可靠的禁用時間，保持 `NULL` 並跳過自動刪除，需由管理員核對，不能把註冊時間或訂閱到期時間當成禁用時間。

沒有觀看記錄的新帳戶從註冊時間起計算活躍寬限期。分區撤權失敗會保留待處理記錄並重試；分區激活若已保存授權但 Emby 更新失敗，同一用戶可用同一碼重試，不會再次延長期限。請維持單一 Bot 執行個體，進程內的用戶鎖不能代替多實例協調。

`.dockerignore` 已排除正式配置、環境密鑰、Session、資料庫、備份和本機虛擬環境；既有鏡像不會因此自動移除舊檔案，升級時需要重新構建。Git 中繼資料保留供原有容器內更新流程使用。

### 8.3 離線回歸驗證

在已安裝 `requirements.txt` 的 Python 3.10 環境執行：

~~~bash
python -B scripts/run_offline_tests.py
~~~

此入口使用範例配置、停用自動遷移並阻止外部網路連線，不讀寫正式 `config.json`。交易與遷移測試使用臨時 SQLite；真實 Emby 註冊測試不執行。Caddy/Nginx 驗證需另外指定 `CADDY_BIN` 與 `NGINX_BIN`，再獨立執行 `python -B scripts/test_proxy_templates.py`，不要透過禁止連線的離線入口執行代理測試。

## 9. Caddy 線路檢測

caddy/caddyfile 使用 Caddy forward_auth：

1. Caddy 把 line、Host、原始 URI、Emby Authorization/Token 傳給 Bot 的 /emby/line_report。
2. Bot 檢查帳戶是否有 VIP 權限。
3. 允許時 Caddy 反代給 Emby；拒絕時回傳 HTTP 403，並依設定終止 session。

模板會檢查 Sessions/Playing、影片/音訊串流、HLS 及下載端點，避免只終止 session 後串流仍繼續。

### 9.0 Caddy/CDN 共享密鑰（CDN 沒有固定回源 IP 時必須設定）

`/emby/line_report` 不是公開 API。當 CDN 回源節點 IP 會變動時，不能只依靠 UFW 來源 IP 或 Host 判斷請求是否可信；必須讓 CDN、Caddy 和 Bot 共用同一串隨機密鑰。下列三處的值必須完全相同：

| 位置 | 配置名稱/標頭 | 作用 |
| --- | --- | --- |
| Bot `config.json` | `api.line_report_token` | 驗證 Caddy → Bot 的內部上報 |
| 源站 Caddy 環境 | `DUSHENGCDN_ORIGIN_TOKEN` | 驗證 CDN → Caddy 的回源請求 |
| CDN VIP 路由 | `X-DuSheng-Origin-Token` | CDN 回源時固定注入的請求頭 |

這是同一個 32-byte（64 位 hex）密鑰，不是三組不同密碼。Caddy 在 `18080` 入口驗證 `X-DuSheng-Origin-Token`；通過後只在本機呼叫 Bot，並把同一值改以 `X-DuSheng-Line-Token` 傳給 Bot。兩個內部標頭都會在轉發給 Emby 前移除。未設定或不匹配時應直接回傳 HTTP 403，Bot API 仍只監聽 `127.0.0.1:8838`。

1. 在源站產生密鑰。命令輸出的值只在受控終端短暫顯示；不要把真實值貼到聊天、截圖、Issue、README 或 Git：

~~~bash
install -d -m 700 /etc/dusheng
umask 077
openssl rand -hex 32
~~~

把輸出的 64 位 hex 值填入 `/opt/Tgbot/config.json` 的 `api.line_report_token`。不要使用 Bot Token、Emby API key 或資料庫密碼代替它。設定後檢查 JSON 和密鑰格式（只輸出 OK，不輸出密鑰）：

~~~bash
cd /opt/Tgbot
chmod 600 config.json
python3 -m json.tool config.json >/dev/null && echo 'config.json JSON OK'
python3 - <<'PY'
import json
c = json.load(open('config.json'))
t = c.get('api', {}).get('line_report_token', '')
ok = len(t) == 64 and all(ch in '0123456789abcdefABCDEF' for ch in t)
print('line_report_token:', 'OK' if ok else '缺失或格式錯誤')
PY
~~~

2. 建立只有 root 可讀的 Caddy 環境檔（把 `<TOKEN>` 換成同一個值；`=` 後不要加引號）：

~~~bash
printf 'DUSHENGCDN_ORIGIN_TOKEN=<TOKEN>\n' > /etc/dusheng/emby-line.env
chmod 600 /etc/dusheng/emby-line.env
awk -F= '$1 == "DUSHENGCDN_ORIGIN_TOKEN" { print "Caddy token length=" length($2) }' /etc/dusheng/emby-line.env
~~~

輸出長度應為 `64`。此檔案不應提交 Git。Caddy 的 `--env-file` 只在建立容器時讀取；修改它後必須依 9.2 的重建命令重新建立 `emby-line-gateway`，單純 `docker restart` 不會載入新值。

3. 在 DuShengCDN 的 VIP「網站/代理配置 → 自定義請求頭」新增一條固定回源標頭：

~~~text
X-DuSheng-Origin-Token: <同一個 TOKEN>
~~~

冒號後保留一個空格，值不要加引號。保存後一定要「發布配置」並等待所有 Agent 套用；在生成的 OpenResty 配置中應能看到該 `proxy_set_header`。標頭必須由 CDN 強制注入並覆蓋客戶端同名標頭，不能讓客戶端自行提供，也不要把它透傳給 Emby。VIP `/emby/*` 快取必須關閉，或至少按完整使用者 Token 隔離，避免快取命中繞過回源檢查。

此模板在 `18080` 入口統一檢查密鑰，因此凡是經由同一個 Caddy 實例的域名都必須由前置代理注入該標頭；若普通線路不經過這個 Caddy 入口，則不受此條件影響。不要為了讓登入成功而移除 `@origin_invalid`，應先檢查 CDN 是否已發布自定義請求頭。

如果公網仍回 403，請在實際 DuShengCDN Agent 節點以 root 檢查「生效中的」OpenResty 配置（不要只執行默認的 `openresty -T`；Agent 可能使用自己的 `-p/-c`）：

~~~bash
ROOT=/opt/dushengcdn-agent/data/etc/nginx
/usr/bin/openresty -T -p "$ROOT" -c "$ROOT/nginx.conf" 2>&1 |
  awk '/X-DuSheng-Origin-Token/ { print "active config header present" }'

awk '
/proxy_set_header[[:space:]]+X-DuSheng-Origin-Token/ {
  v=$0
  sub(/^.*X-DuSheng-Origin-Token[[:space:]]+/, "", v)
  sub(/;[[:space:]]*$/, "", v)
  gsub(/^"|"$/, "", v)
  print "line=" FNR, "value_len=" length(v)
}' "$ROOT/conf.d/dushengcdn_routes.conf"
~~~

正常应看到配置存在且 `value_len=64`；不要把命令输出中的密钥内容贴到聊天或工单。

### 9.1 設定 VIP/普通域名

~~~bash
cd /opt/Tgbot
cp -a caddy/caddyfile "caddy/caddyfile.bak.$(date +%Y%m%d%H%M%S)"
nano caddy/caddyfile
~~~

刪除最後的 localhost 測試 import，換成：

~~~caddyfile
import emby_local_config vip.example.com vip 18080 127.0.0.1:8096 127.0.0.1:8838
import emby_local_config normal.example.com normal 18080 127.0.0.1:8096 127.0.0.1:8838
~~~

參數順序：

~~~text
server_name    公開域名，必須與請求 Host 相同
line_name      上報給 Bot 的標籤，例如 vip、normal
caddy_port     Caddy 監聽埠，例如 18080
emby_upstream  Emby 源站，例如 127.0.0.1:8096
bot_upstream   Bot API，例如 127.0.0.1:8838
~~~

VIP host 必須和 config.json 的 emby_whitelist_line 相同；程式會忽略協定、尾斜線及大小寫：

~~~json
"emby_whitelist_line": "https://vip.example.com"
~~~

### 9.2 驗證與啟動 Caddy

~~~bash
cd /opt/Tgbot
mkdir -p caddy/data caddy/config

docker run --rm \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
~~~

看到 Valid configuration 後：

~~~bash
docker rm -f emby-line-gateway 2>/dev/null || true
docker run -d \
  --name emby-line-gateway \
  --restart unless-stopped \
  --network host \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  -v /opt/Tgbot/caddy/data:/data \
  -v /opt/Tgbot/caddy/config:/config \
  caddy:2-alpine \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile

docker ps --filter name=emby-line-gateway
docker logs --tail=100 emby-line-gateway
ss -lntp | grep ':18080'
~~~

本機 Host 分流測試：

~~~bash
# 沒有 CDN 密鑰時必須被拒絕
curl -sS -o /dev/null -w 'No token => HTTP %{http_code}\n' \
  -H 'Host: vip.example.com' \
  http://127.0.0.1:18080/emby/System/Info/Public

# 帶正確密鑰才會到 Emby
source /etc/dusheng/emby-line.env
curl -sS -o /dev/null -w 'VIP gateway => HTTP %{http_code}\n' \
  -H 'Host: vip.example.com' \
  -H "X-DuSheng-Origin-Token: $DUSHENGCDN_ORIGIN_TOKEN" \
  http://127.0.0.1:18080/emby/System/Info/Public

curl -sS -o /dev/null -w 'Normal gateway => HTTP %{http_code}\n' \
  -H 'Host: normal.example.com' \
  -H "X-DuSheng-Origin-Token: $DUSHENGCDN_ORIGIN_TOKEN" \
  http://127.0.0.1:18080/emby/System/Info/Public
~~~

第一個應得到 403，後兩個才應得到 200。修改 Caddyfile 後先 validate，再重啟 Caddy：

~~~bash
docker restart emby-line-gateway
~~~

如果修改了 `/etc/dusheng/emby-line.env` 裡的密鑰，必須重建容器讓 Docker 重新讀取 `--env-file`；單純 `restart` 不會更新容器環境：

~~~bash
docker rm -f emby-line-gateway
docker run -d \
  --name emby-line-gateway \
  --restart unless-stopped \
  --network host \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  -v /opt/Tgbot/caddy/data:/data \
  -v /opt/Tgbot/caddy/config:/config \
  caddy:2-alpine \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
~~~

### 9.3 CDN 真实客户端 IP 与动态节点列表

管理员私聊 Bot 发送 `/config`，进入「CDN 真实 IP」，可查看、添加、删除节点，或确认清空全部节点。支持 IPv4、IPv6 和 CIDR，多个地址用换行、空格或逗号分隔，最多保存 128 项。只填写你控制的实际回源代理，不填写用户 IP；不接受域名、端口或覆盖全部地址的 `/0` 网段。删除时使用列表中完整的 IP/CIDR。

节点列表保存在 `config.json` 顶层 `trusted_proxy_cidrs`。旧配置默认 `[]`：继续使用连接来源地址，不信任转发标头。清空/删除节点只改变 IP 识别，不是封禁或防火墙操作；未配置节点仍可按原 VIP/普通线路权限访问。新请求立即读取最新列表，无需重启 Bot 或 Caddy，已建立的连接不会被主动切断。

新版链路：

~~~text
客户端 → CDN（追加实际来源到 X-Forwarded-For）→ Caddy
  → 本机 Bot /emby/real_ip（验证代理链并返回单个 IP）
  → 原 VIP/播放列表鉴权 → Emby

普通用户直接访问 8096 → Emby（不经过上述代理链）
~~~

Caddy 先校验现有 `X-DuSheng-Origin-Token`，再用本机调用与内部令牌请求 `/emby/real_ip`，把实际 TCP 对端和原始 XFF 传给 Bot。Bot 仅在直接对端可信时，从右向左跳过已知代理，停在第一个不可信地址；客户端在 XFF 左侧插入的假地址不能覆盖它。无有效代理链、格式无效或配置为空时退回连接来源 IP。解析不访问数据库、Telegram 或 Emby，转换后的 IP 仅用于转发，不替代播放令牌或 VIP 权益判断。

**首次启用需要同时更新 Bot 和 Caddyfile。** 仅增加名单而没有更新 Caddy 不会生效。每个经 Caddy 的 Emby 请求新增一次本机 IP 查询；Bot API 不可用时，该代理路径也会失败，不能退回信任用户提供的 IP。直接 8096 不依赖此查询，支付站点也不使用此配置片段。

已有服务器按以下顺序更新。备份原文件后拉取，先启动新 Bot，确认新增内部路由存在，再验证并重启 Caddy。不要把正式配置替换成示例文件，也不要丢掉自己的 Emby 域名和支付站点。此操作不重启 Emby/MySQL、不更改 8096 监听或防火墙。

~~~bash
(
set -eu
cd /opt/Tgbot
umask 077
ip_backup="/opt/tgbot-realip-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$ip_backup"
cp -a config.json docker-compose.yml "$ip_backup/"
cp -a caddy/caddyfile "$ip_backup/Caddyfile"
git pull --ff-only --autostash origin master
test -z "$(git diff --name-only --diff-filter=U)"
python3 -m json.tool config.json >/dev/null
docker compose config --quiet
docker compose build embyboss
docker compose up -d --no-deps --no-build --force-recreate embyboss

ip_api_ready=0
for attempt in $(seq 1 30); do
  status="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:8838/emby/real_ip || true)"
  if [ "$status" = 403 ]; then ip_api_ready=1; break; fi
  sleep 2
done
test "$ip_api_ready" -eq 1
# 未带内部令牌返回 403 是预期，404 则表示仍在运行旧版本。
gateway_image="$(docker inspect -f '{{.Image}}' emby-line-gateway)"
docker run --rm --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  "$gateway_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker restart emby-line-gateway
host_hash="$(sha256sum caddy/caddyfile | awk '{print $1}')"
container_hash="$(docker exec emby-line-gateway sha256sum /etc/caddy/Caddyfile | awk '{print $1}')"
test "$host_hash" = "$container_hash"
echo "REAL_IP_GATEWAY_READY；原配置备份：$ip_backup"
)
~~~

随后在「CDN 真实 IP → 添加节点」填入实际回源 IP（包括备用节点）。若链路有 NPM、其他反向代理或多级 CDN，要把可信代理各跳的实际来源地址一起加入。节点经 NAT 回源时，填 Caddy 实际收到的出口地址；不要单凭域名 DNS 解析出的入口 IP 推断回源地址。每个可信代理必须正确追加上一跳的真实连接地址，不能用客户端自报地址覆盖整条链。

DuShengCDN 当前渲染 `X-Real-IP: $remote_addr` 和 `X-Forwarded-For: $proxy_add_x_forwarded_for`。本方案依据 XFF 链识别，最终只把确认后的单个 IP 写入发往 Emby 的 XFF 和 X-Real-IP。Caddy 会覆盖外部伪造的 `X-Verified-Client-IP`、`X-Proxy-Peer-IP` 等内部字段，并在转发 Emby 前删除它们及内部令牌。不要在 CDN 自定义标头里另行写死这些字段。

Emby 的代理标头设置因版本而异，可能显示为 `ProxyHeaderMode`，不一定有名为「已知代理」的地址框。先保持原网络配置并重新登录测试；如果仍记录代理地址，再核查该版本如何接受代理标头。靶机 Emby 4.10.0.40 在现有 `ProxyHeaderMode=AllAddresses` 配置下确认可记录转发 IP，普通直接登录也成功；这不是要求业务服务器改成信任所有来源。直接开放的 8096 不经过 Caddy 的标头清理，直接请求的防伪取决于 Emby 自身代理策略，不能拿这次代理链测试代替验证。**不要把可信 CDN 列表填进「远程访问 IP 允许列表」或「局域网网段」，也不要为此把 8096 改成仅监听 127.0.0.1。**

验收时分别测试：手机流量经 VIP 域名登录/播放、普通用户直接 8096 登录/播放、名单添加和删除后的新请求，以及伪造 XFF/真实 IP 标头无法改变记录。既有 VIP 拦截和有/无 `/emby` 前缀、Range、HLS 也需保持正常。Bot `/userip` 读的是 Emby 播放历史，应播放后再查；旧节点 IP 历史不会自动改写，VPN 用户显示的仍是 VPN 公网出口。

本地回归可用 `CADDY_BIN` 指定 Caddy 可执行文件，然后运行 `scripts/test_real_ip_proxy.py` 和 `scripts/test_proxy_templates.py`；运行 `scripts/run_offline_tests.py` 覆盖节点解析、管理权限及 API 鉴权。

## 10. DuShengCDN、NPM、DNS 與防火牆

VIP 網域使用自己的CDN时比如 DuShengCDN/自建權威 DNS。CDN 站點設定：

1. DNS 指向 DuShengCDN 入口/邊緣位址。
2. CDN 源站填伺服器 IP，源站 port 填 18080。
3. 源站協定使用 HTTP（TLS 在 CDN/NPM 終止）。
4. **保留原始 Host `vip.example.com`**，不可改成源站 IP，否則 Caddy 無法匹配 VIP。
5. 在「自定義請求頭」固定加入 `X-DuSheng-Origin-Token: <TOKEN>`；同時轉發 X-Emby-Authorization、X-Emby-Token、Authorization、Range，啟用 WebSocket/長連線；不要快取登入、播放和 HLS。
6. 檢查 CDN 地區防火牆源站防火墙；CDN 和UFW 自己回的 403 不會上報 Bot。

如果 NPM 與 Caddy 在同一台主機，NPM 容器內不要填 127.0.0.1:18080；要填可達的主機 IP/host gateway 和 18080。NPM 公開 HTTPS 再轉到 Caddy 的 HTTP 18080。

~~~bash
dig +short vip.example.com
curl -sk -o /dev/null -w 'VIP public => HTTP %{http_code}\n' \
  https://vip.example.com/emby/System/Info/Public
~~~

因 CDN 回源 IP 不固定，18080 需要對 CDN 節點開放；安全性由上面的共享密鑰提供。8838 不要開公網：

~~~bash
ufw allow 18080/tcp
ufw status
~~~

若將來 CDN 能提供固定且可信的回源 IP，可再疊加來源限制（不是共享密鑰的替代品）：

~~~bash
ufw allow from <CDN_IP> to any port 18080 proto tcp
~~~

## 11. 驗證 VIP 攔截

~~~bash
cd /opt/Tgbot
docker compose logs --tail=0 -f embyboss
~~~

普通用戶從 VIP 播放時，預期看到：

~~~text
线路权限违规(nginx): 用户 ... 通过 vip.example.com 使用白名单线路
成功终止会话: ...
GET /emby/line_report?... 403 Forbidden
~~~

過濾日誌：

~~~bash
docker compose logs --since=5m embyboss | \
  grep -Ei 'line_report|线路权限违规|成功终止|Missing user identity|403 Forbidden'
~~~

內部端點只能由本機且帶共享密鑰呼叫；測試需使用真實 Emby user ID 或認證標頭：

~~~bash
source /etc/dusheng/emby-line.env
curl -i -G 'http://127.0.0.1:8838/emby/line_report' \
  -H "X-DuSheng-Line-Token: $DUSHENGCDN_ORIGIN_TOKEN" \
  --data-urlencode 'line=vip' \
  --data-urlencode 'host=vip.example.com' \
  --data-urlencode 'userId=<EMBY_USER_ID>' \
  -H 'X-Emby-Token: <CLIENT_EMBY_TOKEN>'
~~~

line_report 回傳 403 對 Caddy 來說代表阻止原始播放請求，是預期行為。沒有共享密鑰、沒有有效 Emby Token 或身份不一致時都應是非 2xx（安全失敗）。

## 12. Bot 日常操作

- admins: [] 代表只有 owner 是管理員；不需要把 owner 再放入 admins。
- Owner 對 Bot 使用 /proadmin <TG_ID> 新增管理員，/revadmin <TG_ID> 移除。
- 管理員面板可設定邀請等級、建立註冊碼/續期碼/白名單碼、查看使用者。
- /prouser <TG_ID 或 username> 可把已有有效訂閱的 Emby 帳戶設成白名單；白名單會隨 ex 到期。
- 產生兌換碼格式：

~~~text
30 1 code F   一個 30 天註冊碼
30 1 link F   一條 30 天註冊深連結
90 2 code T   兩個 90 天續期碼
90 2 link T   兩條 90 天續期深連結
5 code W      五個白名單碼
5 link W      五條白名單深連結
~~~

link 是 Telegram 深連結，code 是純文字碼；F 註冊、T 續期、W 白名單。白名單碼沒有天數欄位，必須先有 Emby 帳戶和有效訂閱。

## 13. MoviePilot v2 豆瓣想看

Bot 現在支援讓已註冊的 Emby 使用者從 Telegram 提交豆瓣使用者 ID，並自動寫入 MoviePilot v2 的 `DoubanSync` 插件使用者列表。

### 13.1 MoviePilot 前置條件

1. 在 MoviePilot v2 安裝並啟用 `豆瓣想看/DoubanSync` 插件。
2. 確認插件的配置欄位為 `users`，內容是英文逗號分隔的數字 ID，並以英文逗號結尾，例如 `<DOUBAN_ID_1>,<DOUBAN_ID_2>,`（請替換成實際 ID）。Bot 自動提交時會保持此格式。
3. 在 `config.json` 的 `moviepilot` 中填寫 MoviePilot 地址、管理員使用者名稱和密碼。`status` 是點播開關，`douban_status` 是豆瓣想看獨立開關：

~~~json
"moviepilot": {
  "status": true,
  "douban_status": true,
  "url": "http://127.0.0.1:3000",
  "username": "<MoviePilot 管理員使用者名稱>",
  "password": "<MoviePilot 管理員密碼>",
  "access_token": null,
  "price": 1,
  "lv": "b"
}
~~~

用於 Bot 的 MoviePilot 帳號必須是管理員/超級使用者，因為插件配置接口需要管理權限。Bot 會在首次呼叫時登入並把短期 token 保存回 `config.json`，不要把該檔案提交 Git。

### 13.2 Telegram 操作

管理員在 **配置 → MoviePilot** 分別開啟「點播功能」和「豆瓣想看」後，使用者開啟 `/start` → **使用者功能** → **📚 豆瓣想看**，再提交純數字豆瓣 ID，或個人主頁連結 `https://www.douban.com/people/<ID>`。Bot 會：

- 先讀取完整的 `DoubanSync` 配置，只修改 `users`，保留 `cron`、通知、搜尋下載等其他欄位；
- 記錄 TG 使用者目前綁定的 ID，支援修改和解除綁定；
- 按插件原有定時任務同步豆瓣「想看」，不直接替使用者發送下載請求。

`moviepilot.lv` 為 `a` 時，豆瓣想看和點播都只允許 Bot 白名單使用者；為 `b` 時，有效的普通 Emby 使用者也可以使用。使用者必須已有有效 Emby 帳戶。點播才會按 `price` 扣除資源；豆瓣想看不計費、不讀取 `price`。

這裡的 `users` 是 DoubanSync 的全局列表，不是 MoviePilot 的使用者密碼。若多個 TG 使用者提交同一個豆瓣 ID，Bot 會保留該 ID，直到最後一個綁定者解除綁定。

### 13.3 更新部署

本次版本增加了資料庫表 `moviepilot_douban_users`，Bot 啟動時會自動執行 Alembic 遷移。更新伺服器：

~~~bash
cd /opt/Tgbot
git pull --ff-only --autostash origin master
docker compose build embyboss
docker compose up -d --force-recreate embyboss
docker compose logs --tail=200 embyboss
~~~

日誌看到 `資料庫遷移完成，當前已升級到最新版本` 後，再在 Telegram 測試綁定。若提示插件不存在，請先在 MoviePilot 啟用 `DoubanSync`，並確認 Bot 使用的是管理員帳號。

## 14. 更新、備份與 Token 更換

更新：

~~~bash
cd /opt/Tgbot
git status --short --branch
git pull --ff-only --autostash origin master
docker compose build embyboss
docker compose up -d --force-recreate embyboss
docker compose ps
~~~

config.json、.env、資料庫和 session 被 .gitignore 忽略；不要用範例檔覆蓋正式設定。若出現 Applied autostash，檢查 git status 和 git stash list。

備份：

~~~bash
cd /opt/Tgbot
mkdir -p db_backup
docker exec mysql mysqldump -uroot -p embyboss > "db_backup/embyboss-$(date +%Y%m%d-%H%M%S).sql"
cp -a config.json "db_backup/config-$(date +%Y%m%d-%H%M%S).json"
chmod 600 db_backup/*.sql db_backup/*.json
~~~

更換 Bot Token：

~~~bash
cd /opt/Tgbot
chmod 600 config.json
python3 -m json.tool config.json >/dev/null && echo 'JSON OK'
docker compose up -d --force-recreate embyboss
docker compose logs --since=2m embyboss
~~~

同一 Token 不可同時被其他程式、另一台伺服器或另一容器使用，否則會 Conflict、連線關閉或按鈕無反應。

## 15. 故障排查

### Telegram 按鈕沒有反應

~~~bash
cd /opt/Tgbot
docker compose logs --tail=0 -f embyboss
~~~

從 Telegram 發送 /start 並點擊一次。若 Bot 啟動與探針正常但完全沒有 callback 日誌，常見原因是 Telegram/DC 暫時故障或 Token 被其他程式使用；服務恢復後：

~~~bash
docker compose restart embyboss
~~~

若看到 Traceback、Exception、Conflict、callback 或 ERROR，保留完整日誌再排查，不要先刪資料庫。

### MySQL 連不上

~~~bash
docker compose ps -a
docker logs --tail=200 mysql
docker exec -it mysql mysqladmin ping -h127.0.0.1 -uroot -p
~~~

首次啟動要等待初始化；確認 .env 和 config.json 的資料庫使用者、資料庫名、密碼完全一致。初始化後不要任意更改 MYSQL_*，因為既有資料庫帳戶不會自動更新。

### VIP 公開網址 403

- System/Info/Public 就 403：檢查 DuShengCDN/NPM 地區規則、源站 port、TLS 協定和 Host。
- 只有普通用戶播放 VIP 會 403：這是預期的線路攔截。
- Missing user identity：前置代理刪了 Emby Authorization/Token、原始 query 或 Range，檢查轉發規則。
- Caddy/Bot 一直 403：確認 Caddy 用 --network host，Bot upstream 是 127.0.0.1:8838。

### Caddy 啟動失敗或端口被占用

~~~bash
docker logs --tail=200 emby-line-gateway
ss -lntp | grep -E ':(18080|8838|8096)\b'
docker run --rm \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
~~~

同一端口只能有一個代理程序；修改 Caddyfile 後先 validate，再 `docker restart emby-line-gateway`；修改密鑰環境檔後要依上面的指令重建容器。

## 16. 安全檢查

- config.json、.env、*.session 權限為 600，未提交 Git。
- 8838 只讓本機 Caddy 呼叫，不直接暴露公網。
- Caddy/CDN 保留 Host、Emby 認證標頭和 Range，不快取登入/串流。
- MySQL 使用強密碼，定期備份 db、db_backup、config.json。
- Bot Token 只在一個執行個體使用；更換後使用 --force-recreate。
- 每次更新後檢查 git status、容器狀態及日誌。

### 16.1 Emby 4.9 + SenPlayer identity mapping

Emby 4.9.5.0 does not provide a usable `/emby/Users/Me` route; `Me` is parsed as a GUID and returns `Unrecognized Guid format`. Never use a client-supplied `userId` as the identity source by calling `/emby/Users/{id}`: that endpoint does not bind the path ID to the token.

For SenPlayer, keep VIP enforcement fail-closed and mount Emby's authentication database read-only so the Bot can resolve the authoritative `Tokens(AccessToken, UserId, IsActive)` binding.

Find the host directory mounted as `/config` (do not print tokens):

~~~bash
docker inspect embyserver --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
~~~

Add the corresponding host directory to the `embyboss` service in `docker-compose.yml`:

~~~yaml
volumes:
  - /host/path/to/emby-config:/emby-auth:ro
~~~

Set the matching container path in `config.json`:

~~~json
"emby_auth_db_path": "/emby-auth/data/authentication.db"
~~~

If the mounted directory is already Emby's `data` directory, use `/emby-auth/authentication.db` instead. Mount the directory containing SQLite `-wal` files; do not use a stale copied database.

Deploy:

~~~bash
cd /opt/Tgbot
chmod 600 config.json
python3 -m json.tool config.json >/dev/null && echo 'JSON OK'
docker compose build embyboss
docker compose up -d --force-recreate embyboss
docker compose logs --since=2m embyboss
~~~

If the database is not configured or cannot be read, VIP requests remain non-2xx and are blocked. Do not substitute the Bot API key or trust `userId`, `DeviceId`, or `SessionId` alone.

### 16.2 SenPlayer/Hills 現行部署與升級指南

本節名稱沿用最初排查時使用的客戶端。身份驗證與 VIP 權限檢查依據通訊協定，不針對客戶端名稱或 User-Agent 設定特殊規則；相同的令牌與請求頭規則也適用於其他 Emby 相容應用程式。協定測試通過，不代表已逐一實測所有客戶端和裝置。

本指南取代本節先前僅依賴 `Users/Me` 的操作說明。Emby 4.9.5.0 會將 `/emby/Users/Me` 中的 `Me` 當成 GUID，並回傳 `Unrecognized Guid format`。不要改用 `/emby/Users/{client_user_id}` 驗證身份，因為客戶端可以偽造路徑中的使用者 ID。Bot 會從 Emby 本機的 SQLite 資料驗證播放令牌，再以驗證得到的真實 Emby 使用者 ID 檢查 VIP 權限。

#### 首次設定

1. 找出掛載至 Emby `/config` 的主機目錄，以下指令只輸出路徑：

~~~bash
docker inspect embyserver --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
~~~

2. 修改前先備份配置檔案：

~~~bash
cd /opt/Tgbot
backup_stamp=$(date +%Y%m%d%H%M%S)
cp -a config.json "config.json.bak.$backup_stamp"
cp -a docker-compose.yml "docker-compose.yml.bak.$backup_stamp"
~~~

3. 將 Emby 配置目錄以唯讀方式掛載至 `embyboss` 服務。常見目錄配置如下：

~~~yaml
services:
  embyboss:
    volumes:
      - /opt/embyserver/config:/emby-auth:ro
~~~

請掛載整個目錄，不要只掛載 `authentication.db`，確保 SQLite 能讀取 `-wal` 檔案和 `users.db`。

4. 在 `config.json` 的**最外層**加入以下配置項，與 `emby_url`、`emby_line`、`emby_whitelist_line` 並列，不要放進 `api` 或 `moviepilot`：

~~~json
"emby_auth_db_path": "/emby-auth/data/authentication.db"
~~~

如果掛載的已經是 Emby 的 `data` 目錄，請改用 `/emby-auth/authentication.db`。不要將真實令牌、密碼、CDN 共享密鑰或認證資料庫提交至 Git。

5. 每個 Caddy `forward_auth` 區塊都必須包含以下請求頭轉發規則：

~~~caddyfile
header_up X-DuSheng-Line-Token {$DUSHENGCDN_ORIGIN_TOKEN}
header_up X-Emby-Authorization {header.X-Emby-Authorization}
header_up X-Emby-Token {header.X-Emby-Token}
header_up Authorization {header.Authorization}
~~~

不要在設定令牌的同一個 `forward_auth` 區塊中加入 `header_up -X-DuSheng-Line-Token`，無論放在哪個位置都不行；Caddy 執行請求頭刪除時可能移除剛設定的令牌，造成 `Invalid internal token`。但轉發至 Emby 的 `reverse_proxy` 應刪除這個內部線路令牌，避免傳給 Emby。請保留目前模板的媒體路徑比對規則：Emby 接受有或沒有 `/emby` 前綴的路徑，也支援不同大小寫、任意影片串流檔名、Range 分段請求、HLS、音訊、下載和直播。

6. 修改 Caddy 配置後，先驗證再重新載入：

~~~bash
docker run --rm \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  caddy:2-alpine \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker restart emby-line-gateway
~~~

7. 拉取程式碼並重新構建 Bot。目前同時支援 `Tokens` 和遷移後的 `Tokens_2` 資料表。當 `Tokens_2.UserId` 是內部數字編號時，會透過 `users.db/LocalUsersv2` 將它轉換成對外使用的 GUID：

~~~bash
cd /opt/Tgbot
git pull --ff-only --autostash
chmod 600 config.json
python3 -m json.tool config.json >/dev/null && echo 'JSON OK'
docker compose config >/dev/null && echo 'COMPOSE OK'
docker compose build embyboss
docker compose up -d --force-recreate embyboss
docker exec embyboss test -r /emby-auth/data/authentication.db && echo 'AUTH DB MOUNT OK'
~~~

8. 使用 VIP 帳戶透過 Hills、SenPlayer 或其他相容客戶端實際播放。成功檢查的訊息包含 `线路检查通过`／`Whitelist user`，播放請求回傳 `HTTP 200 OK`：

~~~bash
docker compose logs --since=2m --no-color embyboss | \
  grep -Ei 'line_report|reason=|Unable to authenticate|线路|403|401'
~~~

#### HLS 分片與正向驗證

部分 Emby 版本產生的 HLS 分片網址只包含 `PlaySessionId`，不再附帶播放令牌。Bot 只會在有效 VIP 的主播放列表或子播放列表通過驗證後，暫存該播放 ID 與已驗證使用者、令牌、線路主機及媒體 ID 的關聯。後續無令牌分片必須符合這個關聯，且每次都重新驗證令牌與 VIP 有效期；不能只憑任意 `PlaySessionId` 放行。

此關聯只保存在單一 Bot 程序的記憶體中，最多保留 2048 筆，閒置一小時後失效，正常分片請求會延長閒置期限。Bot 重新啟動、關聯被淘汰或長時間暫停後，客戶端需要重新發起播放，讓帶令牌的播放列表請求建立關聯。不要在多個 Bot 實例之間隨機分配同一段播放的請求。`PlaySessionId` 與令牌都屬於敏感播放資訊，不要對外分享或記錄完整網址。

發布前的正向測試會比較源站與 VIP 閘道有、無 `/emby` 前綴時的影片、音訊、圖片、SRT／VTT 字幕、媒體資訊、Range 分段內容，以及完整 HLS 播放列表和分片。測試使用 `requirements-test.txt` 中的 HLS 解析套件；該套件不屬於正式 Bot 執行依賴。測試通過不代表每一款客戶端或每一種編碼都已實機驗證。

#### Emby 升級或重新啟動

正常重新啟動 Emby 不會隨機改動 SQLite 資料表名稱。`Tokens_2` 通常來自資料庫遷移或資料表重建，重新啟動後仍會保留。未來升級 Emby 時，資料表名稱、欄位、資料庫路徑或內部使用者 ID 的儲存格式仍可能改變。

升級前，請短暫停止 Emby 並備份完整配置目錄，或使用能保證資料庫一致性的備份方式。若存在 `authentication.db`、`authentication.db-wal`、`users.db` 和 `users.db-wal`，都必須納入備份；在 SQLite 運作時分別複製這些檔案，不能保證備份一致。升級後可用以下指令檢查資料表名稱，不輸出表內資料：

~~~bash
docker exec embyboss python3 -c 'import sqlite3;d=sqlite3.connect("file:/emby-auth/data/authentication.db?mode=ro",uri=True);print("tables:",[x[0] for x in d.execute("SELECT name FROM sqlite_master WHERE type=\"table\" ORDER BY name")]);d.close()'
~~~

如果新版 Emby 採用尚未支援的資料庫結構，VIP 請求會回傳非 2xx 狀態並拒絕放行，不會改為信任 URL 中的 `userId`。不要手動重新命名或修改認證資料庫；應先確認新結構，再新增相容支援。

#### 故障排查

- `Unrecognized Guid format`：舊版本仍在使用 `Users/Me`；請拉取並重新構建目前版本的 Bot。
- 顯示 `Tokens table: False`，但存在 `Tokens_2`：資料庫已使用遷移後的結構；請使用支援 `Tokens_2` 的版本。
- `reason=Emby token is not bound to one active user`：確認整個 Emby 配置目錄已唯讀掛載，包含 WAL 檔案，且 `users.db` 存在。
- `reason=userId claim does not match authenticated Emby user`：這是舊版本的行為；目前程式碼會忽略客戶端過期的 `userId`，改用已驗證令牌的真正持有者。
- `Invalid internal token`：驗證配置並重新啟動 `emby-line-gateway`，確認 `forward_auth` 沒有刪除已設定的 `X-DuSheng-Line-Token`。
- Hills／SenPlayer 回傳 502／403，但 Infuse 正常：確認 Caddy 有轉發 `Authorization`、`X-Emby-Authorization` 和 `X-Emby-Token`，並檢查 Bot 日誌是否出現 `Unable to authenticate`。
- Bot 重新啟動後立即出現 HTTP 502：等本機 API 完成資料庫遷移並開始監聽，再測試播放。若持續失敗，檢查 Bot 程序和 Caddy 上游位址。HTTP 503 搭配 `Unable to verify line entitlement` 表示 Bot 無法查詢 MySQL；此時只拒絕目前請求，不封禁帳戶，也不終止會話。
- 某種憑據格式回傳 401：修改閘道前，先用相同請求直連本機 Emby 源站比對。不同 Emby 版本接受的 URL 憑據欄位可能不同；閘道不能將源站不接受的憑據視為已驗證身份。

尋求協助時，只提供已遮蔽敏感資訊的指令輸出，不要傳送 `config.json`、Bot／CDN 令牌、密碼或資料庫內容。

## 17. Stripe 付款网站部署与维护

本节按「收款资格 → 备份更新 → 测试配置 → 网站代理 → 登录上架 → 测试验收 → 正式切换」执行。已经完成的配置不需要重新生成密钥或重复创建 Webhook；日常更新看 17.9，报错看 17.11。

### 17.1 部署前确认

适用于第 1～9 节已部署的单实例 Bot、MySQL 和 Caddy。以下命令在实际业务服务器的 Bash 中执行，不是在 Windows、CDN 边缘节点或另一台靶机中执行。

| 项目 | 本节约定 |
| --- | --- |
| 项目目录 | `/opt/Tgbot`，注意大小写 |
| Bot 服务与容器 | Compose 服务 `embyboss`，容器 `embyboss` |
| MySQL 容器 | `mysql`，数据库名读取容器内的 `MYSQL_DATABASE` |
| Bot API | `127.0.0.1:8838`，不开放公网端口 |
| Caddy 容器与入口 | `emby-line-gateway`，host 网络，HTTP `18080` |
| Caddy 配置与环境文件 | `/opt/Tgbot/caddy/caddyfile`、`/etc/dusheng/emby-line.env` |
| 支付域名示例 | `pay.example.com`，全文替换成实际域名；当前部署为 `xf.dusheng.lol` |

Stripe 设置入口为 [正式付款方式](https://dashboard.stripe.com/settings/payment_methods) 和 [测试付款方式](https://dashboard.stripe.com/test/settings/payment_methods)。也可从 Dashboard 的「设置 → 支付 → 付款方式」进入；先选对商户账户及正式/测试环境。

**当前代码同时指定支付宝 `alipay` 和微信支付 `wechat_pay`，没有只启用其中一种的配置开关。** 正式收款前必须确认这两项均获批并可用于当前商户的 CNY Checkout。支付宝审核未通过、微信仍在审批时，先保持停售；仅换正式密钥或重启无法解决商户能力限制。测试环境能用，不代表正式环境获批。

本项目使用 Stripe 托管 Checkout，CNY 单次付款，每单一份商品、一张码，不自动续费。没有个人收款码回调或人工确认收款功能，用户截图、点击「已付款」和成功页都不能代替服务端到账验证。Telegram 内数字服务遵循 Stars 规则，本节部署的是独立网站。

### 17.2 备份与拉取代码

首次安装先完成前面的基础部署；以下是已有服务器的更新步骤。备份保存在仓库外，不复制正在运行的 MySQL 数据目录。备份失败、Git 冲突或构建失败时，此命令块会停止。

~~~bash
(
set -eu
cd /opt/Tgbot
umask 077
payment_backup="/opt/tgbot-payment-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$payment_backup"
cp -a config.json docker-compose.yml .dockerignore "$payment_backup/"
cp -a caddy/caddyfile "$payment_backup/Caddyfile"
if [ -f .env ]; then cp -a .env "$payment_backup/"; fi
if [ -f /etc/dusheng/emby-line.env ]; then
  cp -a /etc/dusheng/emby-line.env "$payment_backup/emby-line.env"
fi
git rev-parse HEAD > "$payment_backup/previous-commit.txt"
docker image tag "$(docker inspect -f '{{.Image}}' embyboss)" \
  "embyboss:backup-$(basename "$payment_backup")"
docker exec mysql sh -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --events --databases "$MYSQL_DATABASE"' \
  > "$payment_backup/database.sql"
test -s "$payment_backup/database.sql"
echo "备份目录：$payment_backup"

git status --short --branch
git pull --ff-only --autostash origin master
test -z "$(git diff --name-only --diff-filter=U)"
git log -1 --oneline
python3 -m json.tool config.json >/dev/null
docker compose config --quiet
docker compose build embyboss
)
~~~

`--autostash` 会尝试合并本机配置；出现冲突时必须核对备份、Git 差异和 stash，不要反复 `stash apply`，不要用示例配置覆盖正式文件。保留服务器的域名、端口、上游，以及第 16.2 节的 Emby 认证目录只读挂载。构建时保留 `.dockerignore` 对 `.env`、`config.json`、数据库和 Telegram session 的排除规则。

### 17.3 测试密钥与配置

测试推荐使用独立数据库、Emby 和测试 Bot。若在现有业务库验证支付，只允许指定测试账号下单；不要拿测试码给真实用户开通或续期。`test/live` 标记只能隔离兑换环境，不能把已经写入业务库的测试权益自动撤销。

在 Stripe 测试环境取得以下密钥。不要把公钥填入后端密钥字段。

| 变量 | 内容 | 保存位置 |
| --- | --- | --- |
| `TGBOT_STRIPE_SECRET_KEY` | 测试 Secret key，`sk_test_...`；不是 `pk_test_...` 公钥 | `/opt/Tgbot/.env` |
| `TGBOT_STRIPE_WEBHOOK_SECRET` | 对应 Webhook 端点的签名密钥，`whsec_...` | 同上 |
| `TGBOT_PAYMENT_CODE_KEY` | 随机 32 字节的 Base64 URL 安全编码，用于加密兑换码 | 同上 |

在测试环境的「Workbench/开发者 → Webhooks（事件目标）」创建一次端点，URL 使用实际支付域名：

~~~text
https://pay.example.com/payments/stripe/webhook
~~~

选择以下事件，并保存此端点的签名密钥。URL 此时可先登记，公网路由完成后再验证投递。

~~~text
checkout.session.completed
checkout.session.async_payment_succeeded
checkout.session.async_payment_failed
checkout.session.expired
charge.refunded
charge.dispute.created
charge.dispute.updated
charge.dispute.closed
~~~

仅首次部署且没有已加密的兑换码时，生成兑换码密钥一次：

~~~bash
openssl rand -base64 32 | tr '+/' '-_'
~~~

保留完整输出，包括末尾 `=`，通常为 44 字符；程序也接受正确的无填充编码。生成结果只填入服务器 `.env`，不发到聊天、不写进 README。**已有兑换码时禁止直接换这个密钥**，否则原码无法解密，不能靠重新发码修复。

~~~bash
cd /opt/Tgbot
umask 077
nano .env
~~~

在原 `.env` 中加入或修改这三行，替换下面的占位值，保留原有 `MYSQL_*` 配置：

~~~dotenv
TGBOT_STRIPE_SECRET_KEY=sk_test_REPLACE_ME
TGBOT_STRIPE_WEBHOOK_SECRET=whsec_REPLACE_ME
TGBOT_PAYMENT_CODE_KEY=REPLACE_WITH_GENERATED_KEY
~~~

Compose 的 `embyboss.environment` 必须包含以下映射，当前仓库模板已包含。`.env` 不会自动将所有变量传入容器；也不要在 Compose 中写死旧密钥。当前 shell 中同名 `TGBOT_*` 环境变量会覆盖 `.env`，修改后需确保没有残留的 `export` 值。

~~~yaml
environment:
  TGBOT_STRIPE_SECRET_KEY: ${TGBOT_STRIPE_SECRET_KEY:-}
  TGBOT_STRIPE_WEBHOOK_SECRET: ${TGBOT_STRIPE_WEBHOOK_SECRET:-}
  TGBOT_PAYMENT_CODE_KEY: ${TGBOT_PAYMENT_CODE_KEY:-}
~~~

编辑现有 `config.json`，在最外层加入/修改 `payments`。以下只是该字段，不是完整配置文件；`123456789` 替换为测试购买者的 Telegram 数字 ID。`public_url` 只填 HTTPS 根地址，不加 `/payments/shop`、查询参数或空格。

~~~json
"payments": {
  "enabled": false,
  "public_url": "https://pay.example.com",
  "live_mode": false,
  "checkout_minutes": 30,
  "seat_limit": 1000,
  "terms_version": "2026-09-09-v1",
  "test_buyer_ids": [123456789]
}
~~~

`seat_limit` 限制已有账号与已预留注册席位的总量，0 表示不限制，应按实际服务器容量设置。`checkout_minutes` 建议保持 30，创建 Checkout 时增加 60 秒网络传输余量，默认约 31 分钟。须知版本不是须知正文，不能通过随意改版本号更改法律文案。Bot API 同时须为 `api.status: true`，监听 `127.0.0.1:8838`。

编辑后先用新镜像做独立配置校验，不启动第二个 Bot，也不打印密钥：

~~~bash
(
set -eu
cd /opt/Tgbot
chmod 600 .env config.json
python3 -m json.tool config.json >/dev/null
docker compose config --quiet
docker compose run --rm --no-deps -T embyboss python3 - <<'PY'
import json
import runpy
from types import SimpleNamespace

with open('config.json', encoding='utf-8') as f:
    raw = json.load(f)
Settings = runpy.run_path('bot/payments/settings.py')['PaymentSettings']
p = Settings.from_config(SimpleNamespace(payments=SimpleNamespace(**raw['payments'])))
p.validate()
api = raw.get('api', {})
assert api.get('status') is True, 'Bot API is disabled'
assert api.get('http_url') == '127.0.0.1', 'Bot API must stay on loopback'
assert api.get('http_port') == 8838, 'Check Bot API upstream port'
print('PAYMENT_CONFIG_OK')
print('SALES_ENABLED =', p.enabled)
print('LIVE_MODE =', p.live_mode)
print('PUBLIC_URL =', p.public_url)
print('TEST_BUYER_COUNT =', len(p.test_buyer_ids))
print('CODE_KEY_BYTES =', len(p.encryption_key_bytes()))
PY
)
~~~

预期为 `PAYMENT_CONFIG_OK`、`LIVE_MODE = False`、`CODE_KEY_BYTES = 32`。这里验证配置格式，不会验证 Stripe 商户资质或确认 `whsec_...` 属于哪个端点。

### 17.4 支付域名、Caddy 与 CDN

链路为「浏览器/Stripe → HTTPS 支付域名 → CDN/NPM → Caddy HTTP 18080 → 本机 Bot 8838」。DNS 指向你的 HTTPS 入口，CDN 回源地址填实际业务源站，回源协议/端口为 HTTP/18080，保留支付域名 Host。HTTPS 证书必须有效，不能依靠 `curl -k` 通过验收。

支付站关闭缓存，原样转发 Cookie、Set-Cookie、Origin、请求体、查询参数及 `Stripe-Signature`。Webhook 路径不能有验证码、登录挑战或跳转；修改 CDN 后发布配置并等待节点生效。无需对公网开放 8838。

编辑 `/opt/Tgbot/caddy/caddyfile`，在现有 VIP/普通线路配置之后增加以下**独立站点**。已有支付站点则修改原块，勿重复追加；将示例域名替换成实际域名。Caddyfile 中不要包含 Markdown 链接或代码围栏。

~~~caddyfile
http://pay.example.com:18080 {
    header Cache-Control "no-store"
    redir / /payments/shop 302

    handle /payments/* {
        reverse_proxy 127.0.0.1:8838 {
            header_up -X-DuSheng-Origin-Token
            header_up -X-DuSheng-Line-Token
        }
    }

    handle {
        respond "Not Found" 404
    }
}
~~~

这个站点只公开 `/payments/*`，内部 `/emby/*`、`/user/*`、`/auth/*` 不转发；`/payments/admin` 仍由 Bot 登录和角色权限保护。保留原来的 `import emby_local_config ...`、VIP 令牌验证及 HLS 规则。

先验证宿主机的新文件，再重启原 Caddy 容器以刷新挂载。验证失败时不重启，按第 17.2 节的备份修正配置。

~~~bash
(
set -eu
cd /opt/Tgbot
gateway_image="$(docker inspect -f '{{.Image}}' emby-line-gateway)"
docker run --rm \
  --env-file /etc/dusheng/emby-line.env \
  -v /opt/Tgbot/caddy/caddyfile:/etc/caddy/Caddyfile:ro \
  "$gateway_image" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker restart emby-line-gateway
host_hash="$(sha256sum caddy/caddyfile | awk '{print $1}')"
container_hash="$(docker exec emby-line-gateway sha256sum /etc/caddy/Caddyfile | awk '{print $1}')"
test "$host_hash" = "$container_hash"
echo 'CONFIG_MOUNT_OK'
)
~~~

单文件 bind mount 在编辑器或 Git 替换文件后可能仍指向旧文件：容器内 validate/reload 成功不代表加载了宿主机的新内容。两边摘要必须一致。上述操作只更新 Caddyfile；若改了 `/etc/dusheng/emby-line.env`，还需要按第 9.2 节重建网关，restart 不会更新环境变量。

### 17.5 启动与逐层连通检查

~~~bash
(
set -eu
cd /opt/Tgbot
docker compose up -d --no-deps --no-build --force-recreate embyboss
payment_ready=0
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8838/payments/shop >/dev/null; then
    payment_ready=1
    break
  fi
  sleep 2
done
test "$payment_ready" -eq 1
docker compose ps embyboss
docker compose logs --since=3m --tail=100 embyboss
)
~~~

Bot 启动会自动运行 Alembic 迁移。看到 API 就绪后检查响应内容；**仅有 HTTP 200 不够，空白响应仍视为失败**。这一步不会下单或扣款。

~~~bash
(
set -eu
cd /opt/Tgbot
payment_host='pay.example.com' # 替换为实际支付域名

local_page="$(curl -fsS --max-time 15 http://127.0.0.1:8838/payments/shop)"
printf '%s' "$local_page" | grep -q 'payments.css'
curl -fsS --max-time 15 http://127.0.0.1:8838/payments/products >/dev/null
echo 'PAYMENT_LOCAL_OK'

gateway_page="$(curl -fsS --max-time 15 -H "Host: $payment_host" http://127.0.0.1:18080/payments/shop)"
printf '%s' "$gateway_page" | grep -q 'payments.css'
echo 'PAYMENT_GATEWAY_OK'

public_page="$(curl -fsS --max-time 20 "https://$payment_host/payments/shop")"
printf '%s' "$public_page" | grep -q 'payments.css'
public_js="$(curl -fsS --max-time 20 "https://$payment_host/payments/static/payments.js")"
printf '%s' "$public_js" | grep -q 'scheduleOrderPoll'
curl -fsS --max-time 20 "https://$payment_host/payments/static/checkout-wait.css" >/dev/null
echo 'PAYMENT_PUBLIC_OK'

internal_status="$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' \
  -H "Host: $payment_host" http://127.0.0.1:18080/emby/line_report)"
test "$internal_status" = 404
public_internal_status="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
  "https://$payment_host/emby/line_report")"
test "$public_internal_status" = 404
echo 'INTERNAL_ROUTES_NOT_EXPOSED'
)
~~~

页面路由按 GET 验证，不用 `curl -I` 的 HEAD 请求代替；未实现 HEAD 时可能返回 405。本机正常但网关异常，查 Caddy；网关正常但公网异常，查 CDN 回源、证书和 Host。

### 17.6 登录与商品上架

打开 `https://实际支付域名/payments/shop`，点击「Telegram 登录」，在 Bot 核对确认码并确认，再回到发起登录的原浏览器。请求有效期 5 分钟；构建、重启或等待过久后，请刷新并重新发起，不要复用旧链接。

所有者（`config.owner` 的 Telegram ID）登录后进入「销售管理 → 套餐管理」。系统初始生成普通/VIP 注册码、普通/VIP 续期码的 1、3、6、12 个月草稿，价格为 0 且未上架。所有者设置价格、销售上限并上架后才可购买；销售上限同时计算待支付和已支付订单。普通管理员可以查单、对账和补发原码，不能改价。

后台价格输入单位是人民币元，服务端存储为整数分。验收可选择 ¥10 的测试商品，但 **¥10 不是代码规定的最低价，也不是 Stripe 对所有商户的统一门槛**；实际最低金额受商户结算币种等条件影响，以 Stripe 错误码和账户规则为准。

确认测试白名单正确后，在 `config.json` 中将 `payments.enabled` 改为 `true`，保持 `live_mode: false`；执行 17.3 配置校验和 17.5 的 Bot 重建命令。手工修改配置/`.env` 后要重建容器，单纯 `docker restart` 不会加载新的环境变量。

### 17.7 测试验收

使用白名单中的测试账号，分别完成支付宝和微信的 Stripe 测试付款。测试环境不代表实际扣款；完整兑换、并发和故障测试在隔离的数据库/Emby 中进行。

- 未勾选购买须知不能下单；商品改价或须知版本变化后须重新确认。
- Checkout 打开时显示等待提示；原浏览器保留订单页。付款后订单页每 4 秒查状态，最长约 10 分钟，超时后可手动刷新；不能把一直转圈当作已到账。
- 订单应依次确认「已支付」「已发码」。Stripe 托管页的动画和返回由 Stripe 控制；若未自动返回，关闭支付标签页查看原订单，勿因此重新付款。
- 兑换码应为 `DuSheng-Pay_...`；Bot 管理员创建的旧制码保持 `DuSheng-...` 格式，已发出的 `Pay_`、`DuShengPay_` 仍兼容。换前缀不会改变已有订单的原码。
- 验证注册/续期用途、VIP/普通周期、转赠、同一码第二次兑换被拒绝。付款只发码，注册成功或兑换续期时才建立权益。
- Stripe 对应环境的 Webhook 投递返回 2xx。用同一事件重新投递后，仍只有一张码；后台「补发原码」不能生成另一张。
- 在隔离环境验证金额/币种不符、伪造签名、多人抢兑、服务中断恢复及通知失败；订单付款和发码状态独立，通知失败不能让已收款订单丢失。
- 检查非购买者不能查询兑换码，普通用户不能管理商品；同时用合法 VIP 和普通账号回归有/无 `/emby` 前缀及 HLS 播放，保留认证数据库只读挂载。

本地离线回归使用已安装依赖的开发环境执行：

~~~bash
python3 -B scripts/run_offline_tests.py
~~~

该脚本隔离真实配置和网络；输出中的 skipped 是未执行的靶机检查，不算通过。离线测试通过不能代替 Stripe 测试支付、真实 MySQL/Emby 验证或正式小额支付验收。

### 17.8 切换正式收款

**两种支付方式均通过正式审核后才执行。** 测试白名单只在 `live_mode: false` 生效，不能用它限制正式模式的购买者。

1. 先将 `payments.enabled` 改为 `false`，重建 Bot 停止创建新订单；让测试付款和发码任务处理完成。测试侧仍待支付的 Checkout 在 Stripe 测试后台确认取消/过期，并完成对账；不能直接删除订单或数据库。检查测试商品价格，正式开放前将不售卖的测试商品下架，避免沿用测试价。
2. 按 17.2 的备份部分保存当前数据库、配置、密钥和镜像。独立测试部署不要合并测试库到正式库；若此前直接在业务库测试，先核查测试账号权益和已发码情况，再切换。
3. 在 Stripe **正式环境**确认支付宝、微信支付和 CNY 能力，取得 `sk_live_...` Secret key；创建正式 Webhook 端点，使用 17.3 的完整事件列表。已有正确的正式端点可继续用，无需每次升级重建。
4. 修改 `.env` 的 Stripe Secret key 和 Webhook 签名密钥。正式端点也以 `whsec_` 开头，不能靠前缀判断它是测试还是正式，必须核对端点所属环境。测试和正式端点可以先后使用同一公网 URL，但当前实例一次只校验一套签名密钥。

~~~dotenv
TGBOT_STRIPE_SECRET_KEY=sk_live_REPLACE_ME
TGBOT_STRIPE_WEBHOOK_SECRET=whsec_REPLACE_WITH_LIVE_ENDPOINT_SECRET
~~~

**同一数据库从测试切正式、升级或重启时，`TGBOT_PAYMENT_CODE_KEY` 保持原值。** 独立测试部署与独立正式部署分别使用各自的加密密钥，不能把「环境隔离」理解为每次切模式都换密钥。

5. 在 `config.json` 中修改以下三个字段，其余支付域名、容量等配置保持实际值：

~~~json
"enabled": false,
"live_mode": true,
"test_buyer_ids": []
~~~

6. 重新执行 17.3 的独立配置校验，确认 `LIVE_MODE = True`、`SALES_ENABLED = False`、`CODE_KEY_BYTES = 32`。然后执行 17.5 重建 Bot 并检查日志和公网。它不会启动新的购买，但已存在 Checkout 仍可能付款，原订单交付任务也继续运行。
7. 确认商品正式售价和上下架状态后，将 `payments.enabled` 改为 `true`，再次校验并重建 Bot。分别用微信和支付宝完成一笔允许金额的**真实付款**，在正式 Stripe 后台核对金额、币种、成功状态和 Webhook 投递，再确认 Bot 的发码、通知、兑换。全部通过后再对外宣传开放。

正式模式下测试订单/测试码不能兑换是预期行为。测试和正式的 Stripe 密钥、Webhook 不可混用；不要修改旧订单的 `mode` 字段绕过检查。停止使用的测试端点应在测试任务核对完毕后停用，避免测试事件继续投递到正式实例。

同一数据库的商品、销售计数、注册席位和账号权益并非各环境独立。当前代码复用未过期的同商品待支付订单时没有按模式筛选，因此切换前要在测试模式处理完旧待支付单，否则原测试购买者可能暂时遇到订单环境不匹配。测试记录和席位也不会随切换自动清空，这也是完整验收推荐隔离部署的原因。

### 17.9 后续更新

仅更新代码时执行 17.2 完成备份、拉取和构建，然后执行 17.3 校验、17.5 重建和连通检查。保留当前 `live_mode`、`.env`、价格和已售订单，不重新生成密钥，不覆盖本机 Compose/Caddy 配置。代码已经包含配置字段默认值，缺少可选字段通常不需要重建整个 `config.json`。

仅修改密钥或 `config.json` 时，无需再次 build，但必须 `docker compose up -d --no-deps --no-build --force-recreate embyboss`。网站后台的商品修改立即生效。Caddyfile 只有变化时才验证并重启网关；不要每次 Bot 更新都重启 Emby/MySQL。

Git 提交号只能说明宿主机代码版本，不能证明运行容器已更新；必须确保 build 成功后再 recreate。命令块遇错就停止，不能在构建失败后继续用旧镜像宣称升级成功。

### 17.10 停售与回滚

暂停新购买：将 `payments.enabled` 改为 `false` 并重建 Bot。订单查询、Webhook、已支付发码、通知重试及对账仍继续；**停售不等于停止后台任务，也不会自动撤销已有 Stripe Checkout**。审批未完成时已有失败订单仍可能记录 `payment_failure`，需要按请求编号查原因，不能靠清空数据库消除日志。

回滚代码前，先停售并保存当前数据。备份镜像标签记录在 17.2 的备份目录名中，但只有确认该镜像兼容当前数据库迁移及订单字段时才能使用。不要为了消除报错执行 Alembic 降级、删除支付表、恢复付款前的旧数据库，或切回测试密钥处理正式订单，这些操作可能丢失订单和权益。无法确认兼容性时，保持当前服务对账交付并修复代码。

既有订单仍需保留对应 Stripe 环境密钥和原兑换码密钥；通过管理员「对账」「补发原码」处理交付问题。普通售后不提供退款申请或后台退款按钮，但外部退款、拒付及支付平台要求仍需核查；未兑换码可能暂停使用，已兑换订单进入人工处理，不应自行封禁受赠者。

### 17.11 常见故障

先在业务服务器获取脱敏诊断：

~~~bash
cd /opt/Tgbot
docker compose logs --since=10m --tail=300 embyboss 2>&1 | grep -F 'payment_failure'
~~~

日志仅保留操作、异常类型、错误码、已知参数、HTTP 状态和 `request_id=req_...`。用请求编号在**对应环境**的 Stripe Workbench 请求日志定位原始错误；不要仅凭截图中的「请求失败」判断原因。

| 现象 | 核查与处理 |
| --- | --- |
| `no configuration file provided` | 在错误目录运行 Compose，先 `cd /opt/Tgbot`。 |
| Git 提示本地 Compose 修改将被覆盖 | 按 17.2 备份再合并；冲突要保留本机挂载和新增环境映射。 |
| `Stripe credentials are missing or do not match payment mode` | 核对 `sk_test_`/`sk_live_` 与 `live_mode`；公钥 `pk_` 不能用。检查 Compose 映射及 shell 覆盖值，然后 recreate。 |
| `Payment encryption key must be a base64 encoded 32-byte key` | 按 17.3 校验解码长度，不是随意填 32 字符；已发码时找回原密钥备份，不能重新生成替换。 |
| 本机 200，公网 502/504 | 检查 CDN 回源协议、源站 IP、18080 端口、Host 和 Caddy 上游；不是开放 8838。 |
| 200 但页面长度为 0，没有 `payments.css` | 检查是否命中正确 Host 路由，比较宿主机/容器 Caddyfile 摘要，验证新文件后重启网关刷新挂载。 |
| 登录请求已过期 | 五分钟超时、旧深链接或浏览器不一致；刷新后重新发起并返回原浏览器，不反复删除 Telegram session。 |
| 登录/下单 403 | 核对 `public_url` 与实际 HTTPS 地址、Cookie/Origin 转发和浏览器会话；勿通过关闭 CSRF 绕过。内部路由的 404 是隔离预期。 |
| `test_buyer_not_allowed` | 测试模式仅允许 `test_buyer_ids` 中的真实 Telegram ID；空列表拒绝所有测试下单。 |
| `code=amount_too_small` | Stripe 确认金额过低，按该商户最低金额调整商品价格并重新确认；不能仅凭 ¥1 或 ¥10 推断统一门槛。 |
| `param=payment_method_types` / `stripe_payment_methods_unavailable` | 支付方式参数被拒绝。常见是正式支付宝/微信未获批，也可能是币种或商户条件不兼容；按 `request_id` 查看准确原因。测试获批不代表正式获批。 |
| `order_mode_mismatch` / `code_mode_mismatch` | 订单/码不属于当前环境；核对当前模式和实际运行代码。旧测试创建/对账任务在新版本中结束，不代表任何正式款项到账。 |
| Webhook 400 或签名失败 | 使用端点自己的 `whsec_`，确保原始请求体和 `Stripe-Signature` 没被代理修改；裸 curl 不带签名返回 400 是正常的，不能算回调验收通过。 |
| 已扣款，页面仍待支付/待发码 | 核对 Stripe 支付状态和 Webhook，后台提交对账。后台通常每分钟处理任务、每五分钟安排周期对账，网络和重试可能延迟；超过两分钟可开始排查，勿再次付款。 |
| 微信付完停在 Stripe 页面 | 查看原订单页，确认状态自动更新；本站不能修改 Stripe 托管页。不能把跳转动画当成到账证据。 |
| Bot 未收到发码消息 | 先到「我的订单」查看；如果已有原码，只重试通知。`AUTH_KEY_UNREGISTERED` 属于 Telegram 会话问题，需单独核查，不删除支付记录或更换兑换码密钥。 |
| VIP/HLS 异常、`Invalid internal token` | 按第 16.2 节核查只读认证库、Caddy 内部令牌和路由；付款站点配置不能覆盖原线路规则。 |

### 17.12 敏感信息与备份保护

`.env`、`config.json`、Telegram session、MySQL 备份、Emby 认证数据库和完整兑换码均不得提交 Git 或贴入公开聊天。备份目录权限保持 700，文件保持仅所有者可读；升级前后的密钥与数据库备份必须配套保留。对外排查只提供脱敏日志、提交号、状态码和 Stripe 请求编号，不提供整个配置文件或完整回调内容。

付款网站和订单页保持不缓存；不要把仅限内部的 API key、CDN 回源令牌、Stripe Secret key 或兑换码加密密钥发给浏览器。HTTPS 证书、业务域名和正确回源是部署前提，关闭防火墙、放行整个 Bot API 或关闭验签都不能作为故障修复方式。
