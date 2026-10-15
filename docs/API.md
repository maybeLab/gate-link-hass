# App 适配器 API 文档

本文记录 [App 客户端](../custom_components/gate_link_hass/adapters/app_client.py) 当前实际使用的云服务接口，按 2026-10-14 的实现整理。业务版本统一为 **5.2.6 / 3146**。这是一份实现文档，服务端字段的完整定义及未验证语义不在此范围内。

示例中的账号、设备、小区、门禁和凭据均为虚构数据；`<SIGN>` 等占位值需要由调用方计算或替换。响应示例只展示适配器使用的结构，不代表完整原始响应。HA 账号流程及测试方式见 [开发指南](DEVELOPMENT.md)。

## 接口总览

所有接口均使用 HTTPS **POST**，不跟随重定向。

| 用途 | 域名 | 路径 | 参数位置 | 会话 |
| --- | --- | --- | --- | --- |
| 发送短信 | `https://gateway2.qinlinkeji.com` | `/member/sms/sendSecurityCode` | JSON body，签名在请求头 | 不携带 |
| 手机号登录 | `https://mobileapi3.qinlinkeji.com` | `/api/app/v1/login` | 签名 JSON body | URL 中为空字符串 |
| 登录检查 | 同上 | `/api/app/v1/checkLogin` | 签名 JSON body | URL `sessionId` |
| 查询小区 | 同上 | `/api/app/user/v2/communityInfo` | 签名 JSON body | URL `sessionId` |
| 查询全部门禁 | 同上 | `/api/app/user/v2/queryAllUserDoorByCache` | 签名 multipart body | URL `sessionId` |
| 开门 | 同上 | `/api/open/doorcontrol/v2/open` | 业务字段和签名全部放 URL，body 为空 | URL `sessionId` |

## 公共请求约定

### 请求头

所有接口共用以下设备请求头：

| 请求头 | 默认值或来源 |
| --- | --- |
| `User-Agent` | `Dart/3.8 (dart:io)` |
| `openid` | 持久化的设备请求标识；未提供时由 `secrets.token_hex(8)` 生成 |
| `qversioncode` | `3146` |
| `qchannel` | `google` |
| `qplatform` | `0` |
| `qvendor` | `google` |

JSON 接口和空 body 的开门请求设置 `Content-Type: application/json; charset=utf-8`。门禁查询由 HTTP 库生成 `multipart/form-data` 及 boundary。请求超时默认 10 秒，HA 配置允许设置为 1～60 秒。

### 会话传递

业务请求通过 URL 查询参数 `sessionId` 传递会话。登录请求也携带该参数，但值为**空字符串**；短信请求不携带 `sessionId`。用于计算业务签名的 `token` 与本次 `sessionId` 相同，登录时也为空。

```text
POST /api/app/v1/login?sessionId=
POST /api/app/v1/checkLogin?sessionId=<SESSION_ID>
```

### 业务公共字段与签名

短信之外的每个接口，在其业务字段上增加：

| 字段 | 类型 | 值 |
| --- | --- | --- |
| `timestamp` | 整数 | 当前 Unix 毫秒时间戳 |
| `version` | 字符串 | `5.2.6` |
| `nonce` | 字符串 | 随机五位十进制数字，可包含前导零 |
| `sign` | 字符串 | 32 位大写十六进制 MD5 签名 |

签名步骤：

1. 取业务字段和 `timestamp/version/nonce`，额外加入 `token`。
2. 按字段名排序，用 Python `urlencode(..., safe=":")` 编码；空格编码为 `+`，冒号保留。
3. 在结果末尾追加 `&key=` 和固定 `SIGNING_SALT`，对 UTF-8 字节计算 MD5。
4. 转为大写十六进制，将结果作为 `sign` 发送。`token` 仅参与签名，不作为独立业务字段发送；`sign` 自身不参与计算。

```python
import hashlib
from urllib.parse import urlencode

from custom_components.gate_link_hass.adapters.app_client import SIGNING_SALT

# request_fields 已包含业务字段和 timestamp/version/nonce，不包含 sign。
canonical = urlencode(sorted({**request_fields, "token": session_id}.items()), safe=":")
sign = hashlib.md5(
    (canonical + "&key=" + SIGNING_SALT).encode("utf-8"),
    usedforsecurity=False,
).hexdigest().upper()
```

签名函数为 `signed_fields()`，协议常量 `SIGNING_SALT` 定义在 [app_client.py](../custom_components/gate_link_hass/adapters/app_client.py)。JSON 字段中的整数保持整数，multipart 中的所有字段转换为字符串；开门字段由 HTTP 库编码为 URL 查询参数。

