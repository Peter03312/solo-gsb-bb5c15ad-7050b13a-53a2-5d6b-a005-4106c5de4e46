#!/usr/bin/env bash
# 多快照共享凭证（read bundle）端到端示例：
#   发布多个快照 -> operator 提交申请（冻结清单 + 可选到期 + 共享总额度）->
#   申请人不得自审 -> reviewer 批准重新核对清单并一次性签发随机凭证 ->
#   凭凭证在既有公开接口只读清单内脱敏正文（共用额度）->
#   撤回清单中单项：只拒绝该项且不扣额度，其他项仍可读 ->
#   批准时清单失效则整次失败（仍可驳回）-> 维护侧查询/撤销
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

# 创建一条记录并直接发布指定版本，回显 snapshot_id
publish_one() {
  local text="$1" intervals="$2"
  local create sid_tok
  create=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
    -d "{\"text\": \"${text}\", \"intervals\": ${intervals}}")
  local rid
  rid=$(echo "${create}" | jq -r .record_id)
  sid_tok=$(curl -sS -X POST "${BASE_URL}/records/${rid}/publish" "${AUTH[@]}" -d '{"version": 1}')
  echo "${sid_tok}" | jq -r .snapshot_id
}

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 维护端发布两个已发布快照（可来自任意记录、直接或受控发布）"
SID1=$(publish_one "患者张三，身份证号 110101199003077777，联系电话 13800001111。" \
  '[{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]')
SID2=$(publish_one "完全不同的新原文，敏感词ABC。" '[{"start": 9, "end": 15}]')
echo "快照1: ${SID1}"; echo "快照2: ${SID2}"

step "2. 无凭据/角色不足：无凭据 401；bob(仅 reviewer) 提交 403；清单不足/重复 422"
printf '无凭据: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -H 'Content-Type: application/json' -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":5}")"
printf 'bob 提交: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "bob:${BOB_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":5}")"
printf '只给一个快照: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\"],\"max_reads\":5}")"
printf '重复快照 ID: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID1}\"],\"max_reads\":5}")"
printf '额度为 0: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":0}")"

step "3. alice(operator) 提交：冻结清单 + 可选到期策略 + 正整数共享总额度"
SUBMIT=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID2}\"],\"max_reads\":5}")
echo "${SUBMIT}" | jq .
REQUEST_ID=$(echo "${SUBMIT}" | jq -r .request_id)
echo "申请详情（冻结清单与额度，不含原文）:"
curl -sS "${BASE_URL}/bundle-requests/${REQUEST_ID}" -u "alice:${ALICE_PW}" | jq .

step "4. 申请人不得自审：alice 批准/驳回均 403"
printf 'alice 批准: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests/${REQUEST_ID}/approve" -u "alice:${ALICE_PW}")"
printf 'alice 驳回: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests/${REQUEST_ID}/reject" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{}')"

step "5. bob(reviewer) 批准：重新核对全部快照有效后，一次性签发随机凭证并绑定清单"
APPROVE=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${REQUEST_ID}/approve" -u "bob:${BOB_PW}")
echo "${APPROVE}" | jq '{request_id,status,reviewed_by,bundle_id,credential,snapshot_ids,expires_at,max_reads}'
BUNDLE_ID=$(echo "${APPROVE}" | jq -r .bundle_id)
CRED=$(echo "${APPROVE}" | jq -r .credential)

step "6. 凭凭证在既有公开接口读取：只能读清单内脱敏正文，共用 5 次额度"
curl -sS "${BASE_URL}/public/snapshots/${SID1}?token=${CRED}" | jq .
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${CRED}" | jq .
echo "维护侧查询额度（不含原文）:"
curl -sS "${BASE_URL}/read-bundles/${BUNDLE_ID}" "${AUTH[@]}" \
  | jq '{bundle_id,max_reads,reads_used,remaining_reads,active,items:[.items[]|{snapshot_id,version,status}]}'

step "7. 第三个快照不在清单内 -> 模糊 403，不扣共享额度"
SID3=$(publish_one "第三条记录，秘密XYZ。" '[{"start": 6, "end": 12}]')
curl -sS "${BASE_URL}/public/snapshots/${SID3}?token=${CRED}" | jq .
echo "额度仍应为 2（已读 2 次）:"
curl -sS "${BASE_URL}/read-bundles/${BUNDLE_ID}" "${AUTH[@]}" | jq '{reads_used,remaining_reads}'

