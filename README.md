# DuSheng Emby 管理 Bot 部署指南

本文件整理本專案在 Debian 12 上的完整部署流程：Telegram Bot/API、Emby、MySQL、Docker、Caddy 線路檢測，以及 DuShengCDN/NPM 前置代理。

> 將 <...> 換成自己的值。

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

8838 不應透過 CDN 公開；Caddy 必須使用 host network，才能以 127.0.0.1 呼叫 Bot 的內部端點。

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

把自己的數字 ID 填到 owner，群組 ID 填到 group 陣列。main_group、chanel 填公開群/頻道 username（不要加 @）；私密群不要把 -100... 當 username。

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
  "line_report_token": "<與 Caddy/CDN 相同的 64 位 hex 隨機密鑰 如果搞不懂这个什么意思就把这一行"line_report_token"删掉同时下面的9.0步骤也跳过 主要是防止伪造绕过bot验证>"
},
"ranks": {
  "logo": "DuSheng",
  "backdrop": false
}
~~~

ranks.logo 會決定新深連結、註冊碼、續期碼、白名單碼的前綴。設成 DuSheng 後新碼會以 DuSheng- 開頭。

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