## 1. 发送短信验证码

**客户端方法：** `send_sms(phone)`

```text
POST https://gateway2.qinlinkeji.com/member/sms/sendSecurityCode
```

短信使用独立的签名规则。除设备请求头外，增加：

| 请求头 | 值 |
| --- | --- |
| `appid` | `gbDjIZQSOpCMX49P` |
| `version` | `v2`，短信接口版本 |
| `timestamp` | 当前 Unix 毫秒时间戳字符串 |
| `nonce` | 随机四位十进制数字 |
| `sign` | 短信签名，大写 MD5 |

body 为紧凑 JSON，手机号保持明文：

```json
{"mobile":"13800000000"}
```

签名取 `mobile/appid/version/timestamp/nonce`，按字段名排序，直接拼接为 `key=value&key=value`，不做业务签名的 URL 编码；末尾追加 `&appsecret=` 和固定 `SMS_APP_SECRET`，对 ASCII 字节计算大写 MD5。该签名只放在请求头，body 不添加公共业务签名字段。

常量 `SMS_APP_ID/SMS_APP_SECRET/SMS_VERSION` 见 [const.py](../custom_components/gate_link_hass/const.py)，算法见 [default_sms_signer()](../custom_components/gate_link_hass/adapters/mini_app_client.py)。固定协议密钥与账号会话凭据分开使用。

响应示例：

```json
{
  "code": 0,
  "success": true,
  "message": "操作成功",
  "data": null
}
```

满足通用成功条件即视为发送请求成功，方法不返回验证码。验证码需由用户从手机短信中输入，不会持久保存。签名器不可用或输出异常时，在发出请求前返回 `signing_unavailable`。

## 2. 手机号登录

**客户端方法：** `login(phone, verification_code)`

```text
POST https://mobileapi3.qinlinkeji.com/api/app/v1/login?sessionId=
```

签名时 `token=""`。业务字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `mobile` | 字符串 | 手机号 AES 加密后的大写十六进制 |
| `smsCode` | 字符串 | 六位验证码，保留前导零 |
| `appChannel` | 整数 | `1` |

手机号按 UTF-8 编码，使用 AES-ECB、16 字节分组和 PKCS7 填充，对应 Android 的 `AES/ECB/PKCS5Padding`。密钥为 `bytes.fromhex(HEX_AES_KEY)`，常量和 `encrypt_phone()` 位于 [app_client.py](../custom_components/gate_link_hass/adapters/app_client.py)。签名使用**加密后的**手机号；仅手机号字段进行此加密。

JSON body 示例：

```json
{
  "mobile": "<ENCRYPTED_PHONE>",
  "smsCode": "012345",
  "appChannel": 1,
  "timestamp": 1800000000000,
  "version": "5.2.6",
  "nonce": "12345",
  "sign": "<SIGN>"
}
```

已观察到的登录响应包含 `flag/sessionId`，未提供用户 ID 或到期时长。结构示例：

```json
{
  "code": 0,
  "data": {
    "flag": 0,
    "sessionId": "fixture-session-not-a-real-credential"
  }
}
```

`flag` 的含义不参与当前判断。适配器提取 `sessionId`，也兼容 `sessionID/token`；提取 `userId`，也兼容 `uid`。用户 ID 缺失时，立即用新会话调用 `communityInfo` 补充。该步骤失败时，`login()` 不返回完整账号会话。

返回值为 `AccountSession`。登录时保存 `issued_at`；若响应提供正数 `expires`，按秒转换为 `server_expires_at`，否则为 `None`。App 不按该本地时间直接判定登录失效，后续使用 `checkLogin` 和业务接口结果。

## 3. 检查登录状态

**客户端方法：** `check_login()`

```text
POST https://mobileapi3.qinlinkeji.com/api/app/v1/checkLogin?sessionId=<SESSION_ID>
```

业务字段只有 `method=checkLogin`，附加公共签名字段，发送 JSON body：

```json
{
  "method": "checkLogin",
  "timestamp": 1800000000000,
  "version": "5.2.6",
  "nonce": "12345",
  "sign": "<SIGN>"
}
```

有效会话的已验证响应：

```json
{
  "code": 0,
  "data": 7776000,
  "message": "操作成功"
}
```

无效会话的已验证响应，HTTP 状态同样为 200：

```json
{
  "code": 401,
  "data": "",
  "message": "您还没有登录，或者登录已超时，请重新登录！",
  "success": false
}
```

