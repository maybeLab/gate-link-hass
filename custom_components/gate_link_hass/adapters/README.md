# GateLink HASS 协议适配器

本目录同时保留支付宝小程序和 Android App 两套接口实现，共用 HA 的账号配置流、门禁模型、按钮、会话失效提醒及重新认证逻辑。本文按 2026-10-14 的本地实现整理。

App 接口的完整请求字段、签名及响应见 [API 文档](../../../docs/API.md)，本地测试和扩展方式见 [开发指南](../../../docs/DEVELOPMENT.md)。

## 当前使用方式

| 适配器 | 实现 | 当前状态 |
| --- | --- | --- |
| 小程序 | [mini_app_client.py](mini_app_client.py)：`GateLinkHassClient`，端点常量在 [const.py](../const.py) | 保留用于对照研究；主流程不调用，真实登录仍缺少有效支付宝授权码来源 |
| Android App | [app_client.py](app_client.py) | HA 默认客户端，统一 `5.2.6 / 3146`；已在 HA Core 2026.10.0 完成安装与账号加载验证 |

[account.py](../account.py) 的 `create_client()` 在未注入工厂时创建 App 客户端。它继承旧客户端的超时、会话模型和签名提供器等基础能力，覆盖短信、登录、登录检查、小区、门禁和开门的协议实现，不请求支付宝授权 `code`。运行时一个客户端只使用一套协议；旧授权码提供器不会被默认流程调用。

两套协议实现集中在 `adapters/` 包中，账号运行时、配置流、按钮及共享模型位于上一级。Python 导入路径分别为 `custom_components.gate_link_hass.adapters.mini_app_client` 和 `custom_components.gate_link_hass.adapters.app_client`。

配置格式升级为 v3，保存 `protocol=android_app` 和设备 `openid`。新登录、重新认证、配置选项、加载账号及开门均使用默认 App 客户端；重启和修改选项后复用已保存的设备请求标识。同一 domain 下的 v2 账号保留原条目、门禁绑定及 HA 设备和实体标识，先进入短信重新认证，不直接将旧会话发送给 App 接口；新登录成功后恢复。原绑定门未在新列表中出现时，保留绑定并记录缓存权限无效，按钮仍通过开门接口判断实际权限。无账号凭据的 v1 条目仍需重新添加。集成 domain 已改为 `gate_link_hass`，旧名称版本需要重新安装，不执行跨 domain 迁移。

## 接口地址

所有请求均使用 HTTPS POST。下表中的路径附在各自域名后。

| 项目 | 小程序 | Android App |
| --- | --- | --- |
| 业务域名 | `https://mobileapi.qinlinkeji.com` | `https://mobileapi3.qinlinkeji.com` |
| 短信域名 | `https://gateway.qinlinkeji.com` | `https://gateway2.qinlinkeji.com` |
| 短信路径 | `/member/sms/sendSecurityCode` | `/member/sms/sendSecurityCode` |
| 登录 | `/api/alimini/appuser/smsLogin` | `/api/app/v1/login` |
| 登录状态检查 | `/api/alimini/appuser/checkLogin` | `/api/app/v1/checkLogin` |
| 小区 | `/api/auth/v5/getApplyListGroupByCommunity` | `/api/app/user/v2/communityInfo` |
| 门禁 | `/api/doorcontrol/v2/queryUserDoor` | `/api/app/user/v2/queryAllUserDoorByCache` |
| 开门 | `/api/doorcontrol/v2/open` | `/api/open/doorcontrol/v2/open` |

App 另有 `/api/app/user/v2/queryUserDoorByCacheNew`，返回常用／收藏门。此前实测该接口得到三个收藏门，不能视为全部授权门禁。默认适配器使用 `queryAllUserDoorByCache`，处理 `buildingDoorList`（楼栋门）、`customDoorList`（自定义门）和 `gateDoorList`（小区门），按小区 ID 与门 ID 去重。已用保留的实际会话验证返回 21 扇门，本地权限与到期检查均通过；实际开门仍由服务端校验。

## 参数与请求格式

