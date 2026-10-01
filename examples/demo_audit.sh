#!/usr/bin/env bash
# 成功读取审计端到端示例：
#   发布单快照令牌并成功/失败读取 -> 每次成功读取与额度扣减同事务写入不可变事件 ->
#   失败、清单外、已撤回快照的请求不产生事件 -> 多次换发后的成功读取归于同一换发链 ->
#   多快照共享凭证事件 kind=bundle -> 维护人员凭服务密钥按快照+时间范围分页查询（游标）->
#   非法时间范围/游标/未知快照明确拒绝；查询不返回原文、脱敏正文或凭证明文
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（共享凭证申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（共享凭证复核人）
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

# 创建记录并直接发布，回显发布 JSON
create_publish() {
  local text="$1" intervals="$2" pub_body="${3-}"
  local rid
  if [[ -z "${pub_body}" ]]; then
    pub_body='{"version": 1}'
  fi
  rid=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
    -d "{\"text\": \"${text}\", \"intervals\": ${intervals}}" | jq -r .record_id)
  curl -sS -X POST "${BASE_URL}/records/${rid}/publish" "${AUTH[@]}" -d "${pub_body}"
}

# 分页拉取某快照在宽时间范围内的全部事件（跟完 next_cursor）
fetch_all_events() {
  local sid="$1" cursor="" page
  while :; do
    page=$(curl -sS -G "${BASE_URL}/snapshots/${sid}/read-events" "${AUTH[@]}" \
      --data-urlencode "start=2000-01-01T00:00:00Z" \
      --data-urlencode "end=2100-01-01T00:00:00Z" \
      --data-urlencode "limit=2" \
      ${cursor:+--data-urlencode "cursor=${cursor}"})
    echo "${page}" | jq -c '.events[]'
    cursor=$(echo "${page}" | jq -r '.next_cursor // empty')
    [[ -z "${cursor}" ]] && break
  done
}

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 发布一支单快照令牌（上限 10 次）并成功读取 2 次"
PUB=$(create_publish \
  "患者赵六，身份证号 110101199003079999，电话 13700003333。" \
  '[{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 31, "end": 42}]' \
  '{"version": 1, "max_reads": 10}')
SID=$(echo "${PUB}" | jq -r .snapshot_id)
TOK=$(echo "${PUB}" | jq -r .token)
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null

step "2. 失败请求不产生事件：错误凭证、额度规则内的失败同样不写"
printf '错误凭证读取: HTTP %s（模糊 403，不扣次、不写事件）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=not-a-credential")"

step "3. 维护侧按快照 + 时间范围分页查询（服务密钥）：2 条事件"
PAGE=$(curl -sS -G "${BASE_URL}/snapshots/${SID}/read-events" "${AUTH[@]}" \
  --data-urlencode "start=2000-01-01T00:00:00Z" \
  --data-urlencode "end=2100-01-01T00:00:00Z")
echo "${PAGE}" | jq '.events[] | {event_id, snapshot_id, occurred_at, credential_kind, credential_chain_id}'
echo "顶层字段（无正文类字段）与单条事件字段（无凭证字段）："
echo "${PAGE}" | jq -c '{page_keys: keys, event_keys: (.events[0] | keys)}'

step "4. 换发两次：新旧凭证的成功读取归于同一条换发链，旧凭证换发后不能读"
R1=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${TOK}\", \"request_id\": \"audit-demo-1\"}" | jq -r .new_credential)
printf '旧令牌换发后读取: HTTP %s（失败不写事件）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=${TOK}")"
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${R1}" >/dev/null
R2=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${R1}\", \"request_id\": \"audit-demo-2\"}" | jq -r .new_credential)
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${R2}" >/dev/null
echo "全部成功读取事件（分页拉完）："
fetch_all_events "${SID}"
echo "唯一换发链标识数（应为 1）："
fetch_all_events "${SID}" | jq -s 'map(.credential_chain_id) | unique | length'

