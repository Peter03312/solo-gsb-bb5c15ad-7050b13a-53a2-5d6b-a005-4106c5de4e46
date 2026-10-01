#!/usr/bin/env bash
# 读取凭证泄露换发端到端示例：
#   发布单快照令牌（带上限）与多快照共享凭证 -> 凭服务密钥提交（原凭证, 请求标识）换发 ->
#   返回新随机凭证、适用快照、原绝对到期时刻与剩余额度，旧凭证换发后立即不能再读 ->
#   相同（原凭证, 请求标识）重试只回放首次结果（replayed=true），不再次换发 ->
#   不同请求标识对同一原凭证再换发 -> 409 -> 已撤销/已到期/额度耗尽不可换发 ->
#   共享清单中一项已撤回时仍可换发，该项继续不可读且不扣额度
# 依赖：curl、jq（reqid 另需 python3 或 /proc/sys/kernel/random/uuid）
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（共享凭证申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（共享凭证复核人）
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
reqid() {
  python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || cat /proc/sys/kernel/random/uuid
}

# 创建记录并直接发布版本 1（可透传发布请求体），回显 JSON
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

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 发布一支单快照令牌（上限 5 次，2 小时到期），先成功读取 2 次"
PUB=$(create_publish \
  "患者王五，身份证号 110101199003071234，电话 13900002222。" \
  '[{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 31, "end": 42}]' \
  '{"version": 1, "max_reads": 5, "expires_in_seconds": 7200}')
echo "${PUB}" | jq '{snapshot_id, max_reads, expires_at}'
SID=$(echo "${PUB}" | jq -r .snapshot_id)
TOK=$(echo "${PUB}" | jq -r .token)
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" >/dev/null
echo "已用次数: $(curl -sS "${BASE_URL}/tokens/${TOK}" "${AUTH[@]}" | jq -r .reads_used)"

step "2. 疑似泄露：凭服务密钥提交（原令牌, 请求标识）换发"
REQ1=$(reqid)
ROT=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${TOK}\", \"request_id\": \"${REQ1}\"}")
echo "${ROT}" | jq .
NEWTOK=$(echo "${ROT}" | jq -r .new_credential)
echo "kind=token；继承原绝对到期时刻；reads_used=2、remaining_reads=3（额度不重置）"

step "3. 旧令牌换发提交后不能再读取；新令牌在同一快照继续可读并沿用剩余额度"
printf '旧令牌读取: HTTP %s（模糊 403，不透露换发原因，不计数）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=${TOK}")"
printf '新令牌读取: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID}?token=${NEWTOK}")"
curl -sS "${BASE_URL}/tokens/${NEWTOK}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active, rotated_at}'

step "4. 相同原凭证与请求标识重试：原样回放首次结果（replayed=true），不再次换发"
curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${TOK}\", \"request_id\": \"${REQ1}\"}" \
  | jq '{new_credential, replayed, reads_used, remaining_reads}'

step "5. 不同请求标识换发同一原凭证 -> 409（仅一次成功）"
printf '另一请求标识换发旧令牌: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d "{\"credential\": \"${TOK}\", \"request_id\": \"$(reqid)\"}")"

step "6. 鉴权与校验：缺/错密钥 401；未知凭证 404；空白字段 422"
printf '无密钥: HTTP %s；错密钥: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" -H 'Content-Type: application/json' \
    -d "{\"credential\": \"${TOK}\", \"request_id\": \"${REQ1}\"}")" \
  "$(code -X POST "${BASE_URL}/credentials/rotate" -H 'X-Service-Key: wrong' -H 'Content-Type: application/json' \
    -d "{\"credential\": \"${TOK}\", \"request_id\": \"${REQ1}\"}")"
printf '未知凭证: HTTP %s；空白请求标识: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d '{"credential": "not-a-credential", "request_id": "x"}')" \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d "{\"credential\": \"${TOK}\", \"request_id\": \" \"}")"

