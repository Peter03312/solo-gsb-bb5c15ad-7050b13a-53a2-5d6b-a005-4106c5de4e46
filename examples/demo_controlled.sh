#!/usr/bin/env bash
# 受控发布（申请-复核）端到端示例：
#   标记受控 -> 直接发布被拒 -> 个人凭据申请 -> 申请人不得复核 ->
#   冻结版本内容 -> 复核人批准(同事务快照+令牌) -> 公开读取 ->
#   驳回后不能批准 -> 并发复核只有一个终态 -> 撤销令牌
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（复核人）
CAROL_PW="${CAROL_PW:-carol-pw}"   # operator + reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 维护端创建记录（版本 1）"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
echo "${CREATE}" | jq .
RECORD_ID=$(echo "${CREATE}" | jq -r .record_id)

step "2. 非受控记录提交发布申请 -> 409（应使用直接发布）"
curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" -d '{"version": 1}' | jq .

step "3. 机构将记录设为受控（服务密钥，幂等）"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}/controlled" "${AUTH[@]}" | jq .

step "4. 受控记录的既有直接发布请求 -> 409 明确拒绝，且不生成快照"
curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" -d '{"version": 1}' | jq .
curl -sS "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" | jq .

step "5. 无权/无凭据明确拒绝：无凭据 401、仅 reviewer 角色提交 403"
printf '无凭据: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -H 'Content-Type: application/json' -d '{"version":1}')"
printf '错误口令: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u 'alice:wrong' -H 'Content-Type: application/json' -d '{"version":1}')"
printf 'bob(仅 reviewer)提交: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "bob:${BOB_PW}" -H 'Content-Type: application/json' -d '{"version":1}')"

step "6. alice(operator) 提交版本 1 的发布申请（可带 expires_* 与 max_reads；申请时冻结）"
SUBMIT=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" -d '{"version": 1, "max_reads": 2}')
echo "${SUBMIT}" | jq .
REQUEST_ID=$(echo "${SUBMIT}" | jq -r .request_id)
echo "申请详情中的冻结上限 token_max_reads:"
curl -sS "${BASE_URL}/publish-requests/${REQUEST_ID}" -u "alice:${ALICE_PW}" \
  | jq '{status, token_expires_in_seconds, token_expires_at, token_max_reads}'

step "7. 申请人不得复核：alice 批准/驳回自己的申请 -> 403"
printf 'alice 批准: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQUEST_ID}/approve" -u "alice:${ALICE_PW}")"
printf 'alice 驳回: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQUEST_ID}/reject" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{}')"

step "8. 申请后记录被修订到版本 2（待审内容仍冻结在版本 1）"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "完全不同的新原文，敏感词ABC。",
  "intervals": [{"start": 9, "end": 15}]
}' | jq .

step "9. bob(reviewer) 批准：同一事务生成不可变快照 + 新读取令牌（上限随申请冻结值生效）"
APPROVE=$(curl -sS -X POST "${BASE_URL}/publish-requests/${REQUEST_ID}/approve" -u "bob:${BOB_PW}")
echo "${APPROVE}" | jq '{request_id, status, snapshot_id, token, expires_at, max_reads}'
SNAPSHOT_ID=$(echo "${APPROVE}" | jq -r .snapshot_id)
TOKEN=$(echo "${APPROVE}" | jq -r .token)

step "10. 公开读取：得到冻结版本 1 的脱敏正文；上限 2 次，第 3 次起 403"
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}" | jq .
printf '第 2 次: HTTP %s；第 3 次(用尽): HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}")" \
  "$(code "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}")"
echo "维护侧查询已用/剩余次数（不含原文）:"
curl -sS "${BASE_URL}/tokens/${TOKEN}" "${AUTH[@]}" \
  | jq '{snapshot_id, version, max_reads, reads_used, remaining_reads, active}'

step "11. 已终结申请不能再复核（再次批准/驳回均 409）"
printf '再次批准: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQUEST_ID}/approve" -u "bob:${BOB_PW}")"
printf '再驳回: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQUEST_ID}/reject" \
  -u "bob:${BOB_PW}" -H 'Content-Type: application/json' -d '{}')"

step "12. 新申请被驳回后不能批准；驳回不生成快照/令牌"
SUB2=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" -d '{"version": 2}')
REQ2=$(echo "${SUB2}" | jq -r .request_id)
curl -sS -X POST "${BASE_URL}/publish-requests/${REQ2}/reject" \
  -u "bob:${BOB_PW}" -H "Content-Type: application/json" -d '{"reason":"材料不全"}' | jq .
printf '驳回后再批准: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQ2}/approve" -u "bob:${BOB_PW}")"

step "13. 并发复核同一申请：一个成功(200)，另一个收到 409，终态唯一"
SUB3=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" -d '{"version": 2}')
REQ3=$(echo "${SUB3}" | jq -r .request_id)
TMP=$(mktemp -d)
(curl -sS -o "${TMP}/a.out" -w '%{http_code}' -X POST \
  "${BASE_URL}/publish-requests/${REQ3}/approve" -u "bob:${BOB_PW}" > "${TMP}/a.code") &
(curl -sS -o "${TMP}/r.out" -w '%{http_code}' -X POST \
  "${BASE_URL}/publish-requests/${REQ3}/reject" -u "carol:${CAROL_PW}" \
  -H 'Content-Type: application/json' -d '{}' > "${TMP}/r.code") &
wait
printf '批准 HTTP %s；驳回 HTTP %s\n' "$(cat "${TMP}/a.code")" "$(cat "${TMP}/r.code")"
echo "批准响应:"; cat "${TMP}/a.out" | jq -c . 2>/dev/null || cat "${TMP}/a.out"
echo "驳回响应:"; cat "${TMP}/r.out" | jq -c . 2>/dev/null || cat "${TMP}/r.out"
curl -sS "${BASE_URL}/publish-requests/${REQ3}" -u "alice:${ALICE_PW}" | jq .
rm -rf "${TMP}"

step "14. 申请列表与记录状态（只含元数据，不含原文）"
curl -sS "${BASE_URL}/records/${RECORD_ID}/publish-requests" -u "alice:${ALICE_PW}" | jq .

step "15. 批准签发的令牌走既有撤销接口，撤销后公开读取 403"
curl -sS -X POST "${BASE_URL}/tokens/${TOKEN}/revoke" "${AUTH[@]}" | jq .
curl -sS "${BASE_URL}/public/snapshots/${SNAPSHOT_ID}?token=${TOKEN}" | jq .

printf '\n\033[1m受控发布演示完成。\033[0m\n'
