"""Mini-app account and gate protocol with replaceable authentication providers."""

import hashlib
import inspect
import json
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout

from ..const import (
    API_ORIGIN,
    CHECK_PATH,
    COMMUNITIES_PATH,
    DEFAULT_TIMEOUT,
    GATES_PATH,
    LOGIN_PATH,
    OPEN_PATH,
    REFERER_GATES,
    REFERER_LOGIN,
    SMS_APP_ID,
    SMS_APP_SECRET,
    SMS_NONCE,
    SMS_ORIGIN,
    SMS_PATH,
    SMS_VERSION,
    USER_AGENT_GATES,
    USER_AGENT_LOGIN,
)
from ..models import (
    AccountSession,
    ApiError,
    AuthenticationExpired,
    Gate,
    identifier,
    permission_deadline,
)

type AuthCodeProvider = Callable[[], str | Awaitable[str]]
type SmsSigner = Callable[[str, str, str], str | Awaitable[str]]


def default_sms_signer(body: str, timestamp: str, nonce: str) -> str:
    """Match the captured SMS protocol's sorted fields and uppercase MD5."""
    fields = json.loads(body)
    if (
        not isinstance(fields, dict)
        or set(fields) != {"mobile"}
        or not isinstance(fields["mobile"], str)
    ):
        raise ValueError("Invalid SMS body")
    fields.update(
        appid=SMS_APP_ID, nonce=nonce, timestamp=timestamp, version=SMS_VERSION
    )
    canonical = "&".join(f"{key}={fields[key]}" for key in sorted(fields))
    canonical += f"&appsecret={SMS_APP_SECRET}"
    # This protocol uses MD5; the signed SMS fields contain only ASCII.
    return (
        hashlib.md5(canonical.encode("ascii"), usedforsecurity=False)
        .hexdigest()
        .upper()
    )


def placeholder_auth_code() -> str:
    """Produce a shape-compatible placeholder, not valid authorization."""
    return "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
    )


