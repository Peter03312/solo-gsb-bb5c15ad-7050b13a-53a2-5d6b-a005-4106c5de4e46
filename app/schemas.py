from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
class Interval(BaseModel):
    """左闭右开 [start, end)，按 Unicode 码点计。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RecordCreateRequest(BaseModel):
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class RecordReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class PublishRequest(BaseModel):
    version: int = Field(ge=1)
    # 两种到期方式二选一，均不填则令牌永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    # 可选的成功读取次数上限（正整数）；不填表示不限次。
    # 直接发布时确定；受控发布在提交申请时冻结，批准签发后生效。
    max_reads: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class ControlledPublishRequest(PublishRequest):
    """受控发布申请：复用既有发布的版本与令牌到期字段。"""


class ReviewRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class SnapshotWithdrawRequest(BaseModel):
    # 撤回原因：首次撤回必填（非空白），重复撤回时沿用首次结果、忽略本次内容
    reason: str | None = Field(default=None, max_length=2000)


class SnapshotWithdrawResponse(BaseModel):
    """撤回结果：重复撤回也返回首次撤回的时间与状态。"""

    snapshot_id: UUID
    withdrawn_at: datetime
    status: str


class RecordResponse(BaseModel):
    record_id: UUID
    version: int


class ControlledFlagResponse(BaseModel):
    record_id: UUID
    controlled: bool


class SnapshotInfo(BaseModel):
    snapshot_id: UUID
    version: int
    status: str
    published_at: datetime
    # 维护侧状态：撤回时间与原因；未撤回时均为 null（原因不向公开侧透露）
    withdrawn_at: datetime | None = None
    withdraw_reason: str | None = None


class RecordStatusResponse(BaseModel):
    record_id: UUID
    current_version: int
    controlled: bool
    snapshots: list[SnapshotInfo]


class PublishResponse(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    status: str
    token: str
    expires_at: datetime | None
    # 该令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None = None


class TokenStatusResponse(BaseModel):
    """维护侧令牌状态：只含计数与元数据，绝不返回快照原文。"""

    token: str
    snapshot_id: UUID
    record_id: UUID
    version: int
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    # 非空表示该令牌已在泄露换发中被替换，此后旧令牌不能再读取
    rotated_at: datetime | None = None
    # null 表示不限次；此时 remaining_reads 也为 null
    max_reads: int | None
    reads_used: int
    remaining_reads: int | None
    # 便于维护侧排查：invalid/expired 等状态仅用于维护接口，不影响公开侧的模糊拒绝
    active: bool


class RevokeResponse(BaseModel):
    token: str
    revoked: bool
    already_revoked: bool


class PublicSnapshotResponse(BaseModel):
    record_id: UUID
    version: int
    status: str
    redacted_text: str


class PublishRequestInfo(BaseModel):
    """申请详情：只含元数据，绝不返回冻结原文。"""

    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    reviewed_by: str | None
    reason: str | None
    # 提交时选择的令牌到期策略；批准后 token_expires_at 为实际绝对到期时刻
    token_expires_in_seconds: int | None
    token_expires_at: datetime | None
    # 申请时冻结的读取次数上限；null 表示不限次
    token_max_reads: int | None
    snapshot_id: UUID | None
    created_at: datetime
    decided_at: datetime | None


class PublishRequestListResponse(BaseModel):
    record_id: UUID
    requests: list[PublishRequestInfo]


class ControlledSubmitResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    requested_by: str
    created_at: datetime


class ApproveResponse(BaseModel):
    request_id: UUID
    record_id: UUID
    version: int
    status: str
    reviewed_by: str
    snapshot_id: UUID
    token: str
    expires_at: datetime | None
    # 批准签发令牌的成功读取次数上限；null 表示不限次
    max_reads: int | None = None


# ---- 多快照共享凭证（read bundle） ----


class BundleRequestCreate(BaseModel):
    """多快照共享凭证申请：至少两个互不重复的已发布快照 ID + 正整数总额度。"""

    snapshot_ids: list[UUID] = Field(min_length=2)
    # 全部清单快照共用的成功读取总额度（正整数，必填）
    max_reads: int = Field(gt=0)
    # 可选到期策略，二选一；均不填则凭证永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _unique_snapshot_ids_and_single_expiry(self):
        if len(self.snapshot_ids) != len(set(self.snapshot_ids)):
            raise ValueError("snapshot_ids must not contain duplicates")
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class BundleRequestInfo(BaseModel):
    """共享凭证申请详情：只含元数据，绝不返回快照原文。"""

    request_id: UUID
    status: str
    # 冻结的快照清单（提交顺序）
    snapshot_ids: list[UUID]
    requested_by: str
    reviewed_by: str | None
    reason: str | None
    credential_expires_in_seconds: int | None
    credential_expires_at: datetime | None
    max_reads: int
    bundle_id: UUID | None
    created_at: datetime
    decided_at: datetime | None


class BundleRequestListResponse(BaseModel):
    requests: list[BundleRequestInfo]


class BundleSubmitResponse(BaseModel):
    request_id: UUID
    status: str
    snapshot_ids: list[UUID]
    requested_by: str
    max_reads: int
    created_at: datetime


class BundleApproveResponse(BaseModel):
    request_id: UUID
    status: str
    reviewed_by: str
    bundle_id: UUID
    credential: str
    snapshot_ids: list[UUID]
    expires_at: datetime | None
    max_reads: int


class BundleItemInfo(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    position: int
    # 该快照当前维护状态：published / withdrawn（撤回后只拒绝该项、不扣额度）
    status: str
    withdrawn_at: datetime | None = None


class ReadBundleStatusResponse(BaseModel):
    """维护侧凭证状态：清单、额度与状态，绝不返回快照原文。"""

    bundle_id: UUID
    credential: str
    max_reads: int
    reads_used: int
    remaining_reads: int
    active: bool
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    # 非空表示该共享凭证已在泄露换发中被替换，旧凭证此后不能再读取
    rotated_at: datetime | None = None
    items: list[BundleItemInfo]


class ReadBundleListResponse(BaseModel):
    bundles: list[ReadBundleStatusResponse]


class BundleRevokeResponse(BaseModel):
    bundle_id: UUID
    revoked: bool
    already_revoked: bool


# ---- 读取凭证泄露换发（单快照令牌 / 多快照共享凭证统一入口） ----


class CredentialRotateRequest(BaseModel):
    """换发请求：维护人员凭服务密钥提交原凭证与请求标识。

    请求标识由调用方生成（建议 UUID）：与原凭证组合构成幂等键，
    相同原凭证 + 相同请求标识的重试只回放首次换发结果，不再次换发；
    同一原凭证使用不同请求标识并发/重复换发仅一次成功，其余 409。
    """

    credential: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _no_blank_values(self):
        if not self.credential.strip() or not self.request_id.strip():
            raise ValueError("credential and request_id must not be blank")
        return self


class RotationBundleItem(BaseModel):
    """换发响应中的清单项：沿用冻结顺序，并带当前维护状态。"""

    snapshot_id: UUID
    record_id: UUID
    version: int
    position: int
    status: str
    withdrawn_at: datetime | None = None


class CredentialRotationResponse(BaseModel):
    """换发结果：单快照令牌与多快照共享凭证共用同一结构。

    kind 为 token 时使用 snapshot_ids（单项）；kind 为 bundle 时
    snapshot_ids 为冻结清单全部快照，items 带逐项状态（撤回项继续不可读）。
    新凭证继承原范围、原绝对到期时刻与已用次数；剩余额度按换发时已用次数结算。
    """

    kind: str
    request_id: str
    new_credential: str
    old_credential: str
    # 适用快照：单快照令牌为单元素；共享凭证为冻结清单（顺序不变）
    snapshot_ids: list[UUID]
    # 原绝对到期时刻（继承，不延长、不重置）；null 表示永不到期
    expires_at: datetime | None
    # 原读取上限（继承）；null 表示单快照令牌不限次（共享凭证恒为正整数）
    max_reads: int | None
    # 换发时已成功读取次数（继承自原凭证，不重置）
    reads_used: int
    # 换发时剩余额度：max_reads 为 null 时为 null
    remaining_reads: int | None
    rotated_at: datetime
    replayed: bool = False
    # 仅共享凭证：冻结清单逐项当前状态；单快照令牌不返回
    items: list[RotationBundleItem] | None = None


# ---- 成功读取审计事件（不可变日志 + 维护侧分页查询） ----


class ReadAuditEventInfo(BaseModel):
    """单条读取审计事件：只含事件与归并元数据，绝不含凭证明文或任何正文。"""

    # 事件标识
    event_id: UUID
    # 被读取的快照
    snapshot_id: UUID
    # 读取（额度扣减同事务提交）时间
    occurred_at: datetime
    # 凭证类型：token（单快照令牌）/ bundle（多快照共享凭证）
    credential_kind: str
    # 稳定换发链标识：同一凭证经任意次换发后成功读取均归于同一链；不是凭证本身
    credential_chain_id: UUID


class ReadAuditEventPage(BaseModel):
    """审计事件分页结果：不返回原文、脱敏正文或凭证明文。"""

    snapshot_id: UUID
    # 查询窗口（左闭右开，UTC）
    start: datetime
    end: datetime
    events: list[ReadAuditEventInfo]
    # 下一页游标：本页已填满且仍有后续事件时非空，否则为 null
    next_cursor: str | None = None

