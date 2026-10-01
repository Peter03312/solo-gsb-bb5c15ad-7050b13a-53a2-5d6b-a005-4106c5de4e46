"""受控发布端到端集成测试：需要真实 PostgreSQL。

默认尝试用 pgserver（pip 包，内置 Postgres 16 二进制）自动拉起临时库；
也可通过环境变量 TEST_DATABASE_URL 指定已有数据库。

运行：
    python3 -m unittest tests.test_integration_api -v
"""

import base64
import json
import os
import threading
import unittest
import uuid
from datetime import datetime, timezone

# 环境变量必须在导入 app.config 之前设置
os.environ.setdefault("SERVICE_KEY", "dev-service-key-change-me")
os.environ.setdefault(
    "REVIEW_CREDENTIALS",
    json.dumps(
        [
            {"username": "alice", "password": "alice-pw", "roles": ["operator"]},
            {"username": "bob", "password": "bob-pw", "roles": ["reviewer"]},
            {"username": "carol", "password": "carol-pw", "roles": ["operator", "reviewer"]},
        ]
    ),
)

_PG_SERVER = None
_SKIP_REASON = ""
if "TEST_DATABASE_URL" not in os.environ:
    try:
        import pgserver

        _PG_SERVER = pgserver.get_server("/tmp/pgdata-integration-test")
        # unix socket 形式：postgresql+psycopg2://postgres@/postgres?host=<socketdir>
        os.environ["DATABASE_URL"] = (
            "postgresql+psycopg2://postgres@/postgres?host=/tmp/pgdata-integration-test"
        )
    except Exception as exc:  # pragma: no cover - 环境不可用时跳过
        _PG_SERVER = None
        _SKIP_REASON = f"pgserver unavailable: {exc}"
else:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.models import PublishRequest as PublishRequestModel  # noqa: E402

SKIP_INTEGRATION = _PG_SERVER is None and "TEST_DATABASE_URL" not in os.environ

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key-change-me"}
BASIC_ALICE = ("alice", "alice-pw")
BASIC_BOB = ("bob", "bob-pw")
BASIC_CAROL = ("carol", "carol-pw")

