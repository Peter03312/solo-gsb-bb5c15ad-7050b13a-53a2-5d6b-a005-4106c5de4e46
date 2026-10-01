import base64
import binascii
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .config import settings


def require_service_key(x_service_key: str | None = Header(default=None)) -> None:
    """维护接口鉴权：缺少或服务密钥不符时整次拒绝。"""
    if x_service_key is None or not hmac.compare_digest(x_service_key, settings.service_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid service key",
        )


@dataclass(frozen=True)
class Operator:
    """通过个人凭据认证的操作者身份。"""

    username: str
    roles: tuple[str, ...]


def _bearer_www_authenticate() -> dict[str, str]:
    return {"WWW-Authenticate": 'Basic realm="controlled-publish"'}


def require_operator(authorization: str | None = Header(default=None)) -> Operator:
    """受控发布接口鉴权：HTTP Basic，校验操作者个人凭据。

    缺失、格式错误或凭据无效一律 401，且不区分原因，避免探测用户名。
    """
    if authorization is None or not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid operator credentials",
            headers=_bearer_www_authenticate(),
        )
    try:
        decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
        username, _, password = decoded.partition(":")
        if not _:
            raise ValueError("missing colon separator")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid operator credentials",
            headers=_bearer_www_authenticate(),
        ) from exc

    for credential in settings.review_credentials:
        if hmac.compare_digest(
            username.encode("utf-8"), credential.username.encode("utf-8")
        ) and hmac.compare_digest(
            password.encode("utf-8"), credential.password.encode("utf-8")
        ):
            return Operator(username=credential.username, roles=tuple(credential.roles))

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid operator credentials",
        headers=_bearer_www_authenticate(),
    )


def require_role(role: str):
    """生成一个要求指定角色的依赖；不具备角色时 403。"""

    def _dependency(operator: Operator = Depends(require_operator)) -> Operator:
        if role not in operator.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"operator lacks required role {role!r}",
            )
        return operator

    return _dependency