该接口严格要求整数 `code=0`，并按以下规则处理：

| `data` 或错误 | 处理 |
| --- | --- |
| 正整数 | 会话有效；保存本次 `checked_at` 和 `check_validity_seconds=data` |
| 整数 `<= 0` | 会话失效，抛出 `AuthenticationExpired` |
| 字符串、浮点数、布尔值、空值等非整数，或无法用于检查时间的异常大数 | `invalid_response`，不判定失效 |
| HTTP 401、业务码 401 或明确会话失效消息 | 会话失效 |
| 网络、超时或其他请求错误 | 不判定失效，由调用方处理 |

方法成功返回 `None`，更新后的检查记录在 `client.auth` 中，HA 运行时会持久保存。`7776000` 相当于 90 天，当前仅按秒用作**下次检查参考**。到参考时间重新调用接口，有效就继续使用；检查不替换 token，也不改变登录时间或设备标识。服务端是否自动续期尚未确认。

HA 对每个已加载且尚未确认失效的账号每天北京时间 06:00 调用本接口，不受 HA 配置时区影响。服务端参考时间更早时提前检查；后台检查失败时 5 分钟后重试，每日检查时间更早时优先执行。明确失效则取消定时检查、暂停开门并提示重新认证，不进入该重试循环。每日检查不会发送短信或开门请求。

## 4. 查询小区

**客户端方法：** `get_communities()`

```text
POST https://mobileapi3.qinlinkeji.com/api/app/user/v2/communityInfo?sessionId=<SESSION_ID>
```

业务字段只有 `method=communityInfo`，附加公共签名字段并发送 JSON：

```json
{
  "method": "communityInfo",
  "timestamp": 1800000000000,
  "version": "5.2.6",
  "nonce": "12345",
  "sign": "<SIGN>"
}
```

示例结构：

```json
{
  "code": 0,
  "data": [
    {
      "userId": 42,
      "communityId": 101,
      "communityName": "示例小区",
      "hasPermission": true,
      "expiryDate": "2080-01-01 00:00:00",
      "applyStatusDTO": {"statusExpired": 0}
    }
  ]
}
```

`data` 可为列表或对象。适配器递归查找包含 `communityId` 的对象并按小区 ID 去重；小区名称依次兼容 `communityName/name/communityShortName`，缺失时使用小区 ID。登录所需的 `userId/uid` 也从响应中递归查找。

门禁发现时使用的小区权限字段：

| 字段 | 当前处理 |
| --- | --- |
| `hasPermission` | 缺省为 `true`；显式值必须为布尔 `true` 才通过 |
| `applyStatusDTO.statusExpired` | 缺省为 `0`，等于 `0` 才通过 |
| `expiryDate` | 小区权限到期时间；缺省或空值表示未提供本地期限，非空值按 ISO 格式解析 |

`expiryDate` 没有时区时按 `Asia/Shanghai` 解释。它用于小区权限判断，与 token 登录期限分开处理。

## 5. 查询全部授权门禁

**客户端方法：** `discover_gates()`，先查询小区，再为每个小区调用本接口。

```text
POST https://mobileapi3.qinlinkeji.com/api/app/user/v2/queryAllUserDoorByCache?sessionId=<SESSION_ID>
```

业务字段为 `communityId`。数字形式的小区 ID 在签名前转换为整数，其他 ID 保留字符串。所有业务和公共签名字段以 multipart 文本字段发送：

```text
communityId: 101
timestamp: 1800000000000
version: 5.2.6
nonce: 12345
sign: <SIGN>
```

示例结构：

```json
{
  "code": 0,
  "data": {
    "authorizeExpired": 1,
    "buildingDoorList": [
      {"doorControlId": 1, "doorControlName": "楼栋入口"}
    ],
    "customDoorList": [
      {"doorControlId": 2, "doorControlName": "自定义门"}
    ],
    "gateDoorList": [
      {"doorControlId": 3, "doorControlName": "小区入口"}
    ]
  }
}
```

`data` 可为列表或对象。适配器递归查找包含 `doorControlId` 的对象，典型来源为三类门禁列表；跳过空值和 ID `0`，按 `communityId:doorControlId` 去重。门禁名称兼容 `doorControlName/doorName/name`，缺失时使用门禁 ID。`macAddress` 可以缺省或为空，App 开门无需 MAC。

返回值为 `dict[str, Gate]`，每个门继承对应小区的权限和权限到期时间。