| 项目 | 小程序 | Android App |
| --- | --- | --- |
| 客户端标识 | 支付宝 User-Agent 和页面 Referer | `Dart/3.8 (dart:io)`、设备 `openid`、`qchannel/qplatform/qvendor` |
| App 版本参数 | 当前不发送 App 的 `version/qversioncode` | `version=5.2.6`、`qversioncode=3146` |
| 登录字段 | `code`、原始手机号 `mobile`、验证码 `securityCode` | AES 加密手机号 `mobile`、验证码 `smsCode`、`appChannel=1` |
| 授权码依赖 | 需要有效支付宝授权 `code`；默认提供器只生成 32 位随机占位值 | 不需要支付宝授权码 |
| 短信 body | 紧凑 JSON：`{"mobile":"…"}` | 同样为紧凑 JSON |
| 登录／小区 body | `application/x-www-form-urlencoded` | `application/json` |
| 门禁 body | `application/x-www-form-urlencoded` | 签名字段放在 `multipart/form-data` 中 |
| 开门 | 门 ID、MAC、`openType` 放表单 body | 小区 ID、门 ID、`appChannel` 及签名放 URL 查询参数，POST body 为空 |
| 会话传递 | URL 查询参数 `sessionId`；登录时为字符串 `null` | URL 查询参数 `sessionId`；登录时为空字符串 |

小程序短信也携带空 `sessionId` 查询参数，App 短信不携带该参数。App 开门不需要 MAC，门禁配置允许保存空 MAC。

## 签名、加密与响应

两套都没有整包 body 加密，也没有响应解密操作。HTTPS 提供传输加密；以下是额外的应用层处理。

- **短信签名**：两套共用内置 MD5 签名算法，将手机号与 `appid`、`nonce`、毫秒时间戳、`version=v2` 按字段名排序，追加协议固定密钥，生成大写十六进制签名放在请求头。小程序使用固定 `nonce=9999`，App 使用随机四位数字。短信的 `v2` 是接口版本，与 App 的 `5.2.6` 分开使用。
- **业务签名**：小程序当前其余业务请求没有签名；App 对业务字段加上 `timestamp`、`version`、五位随机 `nonce` 和用于签名的会话 `token`，排序并 URL 编码，追加固定签名密钥后计算 MD5。`sign` 随业务字段发送，`token` 本身不作为额外业务字段发送。
- **手机号加密**：App 登录使用 AES-ECB 和 PKCS7 填充（对应 Android 的 `AES/ECB/PKCS5Padding`），输出大写十六进制；只加密手机号字段，验证码及其他字段没有这种加密。小程序当前发送原始手机号。
- **响应解析**：两套按普通 JSON 处理，App 额外支持 `data` 是序列化 JSON 字符串的情况。解析字符串不是解密。
- **成功条件**：小程序要求整数业务码 `code=0`；App 一般请求接受 `success=true`、整数 `code=0` 或 `200..299`，专用登录检查严格要求整数 `code=0` 和正整数 `data`。两套开门均严格要求整数 `code=0`，且返回整数 `openDoorState=1`。

## 会话与权限

| 项目 | 小程序 | Android App |
| --- | --- | --- |
| 登录响应模型 | 要求 `userId` 和 `aliPayminiLoginDTO.sessionId/expires` | 实测只有 `flag/sessionId`；缺少用户 ID 时查询小区补充 |
| 服务器有效期 | 将 `expires` 按秒转换为到期时间 | 支持 `expires`；本次实测未返回，服务器期限保留未知 |
| `check_login()` | 调用专用接口并校验账号身份，不延长会话 | 正整数 `data` 表示有效，并按秒记录下次检查的时长参考 |
| 本地期限 | 请求前执行登录响应期限与 30 天本地上限的较早值 | 不按本地时间强制失效，旧 `lifetime_days` 选项忽略 |
| 到参考时间 | 旧客户端本地拒绝请求 | HA 定时调用 `checkLogin`，有效就继续使用并更新检查时间 |
| 身份失效识别 | HTTP 401 或业务错误中的明确失效消息 | HTTP 401、失败响应中的业务码 401，或明确失效消息 |
| `authorizeExpired` | 非零时视为权限失效 | 常用和全量门禁接口的响应 `data` 中均实测为 1；此前指定门仍可开门，实际含义尚未确认，未套用小程序判断 |