TEXT_V1 = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
TEXT_V2 = "完全不同的新原文，敏感词ABC。"
INTERVALS_V1 = [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ControlledPublishIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 建表（幂等）并通过 TestClient 上下文触发 lifespan 之外的显式初始化
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例使用全新记录，避免相互干扰
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _mark_controlled(self):
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["controlled"])

    def _submit(self, auth=BASIC_ALICE, version=1, expires=None, expected=201):
        body = {"version": version}
        if expires is not None:
            body.update(expires)
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", auth=auth, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 基础鉴权 ----

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_service_key_endpoints_still_require_key(self):
        r = self.client.post("/records", json={"text": "x", "intervals": []})
        self.assertEqual(r.status_code, 401)
        r = self.client.put(f"/records/{self.record_id}/controlled")
        self.assertEqual(r.status_code, 401)

    def test_controlled_endpoints_require_basic_auth(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", json={"version": 1}
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # 错误口令
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("alice", "wrong"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)
        # 未知用户
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("nobody", "x"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)

    # ---- 受控标记与直接发布互斥 ----

    def test_mark_controlled_is_idempotent(self):
        self._mark_controlled()
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["controlled"])

    def test_mark_controlled_unknown_record_404(self):
        import uuid

        r = self.client.put(f"/records/{uuid.uuid4()}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_direct_publish_rejected_for_controlled_record(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(TEXT_V1, r.text)
        # 未留下任何快照或令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_submit_rejected_for_non_controlled_record(self):
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 角色校验 ----

    def test_operator_role_required_to_submit(self):
        self._mark_controlled()
        # bob 只有 reviewer 角色，不能提交
        r = self._submit(auth=BASIC_BOB, expected=403)
        self.assertNotIn(TEXT_V1, r.text)

    def test_reviewer_role_required_to_approve(self):
        self._mark_controlled()
        self._submit()
        # alice 只有 operator 角色，不能复核
        r = self.client.post(
            f"/publish-requests/{self._latest_request_id()}/approve", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 403)

    def _latest_request_id(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    # ---- 未知资源 ----

    def test_unknown_record_and_version(self):
        import uuid

        self._mark_controlled()
        r = self.client.post(
            f"/records/{uuid.uuid4()}/publish-requests", auth=BASIC_ALICE, json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        r = self._submit(version=99, expected=404)
        self.assertNotIn(TEXT_V1, r.text)

    def test_unknown_request_approve_reject_get(self):
        import uuid

        rid = str(uuid.uuid4())
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/approve", auth=BASIC_BOB).status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/reject", auth=BASIC_BOB, json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/publish-requests/{rid}", auth=BASIC_BOB).status_code, 404
        )

    # ---- 申请冻结版本内容 ----

    def test_request_freezes_version_content_at_submission(self):
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()
        # 提交后修订到版本 2
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 批准沿用冻结的版本 1 内容
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "approved")
        token = body["token"]
        snapshot_id = body["snapshot_id"]
        # 公开读取得到版本 1 的脱敏正文，而非版本 2 原文
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        self.assertNotIn(TEXT_V2, r.text)
        # 申请详情不泄露冻结原文
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertNotIn(TEXT_V1, r.text)
        self.assertNotIn("身份证", r.text)

    def test_approved_token_follows_existing_token_rules(self):
        self._mark_controlled()
        self._submit(version=1, expires={"expires_in_seconds": 3600})
        request_id = self._latest_request_id()
        # 提交后到批准前，申请保存的是相对时长策略（尚无绝对到期时刻）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["token_expires_in_seconds"], 3600)
        self.assertIsNone(r.json()["token_expires_at"])
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        # 到期时刻在批准签发时换算，约为批准时刻 + 3600s
        expires_at = r.json()["expires_at"]
        self.assertIsNotNone(expires_at)
        self.assertGreater(
            datetime.fromisoformat(expires_at), datetime.now(timezone.utc)
        )
        # 批准后申请记录实际绝对到期时刻
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertIsNotNone(r.json()["token_expires_at"])
        # 令牌可通过既有撤销接口撤销，撤销后公开读取 403
        r = self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_submit_with_past_expires_at_is_422(self):
        self._mark_controlled()
        r = self._submit(
            expires={"expires_at": "2000-01-01T00:00:00Z"}, expected=422
        )
        self.assertNotIn(TEXT_V1, r.text)

    def test_approval_failure_rolls_back_snapshot_and_token(self):
        """批准事务中途失败时，不得留下快照或可读令牌。"""
        from unittest import mock

        from sqlalchemy import select

        from app import main as main_module
        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()

        # 让令牌在 flush 时超长（read_tokens.token 为 varchar(64)）：
        # 此时快照 INSERT 已执行、令牌已加入事务，但尚未提交。
        with mock.patch.object(
            main_module.secrets, "token_urlsafe", return_value="x" * 65
        ):
            with self.assertRaises(Exception):
                self.client.post(
                    f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
                )

        with SessionLocal() as session:
            # 申请仍为 pending（终态未写入），快照与令牌全部回滚
            pr = session.get(PublishRequestModel, request_id)
            self.assertEqual(pr.status, "pending")
            self.assertIsNone(pr.snapshot_id)
            snapshots = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .all()
            )
            self.assertEqual(snapshots, [])
            orphan_tokens = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(orphan_tokens, 0)
        # 申请仍可被正常批准（证明前一次没有遗留半完成状态）
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    def test_explicit_expires_at_is_kept_at_approval(self):
        from datetime import timedelta

        self._mark_controlled()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._submit(expires={"expires_at": future})
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["expires_at"])

    # ---- 申请人不得复核 ----

    def test_requester_cannot_review_own_request(self):
        # carol 同时具备 operator/reviewer，仍不得复核自己的申请
        self._mark_controlled()
        r = self._submit(auth=BASIC_CAROL)
        request_id = r.json()["request_id"]
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
        )
        self.assertEqual(r.status_code, 403)
        # 申请仍处于 pending，他人可正常批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    # ---- 终态语义 ----

    def test_rejected_request_cannot_be_approved(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "材料不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 驳回后不能批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409)
        # 再次驳回也是 409
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
        )
        self.assertEqual(r.status_code, 409)
        # 驳回未生成快照/令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_approved_request_cannot_be_decided_again(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
            ).status_code,
            409,
        )

    def test_concurrent_reviews_produce_single_terminal_state(self):
        """两个线程同时复核同一申请：恰好一个终态成功，另一个 409。"""
        self._mark_controlled()
        for _ in range(5):
            self._submit()
            request_id = self._latest_request_id()
            results: list[int] = []
            barrier = threading.Barrier(2)

            def approve():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB).status_code
                )

            def reject():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(
                        f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
                    ).status_code
                )

            t1 = threading.Thread(target=approve)
            t2 = threading.Thread(target=reject)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(results), [200, 409], results)
            # 库里申请唯一终态，且至多一份快照
            from app.db import SessionLocal
            from app.models import Snapshot

            with SessionLocal() as session:
                pr = session.get(PublishRequestModel, request_id)
                self.assertIn(pr.status, ("approved", "rejected"))
                self.assertIsNotNone(pr.reviewed_by)
            r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
            self.assertLessEqual(len(r.json()["snapshots"]), 1)

    # ---- 既有接口兼容性（非受控记录） ----

    def test_legacy_flow_unchanged_on_non_controlled_record(self):
        # 直接发布非受控记录仍可用
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        # 状态接口新增字段不破坏既有结构
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        body = r.json()
        self.assertFalse(body["controlled"])
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(len(body["snapshots"]), 1)

    def test_legacy_revision_409_and_422_still_work(self):
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 99, "text": "x", "intervals": []},
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": "短", "intervals": [{"start": 0, "end": 99}]},
        )
        self.assertEqual(r.status_code, 422)

    def test_public_read_unified_403_does_not_leak(self):
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token=definitely-not-a-token"
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    # ---- 读取次数上限（直接发布） ----

    def _direct_publish(self, version=1, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json=body,
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_direct_publish_response_reports_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        self.assertEqual(pub["max_reads"], 2)
        # 不设上限时该字段为 None（再次发布复用同一快照，签出新令牌）
        pub_unlimited = self._direct_publish()
        self.assertIsNone(pub_unlimited["max_reads"])
        self.assertEqual(pub_unlimited["snapshot_id"], pub["snapshot_id"])

    def test_max_reads_must_be_positive_integer(self):
        for bad in (0, -1):
            r = self.client.post(
                f"/records/{self.record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422, r.text)
        # 非整数同样 422
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1.5},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_unlimited_token_can_be_read_repeatedly(self):
        pub = self._direct_publish()
        self.assertIsNone(pub["max_reads"])
        sid, tok = pub["snapshot_id"], pub["token"]
        for _ in range(7):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        status_body = r.json()
        self.assertIsNone(status_body["max_reads"])
        self.assertIsNone(status_body["remaining_reads"])
        self.assertEqual(status_body["reads_used"], 7)
        self.assertTrue(status_body["active"])

    def test_capped_token_allows_exactly_max_reads_then_403(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 前两次成功
        for expected_used in (1, 2):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("redacted_text", r.json())
            q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
            self.assertEqual(q["reads_used"], expected_used)
            self.assertEqual(q["remaining_reads"], 2 - expected_used)
        # 第三次起沿用既有模糊拒绝
        for _ in range(3):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        # 用尽后计数不再增长，维护侧显示已耗尽
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertEqual(q["remaining_reads"], 0)
        self.assertFalse(q["active"])
        # 维护侧响应不含原文
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertNotIn(TEXT_V1, r.text)

    def test_invalid_revoked_and_mismatched_requests_do_not_consume(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 第一次成功
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        # 无效令牌不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token=wrong-token")
        self.assertEqual(r.status_code, 403)
        # 令牌与快照不匹配：给同一快照再签发一个令牌，交叉使用
        other = self._read_token_for_same_snapshot_via_republish(
            sid, body_extra={"max_reads": 2}
        )
        self.assertNotEqual(other, tok)
        r = self.client.get(f"/public/snapshots/{sid}?token={other}")
        self.assertEqual(r.status_code, 200, r.text)
        # 用 other 访问一个不存在的快照：403，不消耗 other
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token={other}"
        )
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{other}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        # 撤销后读取 403，不消耗剩余次数
        self.assertEqual(
            self.client.post(f"/tokens/{tok}/revoke", headers=SERVICE_HEADERS).status_code,
            200,
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertIsNotNone(q["revoked_at"])

    def _read_token_for_same_snapshot_via_republish(self, existing_snapshot_id, body_extra=None):
        """同一快照可由再次直接发布签发出另一支令牌（ON CONFLICT 复用快照）。"""
        pub = self._direct_publish(body_extra=body_extra)
        self.assertEqual(pub["snapshot_id"], existing_snapshot_id)
        return pub["token"]

    def test_distinct_tokens_on_same_snapshot_count_independently(self):
        pub1 = self._direct_publish(body_extra={"max_reads": 1})
        sid = pub1["snapshot_id"]
        tok1 = pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 第二次直接发布未携带 max_reads -> 新令牌不限次
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertIsNone(q2["max_reads"])
        # tok1 用尽
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 403
        )
        # tok2 不受影响，继续可读
        for _ in range(4):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
            )
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q2["reads_used"], 4)

    def test_expiry_or_revocation_takes_precedence_over_remaining_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 5, "expires_in_seconds": 1})
        sid, tok = pub["snapshot_id"], pub["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        import time as _time

        _time.sleep(1.2)
        # 仍有剩余次数，但已到期 -> 403，不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertEqual(q["remaining_reads"], 4)

    def test_concurrent_reads_never_exceed_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 10})
        sid, tok = pub["snapshot_id"], pub["token"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(20)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 10)
        self.assertTrue(all(code == 403 for code in failures))
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 10)
        self.assertEqual(q["remaining_reads"], 0)

    def test_token_status_unknown_token_404_and_requires_service_key(self):
        r = self.client.get("/tokens/not-a-real-token", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/tokens/not-a-real-token")
        self.assertEqual(r.status_code, 401)

    # ---- 读取次数上限（受控发布：申请冻结、批准生效） ----

    def test_controlled_max_reads_frozen_at_submission_and_effective_after_approval(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 申请详情可见冻结的上限（不泄露原文）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 2)
        self.assertNotIn(TEXT_V1, r.text)
        # 批准签发结果携带上限
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_reads"], 2)
        sid, tok = r.json()["snapshot_id"], r.json()["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_controlled_reject_creates_no_token_or_counters(self):
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 3})
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "不予发布"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 3)
        with SessionLocal() as session:
            # 驳回不产生快照/令牌（限定本记录，不受其他用例数据影响）
            token_count = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(token_count, 0)
            snapshot_count = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .count()
            )
            self.assertEqual(snapshot_count, 0)
        # 驳回后不能批准
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )

    def test_controlled_max_reads_must_be_positive(self):
        self._mark_controlled()
        r = self._submit(version=1, expires={"max_reads": 0}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 数据库升级路径 ----

    def test_existing_db_gets_controlled_column(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE records DROP COLUMN controlled"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("records")}
        self.assertIn("controlled", columns)
        # 旧行默认 false，数据未损坏
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["controlled"])

    def test_existing_db_gets_read_limit_columns(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        # 先发布一支令牌，模拟既有部署的 read_tokens / publish_requests 数据
        pub = self._direct_publish()
        token = pub["token"]
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN reads_used"))
            conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_max_reads"))
        app_main._init_db_with_retry()
        inspector = inspect(engine)
        token_columns = {c["name"] for c in inspector.get_columns("read_tokens")}
        request_columns = {c["name"] for c in inspector.get_columns("publish_requests")}
        self.assertIn("max_reads", token_columns)
        self.assertIn("reads_used", token_columns)
        self.assertIn("token_max_reads", request_columns)

        from app.db import SessionLocal
        from app.models import PublishRequest as PRModel
        from app.models import ReadToken as RTModel

        with SessionLocal() as session:
            # 既有令牌迁移后继续不限次，已用次数为 0
            existing = session.get(RTModel, token)
            self.assertIsNone(existing.max_reads)
            self.assertEqual(existing.reads_used, 0)
            pr = session.get(PRModel, request_id)
            self.assertIsNone(pr.token_max_reads)

        # 服务升级后既有令牌仍可继续不限次读取
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(
            f"/public/snapshots/{pub['snapshot_id']}?token={token}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        q = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertIsNone(q["remaining_reads"])


    # ---- 快照永久撤回 ----

    def _withdraw(self, snapshot_id, reason="误发，撤回", expected=200):
        body = {} if reason is None else {"reason": reason}
        r = self.client.post(
            f"/snapshots/{snapshot_id}/withdraw", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def test_withdraw_requires_service_key_and_unknown_is_404(self):
        import uuid

        # 缺失/错误服务密钥沿用 401（且在未知性判断之前整次拒绝）
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw", json={"reason": "x"}
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw",
            headers={"X-Service-Key": "wrong-key"},
            json={"reason": "x"},
        )
        self.assertEqual(r.status_code, 401)
        # 密钥正确但快照未知 -> 404
        r = self._withdraw(uuid.uuid4(), expected=404)

    def test_withdraw_first_time_requires_reason_422(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        # 缺 reason / 空白 reason -> 422，且不产生撤回
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, r.text)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"][0]["status"], "published")

    def test_withdrawn_snapshot_reads_403_for_any_token_without_consuming(self):
        # 两支令牌：一支带次数上限，一支不限次
        pub1 = self._direct_publish(body_extra={"max_reads": 3})
        sid, tok1 = pub1["snapshot_id"], pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 撤回前各成功读一次
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
        )
        r = self._withdraw(sid, reason="发现不应公开的内容")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["snapshot_id"], sid)
        self.assertEqual(body["status"], "withdrawn")
        self.assertIsNotNone(body["withdrawn_at"])
        self.assertNotIn("reason", body)  # 撤回响应不向操作回执以外扩散原因
        # 无论持有哪枚令牌（甚至无效令牌），公开读取统一 403 snapshot unavailable
        for tok in (tok1, tok2, "not-a-token"):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
            self.assertNotIn("撤回", r.text)  # 公开侧不透露撤回原因
        # 撤回后的读取不消耗次数
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q1["remaining_reads"], 2)
        self.assertEqual(q2["reads_used"], 1)

    def test_repeat_withdraw_returns_first_result_and_keeps_first_reason(self):
        pub = self._direct_publish(body_extra={"max_reads": 5})
        sid = pub["snapshot_id"]
        first = self._withdraw(sid, reason="首次撤回原因").json()
        first_at = first["withdrawn_at"]
        # 重复撤回（即便原因不同或为空）返回首次时间与状态
        for body in ({"reason": "另一个原因"}, {}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 200, r.text)
            again = r.json()
            self.assertEqual(again["status"], "withdrawn")
            self.assertEqual(again["withdrawn_at"], first_at)
        # 维护侧记录保留首次撤回时间与原因
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        info = r.json()["snapshots"][0]
        self.assertEqual(info["status"], "withdrawn")
        self.assertEqual(info["withdrawn_at"], first_at)
        self.assertEqual(info["withdraw_reason"], "首次撤回原因")

    def test_withdraw_is_scoped_to_single_snapshot(self):
        # 版本 1 与版本 2 都发布；撤回版本 1 不影响版本 2
        pub1 = self._direct_publish(version=1)
        sid1, tok1 = pub1["snapshot_id"], pub1["token"]
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        pub2 = self._direct_publish(version=2)
        sid2, tok2 = pub2["snapshot_id"], pub2["token"]
        self._withdraw(sid1)
        # 版本 1 不可读；版本 2 继续可读
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid1}?token={tok1}").status_code, 403
        )
        r = self.client.get(f"/public/snapshots/{sid2}?token={tok2}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TEXT_V1, r.text)
        # 维护侧两个快照状态并存；撤回的历史脱敏正文仍留存可见于记录状态
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        snapshots = r.json()["snapshots"]
        statuses = {s["version"]: s["status"] for s in snapshots}
        self.assertEqual(statuses, {1: "withdrawn", 2: "published"})
        self.assertEqual(snapshots[0]["withdraw_reason"], "误发，撤回")

    def test_direct_publish_of_withdrawn_version_conflicts(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        self._withdraw(sid)
        # 再次直接发布同一版本 -> 409，且不签发新令牌
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 9},
        )
        self.assertEqual(r.status_code, 409, r.text)
        # 修订出新版本后仍可正常发布（撤回只作用于该快照）
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 2},
        )
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.get(
            f"/public/snapshots/{r.json()['snapshot_id']}?token={r.json()['token']}"
        )
        self.assertEqual(r.status_code, 200)

    def test_controlled_submit_for_withdrawn_version_conflicts(self):
        # 先发布版本 1（非受控直接发布）再撤回，随后把记录转为受控
        pub = self._direct_publish()
        self._withdraw(pub["snapshot_id"])
        self._mark_controlled()
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    def test_pending_request_can_be_rejected_but_approval_conflicts_after_withdraw(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 期间该版本经另一申请批准产生快照并被撤回（模拟版本曾公开后撤回）
        other = self._submit()
        other_id = self._latest_request_id_from_list()
        r = self.client.post(f"/publish-requests/{other_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["snapshot_id"]
        self._withdraw(sid, reason="复核后发现问题")
        # 撤回不改动待审申请；批准命中已撤回快照 -> 409，申请仍可驳回
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "pending")
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "版本已撤回，不予批准"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 已批准申请记录仍留存，并指向撤回快照
        r = self.client.get(f"/publish-requests/{other_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(r.json()["snapshot_id"], sid)
        # 撤回后公开读取仍 403
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token=x").status_code, 403
        )

    def _latest_request_id_from_list(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    def test_concurrent_withdraw_and_reads_respect_boundary(self):
        pub = self._direct_publish(body_extra={"max_reads": 50})
        sid, tok = pub["snapshot_id"], pub["token"]
        results: list[int] = []
        barrier = threading.Barrier(9)

        def withdraw():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.post(
                f"/snapshots/{sid}/withdraw",
                headers=SERVICE_HEADERS,
                json={"reason": "并发撤回"},
            )
            results.append(0 if r.status_code == 200 else r.status_code)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            # 略作退让，争取让撤回先提交；无论先后，撤回提交后的读取必须 403
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            results.append(code)

        threads = [threading.Thread(target=withdraw)]
        threads += [threading.Thread(target=read_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(0, results)  # 撤回成功
        concurrent_reads = [c for c in results if c != 0]
        # 撤回提交后开始的读取一律 403；在撤回提交前已获行锁的读取至多 8 个 200
        self.assertTrue(all(c in (200, 403) for c in concurrent_reads), results)
        # 撤回完成后的新读取稳定 403
        for _ in range(5):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
            )
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        # 消耗次数恰为并发阶段的 200 数，撤回后的 403 未消耗
        self.assertEqual(q["reads_used"], concurrent_reads.count(200))

    def test_existing_db_gets_snapshot_withdrawal_columns(self):
        from sqlalchemy import inspect, text

        from app.db import SessionLocal, engine
        from app.models import Snapshot as SnapshotModel

        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdraw_reason"))
            conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdrawn_at"))
        app_main._init_db_with_retry()
        columns = {c["name"] for c in inspect(engine).get_columns("snapshots")}
        self.assertIn("withdrawn_at", columns)
        self.assertIn("withdraw_reason", columns)
        # 既有快照迁移后默认未撤回，继续可读
        r = self.client.get(
            f"/public/snapshots/{sid}?token={pub['token']}"
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 补列后撤回功能正常
        r = self._withdraw(sid)
        self.assertEqual(r.json()["status"], "withdrawn")
        with SessionLocal() as session:
            row = session.get(SnapshotModel, sid)
            self.assertIsNotNone(row.withdrawn_at)
            self.assertEqual(row.withdraw_reason, "误发，撤回")


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ReadBundleIntegrationTests(unittest.TestCase):
    """多快照共享凭证：申请-复核、冻结清单、共用额度、撤回单项、维护侧管理。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例准备两条已发布记录/快照（直接发布即可，快照不要求来自受控发布）
        self.snapshot_ids = []
        self.redacted_texts = []
        for idx, (text_, intervals) in enumerate(
            (
                (TEXT_V1, INTERVALS_V1),
                (TEXT_V2, [{"start": 9, "end": 15}]),
            )
        ):
            r = self.client.post(
                "/records",
                headers=SERVICE_HEADERS,
                json={"text": text_, "intervals": intervals},
            )
            self.assertEqual(r.status_code, 201, r.text)
            record_id = r.json()["record_id"]
            r = self.client.post(
                f"/records/{record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": 100},
            )
            self.assertEqual(r.status_code, 201, r.text)
            self.snapshot_ids.append(r.json()["snapshot_id"])
            r = self.client.get(
                f"/public/snapshots/{self.snapshot_ids[-1]}?token={r.json()['token']}"
            )
            self.redacted_texts.append(r.json()["redacted_text"])

    # ---- 工具方法 ----

    def _submit(self, snapshot_ids=None, max_reads=10, auth=BASIC_ALICE, expected=201, extra=None):
        body = {
            "snapshot_ids": snapshot_ids if snapshot_ids is not None else self.snapshot_ids,
            "max_reads": max_reads,
        }
        if extra:
            body.update(extra)
        r = self.client.post("/bundle-requests", auth=auth, json=body)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _approve(self, request_id, auth=BASIC_BOB, expected=200):
        r = self.client.post(f"/bundle-requests/{request_id}/approve", auth=auth)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _approved_bundle(self, max_reads=10, extra=None):
        submit = self._submit(max_reads=max_reads, extra=extra).json()
        approve = self._approve(submit["request_id"]).json()
        self.assertEqual(approve["status"], "approved")
        self.assertEqual(approve["snapshot_ids"], self.snapshot_ids)
        return approve

    def _read(self, snapshot_id, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 提交校验与鉴权 ----

    def test_submit_requires_operator_role_and_valid_credentials(self):
        r = self.client.post(
            "/bundle-requests",
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # bob 只有 reviewer 角色，不能提交
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_BOB,
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 403)
        # 错误口令 401
        r = self.client.post(
            "/bundle-requests",
            auth=("alice", "wrong"),
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 401)

    def test_submit_requires_at_least_two_distinct_ids_and_positive_cap(self):
        # 少于两个
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0]], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 422)
        # 重复 ID
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0], self.snapshot_ids[0]], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 422)
        # 额度非正整数
        for bad in (0, -1):
            r = self.client.post(
                "/bundle-requests",
                auth=BASIC_ALICE,
                json={"snapshot_ids": self.snapshot_ids, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422)

    def test_submit_unknown_snapshot_404_and_withdrawn_409(self):
        import uuid

        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0], str(uuid.uuid4())], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 404, r.text)
        # 撤回其中一个快照后提交 -> 409
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "不应公开"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 409, r.text)

    def test_submit_freezes_list_and_cap(self):
        r = self._submit(max_reads=4, extra={"expires_in_seconds": 3600})
        body = r.json()
        self.assertEqual(body["snapshot_ids"], self.snapshot_ids)
        self.assertEqual(body["max_reads"], 4)
        self.assertEqual(body["status"], "pending")
        detail = self.client.get(
            f"/bundle-requests/{body['request_id']}", auth=BASIC_ALICE
        ).json()
        self.assertEqual(detail["snapshot_ids"], self.snapshot_ids)
        self.assertEqual(detail["max_reads"], 4)
        self.assertEqual(detail["credential_expires_in_seconds"], 3600)
        self.assertIsNone(detail["bundle_id"])
        # 详情不含原文
        self.assertNotIn(TEXT_V1, str(detail))

    def test_submit_expiry_policy_validation(self):
        from datetime import datetime, timezone

        past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        r = self._submit(extra={"expires_at": past}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={
                "snapshot_ids": self.snapshot_ids,
                "max_reads": 3,
                "expires_in_seconds": 60,
                "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
            },
        )
        self.assertEqual(r.status_code, 422)

    # ---- 复核规则 ----

    def test_reviewer_role_required_and_requester_cannot_self_review(self):
        submit = self._submit().json()
        rid = submit["request_id"]
        # alice 只有 operator 角色 -> 403
        r = self.client.post(f"/bundle-requests/{rid}/approve", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 403)
        # carol 虽是 reviewer 但是申请人本人 -> 403（申请人不得自审）
        submit2 = self._submit(auth=BASIC_CAROL).json()
        r = self.client.post(f"/bundle-requests/{submit2['request_id']}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        self.assertIn("may not review their own", r.json()["detail"])
        # bob（非申请人）批准成功
        self.assertEqual(self._approve(rid).status_code, 200)

    def test_unknown_request_404(self):
        import uuid

        r = self.client.post(f"/bundle-requests/{uuid.uuid4()}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            f"/bundle-requests/{uuid.uuid4()}/reject",
            auth=BASIC_BOB,
            json={},
        )
        self.assertEqual(r.status_code, 404)

    def test_reject_is_terminal_and_approves_once(self):
        # 驳回
        submit = self._submit().json()
        rid = submit["request_id"]
        r = self.client.post(
            f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={"reason": "材料不足"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(r.json()["reviewed_by"], "bob")
        self.assertEqual(r.json()["reason"], "材料不足")
        # 驳回后不能批准/再驳回
        self.assertEqual(self._approve(rid, expected=409).status_code, 409)
        r = self.client.post(f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 409)
        # 批准终态同样唯一
        submit2 = self._submit().json()
        rid2 = submit2["request_id"]
        self.assertEqual(self._approve(rid2).status_code, 200)
        r = self.client.post(f"/bundle-requests/{rid2}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 409)

    # ---- 批准时重新核对冻结清单 ----

    def test_approval_fails_if_any_snapshot_withdrawn_after_submission(self):
        submit = self._submit(max_reads=5).json()
        rid = submit["request_id"]
        # 等待复核期间撤回其中一个快照
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "等待期间撤回"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 整次批准失败，不签发凭证
        r = self.client.post(f"/bundle-requests/{rid}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409, r.text)
        detail = self.client.get(f"/bundle-requests/{rid}", auth=BASIC_ALICE).json()
        self.assertEqual(detail["status"], "pending")
        self.assertIsNone(detail["bundle_id"])
        # 申请仍可驳回
        r = self.client.post(f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 200, r.text)

    def test_approval_creates_credential_bound_to_list_in_one_transaction(self):
        approve = self._approved_bundle(max_reads=6)
        bundle_id, credential = approve["bundle_id"], approve["credential"]
        # 维护侧查询：清单、额度、状态
        status_body = self.client.get(
            f"/read-bundles/{bundle_id}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["max_reads"], 6)
        self.assertEqual(status_body["reads_used"], 0)
        self.assertEqual(status_body["remaining_reads"], 6)
        self.assertTrue(status_body["active"])
        items = status_body["items"]
        self.assertEqual([i["snapshot_id"] for i in items], self.snapshot_ids)
        self.assertEqual([i["position"] for i in items], [0, 1])
        self.assertTrue(all(i["status"] == "published" for i in items))
        self.assertNotIn(TEXT_V1, str(status_body))
        # 未知凭证 404，缺服务密钥 401
        import uuid

        self.assertEqual(
            self.client.get(f"/read-bundles/{uuid.uuid4()}", headers=SERVICE_HEADERS).status_code,
            404,
        )
        self.assertEqual(self.client.get(f"/read-bundles/{bundle_id}").status_code, 401)

    # ---- 公开读取：共用额度 ----

    def test_credential_reads_only_listed_snapshots_and_shares_cap(self):
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 清单内两项都可读，返回脱敏正文
        self.assertEqual(
            self._read(self.snapshot_ids[0], cred).json()["redacted_text"],
            self.redacted_texts[0],
        )
        self.assertEqual(
            self._read(self.snapshot_ids[1], cred).json()["redacted_text"],
            self.redacted_texts[1],
        )
        # 第 3 次成功，之后跨任一清单快照全部 403（共用额度）
        self.assertEqual(self._read(self.snapshot_ids[0], cred).status_code, 200)
        for sid in self.snapshot_ids:
            r = self._read(sid, cred, expected=403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 3)
        self.assertEqual(status_body["remaining_reads"], 0)
        self.assertFalse(status_body["active"])

    def test_credential_cannot_read_snapshot_outside_list(self):
        # 第三个快照：发布到一条新记录
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": "第三条记录，秘密XYZ。", "intervals": [{"start": 6, "end": 12}]},
        )
        rid = r.json()["record_id"]
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        other_sid = r.json()["snapshot_id"]
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 不在清单内 -> 模糊 403，不扣额度
        self._read(other_sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        self.assertTrue(status_body["active"])
        # 未知快照 + 凭证也统一 403
        import uuid

        self._read(uuid.uuid4(), cred, expected=403)

    def test_withdrawn_listed_item_rejected_without_consuming_others_remain_readable(self):
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 撤回清单中的第二项
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "该项撤回"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 该项一律 403，且不扣额度
        self._read(self.snapshot_ids[1], cred, expected=403)
        self._read(self.snapshot_ids[1], cred, expected=403)
        # 第一项仍可读；额度仍为 3，读一次后剩 2
        self._read(self.snapshot_ids[0], cred, expected=200)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertEqual(status_body["remaining_reads"], 2)
        self.assertEqual(status_body["items"][1]["status"], "withdrawn")
        self.assertIsNotNone(status_body["items"][1]["withdrawn_at"])
        # 模糊 403 不透露撤回原因
        r = self._read(self.snapshot_ids[1], cred, expected=403)
        self.assertNotIn("该项撤回", r.text)

    def test_invalid_or_expired_or_revoked_credential_does_not_consume(self):
        # 无效凭证
        self._read(self.snapshot_ids[0], "not-a-real-credential", expected=403)
        approve = self._approved_bundle(max_reads=2)
        bid, cred = approve["bundle_id"], approve["credential"]
        # 撤销：撤销后全部拒绝
        r = self.client.post(f"/read-bundles/{bid}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["revoked"])
        self.assertFalse(r.json()["already_revoked"])
        for sid in self.snapshot_ids:
            self._read(sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        self.assertFalse(status_body["active"])
        self.assertIsNotNone(status_body["revoked_at"])
        # 重复撤销幂等
        r = self.client.post(f"/read-bundles/{bid}/revoke", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["already_revoked"])
        # 撤销未知凭证 404
        import uuid

        self.assertEqual(
            self.client.post(
                f"/read-bundles/{uuid.uuid4()}/revoke", headers=SERVICE_HEADERS
            ).status_code,
            404,
        )

    def test_expired_credential_rejects_all_without_consuming(self):
        approve = self._approved_bundle(max_reads=3, extra={"expires_in_seconds": 1})
        cred = approve["credential"]
        self._read(self.snapshot_ids[0], cred, expected=200)
        import time as _time

        _time.sleep(1.2)
        for sid in self.snapshot_ids:
            self._read(sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertFalse(status_body["active"])

    def test_concurrent_reads_never_exceed_shared_cap(self):
        approve = self._approved_bundle(max_reads=10)
        cred = approve["credential"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(24)

        def read_once(i):
            barrier.wait()
            client = TestClient(app_main.app)
            # 交替读取两个清单快照，验证共用额度
            sid = self.snapshot_ids[i % 2]
            code = client.get(f"/public/snapshots/{sid}?token={cred}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 14)
        self.assertTrue(all(code == 403 for code in failures))
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 10)
        self.assertEqual(status_body["remaining_reads"], 0)

    # ---- 维护侧与兼容性 ----

    def test_maintenance_lists_and_legacy_token_unchanged(self):
        approve = self._approved_bundle(max_reads=2)
        cred = approve["credential"]
        # 列表接口
        r = self.client.get("/read-bundles", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(b["bundle_id"] == approve["bundle_id"] for b in r.json()["bundles"]))
        # 申请列表（操作者可查）
        r = self.client.get("/bundle-requests", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(
            any(q["request_id"] == approve["request_id"] for q in r.json()["requests"])
        )
        # 共享凭证不在既有单快照令牌命名空间：/tokens/<credential> 仍 404（接口行为兼容）
        self.assertEqual(
            self.client.get(f"/tokens/{cred}", headers=SERVICE_HEADERS).status_code, 404
        )
        # 既有单快照令牌行为不变：为快照 0 再签一枚既有令牌（同版本复用快照），
        # 其计数与共享凭证互不影响
        record_id = self._record_of_snapshot(self.snapshot_ids[0])
        pub = self.client.post(
            f"/records/{record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1},
        )
        self.assertEqual(pub.status_code, 201, pub.text)
        legacy_token = pub.json()["token"]
        self.assertEqual(
            self._read(self.snapshot_ids[0], legacy_token).status_code, 200
        )
        self.assertEqual(
            self._read(self.snapshot_ids[0], legacy_token, expected=403).status_code, 403
        )
        # 共享凭证额度未被原令牌消耗
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        # 公开读取统一响应结构未变
        body = self._read(self.snapshot_ids[0], cred).json()
        self.assertEqual(set(body.keys()), {"record_id", "version", "status", "redacted_text"})

    def _record_of_snapshot(self, snapshot_id):
        # 通过共享凭证读取响应无法拿 record（避免耦合），改用维护侧记录检索：
        # 直接从快照行查询其 record_id
        from app.db import SessionLocal
        from app.models import Snapshot as SnapshotModel

        with SessionLocal() as session:
            return session.get(SnapshotModel, snapshot_id).record_id


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class CredentialRotationIntegrationTests(unittest.TestCase):
    """读取凭证泄露换发：单快照令牌与多快照共享凭证统一入口。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ---- 工具方法 ----

    def _create_record_publish(self, max_reads=None, extra=None):
        body = {"text": TEXT_V1, "intervals": INTERVALS_V1}
        r = self.client.post("/records", headers=SERVICE_HEADERS, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        rid = r.json()["record_id"]
        pub_body = {"version": 1}
        if max_reads is not None:
            pub_body["max_reads"] = max_reads
        if extra:
            pub_body.update(extra)
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json=pub_body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return rid, r.json()

    def _read(self, sid, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{sid}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _rotate(self, credential, request_id, expected=200):
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": credential, "request_id": request_id},
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 鉴权与校验 ----

    def test_rotate_requires_service_key(self):
        rid, pub = self._create_record_publish()
        r = self.client.post(
            "/credentials/rotate",
            json={"credential": pub["token"], "request_id": "req-1"},
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            "/credentials/rotate",
            headers={"X-Service-Key": "wrong"},
            json={"credential": pub["token"], "request_id": "req-1"},
        )
        self.assertEqual(r.status_code, 401)

    def test_rotate_unknown_credential_404(self):
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": "definitely-not-a-credential", "request_id": "r1"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "credential not found")

    def test_rotate_blank_fields_422(self):
        rid, pub = self._create_record_publish()
        for body in (
            {"credential": "", "request_id": "r1"},
            {"credential": "  ", "request_id": "r1"},
            {"credential": pub["token"], "request_id": ""},
            {"credential": pub["token"], "request_id": "   "},
        ):
            r = self.client.post(
                "/credentials/rotate", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, r.text)

    # ---- 单快照令牌换发 ----

    def test_token_rotation_issues_new_and_invalidates_old(self):
        rid, pub = self._create_record_publish()
        sid, old = pub["snapshot_id"], pub["token"]
        result = self._rotate(old, "req-token-1").json()
        self.assertEqual(result["kind"], "token")
        self.assertEqual(result["request_id"], "req-token-1")
        self.assertEqual(result["old_credential"], old)
        self.assertNotEqual(result["new_credential"], old)
        self.assertEqual(result["snapshot_ids"], [sid])
        self.assertIsNone(result["expires_at"])
        self.assertIsNone(result["max_reads"])
        self.assertEqual(result["reads_used"], 0)
        self.assertIsNone(result["remaining_reads"])
        self.assertFalse(result["replayed"])
        self.assertIsNone(result["items"])
        self.assertIsNotNone(result["rotated_at"])
        new = result["new_credential"]
        # 旧令牌换发提交后不能再读取（模糊 403，不泄露原因）
        self._read(sid, old, expected=403)
        r = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS)
        self.assertFalse(r.json()["active"])
        self.assertIsNotNone(r.json()["rotated_at"])
        # 新令牌在同一快照继续可读
        self._read(sid, new, expected=200)
        r = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["active"])
        self.assertIsNone(r.json()["rotated_at"])

    def test_token_rotation_inherits_expiry_cap_and_used_count(self):
        rid, pub = self._create_record_publish(
            max_reads=5, extra={"expires_in_seconds": 3600}
        )
        sid, old = pub["snapshot_id"], pub["token"]
        self._read(sid, old)
        self._read(sid, old)
        old_status = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS).json()
        result = self._rotate(old, "req-inherit").json()
        # 原绝对到期时刻被继承（不延长）
        self.assertEqual(result["expires_at"], old_status["expires_at"])
        self.assertEqual(result["max_reads"], 5)
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 3)
        new = result["new_credential"]
        # 新令牌沿用继承的计数：再读 3 次后耗尽
        for _ in range(3):
            self.assertEqual(self._read(sid, new).status_code, 200)
        for _ in range(2):
            self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        status_body = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status_body["reads_used"], 5)
        self.assertEqual(status_body["remaining_reads"], 0)
        # 旧令牌继续不可读，计数停在换发时的 2（旧令牌失败读取不计数）
        old_status = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS).json()
        self.assertEqual(old_status["reads_used"], 2)

    def test_rotation_idempotent_for_same_request_id(self):
        rid, pub = self._create_record_publish(max_reads=4)
        sid, old = pub["snapshot_id"], pub["token"]
        first = self._rotate(old, "idem-req").json()
        # 换发后再用旧凭证成功读取是不可能的；直接重试（相同原凭证 + 请求标识）
        second = self._rotate(old, "idem-req").json()
        third = self._rotate(old, "idem-req").json()
        for body in (second, third):
            self.assertTrue(body["replayed"])
            self.assertEqual(body["new_credential"], first["new_credential"])
            self.assertEqual(body["rotated_at"], first["rotated_at"])
            self.assertEqual(body["reads_used"], first["reads_used"])
        self.assertFalse(first["replayed"])
        # 只产生一枚新凭证
        self._read(sid, first["new_credential"], expected=200)
        self._read(sid, second["new_credential"], expected=200)

    def test_different_request_id_after_rotation_conflicts(self):
        rid, pub = self._create_record_publish()
        old = pub["token"]
        self._rotate(old, "req-A")
        # 不同请求标识再次换发同一原凭证 -> 409，不签出新凭证
        r = self._rotate(old, "req-B", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "already_rotated")

    def test_concurrent_rotations_with_distinct_request_ids_succeed_once(self):
        results: list = []
        for attempt in range(5):
            rid, pub = self._create_record_publish(max_reads=20)
            old = pub["token"]
            barrier = threading.Barrier(8)

            def rotate(req_suffix):
                barrier.wait()
                client = TestClient(app_main.app)
                r = client.post(
                    "/credentials/rotate",
                    headers=SERVICE_HEADERS,
                    json={"credential": old, "request_id": f"conc-{req_suffix}"},
                )
                results.append(r.status_code)

            threads = [
                threading.Thread(target=rotate, args=(f"req-{i}",)) for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            batch = results[-8:]
            self.assertEqual(batch.count(200), 1, batch)
            self.assertEqual(batch.count(409), 7, batch)

    def test_revoked_expired_and_exhausted_credentials_cannot_rotate(self):
        # 已撤销
        rid, pub = self._create_record_publish()
        r = self.client.post(
            f"/tokens/{pub['token']}/revoke", headers=SERVICE_HEADERS
        )
        self.assertEqual(r.status_code, 200)
        r = self._rotate(pub["token"], "r-revoked", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "revoked")
        # 额度耗尽
        rid, pub = self._create_record_publish(max_reads=1)
        sid, tok = pub["snapshot_id"], pub["token"]
        self._read(sid, tok)
        r = self._rotate(tok, "r-exhausted", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "quota_exhausted")
        # 已到期
        rid, pub = self._create_record_publish(
            max_reads=5, extra={"expires_in_seconds": 1}
        )
        import time as _time

        _time.sleep(1.2)
        r = self._rotate(pub["token"], "r-expired", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "expired")

    def test_rotation_settles_with_concurrent_reads_without_overspend(self):
        rid, pub = self._create_record_publish(max_reads=10)
        sid, old = pub["snapshot_id"], pub["token"]
        read_codes: list[int] = []
        barrier = threading.Barrier(9)

        def rotate():
            barrier.wait()
            client = TestClient(app_main.app)
            client.post(
                "/credentials/rotate",
                headers=SERVICE_HEADERS,
                json={"credential": old, "request_id": "req-race"},
            )

        def read_old():
            barrier.wait()
            client = TestClient(app_main.app)
            read_codes.append(
                client.get(f"/public/snapshots/{sid}?token={old}").status_code
            )

        threads = [threading.Thread(target=rotate)]
        threads += [threading.Thread(target=read_old) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 换发与读取按事务先后结算：部分读取在换发前成功（200），其余 403
        self.assertTrue(all(c in (200, 403) for c in read_codes), read_codes)
        self.assertIn(403, read_codes)
        result = self._rotate(old, "req-race").json()  # 幂等回放
        new = result["new_credential"]
        # 新凭证继承已用次数：并发阶段对旧凭证的成功次数
        inherited = read_codes.count(200)
        self.assertEqual(result["reads_used"], inherited)
        self.assertEqual(result["remaining_reads"], 10 - inherited)
        # 新凭证只能再成功 (10 - inherited) 次，成功总数绝不超过上限
        for _ in range(10 - inherited):
            self.assertEqual(self._read(sid, new).status_code, 200)
        self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        status_body = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status_body["reads_used"], 10)

    def test_rotated_token_can_be_rotated_again(self):
        rid, pub = self._create_record_publish(max_reads=7)
        sid, tok1 = pub["snapshot_id"], pub["token"]
        self._read(sid, tok1)
        tok2 = self._rotate(tok1, "chain-1").json()["new_credential"]
        self._read(sid, tok2)
        result = self._rotate(tok2, "chain-2").json()
        tok3 = result["new_credential"]
        self.assertNotIn(tok3, (tok1, tok2))
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 5)
        # 前两代都不可读，最新凭证可读
        self._read(sid, tok1, expected=403)
        self._read(sid, tok2, expected=403)
        self._read(sid, tok3, expected=200)

    def test_token_on_withdrawn_snapshot_can_still_rotate_but_unreadable(self):
        rid, pub = self._create_record_publish(max_reads=5)
        sid, old = pub["snapshot_id"], pub["token"]
        self._read(sid, old)
        r = self.client.post(
            f"/snapshots/{sid}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "误发撤回"},
        )
        self.assertEqual(r.status_code, 200)
        # 快照撤回不作为换发障碍
        result = self._rotate(old, "req-withdrawn").json()
        self.assertEqual(result["snapshot_ids"], [sid])
        self.assertEqual(result["remaining_reads"], 4)
        # 但新旧令牌对该快照都读不到正文（模糊 403，不计数）
        self._read(sid, old, expected=403)
        self._read(sid, result["new_credential"], expected=403)
        status_body = self.client.get(
            f"/tokens/{result['new_credential']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)

    # ---- 多快照共享凭证换发 ----

    def _approved_bundle(self, snapshot_ids, max_reads=5, extra=None):
        body = {"snapshot_ids": snapshot_ids, "max_reads": max_reads}
        if extra:
            body.update(extra)
        r = self.client.post("/bundle-requests", auth=BASIC_ALICE, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/bundle-requests/{request_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _two_snapshots(self):
        sids = []
        for text_, intervals in (
            (TEXT_V1, INTERVALS_V1),
            (TEXT_V2, [{"start": 9, "end": 15}]),
        ):
            r = self.client.post(
                "/records",
                headers=SERVICE_HEADERS,
                json={"text": text_, "intervals": intervals},
            )
            rid = r.json()["record_id"]
            r = self.client.post(
                f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
            )
            sids.append(r.json()["snapshot_id"])
        return sids

    def test_bundle_rotation_copies_list_and_inherits_quota(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=5)
        old = approved["credential"]
        self._read(sids[0], old)
        self._read(sids[1], old)
        result = self._rotate(old, "bundle-req-1").json()
        self.assertEqual(result["kind"], "bundle")
        self.assertEqual(result["snapshot_ids"], sids)
        self.assertEqual(result["max_reads"], 5)
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 3)
        self.assertIsNone(result["expires_at"])
        self.assertIsNotNone(result["items"])
        self.assertEqual([i["snapshot_id"] for i in result["items"]], sids)
        self.assertEqual([i["position"] for i in result["items"]], [0, 1])
        self.assertTrue(all(i["status"] == "published" for i in result["items"]))
        new = result["new_credential"]
        # 旧凭证失效，新凭证继承清单与剩余额度
        self._read(sids[0], old, expected=403)
        for _ in range(3):
            self.assertEqual(self._read(sids[0], new).status_code, 200)
        for sid in sids:
            self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        # 维护侧：旧凭证 active=false 且带 rotated_at；新凭证计数继承
        bid = approved["bundle_id"]
        old_status = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertFalse(old_status["active"])
        self.assertIsNotNone(old_status["rotated_at"])
        new_status = self.client.get("/read-bundles", headers=SERVICE_HEADERS).json()
        new_row = next(b for b in new_status["bundles"] if b["credential"] == new)
        self.assertEqual(new_row["reads_used"], 5)
        self.assertFalse(new_row["active"])  # 继承额度已用尽
        self.assertEqual(new_row["remaining_reads"], 0)

    def test_bundle_rotation_with_withdrawn_item_keeps_item_unreadable(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=5)
        old = approved["credential"]
        # 撤回清单第二项后换发：仍可换发，该项继续不可读且不扣额度
        r = self.client.post(
            f"/snapshots/{sids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "该项撤回"},
        )
        self.assertEqual(r.status_code, 200)
        result = self._rotate(old, "bundle-withdrawn-item").json()
        items = result["items"]
        self.assertEqual(items[0]["status"], "published")
        self.assertEqual(items[1]["status"], "withdrawn")
        self.assertIsNotNone(items[1]["withdrawn_at"])
        new = result["new_credential"]
        self._read(sids[1], new, expected=403)
        self._read(sids[1], new, expected=403)
        self._read(sids[0], new, expected=200)
        # 撤回项不扣额度：仅消耗 1 次
        bid = next(
            b["bundle_id"]
            for b in self.client.get("/read-bundles", headers=SERVICE_HEADERS).json()[
                "bundles"
            ]
            if b["credential"] == new
        )
        status_body = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertEqual(status_body["remaining_reads"], 4)

    def test_bundle_rotation_idempotent_and_conflict(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=3)
        old = approved["credential"]
        first = self._rotate(old, "b-idem").json()
        second = self._rotate(old, "b-idem").json()
        self.assertTrue(second["replayed"])
        self.assertEqual(second["new_credential"], first["new_credential"])
        # 不同请求标识 -> 409
        r = self._rotate(old, "b-other", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "already_rotated")

    def test_existing_db_gets_rotation_columns(self):
        from sqlalchemy import inspect, text

        from app.db import engine

        rid, pub = self._create_record_publish()
        token = pub["token"]
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=3)
        credential = approved["credential"]

        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN rotated_at"))
            conn.execute(text("DROP TABLE credential_rotations"))
            conn.execute(text("ALTER TABLE read_bundles DROP COLUMN rotated_at"))
        app_main._init_db_with_retry()
        token_columns = {c["name"] for c in inspect(engine).get_columns("read_tokens")}
        bundle_columns = {c["name"] for c in inspect(engine).get_columns("read_bundles")}
        self.assertIn("rotated_at", token_columns)
        self.assertIn("rotated_at", bundle_columns)
        self.assertIn(
            "credential_rotations", inspect(engine).get_table_names()
        )
        # 既有凭证补列后继续可读，且仍可正常换发
        self.assertEqual(
            self._read(pub["snapshot_id"], token).status_code, 200
        )
        result = self._rotate(credential, "migration-bundle").json()
        self.assertEqual(result["snapshot_ids"], sids)
        self.assertEqual(result["remaining_reads"], 3)

    def test_bundle_rotation_concurrent_once_and_inherits_expiry(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=8, extra={"expires_in_seconds": 3600})
        old = approved["credential"]
        old_status = self.client.get(
            f"/read-bundles/{approved['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        responses = []
        barrier = threading.Barrier(6)

        def rotate(suffix):
            barrier.wait()
            client = TestClient(app_main.app)
            responses.append(
                client.post(
                    "/credentials/rotate",
                    headers=SERVICE_HEADERS,
                    json={"credential": old, "request_id": f"bc-{suffix}"},
                )
            )

        threads = [threading.Thread(target=rotate, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = [r.status_code for r in responses]
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(statuses.count(409), 5, statuses)
        winner = next(r for r in responses if r.status_code == 200).json()
        # 相同（原凭证, 赢家请求标识）重试：幂等回放，不再次换发
        replay = self._rotate(old, winner["request_id"]).json()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["new_credential"], winner["new_credential"])
        self.assertEqual(replay["expires_at"], old_status["expires_at"])


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ReadEventAuditIntegrationTests(unittest.TestCase):
    """成功读取审计：同事务只追加事件、换发链归一、维护侧键集分页。"""

    WINDOW = ("2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ---- 工具方法 ----

    def _create_record_publish(self, max_reads=None):
        body = {"text": TEXT_V1, "intervals": INTERVALS_V1}
        r = self.client.post("/records", headers=SERVICE_HEADERS, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        rid = r.json()["record_id"]
        pub_body = {"version": 1}
        if max_reads is not None:
            pub_body["max_reads"] = max_reads
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json=pub_body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return rid, r.json()

    def _read(self, sid, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{sid}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _rotate(self, credential, request_id, expected=200):
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": credential, "request_id": request_id},
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _events(self, sid, start=None, end=None, params=None, expected=200):
        query = {
            "start": start or self.WINDOW[0],
            "end": end or self.WINDOW[1],
        }
        if params:
            query.update(params)
        r = self.client.get(
            f"/snapshots/{sid}/read-events", headers=SERVICE_HEADERS, params=query
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _all_events(self, sid, limit=10, start=None, end=None):
        """翻完所有页，返回事件列表，并校验游标遍历无重复。"""
        cursor = None
        seen: set = set()
        collected = []
        while True:
            params = {"limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            body = self._events(sid, start=start, end=end, params=params).json()
            for event in body["events"]:
                self.assertNotIn(event["event_id"], seen)
                seen.add(event["event_id"])
            collected.extend(body["events"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        return collected

    def _two_snapshots(self):
        sids = []
        for text_, intervals in (
            (TEXT_V1, INTERVALS_V1),
            (TEXT_V2, [{"start": 9, "end": 15}]),
        ):
            r = self.client.post(
                "/records",
                headers=SERVICE_HEADERS,
                json={"text": text_, "intervals": intervals},
            )
            rid = r.json()["record_id"]
            r = self.client.post(
                f"/records/{rid}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": 100},
            )
            sids.append((r.json()["snapshot_id"], r.json()["token"]))
        return sids

    def _approved_bundle(self, sids, max_reads=5):
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": sids, "max_reads": max_reads},
        )
        self.assertEqual(r.status_code, 201, r.text)
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/bundle-requests/{request_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["credential"]

    # ---- 事件写入规则 ----

    def test_successful_reads_append_events_failures_do_not(self):
        rid, pub = self._create_record_publish(max_reads=2)
        sid, token = pub["snapshot_id"], pub["token"]
        # 两次成功：各一条事件
        self._read(sid, token)
        self._read(sid, token)
        events = self._all_events(sid)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["snapshot_id"] == sid for e in events))
        self.assertTrue(all(e["credential_kind"] == "token" for e in events))
        chain_ids = {e["chain_id"] for e in events}
        self.assertEqual(len(chain_ids), 1)
        for event in events:
            self.assertEqual(set(event), {"event_id", "snapshot_id", "occurred_at",
                                          "credential_kind", "chain_id"})
            self.assertNotIn(token, json.dumps(event))
        # 额度耗尽后的失败读取不产生事件
        self._read(sid, token, expected=403)
        # 错误令牌、错误快照路径均不产生事件
        self._read(sid, "no-such-token", expected=403)
        self.assertEqual(len(self._all_events(sid)), 2)

    def test_bundle_successes_append_bundle_events_and_out_of_list_read_none(self):
        pairs = self._two_snapshots()
        sids = [sid for sid, _ in pairs]
        credential = self._approved_bundle(sids, max_reads=5)
        self._read(sids[0], credential)
        self._read(sids[1], credential)
        # 不在清单内的快照：模糊 403，无事件
        extra = self._two_snapshots()
        self._read(extra[0][0], credential, expected=403)
        for sid in sids:
            events = self._all_events(sid)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["credential_kind"], "bundle")
            self.assertNotIn(credential, json.dumps(events[0]))
        # 两枚快照的共享凭证事件归于同一换发链
        self.assertEqual(
            self._all_events(sids[0])[0]["chain_id"],
            self._all_events(sids[1])[0]["chain_id"],
        )

    def test_withdrawn_snapshot_reads_produce_no_events(self):
        rid, pub = self._create_record_publish(max_reads=5)
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        r = self.client.post(
            f"/snapshots/{sid}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "误发撤回"},
        )
        self.assertEqual(r.status_code, 200)
        self._read(sid, token, expected=403)
        self.assertEqual(len(self._all_events(sid)), 1)

    def test_withdrawn_snapshot_history_still_queryable_but_unknown_snapshot_404(self):
        rid, pub = self._create_record_publish()
        sid = pub["snapshot_id"]
        self._read(sid, pub["token"])
        self.client.post(
            f"/snapshots/{sid}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "误发撤回"},
        )
        # 已撤回快照的历史成功事件仍可审计
        self.assertEqual(len(self._all_events(sid)), 1)
        # 未知快照明确 404
        missing = uuid.uuid4()
        r = self._events(missing, expected=404)
        self.assertEqual(r.json()["detail"], "snapshot not found")

    # ---- 换发链 ----

    def test_chain_id_is_stable_across_rotations_and_events_group_under_it(self):
        rid, pub = self._create_record_publish(max_reads=10)
        sid, tok1 = pub["snapshot_id"], pub["token"]
        self._read(sid, tok1)
        tok2 = self._rotate(tok1, "chain-audit-1").json()["new_credential"]
        self._read(sid, tok2)
        tok3 = self._rotate(tok2, "chain-audit-2").json()["new_credential"]
        self._read(sid, tok3)
        # 旧凭证换发后不能读取（也不产生事件）
        self._read(sid, tok1, expected=403)
        self._read(sid, tok2, expected=403)
        events = self._all_events(sid)
        self.assertEqual(len(events), 3)
        chain_ids = {e["chain_id"] for e in events}
        self.assertEqual(len(chain_ids), 1)
        self.assertTrue(all(e["credential_kind"] == "token" for e in events))
        # 事件载荷不含任何一凭证明文
        raw = json.dumps(events)
        self.assertNotIn(tok1, raw)
        self.assertNotIn(tok2, raw)
        self.assertNotIn(tok3, raw)

    def test_distinct_credentials_have_distinct_chains(self):
        rid1, pub1 = self._create_record_publish()
        rid2, pub2 = self._create_record_publish()
        self.assertNotEqual(pub1["snapshot_id"], pub2["snapshot_id"])
        self._read(pub1["snapshot_id"], pub1["token"])
        self._read(pub2["snapshot_id"], pub2["token"])
        c1 = self._all_events(pub1["snapshot_id"])[0]["chain_id"]
        c2 = self._all_events(pub2["snapshot_id"])[0]["chain_id"]
        self.assertNotEqual(c1, c2)

    # ---- 查询鉴权与参数校验 ----

    def test_query_requires_service_key(self):
        rid, pub = self._create_record_publish()
        sid = pub["snapshot_id"]
        url = f"/snapshots/{sid}/read-events?start={self.WINDOW[0]}&end={self.WINDOW[1]}"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 401)
        r = self.client.get(url, headers={"X-Service-Key": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_query_rejects_bad_time_range(self):
        rid, pub = self._create_record_publish()
        sid = pub["snapshot_id"]
        # 缺参数
        r = self.client.get(
            f"/snapshots/{sid}/read-events?start={self.WINDOW[0]}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 无时区的朴素时间戳
        r = self._events(sid, start="2026-10-01T00:00:00", end=self.WINDOW[1], expected=422)
        # 无法解析
        r = self._events(sid, start="not-a-time", end=self.WINDOW[1], expected=422)
        # start > end
        r = self._events(
            sid, start=self.WINDOW[1], end=self.WINDOW[0], expected=422
        )
        # limit 越界
        r = self._events(sid, params={"limit": 0}, expected=422)
        r = self._events(sid, params={"limit": 501}, expected=422)

    def test_query_rejects_invalid_or_mismatched_cursor(self):
        rid, pub = self._create_record_publish()
        sid = pub["snapshot_id"]
        self._read(sid, pub["token"])
        self._read(sid, pub["token"])
        body = self._events(sid, params={"limit": 1}).json()
        cursor = body["next_cursor"]
        self.assertIsNotNone(cursor)
        # 垃圾游标
        self._events(sid, params={"cursor": "!!!not-base64!!!"}, expected=422)
        bogus = base64.urlsafe_b64encode(b'{"x":1}').decode().rstrip("=")
        self._events(sid, params={"cursor": bogus}, expected=422)
        # 游标与本次时间范围不一致
        self._events(
            sid,
            start="2001-01-01T00:00:00Z",
            end=self.WINDOW[1],
            params={"cursor": cursor},
            expected=422,
        )
        # 游标与快照不一致
        rid2, pub2 = self._create_record_publish()
        self._events(
            pub2["snapshot_id"], params={"cursor": cursor}, expected=422
        )

    # ---- 时间范围半开区间与分页 ----

    def test_time_range_is_half_open_and_pagination_ordered(self):
        rid, pub = self._create_record_publish(max_reads=10)
        sid, token = pub["snapshot_id"], pub["token"]
        for _ in range(3):
            self._read(sid, token)
        all_events = self._all_events(sid, limit=1)
        self.assertEqual(len(all_events), 3)
        occurred = [e["occurred_at"] for e in all_events]
        self.assertEqual(occurred, sorted(occurred))
        # 每页结构与游标
        first = self._events(sid, params={"limit": 2}).json()
        self.assertEqual(len(first["events"]), 2)
        self.assertEqual(first["snapshot_id"], sid)
        self.assertEqual(first["limit"], 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self._events(
            sid, params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        self.assertEqual(len(second["events"]), 1)
        self.assertIsNone(second["next_cursor"])
        # 无重叠
        first_ids = {e["event_id"] for e in first["events"]}
        self.assertNotIn(second["events"][0]["event_id"], first_ids)
        # 半开区间：[start, end)，以中间事件时间为 end 排除该事件及之后
        middle_at = all_events[1]["occurred_at"]
        body = self._events(sid, start=self.WINDOW[0], end=middle_at).json()
        self.assertEqual(len(body["events"]), 1)
        self.assertIsNone(body["next_cursor"])
        # start 等于中间事件时间时包含该事件
        body = self._events(sid, start=middle_at, end=self.WINDOW[1]).json()
        self.assertEqual(len(body["events"]), 2)
        # start == end 合法但为空
        body = self._events(
            sid, start=middle_at, end=middle_at
        ).json()
        self.assertEqual(body["events"], [])
        self.assertIsNone(body["next_cursor"])

    def test_pagination_covers_all_committed_events_during_concurrent_writes(self):
        # 不限次令牌：先提交一批基础事件，再在并发写入期间逐页遍历。
        # 键集按 (occurred_at, id) 严格递增且两列不可变，而并发新事件的时间戳
        # 必然晚于基础事件，因此游标遍历不可能重复或跳过任何基础事件；
        # 遍历期间新提交的事件可在下页/下次查询出现（需求允许）。
        rid, pub = self._create_record_publish()
        sid, token = pub["snapshot_id"], pub["token"]
        baseline_n = 25
        for _ in range(baseline_n):
            self._read(sid, token)
        baseline_ids = {e["event_id"] for e in self._all_events(sid, limit=100)}
        self.assertEqual(len(baseline_ids), baseline_n)

        barrier = threading.Barrier(6)

        def writer():
            barrier.wait()
            client = TestClient(app_main.app)
            for _ in range(9):
                client.get(f"/public/snapshots/{sid}?token={token}")

        def paginator():
            barrier.wait()
            client = TestClient(app_main.app)
            cursor = None
            seen = []
            while True:
                params = {"limit": 3}
                if cursor is not None:
                    params["cursor"] = cursor
                body = client.get(
                    f"/snapshots/{sid}/read-events",
                    headers=SERVICE_HEADERS,
                    params={"start": self.WINDOW[0], "end": self.WINDOW[1], **params},
                ).json()
                seen.extend(e["event_id"] for e in body["events"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            paginator_seen.extend(seen)

        paginator_seen: list = []
        threads = [threading.Thread(target=writer) for _ in range(5)]
        threads.append(threading.Thread(target=paginator))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        seen_set = set(paginator_seen)
        # 无重复（严格键集 + 不可变列）
        self.assertEqual(len(paginator_seen), len(seen_set))
        # 查询起点前已提交的基础事件无一遗漏（其键序始终早于并发新事件）
        self.assertTrue(baseline_ids.issubset(seen_set))
        # 并发写入全部完成后：全部事件完整可分页（25 基础 + 45 并发）
        final_events = self._all_events(sid, limit=7)
        self.assertEqual(len(final_events), baseline_n + 45)

    # ---- 不可变性 ----

    def test_read_events_are_immutable_at_database_level(self):
        from sqlalchemy import text

        rid, pub = self._create_record_publish()
        sid = pub["snapshot_id"]
        self._read(sid, pub["token"])
        event_id = self._all_events(sid)[0]["event_id"]
        from app.db import engine

        with engine.begin() as conn:
            with self.assertRaises(Exception):
                conn.execute(
                    text(
                        "UPDATE read_events SET credential_kind = 'bundle' "
                        "WHERE id = :eid"
                    ),
                    {"eid": event_id},
                )
        with engine.begin() as conn:
            with self.assertRaises(Exception):
                conn.execute(
                    text("DELETE FROM read_events WHERE id = :eid"),
                    {"eid": event_id},
                )
        # 事件原样留存
        event = self._all_events(sid)[0]
        self.assertEqual(event["event_id"], event_id)
        self.assertEqual(event["credential_kind"], "token")

    def test_existing_db_gets_chain_id_columns_and_read_events(self):
        from sqlalchemy import inspect, text

        from app.db import engine, SessionLocal
        from app.models import ReadToken as ReadTokenModel

        # 先构造“升级前已完成的换发链”：令牌与共享凭证各换发一次
        rid, pub = self._create_record_publish(max_reads=5)
        sid, token = pub["snapshot_id"], pub["token"]
        rotated_token = self._rotate(token, "legacy-chain-token").json()["new_credential"]
        pairs = self._two_snapshots()
        sids = [s for s, _ in pairs]
        bundle = self._approved_bundle(sids, max_reads=5)
        rotated_bundle = self._rotate(
            bundle, "legacy-chain-bundle"
        ).json()["new_credential"]

        with engine.begin() as conn:
            # 模拟旧库：删除本轮新增列与审计表（凭证与换发记录保留）
            conn.execute(text("ALTER TABLE read_tokens DROP COLUMN chain_id"))
            conn.execute(text("ALTER TABLE read_bundles DROP COLUMN chain_id"))
            conn.execute(text("DROP TABLE read_events"))
        app_main._init_db_with_retry()
        token_columns = {c["name"] for c in inspect(engine).get_columns("read_tokens")}
        bundle_columns = {c["name"] for c in inspect(engine).get_columns("read_bundles")}
        event_columns = {c["name"] for c in inspect(engine).get_columns("read_events")}
        self.assertIn("chain_id", token_columns)
        self.assertIn("chain_id", bundle_columns)
        self.assertEqual(
            {"id", "snapshot_id", "credential_kind", "chain_id", "occurred_at"},
            event_columns,
        )
        with SessionLocal() as session:
            old_row = session.get(ReadTokenModel, token)
            new_row = session.get(ReadTokenModel, rotated_token)
            self.assertIsNotNone(old_row.chain_id)
            # 升级前已换发的新旧凭证回填后也必须同链
            self.assertEqual(old_row.chain_id, new_row.chain_id)
            old_bundle_chain = session.execute(
                text("SELECT chain_id FROM read_bundles WHERE credential = :c"),
                {"c": bundle},
            ).scalar_one()
            new_bundle_chain = session.execute(
                text("SELECT chain_id FROM read_bundles WHERE credential = :c"),
                {"c": rotated_bundle},
            ).scalar_one()
            self.assertEqual(old_bundle_chain, new_bundle_chain)

        # 既有令牌继续可读且开始产生审计事件
        self._read(sid, rotated_token)
        self._read(sid, rotated_token)
        events = self._all_events(sid)
        self.assertEqual(len(events), 2)
        self.assertEqual({e["credential_kind"] for e in events}, {"token"})
        # 继续换发仍继承同一条链
        tok3 = self._rotate(rotated_token, "migration-chain").json()["new_credential"]
        self._read(sid, tok3)
        events = self._all_events(sid)
        self.assertEqual(len({e["chain_id"] for e in events}), 1)
        self.assertEqual(str(old_row.chain_id), events[0]["chain_id"])


if __name__ == "__main__":
    unittest.main()
