import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Record(Base):
    """一条脱敏记录的当前状态；历史原文存放在 record_versions。"""

    __tablename__ = "records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 受控记录：发布必须走“申请-复核”流程，直接发布请求一律拒绝
    controlled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecordVersion(Base):
    """某次修订提交的原文与敏感区间，按 (record_id, version) 唯一。"""

    __tablename__ = "record_versions"

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    # JSONB 数组，元素为 {"start": int, "end": int}，左闭右开、按 Unicode 码点计
    intervals: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Snapshot(Base):
    """发布产生的不可变快照：只保存脱敏后的正文，后续修订不影响它。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("record_id", "version", name="uq_snapshots_record_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    redacted_text: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 永久撤回：withdrawn_at 为空表示仍公开；非空即永久不可公开读取。
    # 撤回只作用于本快照：同记录其他版本、历史脱敏正文与已批准申请均继续留存。
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 撤回原因仅维护侧可见，公开侧的模糊 403 绝不透露它
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReadToken(Base):
    """快照的随机读取令牌，可设到期时间、可撤销，并可设成功读取次数上限。

    - max_reads 为空表示不限次数（既有及新令牌默认如此）；为正整数时，
      每次凭本令牌成功读取消耗一次，达到上限后的读取沿用既有模糊拒绝。
    - reads_used 记录已消耗次数；不同令牌各自计数，互不影响。
    """

    __tablename__ = "read_tokens"
    __table_args__ = (
        CheckConstraint("max_reads IS NULL OR max_reads > 0", name="ck_read_tokens_max_reads_positive"),
        CheckConstraint("reads_used >= 0", name="ck_read_tokens_reads_used_non_negative"),
        CheckConstraint(
            "max_reads IS NULL OR reads_used <= max_reads",
            name="ck_read_tokens_reads_used_within_limit",
        ),
    )

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 成功读取次数上限：NULL 表示不限次；冻结后不随申请变化
    max_reads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 已成功读取次数；由条件 UPDATE 原子递增，保证并发读取不超过上限
    reads_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 换发时间：非空表示该令牌已在“泄露换发”中被新令牌替换，此后旧令牌不能再读取
    # （到期时刻、范围与已用次数由换发出的新令牌继承，计数不因换发重置）。
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 稳定换发链标识：同一枚凭证经任意次换发，新旧令牌共享同一个 chain_id。
    # 只用于审计归并，不是凭证本身，绝不含凭证明文；审计事件只记录该标识。
    chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True
    )


# 受控发布申请的终态
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_STATUSES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED)


class PublishRequest(Base):
    """受控发布申请：绑定提交时刻的历史版本内容（原文与区间在此冻结）。

    - pending：待复核，可被批准或驳回
    - approved / rejected：终态，终态唯一、不可变更
    """

    __tablename__ = "publish_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_publish_requests_status"),
        Index("ix_publish_requests_record", "record_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    # 申请目标版本；待审内容冻结在 frozen_* 字段，后续修订不改变它
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_text: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_intervals: Mapped[list] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=REQUEST_PENDING, default=REQUEST_PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 令牌到期策略（提交时确定）：相对时长在批准签发时才换算成绝对时刻
    token_expires_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 读取次数上限（申请时冻结）：NULL 表示不限次，批准后原样落到新令牌
    token_max_reads: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 批准后同事务生成的快照（驳回时保持为空；令牌经 snapshot_id 关联，不在此冗余）
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 多快照共享凭证申请的终态（语义与受控发布申请一致）
BUNDLE_REQUEST_PENDING = "pending"
BUNDLE_REQUEST_APPROVED = "approved"
BUNDLE_REQUEST_REJECTED = "rejected"
BUNDLE_REQUEST_STATUSES = (
    BUNDLE_REQUEST_PENDING,
    BUNDLE_REQUEST_APPROVED,
    BUNDLE_REQUEST_REJECTED,
)


class ReadBundleRequest(Base):
    """多快照共享凭证申请：operator 提交、另一名 reviewer 批准或驳回。

    提交时冻结清单（snapshot_ids，JSONB 数组，保持提交顺序、互不重复）
    与共享读取总额度、到期策略；批准时重新核对全部快照仍存在且未撤回，
    任一项失效则整次批准失败，不签发凭证。
    """

    __tablename__ = "read_bundle_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_read_bundle_requests_status",
        ),
        CheckConstraint(
            "jsonb_array_length(frozen_snapshot_ids) >= 2",
            name="ck_read_bundle_requests_at_least_two",
        ),
        CheckConstraint(
            "max_reads IS NOT NULL AND max_reads > 0",
            name="ck_read_bundle_requests_max_reads_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 冻结的快照清单：JSONB 数组，元素为快照 UUID，提交时定形、此后不变
    frozen_snapshot_ids: Mapped[list] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=BUNDLE_REQUEST_PENDING,
        default=BUNDLE_REQUEST_PENDING,
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 凭证到期策略（提交时冻结）：相对时长在批准签发时才换算成绝对时刻
    credential_expires_in_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 全部清单快照共用的成功读取总额度（正整数，必填）
    max_reads: Mapped[int] = mapped_column(Integer, nullable=False)

    # 批准后同事务签发的凭证（驳回时保持为空）
    bundle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("read_bundles.id", ondelete="CASCADE"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReadBundle(Base):
    """批准后一次性签发的多快照共享凭证：绑定冻结清单，共用读取额度。

    凭证经现有公开快照读取接口使用：只能读取清单内快照的脱敏正文；
    每次成功读取由 reads_used 共享计数，并发读取由条件 UPDATE 串行化，
    成功次数绝不超过 max_reads。
    """

    __tablename__ = "read_bundles"
    __table_args__ = (
        CheckConstraint("max_reads > 0", name="ck_read_bundles_max_reads_positive"),
        CheckConstraint("reads_used >= 0", name="ck_read_bundles_reads_used_non_negative"),
        CheckConstraint(
            "reads_used <= max_reads",
            name="ck_read_bundles_reads_used_within_limit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 随机凭证字符串（secrets.token_urlsafe(32)），用于公开读取接口
    credential: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # 全部清单快照共用的成功读取总额度（正整数）
    max_reads: Mapped[int] = mapped_column(Integer, nullable=False)
    # 已成功读取次数；由条件 UPDATE 原子递增，跨清单内所有快照共享
    reads_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 换发时间：非空表示该共享凭证已被新共享凭证替换，旧凭证此后不能再读取
    # （冻结清单、到期时刻与已用额度由新凭证继承）。
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 稳定换发链标识：换发出的新共享凭证继承原凭证的 chain_id，
    # 同一冻结清单的多代凭证成功读取在审计中归于同一链；不含凭证明文。
    chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True
    )

    # 绑定的清单（批准时按冻结清单一次性写入），按提交顺序排列
    items: Mapped[list["ReadBundleItem"]] = relationship(
        back_populates="bundle", cascade="all, delete-orphan", order_by="ReadBundleItem.position"
    )


class ReadBundleItem(Base):
    """共享凭证绑定的清单项：批准时按冻结清单一次性写入，此后不变。"""

    __tablename__ = "read_bundle_items"
    __table_args__ = (
        UniqueConstraint("bundle_id", "snapshot_id", name="uq_read_bundle_items_bundle_snapshot"),
        Index("ix_read_bundle_items_snapshot", "snapshot_id"),
    )

    bundle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("read_bundles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 提交顺序（从 0 开始），维护侧清单按此排序
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    bundle: Mapped["ReadBundle"] = relationship(back_populates="items")


# 泄露换发的凭证类型：单快照令牌 / 多快照共享凭证
ROTATION_KIND_TOKEN = "token"
ROTATION_KIND_BUNDLE = "bundle"
ROTATION_KINDS = (ROTATION_KIND_TOKEN, ROTATION_KIND_BUNDLE)


class CredentialRotation(Base):
    """读取凭证泄露换发记录（单快照令牌与多快照共享凭证统一入口）。

    维护人员凭服务密钥提交（原凭证, 请求标识）换发：
    - 仅未撤销、未到期且额度未耗尽的凭证可换发；
    - 同一原凭证只能换发一次：旧凭证换发提交后即失效（rotated_at），
      新凭证继承原范围（快照/冻结清单）、原绝对到期时刻与已用次数；
    - (old_credential, request_id) 唯一：相同凭证与请求标识的重试幂等，
      原样返回首次换发结果（response_payload），不再次换发；
    - 不同请求标识并发换发同一凭证时，对凭证行的 FOR UPDATE 串行化，
      仅一次成功，其余看到旧凭证已换发 -> 409。
    """

    __tablename__ = "credential_rotations"
    __table_args__ = (
        UniqueConstraint(
            "old_credential", "request_id", name="uq_credential_rotations_old_request"
        ),
        CheckConstraint(
            "kind IN ('token', 'bundle')", name="ck_credential_rotations_kind"
        ),
        Index("ix_credential_rotations_new_credential", "new_credential"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    old_credential: Mapped[str] = mapped_column(String(64), nullable=False)
    new_credential: Mapped[str] = mapped_column(String(64), nullable=False)
    # 调用方提供的请求标识：与原凭证组合构成幂等键
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # 首次换发的完整响应（JSONB）：重试时原样回放，绝不再次换发
    response_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# 审计事件记录的凭证类型（与换发类型一致）：单快照令牌 / 多快照共享凭证
AUDIT_KIND_TOKEN = ROTATION_KIND_TOKEN
AUDIT_KIND_BUNDLE = ROTATION_KIND_BUNDLE


class ReadAuditEvent(Base):
    """已发布脱敏快照成功读取的不可变审计事件。

    每次凭单快照令牌或共享凭证成功读取快照，都与额度扣减在同一事务写入一条
    事件；失败、清单外或已撤回快照的请求不产生事件。

    - id：事件标识（UUID，写入时随机生成，对外暴露）；
    - seq：库内单调递增序号（IDENTITY），与快照行锁配合保证“按快照、时间范围
      分页”不重复不遗漏（提交顺序即可见顺序），不对外暴露；
    - snapshot_id / occurred_at：被读取快照与读取时间；
    - credential_kind：凭证类型（token / bundle）；
    - credential_chain_id：稳定换发链标识，同一凭证多次换发后仍归于同一链，
      只记录该链标识，绝不记录任何凭证明文。

    表为只追加（append-only）：数据库触发器拒绝 UPDATE / DELETE / TRUNCATE，
    应用层也不提供修改或删除入口。
    """

    __tablename__ = "read_audit_events"
    __table_args__ = (
        CheckConstraint(
            "credential_kind IN ('token', 'bundle')",
            name="ck_read_audit_events_kind",
        ),
        # 分页主索引：按快照过滤、按 seq 顺序扫描（occurred_at 先由 seq 有序覆盖）
        Index("ix_read_audit_events_snapshot_seq", "snapshot_id", "seq"),
        # 维护侧按时间排查时的辅助索引
        Index("ix_read_audit_events_snapshot_time", "snapshot_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 库内单调递增序号：仅用于分页稳定性，不随响应返回
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False, unique=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # 不加外键约束：审计事件必须独立留存，快照后续任何状态变化都不级联影响事件
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    credential_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )

