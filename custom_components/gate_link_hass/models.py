"""Validated account sessions and gate descriptions."""

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


class ApiError(Exception):
    """Safe error keys without response bodies or credential-bearing URLs."""

    def __init__(self, key: str = "request_rejected") -> None:
        super().__init__(key)
        self.key = key


class AuthenticationExpired(ApiError):
    def __init__(self) -> None:
        super().__init__("account_expired")


def identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ApiError("invalid_response")
    if not (result := str(value).strip()):
        raise ApiError("invalid_response")
    return result


def positive_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("invalid_response")
    try:
        result = float(value)
    except OverflowError:
        raise ApiError("invalid_response") from None
    if not math.isfinite(result) or result <= 0:
        raise ApiError("invalid_response")
    return result


@dataclass(frozen=True)
class AccountSession:
    account_id: str
    session_id: str = field(repr=False)
    issued_at: float
    server_expires_at: float | None
    checked_at: float | None = None
    check_validity_seconds: int | None = None

    @classmethod
    def from_login(cls, data: Any, now: float) -> AccountSession:
        try:
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise ApiError("invalid_response")
            if data["code"] != 0:
                raise ApiError("verification_failed")
            token = data["aliPayminiLoginDTO"]
            return cls(
                identifier(data["userId"]),
                identifier(token["sessionId"]),
                now,
                now + positive_number(token["expires"]),
            )
        except KeyError, TypeError:
            raise ApiError("invalid_response") from None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountSession:
        try:
            session = cls(
                identifier(data["account_id"]),
                identifier(data["session_id"]),
                positive_number(data["issued_at"]),
                positive_number(data["server_expires_at"])
                if data["server_expires_at"] is not None
                else None,
                positive_number(data["checked_at"])
                if data.get("checked_at") is not None
                else None,
                data.get("check_validity_seconds"),
            )
            if (
                session.server_expires_at is not None
                and session.server_expires_at <= session.issued_at
            ):
                raise ApiError("invalid_response")
            if session.checked_at is None:
                if session.check_validity_seconds is not None:
                    raise ApiError("invalid_response")
            elif (
                type(session.check_validity_seconds) is not int
                or session.check_validity_seconds <= 0
                or session.check_validity_seconds > 253402300799 - session.checked_at
            ):
                raise ApiError("invalid_response")
            return session
        except KeyError, TypeError:
            raise ApiError("invalid_response") from None

    def deadline(self, lifetime_days: int = 30) -> float:
        """Historical mini-app deadline; App authentication ignores this limit."""
        local_deadline = self.issued_at + lifetime_days * 86400
        return (
            min(self.server_expires_at, local_deadline)
            if self.server_expires_at is not None
            else local_deadline
        )

    def expired(self, now: float, lifetime_days: int = 30) -> bool:
        return now >= self.deadline(lifetime_days)

    @property
    def next_check_at(self) -> float | None:
        """Server-provided timing reference, not proof of token expiry."""
        if self.checked_at is None or self.check_validity_seconds is None:
            return None
        return self.checked_at + self.check_validity_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def permission_deadline(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return date.timestamp()
    except ValueError, TypeError:
        raise ApiError("invalid_response") from None


@dataclass(frozen=True)
class Gate:
    community_id: str
    community_name: str
    door_id: str
    name: str
    mac_address: str
    permission_valid: bool = True
    permission_expires_at: float | None = None

    @property
    def key(self) -> str:
        return f"{self.community_id}:{self.door_id}"

    @property
    def label(self) -> str:
        return f"{self.community_name} / {self.name}"

    def available(self, now: float) -> bool:
        return self.permission_valid and (
            self.permission_expires_at is None or now < self.permission_expires_at
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Gate:
        if not isinstance(data, dict):
            raise ApiError("invalid_response")
        try:
            deadline = data.get("permission_expires_at")
            return cls(
                *(
                    identifier(data[key])
                    for key in (
                        "community_id",
                        "community_name",
                        "door_id",
                        "name",
                    )
                ),
                mac_address=_optional_mac(data.get("mac_address", "")),
                permission_valid=data.get("permission_valid", True) is True,
                permission_expires_at=positive_number(deadline)
                if deadline is not None
                else None,
            )
        except KeyError, TypeError:
            raise ApiError("invalid_response") from None


def _optional_mac(value: Any) -> str:
    """App opening uses community/door IDs and does not require a MAC."""
    if not isinstance(value, str):
        raise ApiError("invalid_response")
    return value.strip()
