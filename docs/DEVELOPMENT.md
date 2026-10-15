# 开发指南

本文介绍 GateLink HASS 的本地开发、账号流程和协议扩展。安装与使用见 [项目 README](../README.md)，具体接口见 [API 文档](API.md)。

项目展示名称为 **GateLink HASS**，源码仓库为 [maybeLab/gate-link-hass](https://github.com/maybeLab/gate-link-hass)。内部 domain 为 `gate_link_hass`，集成目录和 Python 导入路径同步使用该名称，配置流、按钮和共用客户端等类使用 `GateLinkHass` 前缀。旧名称版本按重新安装处理，不保留旧 domain 的加载或跨 domain 迁移兼容。

## 环境与离线测试

开发环境使用 Python 3.14，测试依赖固定在 [requirements-test.txt](../requirements-test.txt)。在仓库根目录执行：

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
```

测试使用 Home Assistant Core 2026.10.0 的配置流、设备注册表、实体注册表、按钮和传感器平台；活动测试使用真实事件处理器和 Recorder 数据库结构，写入临时 SQLite 后重新打开并查询。所有 HTTP 响应在内存中模拟。测试会阻止 DNS 解析、TCP 连接和 UDP 发送，不会发送真实短信或开门请求。

覆盖范围包括：

- 短信与业务签名、手机号加密、请求格式和响应解析。
- 登录、跨小区多选、账号去重、重新认证及配置迁移。
- 门禁权限、缺失门保留、解绑后清理设备和实体。
- 按钮加载与卸载、同一门禁的并发限制、不同门禁独立开门。
- 服务端登录失效、检查记录持久化、北京时间每日检查、时区边界、多账号及失败重试。
- 账号诊断状态、连续成功的活动记录、中英文描述、设备和实体筛选、历史查询、重命名及失效账号启动。
- 开门只发送一次请求、跳过登录检查和门禁查询、忽略缓存权限及到期标记；覆盖成功、超时和服务端拒绝，确保不自动重试。

2026-10-14 的 GitHub Actions Linux / Python 3.14 离线测试为 179 项通过，测试进程正常退出，Ruff 检查与格式检查通过。测试环境有 `HomeAssistantApplication` 继承及 `backoff` 使用旧 asyncio API 的弃用警告。最新校验结果以仓库的 Validate 工作流为准。

本地 macOS / Homebrew Python 3.14.6 环境在全部测试报告通过后，可能在解释器垃圾回收阶段发生 `Segmentation fault`（退出码 139）。该问题已在不导入本项目代码、仅加载 HA 活动模块并创建注册表实体的最小脚本中独立复现；具体底层原因仍未确认。不要将此类 macOS 测试报告当作进程正常退出的证明，应核对 Linux CI 的结果与退出状态。

## HACS 与 Home Assistant 自动校验

[Validate 工作流](../.github/workflows/validate.yml) 在推送、Pull Request、每天 UTC 00:00 和手动触发时分别运行 HACS Action、hassfest 和离线测试。HACS 校验使用 `integration` 类别，不配置忽略检查项；hassfest 校验集成 manifest、配置流和翻译等元数据。离线测试在 Linux / Python 3.14 上使用固定测试依赖运行，并检查 Ruff 与格式。工作流只授予读取仓库内容的权限，不发布版本或部署。

两项元数据校验不能替代离线行为测试。每次更新后，都应在 GitHub Actions 确认三个任务均通过，再更新 GitHub Release 或收录申请中的校验链接。

## 集成图标

集成图标位于 [brand/icon.png](../custom_components/gate_link_hass/brand/icon.png)，于 2026-10-15 使用 GPT Image 2 参考原图的橙红主色、白色次色和通用圆角方形外观重新设计。主体为白色门框与双扇门图案，不保留原图的品牌汉字。生成图仅将圆角方块外侧的白底透明化，保留内部白色图案，经裁切留白和等比例缩放，保存为 256×256 的 RGBA PNG。AI 生成不代表已完成版权或商标权利核查。

深色主题版本位于 [brand/dark_icon.png](../custom_components/gate_link_hass/brand/dark_icon.png)，与标准版本使用相同图案、橙红与白色配色及透明区域，同样为 256×256 的 RGBA PNG。

另提供 [brand/icon@2x.png](../custom_components/gate_link_hass/brand/icon@2x.png) 和 [brand/dark_icon@2x.png](../custom_components/gate_link_hass/brand/dark_icon@2x.png)，均直接由 1024×1024 的生成图处理为 512×512，用于高像素密度屏幕。

按 [HA 本地品牌图片说明](https://developers.home-assistant.io/docs/core/integration/brand_images)，HA 2026.3 起会自动读取自定义集成的 `brand/` 目录；本项目要求 HA 2026.10，可直接使用该机制。未提供的 logo 由 HA 按回退规则使用相应图标。

## 代码职责

| 文件 | 职责 |
| --- | --- |
| [app_client.py](../custom_components/gate_link_hass/adapters/app_client.py) | 默认 Android App 协议：短信、登录、登录检查、小区、全部门禁和开门 |
| [mini_app_client.py](../custom_components/gate_link_hass/adapters/mini_app_client.py) | 保留的小程序协议及共用的超时、短信签名与提供器能力 |
| [models.py](../custom_components/gate_link_hass/models.py) | 会话、门禁、权限时间和安全错误类型 |
| [account.py](../custom_components/gate_link_hass/account.py) | 客户端工厂、账号运行时、检查计时器、失效提醒及并发控制 |
| [config_flow.py](../custom_components/gate_link_hass/config_flow.py) | 手机号和验证码配置流、选门、选项及重新认证 |
| [button.py](../custom_components/gate_link_hass/button.py) | 开门按钮及错误翻译 |
| [login_status.py](../custom_components/gate_link_hass/login_status.py) | 账号设备注册、检查来源、最新报告及安全事件发布 |
| [sensor.py](../custom_components/gate_link_hass/sensor.py) | 每个账号的登录状态诊断实体 |
| [logbook.py](../custom_components/gate_link_hass/logbook.py) | 登录检查事件在活动中的中英文描述 |
| [__init__.py](../custom_components/gate_link_hass/__init__.py) | 配置迁移、账号加载、注册表清理和卸载 |

## 默认协议与账号流程

默认使用 App 客户端，业务版本固定为 `version=5.2.6`、请求头 `qversioncode=3146`。登录只需要手机号和短信验证码，无需支付宝授权 `code`。短信的 `version=v2` 是短信接口版本，独立于业务版本。两套实现的差异见 [适配器对比](../custom_components/gate_link_hass/adapters/README.md)。

新建账号先发送短信，登录成功后检查会话并发现门禁，再由用户选择跨小区的门禁。登录响应若缺少用户 ID，会用新会话查询小区并补充 `userId`。保存选门前会再次检查登录状态，避免只使用配置页面打开时的检查结果。验证码不保存到配置。

一个账号对应一个配置条目。门禁设备和按钮的唯一标识由账号、小区 ID 和门禁 ID 组成，修改名称或重新认证不会改变标识。设备请求标识 `openid` 随账号持久保存，重启和修改选项后继续复用。

每扇门的设备默认使用门禁名称，按钮作为设备的主功能实体继承设备名，不追加小区名称或“开门”后缀。选门列表仍使用“小区 / 门禁”标签以区分同名门；已有实体 ID、绑定和用户自定义名称保留。

账号配置页通过描述占位符展示账号概览，使用已有手机号、会话记录和已保存的门禁选择，不额外调用用户资料接口。手机号仅展示后四位，时间按 HA 配置时区显示。登录检查通过后显示“本次检查通过”；检查发生临时错误时显示“本次未确认”，保留最近成功检查时间。门禁和小区数量按已保存的绑定计算，包含查询时暂时缺失或无权限的门禁。配置页不展示会话凭据，也不将检查参考时长显示为确定的到期时间。已确认失效的账号仍按原流程提示重新认证。

全量门禁查询合并楼栋门、自定义门和小区门，并按小区 ID 与门禁 ID 去重。已选择的门暂时缺失时保留绑定并记录缓存权限无效，用户仍可取消绑定；取消绑定后清理对应设备和实体。配置页按查询结果筛选可新增的门禁，但按钮可用性不按缓存权限或到期时间判断。保存相同选项也会重新加载，以更新门禁缓存。门禁列表请求失败后，可提交空表单重试，无需重复登录。

每次点击开门仅使用已保存的会话、小区 ID 和门禁 ID 发送一次开门请求，不调用登录检查、小区或门禁查询接口。App 开门不按缓存权限或到期时间阻止请求，实际登录和权限由开门接口判断。已确认失效的账号仍暂停开门；未绑定门禁和同一门禁并发请求在本地拒绝。开门请求不跟随重定向、不自动重试。超时不表示请求没有执行。

## 会话检查与失效

App 不执行固定 30 天本地上限，也不按登录响应的 `expires` 在本地强制失效。登录未返回 `expires` 时，`server_expires_at` 保留为 `None`。保留的小程序实现仍使用其历史期限模型。

成功的 `checkLogin` 必须返回整数 `code=0` 和正整数 `data`。正数按秒保存为检查参考：

```text
next_check_at = checked_at + check_validity_seconds
```

`checked_at`、`check_validity_seconds` 保存在账号配置中；检查不改变原登录时间、会话凭据或设备标识。当前实测值 `7776000` 相当于 90 天，服务端是否自动续期及该数值的精确语义尚未确认，不能将参考时间当作保证有效的期限。

每个已加载且尚未确认失效的账号，每天按 `Asia/Shanghai` 在北京时间 06:00 检查登录，独立于 HA 的配置时区。使用一个计时器安排每日 06:00、服务端参考时间或失败重试时间中最早的检查；其他请求更新检查记录不会推迟每日检查。启动时已通过账号加载检查，若恰好在 06:00 或之后启动，每日检查安排到下一天。每日任务只检查登录，不查询门禁或开门。

| 情况 | 处理 |
| --- | --- |
| 每日北京时间 06:00 | 每个账号独立调用 `checkLogin`，确认失效只影响对应账号 |
| 到达参考时间 | 调用 `checkLogin`，不直接使会话失效 |
| 接口确认有效 | 持久保存新的检查记录，更新下次检查时间 |
| HTTP 401、失败响应中的业务码 401 或明确的会话失效消息 | 持久标记失效，取消计时器、暂停开门、通知并启动重新认证 |
| 登录检查成功响应返回整数 `data <= 0` | 同样进入重新认证 |
| 超时、网络错误或响应格式异常 | 保留会话；后台检查 5 分钟后重试，每日 06:00 更早时优先检查；当前请求报告错误 |
| 卸载账号 | 取消检查计时器 |

账号加载和配置页面中的检查不等待参考时间。点击开门不触发这些前置检查；若开门响应明确报告登录失效，同样持久标记失效并请求重新认证。已确认失效的凭据不会因为重启而恢复使用；重新认证必须匹配原账号。旧 `lifetime_days` 选项忽略，下次保存选项时移除。

启动时确认失效的账号仍完成平台加载，诊断实体显示失效，开门按钮不可用，且保留重新认证提醒。启动时检查或门禁发现发生临时错误，则使用已保存的绑定并安排 5 分钟后检查登录；诊断状态仅反映登录检查结果，门禁列表查询失败不会把已通过的登录检查改成失败。配置本身损坏时仍使用 HA 的加载失败处理。

## 账号诊断与活动

每个账号增加独立设备，标识为 `(gate_link_hass, account:<account_id>)`，登录状态传感器唯一标识为 `<account_id>:login_status`。设备和实体注册在启动检查前完成，以便首条事件归属正确。后续事件使用实体注册表中的实际实体 ID 和设备 ID，用户重命名、重新认证、修改选门及解绑不会重建账号设备，也不改变现有门禁标识；删除账号时一并清理。

传感器默认启用，使用枚举设备类和诊断类别，状态为 `valid`、`expired`、`error`、`not_checked`。持久失效标记优先显示 `expired`；临时检查失败显示 `error`，保留最近成功时间与凭据，开门可用性不由该失败状态决定。账号失效不会让诊断实体变成不可用。属性时间保存为 UTC ISO 8601 字符串。

已配置账号的实际检查由共用函数记录，包括 `startup`、`daily`、`server_reference`、`retry`、`options_open`、`options_save` 和 `reauth`；直接调用运行时检查时使用 `manual`。每次完成的成功或已处理错误均发布 `gate_link_hass_login_check`，不按状态变化去重。未配置完成的新账号没有设备归属，因此不发布历史；取消或未完成的请求也不记录成功。开门接口确认会话失效时，以 `open_response` 发布一次失效事件，重复点击已失效账号不再发请求或重复记账。

账号配置只持久保存最新的 `login_check` 报告：`checked_at`、`source`、`result` 和 `error`。事件增加脱敏的 `account`、实际 `entity_id` 和 `device_id`，支持活动的实时与历史设备／实体筛选。日志和事件中的错误原因使用白名单键，未知键统一为 `request_rejected`，不发布原始响应、凭据、验证码或完整手机号。

`logbook.py` 注册事件的描述函数，依 HA 界面语言显示中英文来源和结果；`manifest.json` 的 `after_dependencies` 保证已启用的活动组件先完成加载。历史由 Recorder 管理，不向账号配置追加日志数组。部署环境须启用 Recorder 与活动组件，并检查其事件及实体筛选；保留天数、数据库清理和备份沿用 HA 配置。仪表盘卡片示例见 [项目 README](../README.md#登录状态与活动记录)。

## 配置迁移

更换 domain 后，旧名称版本的配置不能由当前集成加载，需删除旧集成后重新安装、短信登录并选门，同时更新自动化及 HomeKit 对按钮的引用。下面的格式迁移仅适用于当前 domain 下的配置条目。

当前配置格式为 v3，记录 `protocol=android_app` 和设备请求标识。v2 账号升级时保留条目、绑定和 HA 设备、实体标识，设置失效状态并要求 App 短信重新认证；旧会话不直接跨协议使用。认证成功后恢复原绑定，未返回的门保留绑定并记录缓存权限无效，按钮仍通过开门响应判断实际权限。没有账号凭据的 v1 配置不能自动迁移，需重新添加。

配置格式版本与集成软件版本分开管理。

## 签名和认证扩展

内置短信签名器将手机号与 `appid`、随机四位 `nonce`、毫秒时间戳、`version=v2` 按字段名排序，追加协议固定密钥后计算大写 MD5 签名。业务请求使用 App 签名，登录手机号使用 AES-ECB 加密；算法和字段位置见 [API 文档](API.md)。协议固定密钥不是用户账号凭据。

默认 App 客户端允许注入同步或异步的 `sms_signer(body, timestamp, nonce)`：

- `body` 为实际发送的紧凑 JSON 请求体。
- `timestamp` 为毫秒时间戳字符串，`nonce` 为本次随机数。
- 提供器返回 32 位十六进制签名，发送时统一转换为大写。
- 显式设置 `sms_signer=None` 会停用签名器；签名器异常或返回无效签名时，在发送验证码前报错。

可通过 `hass.data["gate_link_hass"]["client_factory"]` 注入客户端工厂，用于协议扩展和离线验证。工厂接受 HA 的 HTTP 会话、`timeout` 和可选的 `device_id`。默认工厂创建 App 客户端，产品配置界面不暴露提供器选择。

## 本地研究与凭据

`research/` 是本地参考项目和测试工具目录，已加入 `.gitignore`，不属于公开仓库所需文件。本指南和 API 文档独立于该目录，常规离线测试不依赖它。

`.local/` 同样忽略，用于保留可复用的本地测试会话。目录权限为 `0700`，会话文件为 `0600`；不在每轮测试后清理有效会话，也不保存验证码。需要主动清理时删除本地会话文件。

原始 HAR、真实手机号、验证码和账号凭据不得放入源码、测试夹具或文档。运行日志与错误信息不包含凭据、完整手机号或原始响应。

## 验证与协议来源

2026-10-14 使用默认 App 协议实测通过短信、登录、专用登录检查、小区和全部门禁查询，以及一次用户指定门禁的开门请求。全量接口返回 21 扇门，开门取得 `code=0/openDoorState=1` 服务端回执。随后复用保留会话复核登录检查与检查记录持久化，没有重复开门。服务端回执不证明现场门的物理状态。

同日已在 Home Assistant Core 2026.10.0 完成安装、账号加载及按钮名称验证。按钮默认仅显示门禁名，更新名称后现有实体 ID 和绑定保持不变。

App 基础协议参考 [rponeawa 的实现](https://github.com/rponeawa/QinlinOpen)，相关许可与版权信息保留在 [第三方 MIT 声明](../custom_components/gate_link_hass/adapters/LICENSE-QinlinOpen)。全部门禁和登录检查另参考 [shacha086 的实现](https://github.com/shacha086/QinlinClient) 与 [577fkj 的实现](https://github.com/577fkj/QinlinOpenDoor)。