step "7. 已撤销 / 已到期 / 额度耗尽的凭证不可换发（均 409）"
# 7a. 已撤销
PUB2=$(create_publish "记录甲，内容普通。" '[]')
TOK2=$(echo "${PUB2}" | jq -r .token)
curl -sS -X POST "${BASE_URL}/tokens/${TOK2}/revoke" "${AUTH[@]}" >/dev/null
printf '已撤销令牌换发: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d "{\"credential\": \"${TOK2}\", \"request_id\": \"$(reqid)\"}")"
# 7b. 额度耗尽：max_reads=1，读一次后换发
PUB3=$(create_publish "记录乙，秘密XX。" '[{"start": 4, "end": 8}]' '{"version": 1, "max_reads": 1}')
SID3=$(echo "${PUB3}" | jq -r .snapshot_id); TOK3=$(echo "${PUB3}" | jq -r .token)
curl -sS "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}" >/dev/null
printf '额度耗尽令牌换发: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d "{\"credential\": \"${TOK3}\", \"request_id\": \"$(reqid)\"}")"
# 7c. 已到期（1 秒）
PUB4=$(create_publish "记录乙，内容普通。" '[]' '{"version": 1, "expires_in_seconds": 1}')
TOK4=$(echo "${PUB4}" | jq -r .token)
sleep 1.2
printf '已到期令牌换发: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
    -d "{\"credential\": \"${TOK4}\", \"request_id\": \"$(reqid)\"}")"

step "8. 多快照共享凭证换发（继承冻结清单、到期时刻与共享已用额度）"
BSID1=$(create_publish "共享记录一，敏感词AAAA。" '[{"start": 7, "end": 11}]' | jq -r .snapshot_id)
BSID2=$(create_publish "共享记录二，敏感词BBBB。" '[{"start": 7, "end": 11}]' | jq -r .snapshot_id)
SUB=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${BSID1}\",\"${BSID2}\"],\"max_reads\":5,\"expires_in_seconds\":7200}")
BREQ=$(echo "${SUB}" | jq -r .request_id)
APP=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BREQ}/approve" -u "bob:${BOB_PW}")
CRED=$(echo "${APP}" | jq -r .credential); BID=$(echo "${APP}" | jq -r .bundle_id)
curl -sS "${BASE_URL}/public/snapshots/${BSID1}?token=${CRED}" >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${BSID2}?token=${CRED}" >/dev/null
BREQ2=$(reqid)
BROT=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${CRED}\", \"request_id\": \"${BREQ2}\"}")
echo "${BROT}" | jq '{kind, snapshot_ids, max_reads, reads_used, remaining_reads,
                     items: [.items[] | {snapshot_id, status, position}]}'
NCRED=$(echo "${BROT}" | jq -r .new_credential)
printf '旧凭证读清单内快照: HTTP %s；新凭证读取: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${BSID1}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${BSID1}?token=${NCRED}")"

step "9. 清单中某项快照已撤回：仍可换发，该项继续不可读且不扣额度"
CSID1=$(create_publish "撤回演示记录一。" '[]' | jq -r .snapshot_id)
CSID2=$(create_publish "撤回演示记录二。" '[]' | jq -r .snapshot_id)
SUB2=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${CSID1}\",\"${CSID2}\"],\"max_reads\":5}")
BREQ3=$(echo "${SUB2}" | jq -r .request_id)
APP2=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BREQ3}/approve" -u "bob:${BOB_PW}")
CRED2=$(echo "${APP2}" | jq -r .credential)
curl -sS -X POST "${BASE_URL}/snapshots/${CSID2}/withdraw" "${AUTH[@]}" \
  -d '{"reason":"该版本不再对外提供"}' >/dev/null
CROT=$(curl -sS -X POST "${BASE_URL}/credentials/rotate" "${AUTH[@]}" \
  -d "{\"credential\": \"${CRED2}\", \"request_id\": \"$(reqid)\"}")
NCRED2=$(echo "${CROT}" | jq -r .new_credential)
echo "${CROT}" | jq '{snapshot_ids, items: [.items[] | .status]}'
printf '新凭证读已撤回项: HTTP %s（不扣额度）；读另一项: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${CSID2}?token=${NCRED2}")" \
  "$(code "${BASE_URL}/public/snapshots/${CSID1}?token=${NCRED2}")"

step "10. 维护侧：旧凭证 active=false 且带 rotated_at，计数停在换发时"
curl -sS "${BASE_URL}/read-bundles/${BID}" "${AUTH[@]}" \
  | jq '{credential, active, rotated_at, reads_used, remaining_reads}'

printf '\n\033[1m凭证泄露换发演示完成。\033[0m\n'
