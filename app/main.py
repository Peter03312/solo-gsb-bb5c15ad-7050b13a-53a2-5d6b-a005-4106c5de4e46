import base64
import binascii
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, inspect, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .db import Base, engine, get_db
from .models import (
    BUNDLE_REQUEST_APPROVED,
    BUNDLE_REQUEST_PENDING,
    BUNDLE_REQUEST_REJECTED,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    CredentialRotation,
    PublishRequest as PublishRequestModel,
    ReadBundle,
    ReadBundleItem,
    ReadBundleRequest,
    ReadEvent,
    ReadToken,
    Record,
    RecordVersion,
    ROTATION_KIND_BUNDLE,
    ROTATION_KIND_TOKEN,
    READ_EVENT_CREDENTIAL_KIND_BUNDLE,
    READ_EVENT_CREDENTIAL_KIND_TOKEN,
    Snapshot,
)
from .redaction import IntervalOutOfBoundsError, redact_text, validate_intervals
from .schemas import (
    ApproveResponse,
    BundleApproveResponse,
    BundleItemInfo,
    BundleRequestCreate,
    BundleRequestInfo,
    BundleRequestListResponse,
    BundleRevokeResponse,
    BundleSubmitResponse,
    ControlledFlagResponse,
    ControlledPublishRequest,
    ControlledSubmitResponse,
    CredentialRotateRequest,
    CredentialRotationResponse,
    Interval,
    PublicSnapshotResponse,
    PublishRequest as PublishPayload,
    PublishRequestInfo,
    PublishRequestListResponse,
    PublishResponse,
    ReadBundleListResponse,
    ReadBundleStatusResponse,
    RecordCreateRequest,
    RecordResponse,
    RecordReviseRequest,
    RecordStatusResponse,
    ReadEventInfo,
    ReadEventListResponse,
    ReviewRejectRequest,
    RevokeResponse,
    RotationBundleItem,
    SnapshotInfo,
    SnapshotWithdrawRequest,
    SnapshotWithdrawResponse,
    TokenStatusResponse,
)
from .security import Operator, require_operator, require_role, require_service_key

SNAPSHOT_STATUS = "published"
SNAPSHOT_WITHDRAWN_STATUS = "withdrawn"


def _snapshot_status(snapshot: Snapshot) -> str:
    """快照维护状态：撤回为终态，撤回前沿用 published。"""
    return SNAPSHOT_WITHDRAWN_STATUS if snapshot.withdrawn_at is not None else SNAPSHOT_STATUS


def _init_db_with_retry() -> None:
    """启动时建表（幂等），并等待数据库就绪。"""
    last_exc: Exception | None = None
    for _ in range(settings.db_connect_retries):
        try:
            Base.metadata.create_all(engine)
            _apply_lightweight_migrations()
            return
        except OperationalError as exc:
            last_exc = exc
            time.sleep(settings.db_connect_retry_delay_seconds)
    raise RuntimeError("database is not reachable") from last_exc


def _apply_lightweight_migrations() -> None:
    """对既有库做幂等的加列/补列升级（仅用于本轮新增字段，安全可重复执行）。"""
    inspector = inspect(engine)
    table_columns = {column["name"] for column in inspector.get_columns("records")}
    statements: list[str] = []
    if "controlled" not in table_columns:
        statements.append(
            "ALTER TABLE records ADD COLUMN controlled BOOLEAN NOT NULL DEFAULT false"
        )
    if "snapshots" in inspector.get_table_names():
        snapshot_columns = {
            column["name"] for column in inspector.get_columns("snapshots")
        }
        if "withdrawn_at" not in snapshot_columns:
            # 既有快照默认未撤回（NULL），继续可读
            statements.append(
                "ALTER TABLE snapshots ADD COLUMN withdrawn_at TIMESTAMP"
            )
        if "withdraw_reason" not in snapshot_columns:
            statements.append(
                "ALTER TABLE snapshots ADD COLUMN withdraw_reason TEXT"
            )
    if "read_tokens" in inspector.get_table_names():
        token_columns = {
            column["name"] for column in inspector.get_columns("read_tokens")
        }
        if "max_reads" not in token_columns:
            # 既有令牌不加上限，继续不限次
            statements.append("ALTER TABLE read_tokens ADD COLUMN max_reads INTEGER")
        if "reads_used" not in token_columns:
            statements.append(
                "ALTER TABLE read_tokens ADD COLUMN reads_used INTEGER NOT NULL DEFAULT 0"
            )
        if "rotated_at" not in token_columns:
            # 既有令牌未经过换发，保持可读
            statements.append("ALTER TABLE read_tokens ADD COLUMN rotated_at TIMESTAMP")
        if "chain_id" not in token_columns:
            # 既有凭证各自自成一条换发链（回填随机 UUID），回填后补 NOT NULL
            statements.append("ALTER TABLE read_tokens ADD COLUMN chain_id UUID")
            statements.append(
                "UPDATE read_tokens SET chain_id = gen_random_uuid() WHERE chain_id IS NULL"
            )
            statements.append(
                "ALTER TABLE read_tokens ALTER COLUMN chain_id SET NOT NULL"
            )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_read_tokens_chain_id "
                "ON read_tokens (chain_id)"
            )
    if "read_bundles" in inspector.get_table_names():
        bundle_columns = {
            column["name"] for column in inspector.get_columns("read_bundles")
        }
        if "rotated_at" not in bundle_columns:
            # 既有共享凭证未经过换发，保持可读
            statements.append("ALTER TABLE read_bundles ADD COLUMN rotated_at TIMESTAMP")
        if "chain_id" not in bundle_columns:
            statements.append("ALTER TABLE read_bundles ADD COLUMN chain_id UUID")
            statements.append(
                "UPDATE read_bundles SET chain_id = gen_random_uuid() "
                "WHERE chain_id IS NULL"
            )
            statements.append(
                "ALTER TABLE read_bundles ALTER COLUMN chain_id SET NOT NULL"
            )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_read_bundles_chain_id "
                "ON read_bundles (chain_id)"
            )
    if "publish_requests" in inspector.get_table_names():
        request_columns = {
            column["name"] for column in inspector.get_columns("publish_requests")
        }
        if "token_expires_in_seconds" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests "
                "ADD COLUMN token_expires_in_seconds INTEGER"
            )
        if "token_expires_at" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests ADD COLUMN token_expires_at TIMESTAMP"
            )
        if "token_max_reads" not in request_columns:
            statements.append(
                "ALTER TABLE publish_requests ADD COLUMN token_max_reads INTEGER"
            )
        # 早期版本曾冗余存放令牌；统一以 read_tokens 为准，删除冗余列
        if "published_token" in request_columns:
            statements.append("ALTER TABLE publish_requests DROP COLUMN published_token")
    if statements:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    # 读取审计事件只追加：触发器在数据库层拒绝任何 UPDATE/DELETE（幂等创建）。
    # 在加列迁移之后执行，确保既有库已由 create_all 建好 read_events。
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    token_columns = {
        column["name"] for column in inspector.get_columns("read_tokens")
    } if "read_tokens" in table_names else set()
    bundle_columns = {
        column["name"] for column in inspector.get_columns("read_bundles")
    } if "read_bundles" in table_names else set()
    if (
        "read_events" in table_names
        and "chain_id" in token_columns
        and "chain_id" in bundle_columns
    ):
        with engine.begin() as conn:
            # 升级前已完成的换发链：让新凭证沿用其最初凭证回填得到的链标识，
            # 使升级后的继续换发与读取归于同一条链（幂等：链已一致时不更新）。
            for table, credential_column in (
                ("read_tokens", "token"),
                ("read_bundles", "credential"),
            ):
                conn.execute(
                    text(
                        f"""
                        WITH RECURSIVE lineage(new_credential, root_credential) AS (
                            SELECT r.new_credential, r.old_credential
                            FROM credential_rotations r
                            JOIN {table} child ON child.{credential_column} = r.new_credential
                            UNION ALL
                            SELECT l.new_credential, r.old_credential
                            FROM lineage l
                            JOIN credential_rotations r
                              ON r.new_credential = l.root_credential
                        )
                        UPDATE {table} child
                        SET chain_id = root.chain_id
                        FROM lineage l
                        JOIN {table} root ON root.{credential_column} = l.root_credential
                        WHERE child.{credential_column} = l.new_credential
                          AND child.chain_id <> root.chain_id
                        """
                    )
                )
            conn.execute(text(_READ_EVENTS_IMMUTABILITY_SQL))