step "5. 多快照共享凭证：kind=bundle；清单外与已撤回快照的失败不写事件"
BSID1=$(create_publish "审计共享记录一，敏感词CCCC。" '[{"start": 7, "end": 11}]' | jq -r .snapshot_id)
BSID2=$(create_publish "审计共享记录二，敏感词DDDD。" '[{"start": 7, "end": 11}]' | jq -r .snapshot_id)
SUB=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${BSID1}\",\"${BSID2}\"],\"max_reads\":6}")
BREQ=$(echo "${SUB}" | jq -r .request_id)
CRED=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BREQ}/approve" -u "bob:${BOB_PW}" | jq -r .credential)
curl -sS "${BASE_URL}/public/snapshots/${BSID1}?token=${CRED}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${BSID2}?token=${CRED}" >/dev/null
printf '凭证读清单外快照: HTTP %s（不写事件）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=${CRED}")"
curl -sS -X POST "${BASE_URL}/snapshots/${BSID2}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"审计演示：撤回后读取不写事件"}' >/dev/null
printf '凭证读已撤回清单项: HTTP %s（不写事件）\n' \
  "$(code "${BASE_URL}/public/snapshots/${BSID2}?token=${CRED}")"
echo "BSID1 事件（1 条，kind=bundle）："
curl -sS -G "${BASE_URL}/snapshots/${BSID1}/read-events" "${AUTH[@]}" \
  --data-urlencode "start=2000-01-01T00:00:00Z" --data-urlencode "end=2100-01-01T00:00:00Z" \
  | jq '.events'
echo "BSID2 撤回前的成功事件仍可查（1 条；撤回后的失败未新增）："
curl -sS -G "${BASE_URL}/snapshots/${BSID2}/read-events" "${AUTH[@]}" \
  --data-urlencode "start=2000-01-01T00:00:00Z" --data-urlencode "end=2100-01-01T00:00:00Z" \
  | jq '.events | length'

step "6. 分页游标：limit=2 首页返回 next_cursor，沿游标翻页不重复不遗漏"
FIRST=$(curl -sS -G "${BASE_URL}/snapshots/${SID}/read-events" "${AUTH[@]}" \
  --data-urlencode "start=2000-01-01T00:00:00Z" \
  --data-urlencode "end=2100-01-01T00:00:00Z" --data-urlencode "limit=2")
echo "${FIRST}" | jq '{count: (.events|length), next_cursor: (.next_cursor[0:24] + "...")}'
CUR=$(echo "${FIRST}" | jq -r .next_cursor)
echo "第二页（同一窗口/同一 limit + 游标）："
curl -sS -G "${BASE_URL}/snapshots/${SID}/read-events" "${AUTH[@]}" \
  --data-urlencode "start=2000-01-01T00:00:00Z" --data-urlencode "end=2100-01-01T00:00:00Z" \
  --data-urlencode "limit=2" --data-urlencode "cursor=${CUR}" \
  | jq '{count: (.events|length), next_cursor}'

step "7. 明确拒绝：缺/错密钥 401；未知快照 404；非法时间范围/游标 422"
printf '无密钥: HTTP %s；错密钥: HTTP %s\n' \
  "$(code -G "${BASE_URL}/snapshots/${SID}/read-events" --data-urlencode "start=2020-01-01T00:00:00Z" --data-urlencode "end=2021-01-01T00:00:00Z")" \
  "$(code -G "${BASE_URL}/snapshots/${SID}/read-events" -H "X-Service-Key: wrong" --data-urlencode "start=2020-01-01T00:00:00Z" --data-urlencode "end=2021-01-01T00:00:00Z")"
printf '未知快照: HTTP %s\n' \
  "$(code -G "${BASE_URL}/snapshots/00000000-0000-0000-0000-000000000000/read-events" "${AUTH[@]}" \
    --data-urlencode "start=2020-01-01T00:00:00Z" --data-urlencode "end=2021-01-01T00:00:00Z")"
printf '倒置时间范围: HTTP %s；非法游标: HTTP %s；游标换快照: HTTP %s\n' \
  "$(code -G "${BASE_URL}/snapshots/${SID}/read-events" "${AUTH[@]}" \
    --data-urlencode "start=2021-01-01T00:00:00Z" --data-urlencode "end=2020-01-01T00:00:00Z")" \
  "$(code -G "${BASE_URL}/snapshots/${SID}/read-events" "${AUTH[@]}" \
    --data-urlencode "start=2000-01-01T00:00:00Z" --data-urlencode "end=2100-01-01T00:00:00Z" --data-urlencode "cursor=garbage")" \
  "$(code -G "${BASE_URL}/snapshots/${BSID1}/read-events" "${AUTH[@]}" \
    --data-urlencode "start=2000-01-01T00:00:00Z" --data-urlencode "end=2100-01-01T00:00:00Z" --data-urlencode "cursor=${CUR}")"

printf '\n\033[1m成功读取审计演示完成。\033[0m\n'
