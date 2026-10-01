#!/usr/bin/env bash
# 快照永久撤回端到端示例：
#   发布两个版本 -> 撤回版本 1（缺密钥401/未知404/缺原因422）-> 重复撤回返回首次结果 ->
#   撤回后任意令牌公开读取 403 且不消耗次数 -> 同记录其他版本继续可读 ->
#   再次直接发布 / 提交受控申请均 409 ->
#   另一条受控记录演示：撤回前已待审的申请，批准 409、仍可驳回 ->
#   维护侧记录显示撤回时间与原因，公开侧不透露原因
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

step "1. 创建记录并发布版本 1（签发一支上限 3 次的令牌 + 一支不限次令牌）"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
RECORD_ID=$(echo "${CREATE}" | jq -r .record_id)
PUB1=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 1, "max_reads": 3}')
echo "${PUB1}" | jq '{snapshot_id, version, status, max_reads}'
SID1=$(echo "${PUB1}" | jq -r .snapshot_id)
TOK1=$(echo "${PUB1}" | jq -r .token)
TOK2=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 1}' | jq -r .token)

step "2. 修订并发布版本 2（撤回版本 1 后它应继续可读）"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者李四，联系方式已变更。",
  "intervals": [{"start": 2, "end": 4}]
}' | jq .
PUB2=$(curl -sS -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 2}')
SID2=$(echo "${PUB2}" | jq -r .snapshot_id)
TOK3=$(echo "${PUB2}" | jq -r .token)

step "3. 撤回版本 1：缺密钥 401、未知快照 404、缺原因 422"
printf '缺服务密钥: HTTP %s\n' "$(code -X POST "${BASE_URL}/snapshots/${SID1}/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"x"}')"
UNKNOWN="00000000-0000-0000-0000-000000000000"
printf '未知快照:   HTTP %s\n' "$(code -X POST "${BASE_URL}/snapshots/${UNKNOWN}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"x"}')"
printf '缺少原因:   HTTP %s\n' "$(code -X POST "${BASE_URL}/snapshots/${SID1}/withdraw" "${AUTH[@]}" \
  -d '{}')"

step "4. 首次撤回成功：返回快照标识、首次撤回时间与状态"
WITHDRAW=$(curl -sS -X POST "${BASE_URL}/snapshots/${SID1}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"发现该版本不应继续公开"}')
echo "${WITHDRAW}" | jq .
FIRST_AT=$(echo "${WITHDRAW}" | jq -r .withdrawn_at)

step "5. 重复撤回（不同原因/空原因）幂等返回首次结果，原因保留首次值"
curl -sS -X POST "${BASE_URL}/snapshots/${SID1}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"另一个原因"}' | jq .
printf '重复撤回首次时间一致: '
curl -sS -X POST "${BASE_URL}/snapshots/${SID1}/withdraw" "${AUTH[@]}" -d '{}' \
  | jq --arg first "${FIRST_AT}" -e '.withdrawn_at == $first and .status == "withdrawn"'

step "6. 撤回后公开读取：无论哪枚令牌（或假令牌）一律 403 snapshot unavailable"
for T in "${TOK1}" "${TOK2}" "not-a-token"; do
  printf 'HTTP %s  ' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${T}")"
  curl -sS "${BASE_URL}/public/snapshots/${SID1}?token=${T}"
  echo
done

step "7. 撤回后的 403 不消耗读取次数（TOK1 撤回前未读过 -> 已用 0 / 剩余 3）"
curl -sS "${BASE_URL}/tokens/${TOK1}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active}'

step "8. 撤回只作用于该快照：同记录版本 2 继续可读"
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${TOK3}" | jq .
echo "维护侧记录状态（含撤回时间、原因与状态）:"
curl -sS "${BASE_URL}/records/${RECORD_ID}" "${AUTH[@]}" | jq .

step "9. 该版本再次直接发布 -> 409；改为受控后提交受控申请 -> 409"
printf '再次直接发布: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RECORD_ID}/publish" "${AUTH[@]}" \
  -d '{"version": 1}')"
curl -sS -X PUT "${BASE_URL}/records/${RECORD_ID}/controlled" "${AUTH[@]}" >/dev/null
printf '提交受控申请: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RECORD_ID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{"version": 1}')"

step "10. 另一条受控记录：撤回前已在审的申请，批准 -> 409，仍可驳回"
# 10.1 新记录 -> 受控 -> 同时提交请求 A、B（A 稍后批准生成快照；B 保持待审）
CREATE2=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "另一条记录，版本一内容。", "intervals": [{"start": 4, "end": 7}]}')
RID2=$(echo "${CREATE2}" | jq -r .record_id)
curl -sS -X PUT "${BASE_URL}/records/${RID2}/controlled" "${AUTH[@]}" | jq .
REQ_A=$(curl -sS -X POST "${BASE_URL}/records/${RID2}/publish-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{"version": 1}' \
  | jq -r .request_id)
REQ_B=$(curl -sS -X POST "${BASE_URL}/records/${RID2}/publish-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{"version": 1}' \
  | jq -r .request_id)
# 10.2 批准 A 生成快照，随后撤回该快照；B 仍是撤回前就存在的待审申请
APPROVED=$(curl -sS -X POST "${BASE_URL}/publish-requests/${REQ_A}/approve" -u "bob:${BOB_PW}")
SID3=$(echo "${APPROVED}" | jq -r .snapshot_id)
echo "请求 A 已批准生成快照 ${SID3}；请求 B(${REQ_B}) 保持待审"
curl -sS -X POST "${BASE_URL}/snapshots/${SID3}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"批准后发现不应公开"}' | jq .
# 10.3 撤回后：在审的 B 不能批准，但仍可驳回；已批准的 A 记录继续留存
printf '撤回后批准在审申请 B: HTTP %s\n' "$(code -X POST "${BASE_URL}/publish-requests/${REQ_B}/approve" -u "bob:${BOB_PW}")"
curl -sS -X POST "${BASE_URL}/publish-requests/${REQ_B}/reject" \
  -u "bob:${BOB_PW}" -H 'Content-Type: application/json' \
  -d '{"reason":"版本已撤回，驳回申请"}' | jq '{request_id, status, reason}'
echo "已批准申请 A 的记录仍留存（状态 approved，snapshot_id 指向撤回快照）:"
curl -sS "${BASE_URL}/publish-requests/${REQ_A}" -u "alice:${ALICE_PW}" \
  | jq '{request_id, status, snapshot_id}'

printf '\n\033[1m快照撤回演示完成。\033[0m\n'