_READ_EVENTS_IMMUTABILITY_SQL = """
CREATE OR REPLACE FUNCTION read_events_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'read_events are immutable; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_read_events_no_update ON read_events;
CREATE TRIGGER trg_read_events_no_update
    BEFORE UPDATE ON read_events
    FOR EACH ROW EXECUTE FUNCTION read_events_reject_mutation();

DROP TRIGGER IF EXISTS trg_read_events_no_delete ON read_events;
CREATE TRIGGER trg_read_events_no_delete
    BEFORE DELETE ON read_events
    FOR EACH ROW EXECUTE FUNCTION read_events_reject_mutation();
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="Redacted Records API",
    version="1.6.0",
    summary="机构公开脱敏记录服务（受控发布 + 读取次数上限 + 快照撤回 + 多快照共享凭证 + 泄露换发 + 成功读取审计）",
    lifespan=lifespan,
)


def _validated_intervals(text: str, intervals: list[Interval]) -> list[tuple[int, int]]:
    """越界区间直接 422，且发生在任何写库之前，保证整次拒绝。"""
    try:
        return validate_intervals(len(text), [(iv.start, iv.end) for iv in intervals])
    except IntervalOutOfBoundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _future_expires_at(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> datetime | None:
    """解析令牌到期时间；显式到期时刻必须晚于当前时间，否则 422。"""
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )
        return expires_at
    if expires_in_seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    return None


def _request_info(request: PublishRequestModel) -> PublishRequestInfo:
    """申请详情映射：只输出元数据，冻结原文绝不离开服务端。"""
    return PublishRequestInfo(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        requested_by=request.requested_by,
        reviewed_by=request.reviewed_by,
        reason=request.reason,
        token_expires_in_seconds=request.token_expires_in_seconds,
        token_expires_at=request.token_expires_at,
        token_max_reads=request.token_max_reads,
        snapshot_id=request.snapshot_id,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def _validate_expiry_policy(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> None:
    """校验提交时选择的令牌到期策略：绝对时刻必须晚于提交时刻。

    相对时长（expires_in_seconds）在批准签发时才换算，复核耗时不占用令牌寿命。
    """
    if expires_at is None:
        return
    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(
        tzinfo=timezone.utc
    )
    if normalized <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expires_at must be in the future",
        )


def _bundle_request_info(request: ReadBundleRequest) -> BundleRequestInfo:
    """共享凭证申请详情映射：只输出元数据，快照原文绝不离开服务端。"""
    return BundleRequestInfo(
        request_id=request.id,
        status=request.status,
        snapshot_ids=[UUID(sid) for sid in request.frozen_snapshot_ids],
        requested_by=request.requested_by,
        reviewed_by=request.reviewed_by,
        reason=request.reason,
        credential_expires_in_seconds=request.credential_expires_in_seconds,
        credential_expires_at=request.credential_expires_at,
        max_reads=request.max_reads,
        bundle_id=request.bundle_id,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def _bundle_active(bundle: ReadBundle, now: datetime) -> bool:
    """凭证是否仍可成功读取：未撤销、未换发、未到期且共享额度未耗尽。"""
    if bundle.revoked_at is not None or bundle.rotated_at is not None:
        return False
    if bundle.expires_at is not None and bundle.expires_at <= now:
        return False
    return bundle.reads_used < bundle.max_reads


def _bundle_status_response(
    bundle: ReadBundle, snapshots: dict[UUID, Snapshot]
) -> ReadBundleStatusResponse:
    """维护侧凭证状态映射：清单按提交顺序排列，绝不返回快照原文。"""
    now = datetime.now(timezone.utc)
    items = [
        BundleItemInfo(
            snapshot_id=item.snapshot_id,
            record_id=snapshots[item.snapshot_id].record_id,
            version=snapshots[item.snapshot_id].version,
            position=item.position,
            status=_snapshot_status(snapshots[item.snapshot_id]),
            withdrawn_at=snapshots[item.snapshot_id].withdrawn_at,
        )
        for item in sorted(bundle.items, key=lambda item: item.position)
    ]
    return ReadBundleStatusResponse(
        bundle_id=bundle.id,
        credential=bundle.credential,
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,
        remaining_reads=max(bundle.max_reads - bundle.reads_used, 0),
        active=_bundle_active(bundle, now),
        created_at=bundle.created_at,
        expires_at=bundle.expires_at,
        revoked_at=bundle.revoked_at,
        rotated_at=bundle.rotated_at,
        items=items,
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post(
    "/records",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def create_record(payload: RecordCreateRequest, db: Session = Depends(get_db)):
    """创建记录（版本 1）：提交原文与敏感区间。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    record = Record(current_version=1)
    db.add(record)
    db.flush()
    db.add(
        RecordVersion(
            record_id=record.id,
            version=1,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record.id, version=1)


@app.put(
    "/records/{record_id}",
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def revise_record(record_id: UUID, payload: RecordReviseRequest, db: Session = Depends(get_db)):
    """修订记录：必须携带当前版本号作为基准，过期则 409 并返回当前版本。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    # 条件更新是原子的：并发修订时只有一个能基于同一基准版本成功，
    # 其余因 current_version 已变化而失败，不会产生分叉版本。
    new_version = db.execute(
        update(Record)
        .where(Record.id == record_id, Record.current_version == payload.base_version)
        .values(current_version=payload.base_version + 1)
        .returning(Record.current_version)
    ).scalar_one_or_none()
    if new_version is None:
        db.rollback()
        record = db.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "base_version is stale",
                "current_version": record.current_version,
            },
        )
    db.add(
        RecordVersion(
            record_id=record_id,
            version=new_version,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record_id, version=new_version)


@app.put(
    "/records/{record_id}/controlled",
    response_model=ControlledFlagResponse,
    dependencies=[Depends(require_service_key)],
)
def mark_record_controlled(record_id: UUID, db: Session = Depends(get_db)):
    """将记录设为受控发布（幂等：已是受控状态时直接返回当前状态）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        record.controlled = True
        db.commit()
    return ControlledFlagResponse(record_id=record_id, controlled=True)


@app.post(
    "/records/{record_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishResponse,
    dependencies=[Depends(require_service_key)],
)
def publish_record(record_id: UUID, payload: PublishPayload, db: Session = Depends(get_db)):
    """直接发布指定版本：合并区间生成不可变快照，并签发随机读取令牌。

    受控记录的直接发布请求一律 409 拒绝，必须走申请-复核流程。
    """
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    if record.controlled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is controlled; submit a publish request for review instead",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    expires_at = _future_expires_at(payload.expires_in_seconds, payload.expires_at)
    redacted = redact_text(
        version.original_text, [(iv["start"], iv["end"]) for iv in version.intervals]
    )
    # 快照按 (record_id, version) 唯一：并发发布时唯一约束保证只落库一份，
    # 且快照与令牌在同一事务提交，不会出现部分发布。
    db.execute(
        pg_insert(Snapshot)
        .values(record_id=record_id, version=payload.version, redacted_text=redacted)
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    # 锁定快照行后再判定撤回状态：与撤回操作串行化，
    # 已被永久撤回的版本不能通过再次直接发布重新公开。
    snapshot = db.execute(
        select(Snapshot)
        .where(Snapshot.record_id == record_id, Snapshot.version == payload.version)
        .with_for_update()
    ).scalar_one()
    if snapshot.withdrawn_at is not None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot has been withdrawn and cannot be published again",
        )
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=expires_at,
        max_reads=payload.max_reads,
    )
    db.add(token)
    db.commit()
    return {
        "snapshot_id": snapshot.id,
        "record_id": record_id,
        "version": payload.version,
        "status": SNAPSHOT_STATUS,
        "token": token.token,
        "expires_at": expires_at,
        "max_reads": payload.max_reads,
    }


@app.post(
    "/records/{record_id}/publish-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=ControlledSubmitResponse,
)
def submit_publish_request(
    record_id: UUID,
    payload: ControlledPublishRequest,
    operator: Operator = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """提交受控发布申请：用申请人的个人凭据，冻结提交时刻的历史版本内容。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        # 非受控记录应使用直接发布，明确拒绝以免误用流程
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is not controlled; use the direct publish endpoint",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    existing_snapshot = db.execute(
        select(Snapshot).where(
            Snapshot.record_id == record_id, Snapshot.version == payload.version
        )
    ).scalar_one_or_none()
    if existing_snapshot is not None and existing_snapshot.withdrawn_at is not None:
        # 已永久撤回的版本不能再提交受控申请（冻结内容也不会重新公开）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot of this version has been withdrawn",
        )
    _validate_expiry_policy(payload.expires_in_seconds, payload.expires_at)
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # 待审内容在此冻结：后续修订只产生新版本，不改变本申请绑定的原文与区间
    request = PublishRequestModel(
        record_id=record_id,
        version=payload.version,
        frozen_text=version.original_text,
        frozen_intervals=[dict(iv) for iv in version.intervals],
        status=REQUEST_PENDING,
        requested_by=operator.username,
        token_expires_in_seconds=payload.expires_in_seconds,
        token_expires_at=expires_at,
        token_max_reads=payload.max_reads,
    )
    db.add(request)
    db.commit()
    return ControlledSubmitResponse(
        request_id=request.id,
        record_id=record_id,
        version=payload.version,
        status=request.status,
        requested_by=request.requested_by,
        created_at=request.created_at,
    )


@app.post(
    "/publish-requests/{request_id}/approve",
    response_model=ApproveResponse,
)
def approve_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """批准申请：沿用既有脱敏与令牌规则，同事务生成快照与新读取令牌。

    申请人不得复核；已终态（批准/驳回）的申请一律 409；
    行锁串行化并发复核，保证同一申请只有一个终态。
    """
    # SELECT ... FOR UPDATE：并发复核在此排队，后进入者会看到已终态并被拒绝
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )

    # 快照与令牌在同一事务内生成：任一步失败整体回滚，不会留下可读令牌。
    # 相对到期时长在签发时刻换算为绝对时刻；显式到期时刻沿用申请时的选择。
    if request.token_expires_in_seconds is not None:
        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=request.token_expires_in_seconds
        )
    else:
        token_expires_at = request.token_expires_at
    redacted = redact_text(
        request.frozen_text,
        [(iv["start"], iv["end"]) for iv in request.frozen_intervals],
    )
    db.execute(
        pg_insert(Snapshot)
        .values(
            record_id=request.record_id,
            version=request.version,
            redacted_text=redacted,
        )
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    snapshot = db.execute(
        select(Snapshot)
        .where(
            Snapshot.record_id == request.record_id,
            Snapshot.version == request.version,
        )
        .with_for_update()
    ).scalar_one()
    if snapshot.withdrawn_at is not None:
        # 申请等待期间该版本快照被永久撤回：不能批准重新公开（仍可驳回本申请）
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot of this version has been withdrawn",
        )
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )
    db.add(token)
    db.flush()  # 确保令牌已在事务内落库后再终结申请
    request.status = REQUEST_APPROVED
    request.reviewed_by = operator.username
    request.snapshot_id = snapshot.id
    request.token_expires_at = token_expires_at
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return ApproveResponse(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        reviewed_by=operator.username,
        snapshot_id=snapshot.id,
        token=token.token,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )


@app.post(
    "/publish-requests/{request_id}/reject",
    response_model=PublishRequestInfo,
)
def reject_publish_request(
    request_id: UUID,
    payload: ReviewRejectRequest = Body(default_factory=ReviewRejectRequest),
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """驳回申请：不生成快照与令牌；驳回是终态，此后不能再批准。"""
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )
    request.status = REQUEST_REJECTED
    request.reviewed_by = operator.username
    request.reason = payload.reason
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _request_info(request)


@app.get(
    "/publish-requests/{request_id}",
    response_model=PublishRequestInfo,
)
def get_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """查看单个申请状态（任何已认证操作者可查；不返回冻结原文）。"""
    request = db.get(PublishRequestModel, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    return _request_info(request)


@app.get(
    "/records/{record_id}/publish-requests",
    response_model=PublishRequestListResponse,
)
def list_publish_requests(
    record_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """列出某条记录的全部受控发布申请（不返回冻结原文）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    requests = (
        db.execute(
            select(PublishRequestModel)
            .where(PublishRequestModel.record_id == record_id)
            .order_by(PublishRequestModel.created_at)
        )
        .scalars()
        .all()
    )
    return PublishRequestListResponse(
        record_id=record_id, requests=[_request_info(request) for request in requests]
    )


# ---- 多快照共享凭证（read bundle）：申请-复核 + 共用读取额度 ----


@app.post(
    "/bundle-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=BundleSubmitResponse,
)
def submit_bundle_request(
    payload: BundleRequestCreate,
    operator: Operator = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """提交多快照共享凭证申请：冻结清单、可选到期策略与正整数共享总额度。

    清单在提交时定形：至少两个互不重复的快照 ID，且全部为已存在且未撤回
    的快照；未知快照 404、已撤回快照 409。批准时会再次重新核对全部快照。
    """
    snapshot_ids = list(dict.fromkeys(payload.snapshot_ids))  # 去重保序（重复已在 schema 拒绝）
    snapshots = (
        db.execute(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    missing = [sid for sid in snapshot_ids if sid not in by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    withdrawn = [sid for sid in snapshot_ids if by_id[sid].withdrawn_at is not None]
    if withdrawn:
        # 已撤回快照不能进入共享凭证；批准时还会重新核对
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="one or more snapshots have been withdrawn",
        )
    _validate_expiry_policy(payload.expires_in_seconds, payload.expires_at)
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # 清单在此冻结：JSONB 以字符串数组存放 UUID，之后快照撤回等变化不改变本申请清单
    request = ReadBundleRequest(
        frozen_snapshot_ids=[str(sid) for sid in snapshot_ids],
        status=BUNDLE_REQUEST_PENDING,
        requested_by=operator.username,
        credential_expires_in_seconds=payload.expires_in_seconds,
        credential_expires_at=expires_at,
        max_reads=payload.max_reads,
    )
    db.add(request)
    db.commit()
    return BundleSubmitResponse(
        request_id=request.id,
        status=request.status,
        snapshot_ids=snapshot_ids,
        requested_by=request.requested_by,
        max_reads=request.max_reads,
        created_at=request.created_at,
    )


@app.post(
    "/bundle-requests/{request_id}/approve",
    response_model=BundleApproveResponse,
)
def approve_bundle_request(
    request_id: UUID,
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """批准共享凭证申请：重新核对冻结清单全部快照，全部有效才一次性签发凭证。

    - 申请人不得复核自己的申请 -> 403；
    - 已终态的申请再次操作 -> 409；并发复核由申请行行锁串行化；
    - 任一快照已不存在或已撤回 -> 整次批准失败（409），不签发凭证，申请仍可驳回；
    - 凭证、清单项与申请终态在同一事务提交，任一异常整体回滚。
    """
    request = db.execute(
        select(ReadBundleRequest)
        .where(ReadBundleRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    if request.status != BUNDLE_REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "bundle request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own bundle request",
        )

    snapshot_ids = [UUID(sid) for sid in request.frozen_snapshot_ids]
    # 批准时重新核对：对全部快照行加锁，任一项失效则整次批准失败、不签发凭证。
    # FOR UPDATE 与撤回操作串行化；撤回提交后才进入核对的批准必定看到失效项。
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.id.in_(snapshot_ids)).with_for_update()
        )
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    if len(by_id) != len(snapshot_ids) or any(
        sid not in by_id or by_id[sid].withdrawn_at is not None for sid in snapshot_ids
    ):
        # 申请保持 pending：不签发凭证，本次批准失败，但申请仍可被驳回
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="one or more snapshots in the frozen list are no longer available",
        )

    if request.credential_expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=request.credential_expires_in_seconds
        )
    else:
        expires_at = request.credential_expires_at
    # 一次性签发随机凭证并绑定冻结清单：凭证、清单项与申请终态同事务提交
    bundle = ReadBundle(
        credential=secrets.token_urlsafe(32),
        max_reads=request.max_reads,
        expires_at=expires_at,
    )
    db.add(bundle)
    db.flush()  # 取得 bundle.id 后写入清单项
    for position, snapshot_id in enumerate(snapshot_ids):
        db.add(
            ReadBundleItem(
                bundle_id=bundle.id,
                snapshot_id=snapshot_id,
                position=position,
            )
        )
    request.status = BUNDLE_REQUEST_APPROVED
    request.reviewed_by = operator.username
    request.bundle_id = bundle.id
    request.credential_expires_at = expires_at
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return BundleApproveResponse(
        request_id=request.id,
        status=request.status,
        reviewed_by=operator.username,
        bundle_id=bundle.id,
        credential=bundle.credential,
        snapshot_ids=snapshot_ids,
        expires_at=expires_at,
        max_reads=request.max_reads,
    )


@app.post(
    "/bundle-requests/{request_id}/reject",
    response_model=BundleRequestInfo,
)
def reject_bundle_request(
    request_id: UUID,
    payload: ReviewRejectRequest = Body(default_factory=ReviewRejectRequest),
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """驳回共享凭证申请：不签发凭证；驳回是终态，此后不能再批准。"""
    request = db.execute(
        select(ReadBundleRequest)
        .where(ReadBundleRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    if request.status != BUNDLE_REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "bundle request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own bundle request",
        )
    request.status = BUNDLE_REQUEST_REJECTED
    request.reviewed_by = operator.username
    request.reason = payload.reason
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _bundle_request_info(request)


@app.get(
    "/bundle-requests/{request_id}",
    response_model=BundleRequestInfo,
)
def get_bundle_request(
    request_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """查看单个共享凭证申请（任何已认证操作者可查；不返回快照原文）。"""
    request = db.get(ReadBundleRequest, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    return _bundle_request_info(request)


@app.get(
    "/bundle-requests",
    response_model=BundleRequestListResponse,
)
def list_bundle_requests(
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """列出全部共享凭证申请（任何已认证操作者可查；只含元数据）。"""
    requests = (
        db.execute(
            select(ReadBundleRequest).order_by(ReadBundleRequest.created_at)
        )
        .scalars()
        .all()
    )
    return BundleRequestListResponse(
        requests=[_bundle_request_info(request) for request in requests]
    )


@app.get(
    "/read-bundles",
    response_model=ReadBundleListResponse,
    dependencies=[Depends(require_service_key)],
)
def list_read_bundles(db: Session = Depends(get_db)):
    """维护侧查询全部共享凭证的清单、额度与状态（绝不返回快照原文）。"""
    bundles = (
        db.execute(
            select(ReadBundle)
            .options(selectinload(ReadBundle.items))
            .order_by(ReadBundle.created_at)
        )
        .scalars()
        .all()
    )
    snapshots = _load_bundle_snapshots(db, bundles)
    return ReadBundleListResponse(
        bundles=[_bundle_status_response(bundle, snapshots) for bundle in bundles]
    )


@app.get(
    "/read-bundles/{bundle_id}",
    response_model=ReadBundleStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_read_bundle(bundle_id: UUID, db: Session = Depends(get_db)):
    """维护侧查询单个共享凭证的清单、额度与状态（绝不返回快照原文）。"""
    bundle = db.get(ReadBundle, bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="read bundle not found"
        )
    snapshots = _load_bundle_snapshots(db, [bundle])
    return _bundle_status_response(bundle, snapshots)


@app.post(
    "/read-bundles/{bundle_id}/revoke",
    response_model=BundleRevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_read_bundle(bundle_id: UUID, db: Session = Depends(get_db)):
    """撤销共享凭证：原子条件 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadBundle)
        .where(ReadBundle.id == bundle_id, ReadBundle.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return BundleRevokeResponse(
            bundle_id=bundle_id, revoked=True, already_revoked=False
        )
    if db.get(ReadBundle, bundle_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="read bundle not found"
        )
    return BundleRevokeResponse(
        bundle_id=bundle_id, revoked=True, already_revoked=True
    )


def _load_bundle_snapshots(
    db: Session, bundles: list[ReadBundle]
) -> dict[UUID, Snapshot]:
    """批量取清单项引用的快照（维护侧需要其 record/version 与撤回状态）。"""
    snapshot_ids = {
        item.snapshot_id
        for bundle in bundles
        for item in bundle.items
    }
    if not snapshot_ids:
        return {}
    snapshots = (
        db.execute(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))
        .scalars()
        .all()
    )
    return {snapshot.id: snapshot for snapshot in snapshots}


# ---- 读取凭证泄露换发（单快照令牌 / 多快照共享凭证统一入口） ----


def _rotation_conflict(reason: str) -> None:
    """换发资格不满足或并发落选：统一 409，不把可换发凭证换成新凭证。"""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"message": "credential is not eligible for rotation", "reason": reason},
    )


def _rotation_payload(
    *,
    kind: str,
    request_id: str,
    old_credential: str,
    new_credential: str,
    snapshot_ids: list[UUID],
    expires_at: datetime | None,
    max_reads: int | None,
    reads_used: int,
    rotated_at: datetime,
    items: list[RotationBundleItem] | None = None,
) -> CredentialRotationResponse:
    """组装换发响应：剩余额度按换发时已用次数结算（不限次时为 null）。"""
    remaining = None if max_reads is None else max(max_reads - reads_used, 0)
    return CredentialRotationResponse(
        kind=kind,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_credential,
        snapshot_ids=snapshot_ids,
        expires_at=expires_at,
        max_reads=max_reads,
        reads_used=reads_used,
        remaining_reads=remaining,
        rotated_at=rotated_at,
        items=items,
    )


def _replay_rotation(record: CredentialRotation) -> CredentialRotationResponse:
    """相同（原凭证, 请求标识）重试：原样回放首次换发结果，不再次换发。"""
    response = CredentialRotationResponse.model_validate(record.response_payload)
    response.replayed = True
    return response


def _rotate_token(
    db: Session, snapshot_id: UUID, request_id: str, old_credential: str
) -> CredentialRotationResponse:
    """单快照令牌换发：新令牌继承快照范围、原绝对到期时刻与已用次数。"""
    # 锁序与公开读取一致：先快照行、再凭证行，避免与并发读取形成死锁；
    # 换发等待快照锁期间，先提交的读取按既有规则计入旧令牌并被新令牌继承。
    snapshot = db.execute(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()
    ).scalar_one_or_none()
    read_token = db.execute(
        select(ReadToken)
        .where(ReadToken.token == old_credential)
        .with_for_update()
        # 锁可能在队列中等待并发读取/换发提交：必须刷新为当前已提交版本，
        # 否则会读到定位阶段缓存的旧 rotated_at/reads_used（identity map）。
        .execution_options(populate_existing=True)
    ).scalar_one()

    prior = db.execute(
        select(CredentialRotation).where(
            CredentialRotation.old_credential == old_credential,
            CredentialRotation.request_id == request_id,
        )
    ).scalar_one_or_none()
    if prior is not None:
        db.rollback()
        return _replay_rotation(prior)

    # 仅未撤销、未换发、未到期且额度未耗尽的凭证可换发。
    # 快照是否已撤回不作为换发条件（撤回快照的令牌仍可换发，只是继续读不到正文）。
    if read_token.revoked_at is not None:
        _rotation_conflict("revoked")
    if read_token.rotated_at is not None:
        # 不同请求标识对同一原凭证的换发：仅一次成功，落选方在此冲突
        _rotation_conflict("already_rotated")
    now = datetime.now(timezone.utc)
    if read_token.expires_at is not None and read_token.expires_at <= now:
        _rotation_conflict("expired")
    if (
        read_token.max_reads is not None
        and read_token.reads_used >= read_token.max_reads
    ):
        _rotation_conflict("quota_exhausted")

    rotated_at = now
    new_token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=read_token.snapshot_id,
        expires_at=read_token.expires_at,  # 继承原绝对到期时刻，不延长
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,  # 继承已用次数，不重置
        chain_id=read_token.chain_id,  # 继承换发链标识，多次换发仍同链
    )
    db.add(new_token)
    db.flush()
    read_token.rotated_at = rotated_at  # 旧令牌换发提交后不能再读取
    payload = _rotation_payload(
        kind=ROTATION_KIND_TOKEN,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_token.token,
        snapshot_ids=[read_token.snapshot_id],
        expires_at=read_token.expires_at,
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,
        rotated_at=rotated_at,
    )
    db.add(
        CredentialRotation(
            kind=ROTATION_KIND_TOKEN,
            old_credential=old_credential,
            new_credential=new_token.token,
            request_id=request_id,
            response_payload=payload.model_dump(mode="json"),
        )
    )
    db.commit()
    return payload


def _rotate_bundle(
    db: Session, bundle_id: UUID, request_id: str, old_credential: str
) -> CredentialRotationResponse:
    """多快照共享凭证换发：新凭证继承冻结清单、原到期时刻与已用共享额度。

    清单中个别快照已撤回时仍可换发：新凭证复制冻结清单，撤回项继续不可读、
    读取该项仍不扣共享额度，其他项继续可读。
    """
    frozen_items = (
        db.execute(
            select(ReadBundleItem)
            .where(ReadBundleItem.bundle_id == bundle_id)
            .order_by(ReadBundleItem.position)
        )
        .scalars()
        .all()
    )
    snapshot_ids = [item.snapshot_id for item in frozen_items]
    # 先按清单快照行加锁（排序保证加锁顺序一致），再锁凭证行：
    # 与公开读取的“快照行锁 → 共享额度条件 UPDATE”同序，串行化而不死锁。
    snapshots = (
        db.execute(
            select(Snapshot)
            .where(Snapshot.id.in_(snapshot_ids))
            .order_by(Snapshot.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    bundle = db.execute(
        select(ReadBundle)
        .where(ReadBundle.id == bundle_id)
        .with_for_update()
        # 获得行锁后刷新为当前已提交版本（等待锁期间可能有读取扣次或换发完成）
        .execution_options(populate_existing=True)
    ).scalar_one()

    prior = db.execute(
        select(CredentialRotation).where(
            CredentialRotation.old_credential == old_credential,
            CredentialRotation.request_id == request_id,
        )
    ).scalar_one_or_none()
    if prior is not None:
        db.rollback()
        return _replay_rotation(prior)

    if bundle.revoked_at is not None:
        _rotation_conflict("revoked")
    if bundle.rotated_at is not None:
        _rotation_conflict("already_rotated")
    now = datetime.now(timezone.utc)
    if bundle.expires_at is not None and bundle.expires_at <= now:
        _rotation_conflict("expired")
    if bundle.reads_used >= bundle.max_reads:
        _rotation_conflict("quota_exhausted")

    rotated_at = now
    new_bundle = ReadBundle(
        credential=secrets.token_urlsafe(32),
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,  # 继承已用共享额度，不重置
        expires_at=bundle.expires_at,  # 继承原绝对到期时刻
        chain_id=bundle.chain_id,  # 继承换发链标识，多次换发仍同链
    )
    db.add(new_bundle)
    db.flush()
    for item in frozen_items:
        db.add(
            ReadBundleItem(
                bundle_id=new_bundle.id,
                snapshot_id=item.snapshot_id,
                position=item.position,
            )
        )
    bundle.rotated_at = rotated_at
    response_items = [
        RotationBundleItem(
            snapshot_id=item.snapshot_id,
            record_id=by_id[item.snapshot_id].record_id,
            version=by_id[item.snapshot_id].version,
            position=item.position,
            status=_snapshot_status(by_id[item.snapshot_id]),
            withdrawn_at=by_id[item.snapshot_id].withdrawn_at,
        )
        for item in frozen_items
    ]
    payload = _rotation_payload(
        kind=ROTATION_KIND_BUNDLE,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_bundle.credential,
        snapshot_ids=snapshot_ids,
        expires_at=bundle.expires_at,
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,
        rotated_at=rotated_at,
        items=response_items,
    )
    db.add(
        CredentialRotation(
            kind=ROTATION_KIND_BUNDLE,
            old_credential=old_credential,
            new_credential=new_bundle.credential,
            request_id=request_id,
            response_payload=payload.model_dump(mode="json"),
        )
    )
    db.commit()
    return payload


@app.post(
    "/credentials/rotate",
    response_model=CredentialRotationResponse,
    dependencies=[Depends(require_service_key)],
)
def rotate_credential(
    payload: CredentialRotateRequest, db: Session = Depends(get_db)
):
    """读取凭证疑似泄露时换发：单快照令牌或多快照共享凭证同一入口。

    维护人员凭服务密钥提交（原凭证, 请求标识）：
    - 仅未撤销、未到期且额度未耗尽的凭证可换发；新凭证继承原范围（适用快照/
      冻结清单）、原绝对到期时刻与已用次数，旧凭证换发提交后不能再读取；
    - 相同原凭证与请求标识重试 -> 原样返回首次换发结果（replayed=true），
      不再次换发；不同请求标识并发/重复换发仅一次成功，其余 409；
    - 换发与同时进行的成功读取按事务先后结算（快照行锁串行化），
      不重置也不超支额度；
    - 未知凭证 404；共享清单中个别快照已撤回仍可换发，该项继续不可读。
    """
    old_credential = payload.credential
    request_id = payload.request_id
    # 先以普通读定位凭证及其类型（随后在类型分支内按“先快照后凭证”顺序加锁）
    read_token = db.execute(
        select(ReadToken).where(ReadToken.token == old_credential)
    ).scalar_one_or_none()
    if read_token is not None:
        return _rotate_token(db, read_token.snapshot_id, request_id, old_credential)
    bundle = db.execute(
        select(ReadBundle).where(ReadBundle.credential == old_credential)
    ).scalar_one_or_none()
    if bundle is not None:
        return _rotate_bundle(db, bundle.id, request_id, old_credential)
    db.rollback()
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="credential not found"
    )


@app.post(
    "/tokens/{token}/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_token(token: str, db: Session = Depends(get_db)):
    """撤销令牌：单条原子 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadToken)
        .where(ReadToken.token == token, ReadToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return RevokeResponse(token=token, revoked=True, already_revoked=False)
    if db.get(ReadToken, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return RevokeResponse(token=token, revoked=True, already_revoked=True)


@app.get(
    "/tokens/{token}",
    response_model=TokenStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_token_status(token: str, db: Session = Depends(get_db)):
    """维护侧查询令牌的读取次数上限、已用与剩余次数（绝不返回快照原文）。"""
    read_token = db.get(ReadToken, token)
    if read_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    snapshot = db.get(Snapshot, read_token.snapshot_id)
    now = datetime.now(timezone.utc)
    expired = read_token.expires_at is not None and read_token.expires_at <= now
    exhausted = (
        read_token.max_reads is not None and read_token.reads_used >= read_token.max_reads
    )
    remaining = (
        None
        if read_token.max_reads is None
        else max(read_token.max_reads - read_token.reads_used, 0)
    )
    return TokenStatusResponse(
        token=read_token.token,
        snapshot_id=read_token.snapshot_id,
        record_id=snapshot.record_id,
        version=snapshot.version,
        created_at=read_token.created_at,
        expires_at=read_token.expires_at,
        revoked_at=read_token.revoked_at,
        rotated_at=read_token.rotated_at,
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,
        remaining_reads=remaining,
        active=(
            read_token.revoked_at is None
            and read_token.rotated_at is None
            and not expired
            and not exhausted
        ),
    )


@app.post(
    "/snapshots/{snapshot_id}/withdraw",
    response_model=SnapshotWithdrawResponse,
    dependencies=[Depends(require_service_key)],
)
def withdraw_snapshot(
    snapshot_id: UUID,
    payload: SnapshotWithdrawRequest = Body(default_factory=SnapshotWithdrawRequest),
    db: Session = Depends(get_db),
):
    """将快照永久撤回：记录首次撤回时间与原因，重复撤回返回首次结果。

    - 未知快照 -> 404；缺失/错误服务密钥 -> 401；
    - 首次撤回必须提供非空白原因 -> 422；
    - 条件 UPDATE 只命中未撤回行：并发撤回恰好一个成功，
      落选方随后读回首次撤回时间与状态，结果幂等。
    """
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    if snapshot.withdrawn_at is None and not (payload.reason and payload.reason.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reason is required when withdrawing a snapshot",
        )
    # 原子条件更新：只有未撤回行能命中，首次撤回时间由此固定，永不被覆盖。
    withdrawn_at = db.execute(
        update(Snapshot)
        .where(Snapshot.id == snapshot_id, Snapshot.withdrawn_at.is_(None))
        .values(withdrawn_at=func.now(), withdraw_reason=payload.reason)
        .returning(Snapshot.withdrawn_at)
    ).scalar_one_or_none()
    if withdrawn_at is None:
        # 已被（并发的）首次撤回终结：回滚本次更新，读回首次结果
        db.rollback()
        snapshot = db.get(Snapshot, snapshot_id)
        withdrawn_at = snapshot.withdrawn_at
    else:
        db.commit()
    return SnapshotWithdrawResponse(
        snapshot_id=snapshot_id,
        withdrawn_at=withdrawn_at,
        status=SNAPSHOT_WITHDRAWN_STATUS,
    )


@app.get(
    "/records/{record_id}",
    response_model=RecordStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_record(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查看记录当前版本、受控标记与已发布快照列表。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.record_id == record_id).order_by(Snapshot.version)
        )
        .scalars()
        .all()
    )
    return RecordStatusResponse(
        record_id=record.id,
        current_version=record.current_version,
        controlled=record.controlled,
        snapshots=[
            SnapshotInfo(
                snapshot_id=s.id,
                version=s.version,
                status=_snapshot_status(s),
                published_at=s.published_at,
                withdrawn_at=s.withdrawn_at,
                withdraw_reason=s.withdraw_reason,
            )
            for s in snapshots
        ],
    )


@app.get("/public/snapshots/{snapshot_id}", response_model=PublicSnapshotResponse)
def read_snapshot(
    snapshot_id: UUID,
    token: str = Query(..., description="发布时签发的读取令牌或共享凭证"),
    db: Session = Depends(get_db),
):
    """公开读取：凭有效令牌或多快照共享凭证返回脱敏正文。

    先对快照行加锁再判定有效性，使读取与永久撤回由数据库行锁串行化：
    撤回提交后才开始的读取在此看到 withdrawn_at 非空，直接返回模糊 403，
    既不返回正文也不执行计数 UPDATE（不消耗任何额度）。

    单快照令牌：有效性判定与次数消耗由同一条条件 UPDATE 原子完成
    （未撤销、未到期、(不限次或 reads_used < max_reads)）。

    多快照共享凭证：额外要求该快照在凭证绑定清单内，且凭证未撤销、未到期、
    共享额度未耗尽；计数条件合并在同一条 JOIN 条件 UPDATE 中原子完成：
    - 凭证不存在、与清单不匹配、快照不在清单内、已撤销、已到期、额度耗尽 -> rowcount=0，
      统一返回模糊 403，且这些请求不消耗共享额度；
    - 清单内某项快照后来被撤回：只拒绝该项（快照行锁阶段即 403），不扣额度，
      其他项仍可凭同一凭证读取；
    - 到期、撤销或额度耗尽时全部拒绝。
    """
    snapshot = db.execute(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()
    ).scalar_one_or_none()
    if snapshot is None or snapshot.withdrawn_at is not None:
        # 不区分“快照不存在 / 已撤回 / 令牌或凭证无效 / 已撤销 / 已过期 / 次数用尽 / 不在清单”，
        # 快照不存在或已撤回时直接拒绝，不触碰任何读取计数。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    now = datetime.now(timezone.utc)
    # 优先按既有单快照令牌判定（行为与响应完全保持兼容）
    # 条件 UPDATE 命中时一并 RETURNING 换发链标识：扣次成功即据此在同事务写审计事件。
    chain_id = db.execute(
        update(ReadToken)
        .where(
            ReadToken.token == token,
            ReadToken.snapshot_id == snapshot.id,
            ReadToken.revoked_at.is_(None),
            ReadToken.rotated_at.is_(None),
            or_(ReadToken.expires_at.is_(None), ReadToken.expires_at > now),
            or_(
                ReadToken.max_reads.is_(None),
                ReadToken.reads_used < ReadToken.max_reads,
            ),
        )
        .values(reads_used=ReadToken.reads_used + 1)
        .returning(ReadToken.chain_id)
    ).scalar_one_or_none()
    if chain_id is not None:
        credential_kind = READ_EVENT_CREDENTIAL_KIND_TOKEN
    else:
        # 不是有效的单快照令牌：按多快照共享凭证判定（凭证只能读取清单内快照）。
        # 成员关系、撤销/换发/到期/额度判定与共享计数递增合并为同一条条件 UPDATE，
        # 命中行即 reads_used = reads_used + 1 并 RETURNING 换发链标识：
        # 行锁串行化并发读取，跨清单的成功次数绝不超过共享上限。
        chain_id = db.execute(
            update(ReadBundle)
            .where(
                ReadBundle.credential == token,
                ReadBundle.revoked_at.is_(None),
                ReadBundle.rotated_at.is_(None),
                or_(ReadBundle.expires_at.is_(None), ReadBundle.expires_at > now),
                ReadBundle.reads_used < ReadBundle.max_reads,
                ReadBundle.id.in_(
                    select(ReadBundleItem.bundle_id).where(
                        ReadBundleItem.snapshot_id == snapshot.id
                    )
                ),
            )
            .values(reads_used=ReadBundle.reads_used + 1)
            .returning(ReadBundle.chain_id)
        ).scalar_one_or_none()
        if chain_id is None:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
            )
        credential_kind = READ_EVENT_CREDENTIAL_KIND_BUNDLE
    # 成功读取：与额度扣减同事务追加一条不可变审计事件（不含凭证明文与任何正文）。
    db.add(
        ReadEvent(
            snapshot_id=snapshot.id,
            credential_kind=credential_kind,
            chain_id=chain_id,
        )
    )
    db.commit()
    return {
        "record_id": snapshot.record_id,
        "version": snapshot.version,
        "status": SNAPSHOT_STATUS,
        "redacted_text": snapshot.redacted_text,
    }


# ---- 成功读取审计事件查询（维护侧，服务密钥；按快照 + 时间范围键集分页） ----

# 单页上限：防止一次性拉取过大的结果集
_READ_EVENTS_DEFAULT_LIMIT = 100
_READ_EVENTS_MAX_LIMIT = 500


def _bad_request_422(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _encode_events_cursor(
    *,
    snapshot_id: UUID,
    start: datetime,
    end: datetime,
    occurred_at: datetime,
    event_id: UUID,
) -> str:
    """游标不透明编码：绑定本次查询的快照与时间范围，并携带键集位置。"""
    payload = json.dumps(
        {
            "snapshot_id": str(snapshot_id),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "occurred_at": occurred_at.isoformat(),
            "event_id": str(event_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_events_cursor(
    raw_cursor: str, *, snapshot_id: UUID, start: datetime, end: datetime
) -> tuple[datetime, UUID]:
    """解码并校验游标：非法、伪造或与本次查询参数不一致一律明确拒绝（422）。"""
    try:
        padded = raw_cursor + "=" * (-len(raw_cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        cursor_snapshot = UUID(payload["snapshot_id"])
        cursor_start = datetime.fromisoformat(payload["start"])
        cursor_end = datetime.fromisoformat(payload["end"])
        occurred_at = datetime.fromisoformat(payload["occurred_at"])
        event_id = UUID(payload["event_id"])
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise _bad_request_422("invalid cursor") from exc
    if any(
        dt.tzinfo is None
        for dt in (cursor_start, cursor_end, occurred_at)
    ):
        raise _bad_request_422("invalid cursor")
    # 游标只能在它所属的（快照, 时间范围）查询上继续翻页
    if (
        cursor_snapshot != snapshot_id
        or cursor_start != start
        or cursor_end != end
    ):
        raise _bad_request_422(
            "cursor does not match the snapshot_id/start/end of this query"
        )
    return occurred_at, event_id


@app.get(
    "/snapshots/{snapshot_id}/read-events",
    response_model=ReadEventListResponse,
    dependencies=[Depends(require_service_key)],
)
def list_snapshot_read_events(
    snapshot_id: UUID,
    start: datetime = Query(..., description="起始时刻（含），必须带时区，如 2026-10-01T00:00:00Z"),
    end: datetime = Query(..., description="结束时刻（不含），必须带时区"),
    limit: int = Query(default=_READ_EVENTS_DEFAULT_LIMIT, ge=1, le=_READ_EVENTS_MAX_LIMIT),
    cursor: str | None = Query(default=None, description="上一页返回的 next_cursor"),
    db: Session = Depends(get_db),
):
    """维护侧按快照与时间范围分页查询成功读取审计事件（服务密钥）。

    - 仅记录成功读取：失败、清单外或已撤回快照的请求不产生事件；
    - 返回事件标识、快照、时间、凭证类型（token/bundle）与稳定换发链标识，
      不返回原文、脱敏正文或任何凭证明文；
    - 以 (occurred_at, id) 键集分页：两列写入后均不可变，并发写入期间翻页
      不会重复或遗漏查询起点前已提交的事件；查询期间新提交的事件可能在
      下一页才出现（不影响本页一致性）；
    - 未知快照 404；时间范围非法（无时区/start>end）或游标非法 422；
    - 时间范围为半开区间 [start, end)。
    """
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        # 快照不存在（含从未存在）明确 404；已撤回快照的历史成功事件仍可审计
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    if start.tzinfo is None or end.tzinfo is None:
        raise _bad_request_422("start and end must be timezone-aware timestamps")
    if start > end:
        raise _bad_request_422("start must be earlier than or equal to end")

    conditions = [
        ReadEvent.snapshot_id == snapshot_id,
        ReadEvent.occurred_at >= start,
        ReadEvent.occurred_at < end,
    ]
    if cursor is not None:
        cursor_at, cursor_event_id = _decode_events_cursor(
            cursor, snapshot_id=snapshot_id, start=start, end=end
        )
        conditions.append(
            tuple_(ReadEvent.occurred_at, ReadEvent.id)
            > (cursor_at, cursor_event_id)
        )

    rows = (
        db.execute(
            select(ReadEvent)
            .where(*conditions)
            .order_by(ReadEvent.occurred_at, ReadEvent.id)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_next = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_next:
        last = page[-1]
        next_cursor = _encode_events_cursor(
            snapshot_id=snapshot_id,
            start=start,
            end=end,
            occurred_at=last.occurred_at,
            event_id=last.id,
        )
    return ReadEventListResponse(
        snapshot_id=snapshot_id,
        start=start,
        end=end,
        limit=limit,
        events=[
            ReadEventInfo(
                event_id=event.id,
                snapshot_id=event.snapshot_id,
                occurred_at=event.occurred_at,
                credential_kind=event.credential_kind,
                chain_id=event.chain_id,
            )
            for event in page
        ],
        next_cursor=next_cursor,
    )
