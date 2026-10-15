"""Constants for GateLink HASS."""

DOMAIN = "gate_link_hass"
CONF_PHONE = "phone"
CONF_VERIFICATION_CODE = "verification_code"
CONF_SELECTED_GATES = "selected_gates"
CONF_PROTOCOL = "protocol"
CONF_DEVICE_ID = "device_id"
PROTOCOL_APP = "android_app"
DEFAULT_TIMEOUT = 10
CLIENT_FACTORY = "client_factory"
SMS_ORIGIN = "https://gateway.qinlinkeji.com"
API_ORIGIN = "https://mobileapi.qinlinkeji.com"
SMS_PATH = "/member/sms/sendSecurityCode"
LOGIN_PATH = "/api/alimini/appuser/smsLogin"
CHECK_PATH = "/api/alimini/appuser/checkLogin"
COMMUNITIES_PATH = "/api/auth/v5/getApplyListGroupByCommunity"
GATES_PATH = "/api/doorcontrol/v2/queryUserDoor"
OPEN_PATH = "/api/doorcontrol/v2/open"
SMS_APP_ID = "gbDjIZQSOpCMX49P"
# Shared protocol credential from the supplied client bundle, not an account token.
SMS_APP_SECRET = "OKFoQ9MmNXQtcyXROo4PnaFkfDPTHuDg"
SMS_NONCE = "9999"
SMS_VERSION = "v2"
# These wire values match the capture, including platform-specific strings.
USER_AGENT_LOGIN = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 27_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/24B5089g ChannelId(56) "
    "Ariver/1.1.0 AliApp(AP/12.12.30.6000) Nebula WK RVKType(0) "
    "AlipayDefined(nt:WIFI,ws:430|868|3.0) AlipayClient/12.12.30.6000 "
    "Language/zh-Hans Region/CN NebulaX/1.0.0 XRiver/10.2.58.1 DTN/2.0"
)
USER_AGENT_GATES = USER_AGENT_LOGIN.replace("ChannelId(56)", "ChannelId(39)")
REFERER_ROOT = (
    "https://2018121762538986.hybrid.alipay-eco.com/"
    "2018121762538986/0.2.2610092221.2/index.html"
)
REFERER_LOGIN = REFERER_ROOT + "#pages/login/login/login?__appxPageId=4"
REFERER_GATES = REFERER_ROOT + "#tabBar/door/door"
