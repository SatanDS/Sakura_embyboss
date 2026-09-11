"""Editable notice delivered after a successful account registration."""

NOTICE_MAX_UNITS = 4000

DEFAULT_REGISTRATION_NOTICE = (
    "📢 **用户须知**\n\n"
    "1. **私人影视服务器，线路严禁外传。**禁止下载影片、使用爆米花、媒体库模式或创建播放列表；开启这些功能可能导致各种问题，发现违规将直接 Ban。\n\n"
    "2. 私人服务器成本较高，请大家珍惜使用。访问白名单优化线路时请关闭代理，或将白名单优化线路加入直连规则。\n\n"
    "3. 如遇“继续播放”无速度，请移除“继续观看”记录或点击“已观看”，重新播放后再拖到之前看到的时间节点；这是本地缓存机制导致的。\n\n"
    "4. 发送口令领取注册码后，请联系 `@emby_dusheng_bot`。电脑端和安卓手机端推荐免费的 [Hills](https://t.me/Hills_app)；iOS 推荐免费的 [Lenna](https://t.me/Lenna_App)，需使用外区 Apple ID 下载（国区没有）。Infuse、SenPlayer 为付费客户端。\n\n"
    "5. 注册完成后，下载并注册豆瓣 App，复制豆瓣 ID，然后在 Bot「用户功能 → 豆瓣想看」中绑定 ID。搜索想看的内容并点击“想看”；服务器每 30 分钟同步一次大家的想看动态并自动下载。若自动下载的清晰度不理想，可联系服主洗版。\n\n"
    "阅读完毕后，点击下方按钮加入 Bot 管理的群组。"
)


def validate_registration_notice(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("注册须知不能为空，请发送文字内容。")
    text = text.strip()
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError("注册须知包含无效字符，请重新输入。") from exc
    if units > NOTICE_MAX_UNITS:
        raise ValueError(f"注册须知过长，最多 {NOTICE_MAX_UNITS} 个字符，部分表情占两个字符。")
    return text


def get_registration_notice(config) -> str:
    try:
        return validate_registration_notice(getattr(config, "registration_notice", None))
    except ValueError:
        return DEFAULT_REGISTRATION_NOTICE
