import json
import os
from pydantic import BaseModel, Field, AliasChoices, ConfigDict
from typing import Dict, List, Optional, Union

# 嵌套式的数据设计，规范数据 config.json

MAX_INT_VALUE = 2147483647  # 2^31 - 1
MIN_INT_VALUE = -2147483648  # -2^31

class ExDate(BaseModel):
    mon: int = 30
    sea: int = 90
    half: int = 180
    year: int = 365
    used: int = 0
    unused: int = -1
    code: str = 'code'
    link: str = 'link'


# class UserBuy(BaseModel):
#     stat: StrictBool
#
#     # 转换 字符串为布尔
#     @field_validator('stat', mode='before')
#     def convert_to_bool(cls, v):
#         if isinstance(v, str):
#             return v.lower() == 'y'
#         return v
#
#     text: bool
#     button: List[str]


class Open(BaseModel):
    stat: bool
    open_us: int = 30
    all_user: int
    register_worker_count: int = 5
    register_queue_limit: int = 100
    timing: int = 0
    tem: Optional[int] = 0
    # allow_code: StrictBool
    # @field_validator('allow_code', mode='before')
    # def convert_to_bool(cls, v):
    #     if isinstance(v, str):
    #         return v.lower() == 'y'
    #     return v

    checkin: bool
    checkin_lv: Optional[str] = 'd'
    exchange: bool
    whitelist: bool
    use_whitelist_code: bool = False
    invite: bool
    # Invitation eligibility: a/b/c/d keep the historical account-level
    # policy; ``admin`` restricts invitation-code exchange to owner/admins.
    invite_lv: Optional[str] = 'b'
    leave_ban: bool
    uplays: bool = True
    checkin_reward: Optional[List[int]] = [1, 10]
    exchange_cost: int = 300
    whitelist_cost: int = 9999
    invite_cost: int = 1000
    srank_cost: int = 5

    # 每次创建 Open 对象时被重置为 0
    def __init__(self, **data):
        super().__init__(**data)
        self.timing = 0


class Ranks(BaseModel):
    # Prefix used in Telegram messages, invite links and all generated codes.
    logo: str = "DuSheng"
    backdrop: bool = False


class Schedall(BaseModel):
    dayrank: bool = True
    weekrank: bool = True
    dayplayrank: bool = False
    weekplayrank: bool = True
    check_ex: bool = True
    low_activity: bool = False
    partition_check: bool = True
    day_ranks_message_id: int = 0
    week_ranks_message_id: int = 0
    restart_chat_id: int = 0
    restart_msg_id: int = 0
    backup_db: bool = True

    def __init__(self, **data):
        super().__init__(**data)
        if self.day_ranks_message_id == 0 or self.week_ranks_message_id == 0:
            if os.path.exists("log/rank.json"):
                with open("log/rank.json", "r") as f:
                    i = json.load(f)
                    self.day_ranks_message_id = i.get("day_ranks_message_id", 0)
                    self.week_ranks_message_id = i.get("week_ranks_message_id", 0)


class Proxy(BaseModel):
    scheme: Optional[str] = ""  # "socks4", "socks5" and "http" are supported
    hostname: Optional[str] = ""
    port: Optional[int] = None
    username: Optional[str] = ""
    password: Optional[str] = ""


