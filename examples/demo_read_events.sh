#!/usr/bin/env bash
# 成功读取审计端到端示例：
#   发布单快照令牌（带上限）与多快照共享凭证 -> 成功读取（每次与额度扣减同事务写一条审计事件）->
#   失败 / 清单外 / 已撤回快照的读取不产生事件 -> 泄露换发后新旧凭证的成功读取归于同一换发链 ->
#   维护人员凭服务密钥按快照 + 时间范围分页查询（下一页游标，不返回原文/脱敏正文/凭证明文）->
#   非法时间范围 / 游标 / 未知快照被明确拒绝
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（共享凭证申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（共享凭证复核人）
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

# 创建记录并直接发布版本 1，回显发布 JSON
create_publish() {
  local text="$1" intervals="$2" pub_body="${3-}"
  local rid
  if [[ -z "${pub_body}" ]]; then
    pub_body='{"version": 1}'
  fi
  rid=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"${text}\", \"intervals\": ${intervals}}" | jq -r .record_id)
  curl -sS -X POST "${BASE_URL}/records/${rid}/publish" "${AUTH[@]}" \
    -H "Content-Type: application/json" -d "${pub_body}"
}

# 申请并经 reviewer 批准一支多快照共享凭证，回显凭证字符串
approved_bundle() {
  local sids="$1" max_reads="$2"
  local req
  req=$(curl -sS -X POST "${BASE_URL}/bundle-requests" -u "alice:${ALICE_PW}" \
    -H "Content-Type: application/json" \
    -d "{\"snapshot_ids\": ${sids}, \"max_reads\": ${max_reads}}" | jq -r .request_id)
  curl -sS -X POST "${BASE_URL}/bundle-requests/${req}/approve" -u "bob:${BOB_PW}" \
    | jq -r .credential
}

START="2000-01-01T00:00:00Z"
END="2100-01-01T00:00:00Z"

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 发布单快照令牌（上限 5 次），成功读取 2 次（每次与扣次同事务写审计事件）"
PUB=$(create_publish \
  "患者赵六，身份证号 110101199003078888，电话 13700003333。" \
  '[{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 31, "end": 42}]' \
  '{"version": 1, "max_reads": 5}')
SID=$(echo "${PUB}" | jq -r .snapshot_id)
TOK=$(echo "${PUB}" | jq -r .token)
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null
echo "成功读取后令牌已用次数: $(curl -sS "${BASE_URL}/tokens/${TOK}" "${AUTH[@]}" | jq -r .reads_used)"

step "2. 失败读取（错误令牌、额度外）不产生事件；公开侧仍是模糊 403"
printf '错误令牌: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID}?token=wrong-token")"

step "3. 泄露换发：换发链不变，旧凭证立即不可读，新凭证成功读取也归入同一链"
REQ="demo-audit-$(date +%s)"
ROT=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"credential\": \"${TOK}\", \"request_id\": \"${REQ}\"}")
NEWTOK=$(echo "${ROT}" | jq -r .new_credential)
printf '旧凭证读取: HTTP %s（模糊 403，不计数、不产生事件）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=${TOK}")"
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${NEWTOK}" >/dev/null
echo "新凭证成功读取 1 次（与旧凭证的 2 次归于同一换发链）"

step "4. 多快照共享凭证：清单内成功读取各记一条 bundle 事件（同一换发链）"
PUB2=$(create_publish "完全不同的新原文，敏感词XYZ。" '[{"start":9,"end":15}]' '{"version":1,"max_reads":10}')
SID2=$(echo "${PUB2}" | jq -r .snapshot_id)
CRED=$(approved_bundle "[\"${SID}\",\"${SID2}\"]" 5)
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${CRED}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${CRED}" >/dev/null
echo "共享凭证已用次数: $(curl -sS "${BASE_URL}/read-bundles" "${AUTH[@]}" \
  | jq -r --arg c "${CRED}" '.bundles[] | select(.credential==$c) | .reads_used')"

step "5. 清单外快照 / 已撤回快照的请求不产生事件"
PUB3=$(create_publish "另一条记录正文，敏感词Q。" '[{"start":8,"end":10}]')
SID3=$(echo "${PUB3}" | jq -r .snapshot_id)
TOK3=$(echo "${PUB3}" | jq -r .token)
printf '清单外快照（共享凭证）: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${CRED}")"
curl -sS -X POST "${BASE_URL}/snapshots/${SID3}/withdraw" "${AUTH[@]}" \
  -H "Content-Type: application/json" -d '{"reason":"演示撤回"}' >/dev/null
printf '已撤回快照（其自己的令牌）: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}")"
echo "两种失败读取都不产生事件：该快照查询结果应为空"
curl -sS "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID3}/read-events" \
  --data-urlencode "start=${START}" --data-urlencode "end=${END}" | jq .

step "6. 维护侧分页查询审计事件（服务密钥；每页 2 条，游标翻页）"
echo "--- 第 1 页 ---"
PAGE=$(curl -sS "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID}/read-events" \
  --data-urlencode "start=${START}" --data-urlencode "end=${END}" --data-urlencode "limit=2")
echo "${PAGE}" | jq '{snapshot_id, limit, next_cursor, events}'
CURSOR=$(echo "${PAGE}" | jq -r .next_cursor)
echo "--- 第 2 页（携带上一页游标）---"
curl -sS "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID}/read-events" \
  --data-urlencode "start=${START}" --data-urlencode "end=${END}" \
  --data-urlencode "limit=2" --data-urlencode "cursor=${CURSOR}" \
  | jq '{snapshot_id, limit, next_cursor, events}'
echo "字段仅含 event_id/snapshot_id/occurred_at/credential_kind/chain_id；"
echo "不含原文、脱敏正文与任何凭证明文（token / bundle credential 均不出现）。"

step "7. 明确拒绝：缺少服务密钥 401 / 未知快照 404 / 非法时间范围或游标 422"
printf '无服务密钥: HTTP %s\n' \
  "$(code -G "${BASE_URL}/snapshots/${SID}/read-events" \
      --data-urlencode "start=${START}" --data-urlencode "end=${END}")"
printf '未知快照: HTTP %s\n' \
  "$(code "${AUTH[@]}" -G "${BASE_URL}/snapshots/00000000-0000-0000-0000-000000000000/read-events" \
      --data-urlencode "start=${START}" --data-urlencode "end=${END}")"
printf '朴素时间戳（无时区）: HTTP %s\n' \
  "$(code "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID}/read-events" \
      --data-urlencode "start=2026-10-01T00:00:00" --data-urlencode "end=${END}")"
printf 'start>end: HTTP %s\n' \
  "$(code "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID}/read-events" \
      --data-urlencode "start=${END}" --data-urlencode "end=${START}")"
printf '伪造游标: HTTP %s\n' \
  "$(code "${AUTH[@]}" -G "${BASE_URL}/snapshots/${SID}/read-events" \
      --data-urlencode "start=${START}" --data-urlencode "end=${END}" \
      --data-urlencode "cursor=bogus-cursor")"