class GateLinkHassClient:
    def __init__(
        self,
        session: ClientSession,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        auth_code_provider: AuthCodeProvider = placeholder_auth_code,
        sms_signer: SmsSigner | None = default_sms_signer,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._timeout = ClientTimeout(total=timeout)
        self._code_provider = auth_code_provider
        self._sms_signer = sms_signer
        self.clock = clock
        self.auth: AccountSession | None = None
        self.lifetime_days = 30

    def require_auth(self) -> AccountSession:
        if self.auth is None or self.auth.expired(self.clock(), self.lifetime_days):
            raise AuthenticationExpired()
        return self.auth

    async def _code(self) -> str:
        try:
            code = self._code_provider()
            if inspect.isawaitable(code):
                code = await code
        except Exception:
            raise ApiError("authorization_unavailable") from None
        if (
            not isinstance(code, str)
            or len(code) != 32
            or not code.isascii()
            or not code.isalnum()
        ):
            raise ApiError("authorization_unavailable")
        return code

    async def _request(
        self,
        path: str,
        *,
        fields: dict[str, Any] | None = None,
        login: bool = False,
        sms: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        token = "" if sms else "null" if login else self.require_auth().session_id
        headers = {
            "User-Agent": USER_AGENT_LOGIN if login or sms else USER_AGENT_GATES,
            "Referer": REFERER_LOGIN if login or sms else REFERER_GATES,
            "Accept": "*/*",
            "Accept-Charset": "utf-8",
            "Accept-Encoding": "gzip",
            "Cookie": "",
            "Content-Type": "application/json"
            if sms
            else "application/x-www-form-urlencoded",
            **(extra_headers or {}),
        }
        body = (
            json.dumps(fields, separators=(",", ":"))
            if sms
            else urlencode(fields or {})
        )
        try:
            async with self._session.request(
                "POST",
                (SMS_ORIGIN if sms else API_ORIGIN) + path,
                params={"sessionId": token},
                data=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    if not login and not sms:
                        raise AuthenticationExpired()
                    raise ApiError("verification_failed")
                if response.status == 403:
                    raise ApiError("permission_denied")
                if not 200 <= response.status < 300:
                    raise ApiError("http_error")
                try:
                    result = await response.json(content_type=None)
                except ValueError, UnicodeError:
                    raise ApiError("invalid_response") from None
                if not isinstance(result, dict) or type(result.get("code")) is not int:
                    raise ApiError("invalid_response")
                if result["code"] != 0:
                    message = str(result.get("message", "")).lower()
                    if (
                        not login
                        and not sms
                        and any(
                            phrase in message
                            for phrase in (
                                "session expired",
                                "session invalid",
                                "token expired",
                                "登录已失效",
                                "登录已过期",
                                "会话已过期",
                                "请重新登录",
                                "sessionid无效",
                                "未登录",
                            )
                        )
                    ):
                        raise AuthenticationExpired()
                    raise ApiError(
                        "verification_failed" if login else "request_rejected"
                    )
                return result.get("data")
        except TimeoutError:
            raise ApiError("timeout") from None
        except ClientError:
            raise ApiError("cannot_connect") from None

    async def send_sms(self, phone: str) -> None:
        if self._sms_signer is None:
            raise ApiError("signing_unavailable")
        timestamp = str(int(self.clock() * 1000))
        nonce = SMS_NONCE
        body = json.dumps({"mobile": phone}, separators=(",", ":"))
        try:
            sign = self._sms_signer(body, timestamp, nonce)
            if inspect.isawaitable(sign):
                sign = await sign
        except Exception:
            raise ApiError("signing_unavailable") from None
        if (
            not isinstance(sign, str)
            or len(sign) != 32
            or any(c not in string.hexdigits for c in sign)
        ):
            raise ApiError("signing_unavailable")
        await self._request(
            SMS_PATH,
            sms=True,
            fields={"mobile": phone},
            extra_headers={
                "appid": SMS_APP_ID,
                "version": SMS_VERSION,
                "timestamp": timestamp,
                "nonce": nonce,
                "sign": sign.upper(),
            },
        )

    async def login(self, phone: str, verification_code: str) -> AccountSession:
        data = await self._request(
            LOGIN_PATH,
            login=True,
            fields={
                "code": await self._code(),
                "mobile": phone,
                "securityCode": verification_code,
            },
        )
        self.auth = AccountSession.from_login(data, self.clock())
        return self.auth

    async def check_login(self) -> None:
        auth = self.require_auth()
        data = await self._request(CHECK_PATH, fields={"code": await self._code()})
        checked = AccountSession.from_login(data, self.clock())
        if checked.account_id != auth.account_id:
            raise AuthenticationExpired()
        # Checking a session never extends its original lifetime.

    async def discover_gates(self) -> dict[str, Gate]:
        communities = await self._request(COMMUNITIES_PATH)
        if not isinstance(communities, list):
            raise ApiError("invalid_response")
        gates: dict[str, Gate] = {}
        seen: set[str] = set()
        for community in communities:
            if not isinstance(community, dict):
                raise ApiError("invalid_response")
            try:
                community_id = identifier(community["communityId"])
                community_name = identifier(community["communityName"])
            except KeyError:
                raise ApiError("invalid_response") from None
            if community_id in seen:
                continue
            seen.add(community_id)
            data = await self._request(GATES_PATH, fields={"communityId": community_id})
            if not isinstance(data, dict) or not isinstance(
                data.get("userDoorDTOS"), list
            ):
                raise ApiError("invalid_response")
            candidates = list(data["userDoorDTOS"])
            if common := data.get("commonlyUsedDoor"):
                candidates.append(common)
            status = community.get("applyStatusDTO") or {}
            if not isinstance(status, dict):
                raise ApiError("invalid_response")
            permission = (
                community.get("hasPermission", True) is True
                and status.get("statusExpired", 0) == 0
                and data.get("authorizeExpired", 0) == 0
            )
            deadline = permission_deadline(community.get("expiryDate"))
            for door in candidates:
                try:
                    gate = Gate(
                        community_id,
                        community_name,
                        identifier(door["doorControlId"]),
                        identifier(door["doorControlName"]),
                        identifier(door["macAddress"]),
                        permission,
                        deadline,
                    )
                except KeyError, TypeError:
                    raise ApiError("invalid_response") from None
                gates.setdefault(gate.key, gate)
        return gates

    async def open_door(self, gate: Gate) -> None:
        self.require_auth()
        if not gate.available(self.clock()):
            raise ApiError("permission_denied")
        data = await self._request(
            OPEN_PATH,
            fields={
                "doorControlId": gate.door_id,
                "macAddress": gate.mac_address,
                "openType": "",
            },
        )
        if not isinstance(data, dict):
            raise ApiError("invalid_response")
        if type(data.get("openDoorState")) is not int or data["openDoorState"] != 1:
            raise ApiError("open_failed")