class MP(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    status: bool = False
    # 点播和豆瓣想看独立开关；豆瓣流程不消耗 price。
    douban_status: bool = False
    url: Optional[str] = Field("", validation_alias=AliasChoices("url", "host"))
    username: Optional[str] = ""
    password: Optional[str] = ""
    access_token: Optional[str] = ""
    price: int = 1
    download_log_chatid: Optional[int] = None
    lv: Optional[str] = "b"

class AutoUpdate(BaseModel):
    status: bool = True
    git_repo: Optional[str] = "SatanDS/Sakura_embyboss"  # github仓库名/魔改的请填自己的仓库
    commit_sha: Optional[str] = None  # 最近一次commit
    up_description: Optional[str] = None  # 更新描述


class API(BaseModel):
    status: bool = False  # 默认关闭
    # The API is consumed by the same-host Caddy gateway. Keep it on loopback
    # by default so line-enforcement endpoints are not Internet-facing.
    http_url: Optional[str] = "127.0.0.1"
    http_port: Optional[int] = 8838
    allow_origins: Optional[List[Union[str, int]]] = None
    # Shared secret used by the local Caddy/CDN gateway when it calls the
    # internal line-enforcement endpoints.  This intentionally has no
    # default value: an unset secret must fail closed in the API dependency.
    line_report_token: Optional[str] = None
    api_key: Optional[str] = None
    allow_legacy_bot_token: bool = False

    def __init__(self, **data):
        super().__init__(**data)
        if self.allow_origins is None:
            self.allow_origins = ["*"]
            # 如果未设置，默认为 ["*"]，为了安全可以设置成本机ip&反代的域名，列表可包含多个

class Payments(BaseModel):
    enabled: bool = False
    public_url: str = ""
    live_mode: bool = False
    checkout_minutes: int = 30
    seat_limit: int = 0
    terms_version: str = "2026-09-09-v1"
class RedEnvelope(BaseModel):
    status: bool = True  # 是否开启红包
    allow_private: bool = True # 是否允许专属红包

class Config(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    bot_name: str
    bot_token: str
    owner_api: int
    owner_hash: str
    owner: int
    group: List[int]
    main_group: str
    chanel: str
    bot_photo: str
    open: Open
    admins: List[int] = Field(default_factory=list)
    money: str
    emby_api: str
    emby_url: str
    emby_block: Optional[List[str]] = []
    emby_line: str
    # Optional read-only mount of Emby's data/authentication.db.  Emby 4.9
    # does not provide a safe Users/Me endpoint, so this is required to map a
    # client access token to its canonical user without trusting URL claims.
    emby_auth_db_path: Optional[str] = None
    extra_emby_libs: Optional[List[str]] = []
    db_host: str
    db_user: str
    db_pwd: str
    db_name: str
    db_port: int = 3306
    tz_ad: Optional[str] = None
    tz_api: Optional[str] = None
    tz_id: Optional[List[Union[int, str]]] = []  # int for Nezha, str (UUID) for Komari
    tz_version: Optional[str] = "v0"  # "v0" for Nezha V0, "v1" for Nezha V1, "komari" for Komari
    tz_username: Optional[str] = None  # V1 API only
    tz_password: Optional[str] = None  # V1 API only
    ranks: Ranks
    schedall: Schedall
    db_is_docker: bool = False
    db_docker_name: str = "mysql"
    db_backup_dir: str = "./db_backup"
    db_backup_maxcount: int = 7
    # another_line: Optional[List[str]] = []
    # 如果使用的是 Python 3.10+ ，|运算符能用
    # w_anti_channel_ids: Optional[List[str | int]] = []
    w_anti_channel_ids: Optional[List[Union[str, int]]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("w_anti_channel_ids", "w_anti_chanel_ids"),
    )
    proxy: Proxy = Field(default_factory=Proxy)
    # kk指令中赠送资格的天数
    kk_gift_days: int = 30
    # 是否狙杀皮套人
    fuxx_pitao: bool = True
    # 活跃检测天数，默认21天
    activity_check_days: int = 21
    # 封存账号天数，默认5天
    freeze_days: int = 5
    # 白名单用户专属的emby线路
    emby_whitelist_line: Optional[str] = None
    # 客户端过滤总开关
    client_filter_enabled: bool = False
    # 被拦截的user-agent模式列表
    blocked_clients: Optional[List[str]] = None
    # 客户端过滤模式：blacklist=命中黑名单拦截，whitelist=未命中白名单拦截
    client_filter_mode: str = "blacklist"
    # 被允许的user-agent模式列表，仅client_filter_mode为whitelist时生效
    allowed_clients: Optional[List[str]] = None
    # 是否在检测到可疑客户端时终止会话
    client_filter_terminate_session: bool = True
    # 是否在检测到可疑客户端时封禁用户
    client_filter_block_user: bool = False
    # 是否在检测到线路权限违规时终止会话
    line_filter_terminate_session: bool = True
    # 是否在检测到线路权限违规时封禁用户
    line_filter_block_user: bool = False
    # 分区名 -> 库名列表
    partition_libs: Dict[str, List[str]] = Field(default_factory=dict)
    moviepilot: MP = Field(default_factory=MP)
    auto_update: AutoUpdate = Field(default_factory=AutoUpdate)
    red_envelope: RedEnvelope = Field(default_factory=RedEnvelope)
    api: API = Field(default_factory=API)
    payments: Payments = Field(default_factory=Payments)

    def __init__(self, **data):
        super().__init__(**data)
        if self.admins and self.owner in self.admins:
            self.admins.remove(self.owner)

    @classmethod
    def load_config(cls):
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
            # Migrate the former built-in branding once.  ``config.json`` is
            # intentionally ignored by Git, so existing installations would
            # otherwise keep generating Sakura-prefixed links forever.
            ranks = config.get("ranks")
            if isinstance(ranks, dict) and str(ranks.get("logo", "")).strip().casefold() == "sakura":
                ranks["logo"] = "DuSheng"
            return cls(**config)

    def save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.model_dump(), f, indent=4, ensure_ascii=False)


class Yulv(BaseModel):
    wh_msg: List[str]
    red_bag: List[str]

    @classmethod
    def load_yulv(cls):
        with open("bot/func_helper/yvlu.json", "r", encoding="utf-8") as f:
            yulv = json.load(f)
            return cls(**yulv)
