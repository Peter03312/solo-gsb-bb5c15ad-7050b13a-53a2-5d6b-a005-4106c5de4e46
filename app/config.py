"""服务配置：全部通过环境变量注入（可搭配 .env 文件）。"""

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_ROLES = ("operator", "reviewer")


class ReviewCredential(BaseModel):
    """一名操作者的凭据与角色。

    - operator：可提交受控发布申请
    - reviewer：可批准/驳回申请（申请人不得复核自己的申请）
    """

    username: str
    password: str
    roles: list[str]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL 连接串，Compose 中指向 db 服务
    database_url: str = "postgresql+psycopg2://redact:redact@localhost:5432/redact"
    # 维护人员服务密钥，必填，未配置时服务拒绝启动
    service_key: str
    # 受控发布操作者凭据（JSON 数组），例如：
    # [{"username":"alice","password":"...","roles":["operator"]},
    #  {"username":"bob","password":"...","roles":["reviewer"]}]
    # 留空则受控发布接口始终无人可用（401），不影响其他接口。
    review_credentials: list[ReviewCredential] = []
    # 启动时等待数据库就绪的重试参数
    db_connect_retries: int = 30
    db_connect_retry_delay_seconds: float = 1.0

    @field_validator("review_credentials")
    @classmethod
    def _validate_review_credentials(cls, credentials: list[ReviewCredential]):
        """启动即校验，配置非法时快速失败（错误信息不含口令）。"""
        seen: set[str] = set()
        for credential in credentials:
            if not credential.username.strip():
                raise ValueError("review_credentials contains an empty username")
            if not credential.password:
                raise ValueError(
                    f"review_credentials for {credential.username!r} is missing a password"
                )
            if credential.username in seen:
                raise ValueError(
                    f"review_credentials contains duplicate username {credential.username!r}"
                )
            seen.add(credential.username)
            if not credential.roles:
                raise ValueError(
                    f"operator {credential.username!r} must have at least one role"
                )
            unknown = set(credential.roles) - set(VALID_ROLES)
            if unknown:
                raise ValueError(
                    f"operator {credential.username!r} has unknown roles: "
                    f"{sorted(unknown)}; valid roles are {list(VALID_ROLES)}"
                )
        return credentials


settings = Settings()
