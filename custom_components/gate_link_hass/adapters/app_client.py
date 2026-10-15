"""Default Android App 5.2.6 transport for the HA account flow.

Protocol reference: https://github.com/rponeawa/QinlinOpen (MIT).
Full-door endpoint reference: https://github.com/shacha086/QinlinClient.
This adapter is the default transport and supports client_factory injection.
"""

import hashlib
import inspect
import json
import secrets
import string
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, FormData
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..const import SMS_APP_ID, SMS_VERSION
from ..models import (
    AccountSession,
    ApiError,
    AuthenticationExpired,
    Gate,
    identifier,
    permission_deadline,
    positive_number,
)
from .mini_app_client import GateLinkHassClient

API_ORIGIN = "https://mobileapi3.qinlinkeji.com"
SMS_ORIGIN = "https://gateway2.qinlinkeji.com"
SMS_PATH = "/member/sms/sendSecurityCode"
LOGIN_PATH = "/api/app/v1/login"
CHECK_PATH = "/api/app/v1/checkLogin"
COMMUNITIES_PATH = "/api/app/user/v2/communityInfo"
# queryUserDoorByCacheNew only returns commonly used/favorited doors.
GATES_PATH = "/api/app/user/v2/queryAllUserDoorByCache"
OPEN_PATH = "/api/open/doorcontrol/v2/open"
APP_VERSION = "5.2.6"
APP_VERSION_CODE = "3146"
SIGNING_SALT = "qiAnlPinP"
HEX_AES_KEY = "FBC213C4C7BEEBD2AA4EDBF0F681C41B"