step "8. 撤回清单中的快照2：只拒绝该项且不扣额度，快照1仍可读"
curl -sS -X POST "${BASE_URL}/snapshots/${SID2}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"该版本不再对外提供"}' | jq .
printf '读快照2(已撤回): HTTP %s；再读一次仍不扣额度\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID2}?token=${CRED}")"
printf '读快照1(仍有效): HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${CRED}")"
curl -sS "${BASE_URL}/read-bundles/${BUNDLE_ID}" "${AUTH[@]}" \
  | jq '{reads_used,remaining_reads,items:[.items[]|{snapshot_id,status,withdrawn_at}]}'

step "9. 用尽剩余额度后，全部清单快照都返回模糊 403"
# 已用 3 次，额度 5：再读 2 次成功，之后 403
printf '第4次: HTTP %s；第5次: HTTP %s；第6次(耗尽): HTTP %s；已撤回项: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${SID2}?token=${CRED}")"

step "10. 新申请在批准前清单失效 -> 整次批准失败(409)，不签发凭证，但仍可驳回"
SUB2=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID3}\"],\"max_reads\":3}")
REQ2=$(echo "${SUB2}" | jq -r .request_id)
curl -sS -X POST "${BASE_URL}/snapshots/${SID3}/withdraw" "${AUTH[@]}" -d '{"reason":"复核期间撤回"}' >/dev/null
printf '批准失效清单: HTTP %s\n' "$(code -X POST "${BASE_URL}/bundle-requests/${REQ2}/approve" -u "bob:${BOB_PW}")"
curl -sS "${BASE_URL}/bundle-requests/${REQ2}" -u "alice:${ALICE_PW}" | jq '{status,bundle_id}'
curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ2}/reject" \
  -u "bob:${BOB_PW}" -H "Content-Type: application/json" -d '{"reason":"清单已失效"}' | jq .

step "11. 驳回路径：另一枚凭证申请被 reviewer 驳回，驳回后不能批准"
SID4=$(publish_one "又一条记录，内容不敏感。" '[]')
SUB3=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID4}\"],\"max_reads\":2}")
REQ3=$(echo "${SUB3}" | jq -r .request_id)
curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ3}/reject" \
  -u "bob:${BOB_PW}" -H "Content-Type: application/json" -d '{"reason":"暂不开放"}' | jq .
printf '驳回后再批准: HTTP %s；对已批准申请再操作: 批准 HTTP %s / 驳回 HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/bundle-requests/${REQ3}/approve" -u "bob:${BOB_PW}")" \
  "$(code -X POST "${BASE_URL}/bundle-requests/${REQUEST_ID}/approve" -u "bob:${BOB_PW}")" \
  "$(code -X POST "${BASE_URL}/bundle-requests/${REQUEST_ID}/reject" -u "bob:${BOB_PW}" -H 'Content-Type: application/json' -d '{}')"

step "12. 维护侧：凭证清单、额度与状态查询，以及撤销（撤销后全部拒绝）"
# 再签发一枚带到期策略的凭证用于撤销演示
SUB4=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID1}\",\"${SID4}\"],\"max_reads\":3,\"expires_in_seconds\":3600}")
REQ4=$(echo "${SUB4}" | jq -r .request_id)
APP4=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${REQ4}/approve" -u "bob:${BOB_PW}")
BID4=$(echo "${APP4}" | jq -r .bundle_id); CRED4=$(echo "${APP4}" | jq -r .credential)
echo "全部凭证数量: $(curl -sS "${BASE_URL}/read-bundles" "${AUTH[@]}" | jq '.bundles | length')"
curl -sS "${BASE_URL}/read-bundles/${BID4}" "${AUTH[@]}" | jq \
  '{bundle_id,max_reads,reads_used,remaining_reads,active,expires_at,items:[.items[]|{snapshot_id,version,status}]}'
curl -sS -X POST "${BASE_URL}/read-bundles/${BID4}/revoke" "${AUTH[@]}" | jq .
printf '撤销后读快照1: HTTP %s（不扣额度，模糊拒绝）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${CRED4}")"

step "13. 申请列表（任一已认证操作者可查，只含元数据）"
curl -sS "${BASE_URL}/bundle-requests" -u "alice:${ALICE_PW}" \
  | jq '[.requests[] | {request_id,status,requested_by,reviewed_by,max_reads,bundle_id}]'

printf '\n\033[1m多快照共享凭证演示完成。\033[0m\n'