`authorizeExpired` 不是 token 到期字段。实测响应为 `1` 时，已选择门禁仍取得开门成功回执；其准确语义尚未确认，App 适配器不据此统一拒绝门禁。小区明确的权限状态及实际开门接口仍执行校验。

`queryUserDoorByCacheNew` 是常用／收藏门查询，本项目主流程使用上述全量接口，空列表时也不会回退查询收藏门。

## 6. 开门

**客户端方法：** `open_door(gate)`

```text
POST https://mobileapi3.qinlinkeji.com/api/open/doorcontrol/v2/open
```

**全部字段放在 URL 查询参数中，POST body 为空。**

| 查询参数 | 说明 |
| --- | --- |
| `sessionId` | 当前会话 |
| `appChannel` | `1` |
| `doorControlId` | 门禁 ID，数字形式在签名前转换为整数 |
| `communityId` | 小区 ID，数字形式在签名前转换为整数 |
| `timestamp/version/nonce/sign` | 公共业务签名字段 |

字段布局示例，实际发送时由 HTTP 库编码：

```text
sessionId=<SESSION_ID>
appChannel=1
doorControlId=1
communityId=101
timestamp=1800000000000
version=5.2.6
nonce=12345
sign=<SIGN>
```

响应示例：

```json
{
  "code": 0,
  "message": "操作成功",
  "data": {"openDoorState": 1}
}
```

只有整数 `code=0`、对象类型的 `data` 和整数 `openDoorState=1` 同时满足才视为成功，`success=true` 本身不能替代这些条件。其他开门状态返回 `open_failed`；响应结构异常返回 `invalid_response`。

HA 的 `AccountRuntime.open()` 和 App 客户端的 `open_door()` 均直接发送一次开门请求，不做登录检查、小区或门禁查询，也不按缓存的权限或到期时间阻止请求。使用已有会话和绑定门禁的 ID，由开门接口判断实际登录与权限；明确的会话失效响应会持久保存失效状态、通知并请求重新认证。已确认失效、没有会话、未绑定门禁及同门并发请求仍在本地拒绝。开门不自动重试，服务端成功回执不等于现场状态确认。

## 响应解析与错误处理

响应按普通 JSON 解析，没有响应解密或整包 body 加密操作。支持 `data` 为序列化 JSON 对象／数组字符串的情况；解析这种字符串不是解密。短信、登录、小区及门禁查询的通用成功条件为 `success=true`，或整数 `code=0/200..299`；登录检查与开门只接受整数 `code=0`。接口成功后还需通过各自的数据校验。

| 条件 | 错误或行为 |
| --- | --- |
| 已认证业务请求 HTTP 401 | `AuthenticationExpired`，错误键 `account_expired` |
| 失败业务响应的码为 401，或包含明确的会话失效消息 | 同上；不将普通业务拒绝一律当成 token 失效 |
| 短信或登录请求 HTTP 401 | `verification_failed` |
| HTTP 403 | `permission_denied` |
| 其他非 2xx，包括重定向 | `http_error` |
| HTTP 成功但业务拒绝 | 登录为 `verification_failed`，其他接口为 `request_rejected` |
| 超时 | `timeout`，开门不自动重试 |
| HTTP 客户端连接错误 | `cannot_connect` |
| JSON、数据类型或必要字段异常 | `invalid_response` |

对已认证请求识别的会话失效消息包括 `session expired`、`session invalid`、`token expired`、`登录已失效`、`登录已过期`、`会话已过期`、`请重新登录`、`sessionid无效` 和 `未登录`。

错误使用安全键传给 HA，不附带原始响应或含会话凭据的请求 URL。业务字段签名、短信签名和手机号加密均由客户端本地完成，HTTPS 提供传输加密。

## 实现与来源

- [app_client.py](../custom_components/gate_link_hass/adapters/app_client.py)：本文件对应的实际协议实现。
- [account.py](../custom_components/gate_link_hass/account.py)：HA 检查调度、失效持久化和开门流程。
- [适配器对比](../custom_components/gate_link_hass/adapters/README.md)：App 与保留的小程序实现。
- [rponeawa 的参考实现](https://github.com/rponeawa/QinlinOpen)：App 基础协议参考。
- [shacha086 的参考实现](https://github.com/shacha086/QinlinClient)、[577fkj 的参考实现](https://github.com/577fkj/QinlinOpenDoor)：全量门禁与专用登录检查参考。

第三方代码的许可与版权信息保留在 [MIT 声明](../custom_components/gate_link_hass/adapters/LICENSE-QinlinOpen)。