App 会话新增可选 `checked_at`、`check_validity_seconds`，参考时间为两者相加。每次成功检查后持久保存，重启不会重置登录时间；旧数据缺少检查记录时在加载过程中补充。默认 HA 流程取消固定天数设置，旧选项忽略，保存选项时清理。小程序保留原期限判断供协议对照。

两套都没有单独的自动刷新 token 接口。App 由服务端登录检查和业务请求决定是否失效；明确拒绝后持久保存失效状态、暂停开门、通知并请求重新认证。HA 每次点击开门仅发送一次 App 开门请求，不调用登录检查或门禁发现接口，也不按缓存权限或到期时间阻止请求；实际授权由开门接口判断。保留的小程序适配器仍使用其历史本地会话和权限判断。

App 适配器使用专用 `/api/app/v1/checkLogin`，发送签名 JSON（`method=checkLogin`），通过 URL 的 `sessionId` 携带会话。HA 在新建配置、账号加载、刷新门禁权限、配置选项查询和保存，以及定时检查时调用该方法。每个账号每天北京时间 06:00 检查，按 `Asia/Shanghai` 计算，不受 HA 配置时区影响；服务端参考时间更早时提前检查。成功响应的整数 `data <= 0`、HTTP 401 或业务码 401 时报告账号失效；非整数数据报告响应异常。后台超时、网络故障和其他请求错误不触发重新认证，5 分钟后重试，每日检查时间更早时优先执行。成功检查会更新参考时间，单个计时器安排最早的检查，其他请求不会推迟每日检查。卸载账号或确认登录失效会取消计时器。检查保留原登录时间、凭据、登录响应期限和设备标识；登录响应期限不用于 App 本地失效判断。

`shacha086` 和 `577fkj` 的参考实现同样提供该专用接口，后者每 60 秒检查在线账号。`rponeawa` 的参考实现在业务请求失败时处理登录失效；`ship-it-th1` 的参考实现不做独立检查。

2026-10-14 已用同一实际账号分别按 `5.1.0 / 3130` 和 `5.2.6 / 3146` 构造签名 JSON 请求验证专用接口：有效会话均返回 HTTP 200、`code=0`、整数 `data=7776000`；人为构造的无效会话均返回 HTTP 200、业务码 `401`、`success=false`。该数值相当于 90 天，按用户要求作为下次检查的秒数参考。其是否为剩余有效期、以及检查是否在服务端自动续期，目前没有足够证据确认；本地参考时间既不保证 token 可用，也不判定到点失效。

两套开门都只发送一次、不跟随重定向、不自动重试。服务端成功回执不能证明现场门的物理状态。旧会话能否跨协议使用尚未验证，切换时应通过目标协议重新登录。

## 验证状态与参考

- 旧小程序实现以抓包对照和离线测试为主，有效支付宝授权码来源仍待补齐；已从默认账号流程移出。
- App 已实测短信、登录、小区、收藏门查询和一次用户指定门的开门，开门回执为 `code=0/openDoorState=1`。
- 专用 App 登录检查接口已在 `5.1.0 / 3130`、`5.2.6 / 3146` 下实测有效与无效会话；该轮只检查登录，没有开门。
- App 全量门禁查询已实测返回 21 扇门，本地权限检查均通过；`authorizeExpired` 的精确含义仍未确认。
- 使用保留的本地会话调用适配器公开 `check_login()`，实测成功。离线测试覆盖无需授权码、统一版本参数、设备请求标识复用、v2 迁移与重新认证，以及按钮加载、开门和卸载；另覆盖到参考时间重新检查、有效继续使用、失败重试、服务端拒绝及检查记录持久化。真实手机号、验证码和会话凭据不写入本说明或测试数据。

验证结果概要见 [开发指南](../../../docs/DEVELOPMENT.md#验证与协议来源)。App 基础协议参考 [rponeawa 的实现](https://github.com/rponeawa/QinlinOpen)，许可与版权信息见 [第三方 MIT 声明](LICENSE-QinlinOpen)；全部门禁接口另参考 [shacha086 的实现](https://github.com/shacha086/QinlinClient) 和 [577fkj 的实现](https://github.com/577fkj/QinlinOpenDoor)。本地研究目录不随公开仓库发布，公开文档以 `docs/` 为入口。