def random_nonce(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def encrypt_phone(phone: str) -> str:
    """Match Android AES/ECB/PKCS5Padding (PKCS7 for a 16-byte block)."""
    padder = padding.PKCS7(128).padder()
    plain = padder.update(phone.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(bytes.fromhex(HEX_AES_KEY)), modes.ECB()
    ).encryptor()
    return (encryptor.update(plain) + encryptor.finalize()).hex().upper()


def normalize_json(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except ValueError:
            raise ApiError("invalid_response") from None
    return value


def objects_with(root: Any, key: str) -> Iterator[dict[str, Any]]:
    root = normalize_json(root)
    if isinstance(root, dict):
        if key in root:
            yield root
        for value in root.values():
            yield from objects_with(value, key)
    elif isinstance(root, list):
        for value in root:
            yield from objects_with(value, key)


def find_value(root: Any, keys: tuple[str, ...]) -> Any:
    root = normalize_json(root)
    if isinstance(root, dict):
        for key in keys:
            if root.get(key) not in (None, ""):
                return root[key]
        children = root.values()
    elif isinstance(root, list):
        children = root
    else:
        return None
    for value in children:
        if (found := find_value(value, keys)) is not None:
            return found
    return None


class QinlinAppClient(GateLinkHassClient):
    def __init__(
        self,
        *args: Any,
        device_id: str | None = None,
        nonce_factory: Callable[[int], str] = random_nonce,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._nonce = nonce_factory
        self._headers = {
            "User-Agent": "Dart/3.8 (dart:io)",
            "openid": device_id or secrets.token_hex(8),
            "qversioncode": APP_VERSION_CODE,
            "qchannel": "google",
            "qplatform": "0",
            "qvendor": "google",
        }

    @property
    def device_id(self) -> str:
        return self._headers["openid"]

    def require_auth(self) -> AccountSession:
        """Only the server decides whether an existing App session is valid."""
        if self.auth is None:
            raise AuthenticationExpired()
        return self.auth

    def signed_fields(self, fields: dict[str, Any], token: str) -> dict[str, Any]:
        fields = {
            **fields,
            "timestamp": int(self.clock() * 1000),
            "version": APP_VERSION,
            "nonce": self._nonce(5),
        }
        canonical = urlencode(sorted({**fields, "token": token}.items()), safe=":")
        fields["sign"] = (
            hashlib.md5(
                (canonical + "&key=" + SIGNING_SALT).encode(), usedforsecurity=False
            )
            .hexdigest()
            .upper()
        )
        return fields

    async def _app_request(
        self,
        path: str,
        fields: dict[str, Any],
        *,
        login: bool = False,
        sms: bool = False,
        mode: str = "json",
        extra_headers: dict[str, str] | None = None,
        token_override: str | None = None,
    ) -> Any:
        token = (
            token_override
            if token_override is not None
            else ""
            if login or sms
            else self.require_auth().session_id
        )
        params = {} if sms else {"sessionId": token}
        fields = fields if sms else self.signed_fields(fields, token)
        headers = {**self._headers, **(extra_headers or {})}
        if mode == "multipart":
            body = FormData(default_to_multipart=True)
            for key, value in fields.items():
                body.add_field(key, str(value))
        else:
            headers["Content-Type"] = "application/json; charset=utf-8"
            if mode == "query":
                params.update(fields)
                body = b""
            else:
                body = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        try:
            async with self._session.request(
                "POST",
                (SMS_ORIGIN if sms else API_ORIGIN) + path,
                params=params,
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
                if not isinstance(result, dict):
                    raise ApiError("invalid_response")
                code = result.get("code")
                successful = result.get("success") is True or (
                    type(code) is int and (code == 0 or 200 <= code < 300)
                )
                if path in (OPEN_PATH, CHECK_PATH):
                    successful = type(code) is int and code == 0
                if not successful:
                    message = str(result.get("message", "")).lower()
                    if (
                        not login
                        and not sms
                        and (
                            code == 401
                            or any(
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
                        )
                    ):
                        raise AuthenticationExpired()
                    raise ApiError(
                        "verification_failed" if login else "request_rejected"
                    )
                return normalize_json(result.get("data"))
        except TimeoutError:
            raise ApiError("timeout") from None
        except ClientError:
            raise ApiError("cannot_connect") from None

    async def send_sms(self, phone: str) -> None:
        if self._sms_signer is None:
            raise ApiError("signing_unavailable")
        timestamp = str(int(self.clock() * 1000))
        nonce = self._nonce(4)
        body = json.dumps({"mobile": phone}, separators=(",", ":"))
        # Keep the existing sync/async signing-provider contract.
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
        await self._app_request(
            SMS_PATH,
            {"mobile": phone},
            sms=True,
            extra_headers={
                "appid": SMS_APP_ID,
                "version": SMS_VERSION,
                "timestamp": timestamp,
                "nonce": nonce,
                "sign": sign.upper(),
            },
        )

    async def login(self, phone: str, verification_code: str) -> AccountSession:
        data = await self._app_request(
            LOGIN_PATH,
            {
                "mobile": encrypt_phone(phone),
                "smsCode": verification_code,
                "appChannel": 1,
            },
            login=True,
        )
        token = identifier(find_value(data, ("sessionId", "sessionID", "token")))
        user_id = find_value(data, ("userId", "uid"))
        if user_id is None:
            communities = await self._app_request(
                COMMUNITIES_PATH,
                {"method": "communityInfo"},
                token_override=token,
            )
            user_id = find_value(communities, ("userId", "uid"))
        user_id = identifier(user_id)
        now = self.clock()
        expires = find_value(data, ("expires",))
        deadline = now + positive_number(expires) if expires is not None else None
        self.auth = AccountSession(user_id, token, now, deadline)
        return self.auth

    async def check_login(self) -> None:
        """Check validity and save the returned seconds for the next check."""
        data = await self._app_request(CHECK_PATH, {"method": "checkLogin"})
        if type(data) is not int:
            raise ApiError("invalid_response")
        if data <= 0:
            raise AuthenticationExpired()
        self.auth = AccountSession.from_dict(
            replace(
                self.require_auth(),
                checked_at=self.clock(),
                check_validity_seconds=data,
            ).to_dict()
        )

    async def get_communities(self) -> list[dict[str, Any]]:
        data = await self._app_request(COMMUNITIES_PATH, {"method": "communityInfo"})
        if not isinstance(data, (list, dict)):
            raise ApiError("invalid_response")
        communities = {}
        for item in objects_with(data, "communityId"):
            communities.setdefault(identifier(item["communityId"]), item)
        return list(communities.values())

    async def discover_gates(self) -> dict[str, Gate]:
        gates = {}
        for community in await self.get_communities():
            cid = identifier(community["communityId"])
            name = find_value(
                community, ("communityName", "name", "communityShortName")
            )
            community_name = identifier(name) if name is not None else cid
            data = await self._app_request(
                GATES_PATH,
                {"communityId": int(cid) if cid.isdecimal() else cid},
                mode="multipart",
            )
            if not isinstance(data, (list, dict)):
                raise ApiError("invalid_response")
            status = community.get("applyStatusDTO") or {}
            if not isinstance(status, dict):
                raise ApiError("invalid_response")
            permission = (
                community.get("hasPermission", True) is True
                and status.get("statusExpired", 0) == 0
            )
            # QinlinOpen displays the returned keys without interpreting
            # authorizeExpired. Its App-endpoint semantics are unverified;
            # do not apply the old mini-app endpoint's flag mapping here.
            # Explicit community expiry and the open API still enforce access.
            for item in objects_with(data, "doorControlId"):
                if item["doorControlId"] in (None, "", 0):
                    continue
                door_id = identifier(item["doorControlId"])
                name = find_value(item, ("doorControlName", "doorName", "name"))
                gate = Gate(
                    cid,
                    community_name,
                    door_id,
                    identifier(name) if name is not None else door_id,
                    str(item.get("macAddress") or ""),
                    permission,
                    permission_deadline(community.get("expiryDate")),
                )
                gates.setdefault(gate.key, gate)
        return gates

    async def open_door(self, gate: Gate) -> None:
        """Let the opening endpoint decide current session and gate permissions."""
        self.require_auth()
        data = await self._app_request(
            OPEN_PATH,
            {
                "appChannel": 1,
                "doorControlId": int(gate.door_id)
                if gate.door_id.isdecimal()
                else gate.door_id,
                "communityId": int(gate.community_id)
                if gate.community_id.isdecimal()
                else gate.community_id,
            },
            mode="query",
        )
        if not isinstance(data, dict):
            raise ApiError("invalid_response")
        if type(data.get("openDoorState")) is not int or data["openDoorState"] != 1:
            raise ApiError("open_failed")
