# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

支持两种发布方式：

- **直接发布**（既有能力）：维护人员凭服务密钥直接发布指定版本；
- **受控发布**（申请-复核）：机构先把记录标记为受控，操作者用**各自的凭据与身份**提交指定历史版本的发布申请，由**另一名具复核资格者**批准或驳回（申请人不得复核）；批准时在同一事务生成不可变快照与新读取令牌。

已发布版本若不应继续公开，机构可凭服务密钥将对应**快照永久撤回**：撤回只作用于该快照，同记录其他版本继续可读；撤回后无论持有该快照的哪枚令牌，新开始的公开读取一律沿用模糊 `403 snapshot unavailable` 且不消耗读取次数；该版本再次直接发布、提交受控申请或批准撤回前已在审的申请均返回 `409`，待审申请仍可驳回。

此外，机构可把**多个已发布快照一次性交给同一外部审阅者**：operator 用本人凭据提交多快照共享凭证申请（**至少两个互不重复的快照 ID**、可选到期策略与**正整数共享读取总额度**，提交时冻结清单），由**另一名 reviewer** 批准或驳回（申请人不得自审）；批准时**重新核对全部快照仍存在且未撤回**，任一项失效则整次批准失败、不签发凭证（申请仍可驳回）。批准成功后**一次性签发随机共享凭证**，绑定冻结清单并**共用读取额度**：凭证在**现有公开快照读取接口**上只能读取清单内快照的脱敏正文，沿用原响应与模糊 403；清单内某项快照后来撤回，只拒绝该项且**不扣额度**，其他项仍可读；到期、撤销或额度耗尽时全部拒绝。维护人员凭服务密钥可查询凭证清单、额度与状态并撤销。

发现读取凭证（单快照令牌或多快照共享凭证）**疑似泄露**时，维护人员可凭服务密钥向统一换发接口提交**原凭证与请求标识**，换发**新随机凭证**：响应返回新凭证、适用快照、**原绝对到期时刻**与**剩余额度**。仅**未撤销、未到期且额度未耗尽**的凭证可换发；新凭证继承原范围（适用快照/冻结清单）、到期时刻与**已用次数**（额度不重置），旧凭证在换发提交后立即不能再读取。相同原凭证与请求标识重试只返回首次换发结果（幂等，不再次换发）；不同请求标识并发换发同一凭证仅一次成功，其余 `409`。共享清单中个别快照已撤回时仍可换发，该项继续不可读；换发与同时进行的成功读取按事务先后结算，不重置也不超支额度。公开读取响应、模糊 403 与成功扣次规则保持不变。

签发快照读取令牌时可**可选**地设置正整数**读取次数上限**（`max_reads`）：直接发布在请求时确定，受控发布在提交申请时冻结、批准签发后生效。公开读取沿用既有脱敏正文响应，每次凭有效令牌**成功读取**消耗一次；无效、撤销、到期、快照已撤回、令牌与快照不匹配或已达上限的请求不消耗次数。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化（启动时幂等建表，并重试等待数据库就绪）。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `REVIEW_CREDENTIALS` | 受控发布操作者凭据，JSON 数组，见下 | `[]`（受控接口无人可用，不影响其他接口） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |

维护接口（创建、修订、直接发布、撤销、状态、标记受控）通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

受控发布接口（提交、批准、驳回、查询申请）使用 **HTTP Basic 认证**（`Authorization: Basic base64(用户名:口令)`），按 `REVIEW_CREDENTIALS` 校验**操作者个人凭据**，并区分角色：

- `operator`：可提交发布申请；
- `reviewer`：可批准/驳回申请（申请人不得复核自己的申请）；
- 一人可同时具备两种角色（如机构的备用复核人）。

`REVIEW_CREDENTIALS` 为 JSON 数组，每项形如：

```json
[
  {"username": "alice", "password": "强口令A", "roles": ["operator"]},
  {"username": "bob",   "password": "强口令B", "roles": ["reviewer"]},
  {"username": "carol", "password": "强口令C", "roles": ["operator", "reviewer"]}
]
```

服务启动时校验该配置：用户名非空且不重复、口令非空、角色只能是 `operator`/`reviewer` 且至少一个，非法配置会让服务**快速失败**（报错信息不含口令）。至少需配置一名 `operator` 与一名**不同于**常用申请人的 `reviewer`，否则申请无法被独立复核。生产环境请通过机密管理系统注入，不要把真实口令提交到仓库。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `PUT` | `/records/{record_id}/controlled` | 服务密钥 | 将记录设为受控发布（幂等） |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 直接发布指定版本（可带 `max_reads` 上限）；**受控记录一律 `409` 拒绝** |
| `POST` | `/records/{record_id}/publish-requests` | operator | 提交受控发布申请（冻结指定历史版本内容与 `max_reads`） |
| `POST` | `/publish-requests/{request_id}/approve` | reviewer（非申请人） | 批准：同事务生成快照 + 新读取令牌（冻结的上限生效） |
| `POST` | `/publish-requests/{request_id}/reject` | reviewer（非申请人） | 驳回（可带 `reason`），驳回为终态，不生成令牌或计数 |
| `GET` | `/publish-requests/{request_id}` | 任一已认证操作者 | 查询单个申请状态（含冻结的 `token_max_reads`，不含原文） |
| `GET` | `/records/{record_id}/publish-requests` | 任一已认证操作者 | 列出某记录的全部申请（不含原文） |
| `GET` | `/tokens/{token}` | 服务密钥 | 查询令牌状态：上限 `max_reads`、已用 `reads_used`、剩余 `remaining_reads`（不含原文） |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销读取令牌（含受控发布签发的令牌） |
| `POST` | `/snapshots/{snapshot_id}/withdraw` | 服务密钥 | **永久撤回快照**（首次需带 `reason`）；重复撤回返回首次撤回时间与状态 |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本、受控标记与快照列表（含撤回时间、原因与状态） |
| `POST` | `/bundle-requests` | operator | 提交多快照共享凭证申请：≥2 个互不重复快照 ID + 正整数 `max_reads`（可带到期策略），提交时冻结清单 |
| `POST` | `/bundle-requests/{request_id}/approve` | reviewer（非申请人） | 批准：重新核对全部快照仍存在且未撤回，全部有效才一次性签发随机共享凭证 |
| `POST` | `/bundle-requests/{request_id}/reject` | reviewer（非申请人） | 驳回共享凭证申请（可带 `reason`），终态，不签发凭证 |
| `GET` | `/bundle-requests/{request_id}` | 任一已认证操作者 | 查询单个共享凭证申请（冻结清单与额度，不含原文） |
| `GET` | `/bundle-requests` | 任一已认证操作者 | 列出全部共享凭证申请（不含原文） |
| `GET` | `/read-bundles` | 服务密钥 | 列出全部已签发共享凭证：清单、额度、状态（不含原文） |
| `GET` | `/read-bundles/{bundle_id}` | 服务密钥 | 查询单个共享凭证的清单、`max_reads`/`reads_used`/`remaining_reads` 与状态 |
| `POST` | `/read-bundles/{bundle_id}/revoke` | 服务密钥 | 撤销共享凭证（撤销后全部读取 403） |
| `POST` | `/credentials/rotate` | 服务密钥 | **泄露换发**：提交原凭证 + 请求标识，单快照令牌/多快照共享凭证统一换发为新随机凭证 |
| `GET` | `/snapshots/{snapshot_id}/read-events` | 服务密钥 | **成功读取审计**：按快照 + 时间范围（`[start, end)`）键集分页查询不可变读取事件（不含正文/凭证明文） |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭令牌或共享凭证） | 公开读取脱敏正文（单快照令牌与多快照共享凭证同一入口） |
| `GET` | `/healthz` | 无 | 健康检查 |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 读取次数上限（`max_reads`）

签发读取令牌时可**可选**地携带正整数 `max_reads`（不传或为 `null` 表示**不限次**，既有及新令牌默认如此）：

- **确定/冻结时点**：直接发布在发布请求中确定；受控发布在 `POST .../publish-requests` 提交申请时**冻结**（申请详情以 `token_max_reads` 返回），复核耗时与后续修订不改变它，**批准签发后**才落到新令牌上；申请被驳回不生成令牌或计数。
- **计数口径**：每次凭**有效**令牌成功读取脱敏正文消耗一次；`reads_used` 只增不减。同一快照的**不同令牌各自独立计数**（同一版本重复发布会复用快照、签出新令牌）。
- **达上限后**：读取沿用既有模糊拒绝 `403 snapshot unavailable`，公开响应结构不变，不额外暴露“已用尽”这一原因。
- **不消耗次数的请求**：令牌无效、已撤销、已到期，或令牌与快照不匹配（含快照不存在）。
- **撤销/到期优先**：即使尚有剩余次数，撤销或到期后读取一律 `403`，不能因尚有次数而放行。
- **并发安全**：次数判定与递增由公开读取接口内的**同一条条件 `UPDATE`** 原子完成（`reads_used < max_reads` 命中行锁串行化），并发读取的成功次数绝不超过上限；撤销是独立的原子 `UPDATE`，提交后优先生效。
- **维护侧查询**：`GET /tokens/{token}`（服务密钥）返回 `max_reads` / `reads_used` / `remaining_reads`（不限次时后两者中的上限与剩余为 `null`）及 `active` 等元数据；该接口**不返回快照原文或脱敏正文**，未知令牌 `404`，缺密钥 `401`。

### 快照永久撤回

机构发现某个已发布版本不应继续公开时，凭服务密钥调用
`POST /snapshots/{snapshot_id}/withdraw`，请求体携带撤回原因 `{"reason": "..."}`：

- **响应**：`{"snapshot_id", "withdrawn_at", "status": "withdrawn"}`，其中 `withdrawn_at` 为**首次**撤回时间。
- **幂等**：重复撤回（即使原因不同或不带原因）返回**首次**撤回时间与状态，首次原因保留不变；并发撤回由条件 `UPDATE` 串行化，恰好一个首次结果。
- **作用域**：撤回只作用于该快照。同记录其他版本的快照与令牌继续可读；快照中的**历史脱敏正文、已批准的发布申请记录均继续留存**（维护侧可查）。
- **公开侧效果**：撤回提交后才开始的公开读取，无论持有该快照的**哪枚令牌**（或无效令牌），一律沿用既有模糊拒绝 `403 snapshot unavailable`；这些请求**不消耗**任何令牌的读取次数，公开响应也**不透露撤回原因**。
- **并发界限**：公开读取先对快照行加锁再判定，读取与撤回由数据库行锁串行化——撤回提交前已在读的请求按既有规则处理，撤回提交后开始的读取必定 403 且不计数。
- **发布冲突**：该版本
  - 再次**直接发布** → `409`（不会复用撤回快照签出新令牌）；
  - **提交受控申请** → `409`；
  - 撤回前已存在的**待审申请**，批准时 → `409`（不签发令牌），但**仍可驳回**；已批准申请记录继续留存。
- **状态码**：未知快照 `404`；缺失或错误服务密钥沿用 `401`；首次撤回缺少非空白 `reason` → `422`。
- **维护侧可见**：`GET /records/{record_id}` 的每个快照条目含 `status`（`published` / `withdrawn`）、`withdrawn_at`、`withdraw_reason`。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600、"expires_at": "2026-10-01T00:00:00Z"、"max_reads": 5）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2, "max_reads": 5}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null,"max_reads":5}

# 3b. 维护侧查询已用/剩余次数（不限次令牌 max_reads 与 remaining_reads 为 null）
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"token":"<TOKEN>","snapshot_id":"<SID>","record_id":"<RID>","version":2,
#     "max_reads":5,"reads_used":0,"remaining_reads":5,"active":true, ...}

# 4. 公开读取（无需密钥，只返回脱敏正文）；前 5 次 200，第 6 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，即使尚有剩余次数，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"

# 6. 永久撤回快照（首次需带原因；重复撤回返回首次 withdrawn_at 与 status）
curl -sS -X POST http://localhost:8000/snapshots/<SID>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"发现该版本不应继续公开"}'
# => {"snapshot_id":"<SID>","withdrawn_at":"2026-09-30T12:00:00Z","status":"withdrawn"}
# 撤回后：公开读取（任意令牌）一律 403 snapshot unavailable 且不消耗次数；
#        该版本再次直接发布/提交受控申请/批准在审申请均 409，待审申请仍可驳回。
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => 403 {"detail":"snapshot unavailable"}
# 维护侧记录状态可见撤回时间与原因（公开侧不透露原因）
curl -sS http://localhost:8000/records/<RID> -H "X-Service-Key: dev-service-key-change-me"
# => {"snapshots":[{"version":2,"status":"withdrawn",
#                   "withdrawn_at":"...","withdraw_reason":"发现该版本不应继续公开", ...}, ...]}
```

完整端到端演示（含 401/409/422、快照不可变、`max_reads` 次数上限与独立计数、维护侧计数查询、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

完整快照撤回演示（含 401/404/422、重复撤回幂等、撤回后任意令牌 403 不计数、同记录其他版本可读、三类发布冲突 409、在审申请仍可驳回）：

```bash
./examples/demo_withdraw.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 受控发布（申请-复核）

受控记录**不能**直接发布，必须走申请-复核流程。

### 语义与规则

1. **标记受控**：机构凭服务密钥 `PUT /records/{id}/controlled`（幂等）。受控记录的直接发布请求返回 `409`；非受控记录的受控申请同样返回 `409`（引导使用正确入口）。
2. **提交申请**：具备 `operator` 角色者用**本人凭据** `POST /records/{id}/publish-requests`，指定一个**历史版本**（`version`，可带 `expires_in_seconds`/`expires_at` 与可选正整数 `max_reads`，规则与直接发布一致）。申请在提交时**冻结该版本的原文与敏感区间，以及读取次数上限**，后续修订只产生新版本，不改变待审内容与上限。
3. **复核**：具备 `reviewer` 角色的**另一人**批准或驳回：
   - **申请人不得复核**自己的申请（即使其同时拥有 reviewer 角色）→ `403`；
   - 批准与驳回互斥：驳回后不能批准、批准后不能再驳回；对已终结申请再次操作 → `409`；
   - 并发复核由数据库行锁串行化，**同一申请只会产生一个终态**，落选方收到 `409`。
4. **批准事务**：沿用既有脱敏（合并相交/相邻区间→`[已遮蔽]`）与令牌（`secrets.token_urlsafe(32)`、可选到期）规则，在**同一事务**内生成不可变快照、写入新读取令牌（携带申请时冻结的 `max_reads`）并终结申请；任何一步失败整体回滚，**不会留下可读令牌或计数**。
5. **驳回**：只终结申请（可记录 `reason`），不生成快照、令牌或读取计数。
6. **拒绝不泄露原文**：无凭据/凭据错误 → `401`，无角色 → `403`，未知记录/版本/申请 → `404`，已终结/入口错误 → `409`；申请查询只返回元数据（含 `token_max_reads`），冻结原文绝不离开服务端。
7. 批准签发的令牌与直接发布令牌完全一致：遵守 `max_reads` 次数上限与到期规则，可用 `POST /tokens/{token}/revoke` 撤销，可用 `GET /tokens/{token}` 查询已用/剩余次数。

### 调用示例

```bash
# 0. 机构把记录设为受控（服务密钥；幂等）
curl -sS -X PUT http://localhost:8000/records/<RID>/controlled \
  -H "X-Service-Key: dev-service-key-change-me"
# => {"record_id":"<RID>","controlled":true}

# 0b. 受控记录的直接发布请求被明确拒绝（不生成快照）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => 409 {"detail":"record is controlled; submit a publish request for review instead"}

# 1. 申请人 alice（operator）用本人凭据提交历史版本 1 的发布申请
#    max_reads 在申请时冻结，批准签发后生效（驳回则不生成令牌/计数）
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"version": 1, "max_reads": 3}'
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"pending",
#     "requested_by":"alice","created_at":"..."}

# 1b. 申请详情可见冻结的上限（只含元数据，不含原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
# => {...,"token_expires_in_seconds":null,"token_expires_at":null,"token_max_reads":3,...}

# 2. 申请人不能复核自己的申请
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own publish request"}

# 3. 另一人 bob（reviewer）批准：同事务生成不可变快照 + 新读取令牌
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve -u bob:bob-pw
# => {"request_id":"<PRID>","record_id":"<RID>","version":1,"status":"approved",
#     "reviewed_by":"bob","snapshot_id":"<SID>","token":"<TOKEN>","expires_at":null,"max_reads":3}

# 4. 公众凭令牌读取冻结版本的脱敏正文：恰好 3 次 200，第 4 次起 403 snapshot unavailable
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":1,"status":"published","redacted_text":"患者[已遮蔽]，……"}
curl -sS http://localhost:8000/tokens/<TOKEN> -H "X-Service-Key: dev-service-key-change-me"
# => {"max_reads":3,"reads_used":1,"remaining_reads":2,"active":true,...}（不含原文）

# —— 驳回路径 ——
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -u alice:alice-pw -H "Content-Type: application/json" -d '{"version": 2}'
# => {"request_id":"<PRID2>",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/reject \
  -u bob:bob-pw -H "Content-Type: application/json" -d '{"reason":"材料不全"}'
# => {"request_id":"<PRID2>","status":"rejected","reviewed_by":"bob","reason":"材料不全",...}
curl -sS -X POST http://localhost:8000/publish-requests/<PRID2>/approve -u bob:bob-pw
# => 409 {"detail":{"message":"publish request already decided","status":"rejected"}}

# 查询（任一已认证操作者；只返回元数据，不返回原文）
curl -sS http://localhost:8000/publish-requests/<PRID> -u alice:alice-pw
curl -sS http://localhost:8000/records/<RID>/publish-requests -u alice:alice-pw
```

完整受控流程演示（含 401/403/404/409、冻结内容与 `max_reads`、申请人回避、并发复核终态唯一、次数用尽与撤销）：

```bash
./examples/demo_controlled.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（受控接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少所需角色（operator/reviewer） | `403` |
| 申请人复核自己的申请 | `403` |
| 未知记录 / 版本 / 申请 | `404` |
| 受控记录直接发布、非受控记录提交申请、对已终结申请再操作、提交/批准命中已撤回版本、令牌到期时间非法 | `409` / `422` |

## 多快照共享凭证（一次把多个快照交给同一外部审阅者）

机构需要把**多个已发布快照**交给同一名外部审阅者时，可申请一枚**跨快照共享凭证**：凭证仍在**现有公开读取接口** `GET /public/snapshots/{snapshot_id}?token=...` 使用，但只能读取**申请清单内**快照的脱敏正文，且所有成功读取**共用同一读取额度**。快照不要求来自受控发布——任何已存在且未撤回的快照都可入单。

### 流程与规则

1. **提交（operator）**：`POST /bundle-requests`，用申请人本人 Basic 凭据，请求体：
   - `snapshot_ids`：**至少两个、互不重复**的快照 ID（顺序即冻结清单顺序）；
   - `max_reads`：**正整数**共享成功读取**总额度**（必填，清单内所有快照共用）；
   - 可选 `expires_in_seconds` / `expires_at`（二选一，规则与令牌到期一致；不传则永不到期）。

   提交时即**冻结清单**：未知快照 → `404`，含已撤回快照 → `409`，少于两个/重复/额度非正整数 → `422`。
2. **复核（另一名 reviewer）**：
   - 申请人**不得自审**（即使同时具备 reviewer 角色）→ `403`；
   - 批准时**重新核对冻结清单的全部快照仍存在且未撤回**（对快照行加锁，与撤回串行化），**任一项失效则整次批准失败** → `409`，**不签发凭证**，申请保持待审、**仍可驳回**；
   - 批准与驳回互斥、终态唯一，并发复核由申请行行锁串行化（落选方 `409`）。
3. **一次性签发**：批准成功在**同一事务**内生成随机凭证（`secrets.token_urlsafe(32)`）、写入绑定的冻结清单并终结申请；任一步失败整体回滚。响应返回 `bundle_id`、`credential`、冻结 `snapshot_ids`、`expires_at` 与 `max_reads`。
4. **公开读取**：`GET /public/snapshots/{snapshot_id}?token=<共享凭证>`——与单快照令牌**同一入口、同一响应结构、同一模糊 `403 snapshot unavailable`**：
   - 凭证只能读取**清单内**快照；读取清单外快照（含快照不存在）→ `403`，不扣额度；
   - 每次**成功**读取扣一次**共享**额度（`reads_used` 跨清单累计，只增不减）；并发读取由同一条条件 `UPDATE` 原子判定并递增，成功总次数绝不超过 `max_reads`；
   - 清单内**某项快照后来撤回**：只拒绝该项（快照行锁阶段即 403），**不扣额度**，其他项仍可凭同一凭证读取；
   - **到期、撤销或额度耗尽**时，清单内全部快照一律 `403`；这些失败请求都不消耗额度。
5. **维护侧（服务密钥）**：`GET /read-bundles` 与 `GET /read-bundles/{bundle_id}` 返回凭证清单（每项含 `snapshot_id`、`record_id`、`version`、`position` 与当前 `status`/`withdrawn_at`）、`max_reads` / `reads_used` / `remaining_reads` / `active` 与到期、撤销时间，**不含任何原文**；`POST /read-bundles/{bundle_id}/revoke` 撤销凭证（幂等），撤销提交后新开始的读取全部 `403`。
6. **与既有能力兼容**：原有单快照令牌的读取、计数、查询（`/tokens/{token}`）、撤销与受控/直接发布接口的行为与响应**保持不变**；共享凭证是独立命名空间，不会出现在 `/tokens` 中，单快照令牌也不受共享额度影响。

### 调用示例

```bash
# 1. operator（alice）提交：冻结两个已发布快照 + 共享总额度 5（可另加 expires_in_seconds/expires_at）
curl -sS -X POST http://localhost:8000/bundle-requests \
  -u alice:alice-pw -H "Content-Type: application/json" \
  -d '{"snapshot_ids": ["<SID1>", "<SID2>"], "max_reads": 5}'
# => {"request_id":"<BRID>","status":"pending","snapshot_ids":["<SID1>","<SID2>"],
#     "requested_by":"alice","max_reads":5,"created_at":"..."}

# 申请详情：冻结清单与额度（不含原文）
curl -sS http://localhost:8000/bundle-requests/<BRID> -u alice:alice-pw

# 2. 申请人不得自审
curl -sS -X POST http://localhost:8000/bundle-requests/<BRID>/approve -u alice:alice-pw
# => 403 {"detail":"the requester may not review their own bundle request"}

# 3. 另一人 bob（reviewer）批准：重新核对清单全部有效后一次性签发凭证
curl -sS -X POST http://localhost:8000/bundle-requests/<BRID>/approve -u bob:bob-pw
# => {"request_id":"<BRID>","status":"approved","reviewed_by":"bob",
#     "bundle_id":"<BID>","credential":"<CRED>","snapshot_ids":["<SID1>","<SID2>"],
#     "expires_at":null,"max_reads":5}

# 4. 凭凭证在既有公开接口读取清单内脱敏正文（两个快照共用 5 次额度）
curl -sS "http://localhost:8000/public/snapshots/<SID1>?token=<CRED>"
# => {"record_id":"<RID1>","version":1,"status":"published","redacted_text":"……"}
curl -sS "http://localhost:8000/public/snapshots/<SID2>?token=<CRED>"
# 清单外快照（或不存在）=> 403 {"detail":"snapshot unavailable"}，不扣额度

# 5. 维护侧查询清单、额度与状态（不含原文）
curl -sS http://localhost:8000/read-bundles/<BID> -H "X-Service-Key: dev-service-key-change-me"
# => {"bundle_id":"<BID>","credential":"<CRED>","max_reads":5,"reads_used":2,
#     "remaining_reads":3,"active":true,"expires_at":null,"revoked_at":null,
#     "items":[{"snapshot_id":"<SID1>","record_id":"...","version":1,"position":0,
#               "status":"published","withdrawn_at":null}, ...]}

# 6. 清单中某项快照撤回：只拒绝该项、不扣额度，其他项仍可读
curl -sS -X POST http://localhost:8000/snapshots/<SID2>/withdraw \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"reason":"该版本不再对外提供"}'
curl -sS "http://localhost:8000/public/snapshots/<SID2>?token=<CRED>"   # => 403 snapshot unavailable（不扣额度）
curl -sS "http://localhost:8000/public/snapshots/<SID1>?token=<CRED>"   # => 200（继续可读）

# 7. 撤销共享凭证（撤销后全部 403）；驳回/终态与受控发布语义一致
curl -sS -X POST http://localhost:8000/read-bundles/<BID>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
```

完整端到端演示（含 401/403/404/409/422、申请人回避、批准时清单失效整次失败仍可驳回、清单外读取不扣额度、撤回单项不扣额度、共用额度耗尽与并发不越限、到期与撤销、维护侧查询）：

```bash
./examples/demo_bundles.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW / CAROL_PW 覆盖
```

### 状态码约定（共享凭证接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误 Basic 凭据 | `401`（带 `WWW-Authenticate: Basic`） |
| 已认证但缺少 operator/reviewer 角色 | `403` |
| 申请人复核自己的申请 | `403` |
| 提交含未知快照；未知申请；维护侧查询未知凭证 | `404` |
| 提交/批准命中已撤回快照（批准时任一项失效）、对已终结申请再操作 | `409` |
| 清单少于两个、含重复 ID、`max_reads` 非正整数、到期时间非法、两种到期方式同传 | `422` |
| 凭证读取清单外快照、凭证无效/撤销/到期/额度耗尽、快照已撤回 | 公开侧统一 `403 snapshot unavailable` |

## 读取凭证泄露换发（单快照令牌 / 多快照共享凭证统一入口）

机构发现某枚读取凭证**疑似泄露**（例如令牌或共享凭证出现在不受控渠道）时，可凭服务密钥把它**换发**为一枚新随机凭证，旧凭证随即失效。**单快照读取令牌与多快照共享凭证使用同一个接口**，服务端自动识别凭证类型并返回对应内容。

### 流程与规则

1. **换发（服务密钥）**：`POST /credentials/rotate`，请求头带 `X-Service-Key`，请求体：
   - `credential`：**原凭证**（发布/批准时签发的随机令牌或共享凭证字符串，非空白）；
   - `request_id`：**请求标识**（调用方生成，建议 UUID，非空白，最长 200 字符）。
2. **换发资格**：仅**未撤销、未到期且额度未耗尽**的凭证可换发；否则返回 `409`（`detail.reason` 为 `revoked` / `expired` / `quota_exhausted` / `already_rotated`）。快照本身是否已撤回**不影响**换发资格。
3. **继承语义**：新凭证继承原凭证的
   - **范围**：单快照令牌仍绑定同一快照；共享凭证复制同一份**冻结清单**（顺序不变）；
   - **原绝对到期时刻**（`expires_at`，不延长、不重置；相对到期策略在首次签发时已换算为绝对时刻）；
   - **已用次数**（`reads_used`，只增不减，不因换发重置），因此响应里的**剩余额度**为 `max_reads - reads_used`；单快照令牌不限次时 `max_reads`/`remaining_reads` 为 `null`。
4. **旧凭证立即失效**：换发提交后，旧凭证在公开读取接口一律沿用模糊 `403 snapshot unavailable`，且这些失败请求**不消耗次数**；旧凭证的 `reads_used` 停在换发时刻。新凭证在同一入口 `GET /public/snapshots/{snapshot_id}?token=...` 使用，公开响应结构、模糊 403 与成功扣次规则完全不变。
5. **幂等**：相同（原凭证, 请求标识）重试（例如网络超时后重发）**原样返回首次换发结果**（同一枚新凭证、同一 `rotated_at`，并带 `"replayed": true`），**不会再次换发**。首次成功响应不含 `replayed`（即 `false`）。
6. **并发唯一**：同一原凭证以**不同**请求标识并发或重复换发时，数据库行锁串行化，**仅一次成功**，落选请求返回 `409`（`reason: "already_rotated"`）。
7. **与读取并发的结算**：换发按“先快照行、再凭证行”加锁，与公开读取的加锁顺序一致，换发与同时进行的成功读取**按事务先后结算**——换发提交前已获锁的成功读取计入旧凭证并被新凭证**继承**，换发提交后开始的读取用旧凭证一律 403；成功总次数绝不超过原上限（**不超支、不重置**），两者不会死锁。
8. **撤回的清单快照**：共享凭证冻结清单中**个别快照已撤回时仍可换发**，新凭证复制该清单后，**该项继续不可读**（读它仍 403 且不扣共享额度），其他项继续可读；响应 `items` 中逐项给出当前 `status`（`published` / `withdrawn`）。
9. **链式换发**：换发出的新凭证自身也是普通凭证，可再次凭（新凭证, 新请求标识）继续换发；前几代凭证均不可再读。

### 状态码约定（换发接口）

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误服务密钥 | `401` |
| `credential` 或 `request_id` 缺失、空白或超长 | `422` |
| 原凭证既不是单快照令牌也不是共享凭证 | `404`（`credential not found`） |
| 已撤销 / 已到期 / 额度耗尽；不同请求标识对同一原凭证再次换发 | `409` |
| 相同（原凭证, 请求标识）重试 | `200`（幂等回放，`replayed: true`） |

> 换发不改变维护侧既有能力：旧凭证仍可查询（`GET /tokens/{token}`、`GET /read-bundles/{id}`，返回非空 `rotated_at`、`active=false`），也仍可调用撤销接口（幂等）。

### 调用示例

```bash
# 1. 怀疑令牌泄露：凭服务密钥提交原令牌与请求标识换发
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"credential": "<原令牌TOKEN>", "request_id": "req-20261001-0001"}'
# 单快照令牌 =>
# {"kind":"token","request_id":"req-20261001-0001",
#  "old_credential":"<原令牌TOKEN>","new_credential":"<新令牌NEW>",
#  "snapshot_ids":["<SID>"],"expires_at":"2026-10-01T12:00:00Z",
#  "max_reads":5,"reads_used":2,"remaining_reads":3,
#  "rotated_at":"2026-10-01T05:00:00Z","replayed":false,"items":null}

# 2. 旧令牌立即失效；新令牌在同一公开接口继续使用，沿用剩余额度
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<原令牌TOKEN>"
# => 403 {"detail":"snapshot unavailable"}（不计数）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<新令牌NEW>"
# => 200 脱敏正文（继承已用 2 次，仅剩 3 次）

# 3. 相同（原凭证, 请求标识）重试：原样回放首次结果，不再次换发
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"credential": "<原令牌TOKEN>", "request_id": "req-20261001-0001"}'
# => 同一枚 new_credential，"replayed":true，rotated_at 与首次一致

# 4. 换发共享凭证：同样的请求体，响应 kind=bundle 并带冻结清单逐项状态
curl -sS -X POST http://localhost:8000/credentials/rotate \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"credential": "<原共享凭证CRED>", "request_id": "req-bundle-1"}'
# => {"kind":"bundle", "new_credential":"<新共享凭证>", "snapshot_ids":["<SID1>","<SID2>"],
#     "max_reads":5,"reads_used":2,"remaining_reads":3,"expires_at":null,
#     "items":[{"snapshot_id":"<SID1>","status":"published",...},
#              {"snapshot_id":"<SID2>","status":"withdrawn","withdrawn_at":"...",...}]}
# 清单内已撤回的 <SID2> 继续不可读、读取不扣额度；<SID1> 继续可读

# 5. 不可换发的情况：已撤销 / 已到期 / 额度耗尽 / 已被换发（不同请求标识）=> 409
# => 409 {"detail":{"message":"credential is not eligible for rotation","reason":"expired"}}
```

完整端到端演示（含 401/404/422/409、幂等回放、并发仅一次成功、继承到期/已用/剩余、撤回清单单项继续不可读、与读取并发结算不超支）：

```bash
./examples/demo_rotate.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 成功读取审计（不可变事件 + 维护侧分页查询）

自启用后，机构可审计已发布脱敏快照的**实际成功读取**：单快照令牌或多快照共享凭证**每次成功读取**都与本次额度扣减**在同一事务**内追加一条不可变审计事件。

### 事件内容与规则

1. **只记录成功读取**：事件与额度扣减同事务提交（扣次条件 `UPDATE` 命中后立即写入，任一失败整体回滚，不可能出现“扣了次数没有事件”或反之）；失败请求、清单外快照请求、已撤回快照请求一律沿用模糊 `403` 且**不产生事件、不扣额度**。公开响应结构、模糊 403 与扣次规则完全保持原样。
2. **事件字段**（仅元数据）：
   - `event_id`：事件标识（写入时生成的随机 UUID）；
   - `snapshot_id`：被读取的快照；
   - `occurred_at`：事件时间（数据库事务时间，UTC）；
   - `credential_kind`：凭证类型，`token`（单快照令牌）/ `bundle`（多快照共享凭证）；
   - `chain_id`：**稳定换发链标识**（随机 UUID），不含任何凭证明文。
3. **换发链归一**：每枚最初签发的凭证各有一条链；泄露换发出的新凭证**继承同一 `chain_id`**，因此同一条链上（最初凭证 + 多次换发后的各代凭证）的全部成功读取都归于同一链；旧凭证换发后继续不可读，不会产生新事件。
4. **不可变**：事件只追加；数据库层有 `BEFORE UPDATE/DELETE` 触发器拒绝任何修改与删除（直接 SQL 也会被拒绝）。事件表不存令牌/共享凭证明文，也不存原文或脱敏正文。

### 查询接口（服务密钥）

`GET /snapshots/{snapshot_id}/read-events`，请求头带 `X-Service-Key`，查询参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `start` | 是 | 起始时刻（**含**），必须带时区，如 `2026-10-01T00:00:00Z` |
| `end` | 是 | 结束时刻（**不含**），必须带时区；半开区间 `[start, end)`，允许 `start == end`（结果为空） |
| `limit` | 否 | 每页条数，默认 `100`，范围 `1–500` |
| `cursor` | 否 | 上一页返回的 `next_cursor`（不透明，绑定该快照与时间范围） |

- 按 `(occurred_at, event_id)` 升序**键集分页**：两列写入后均不可变，因此**并发写入期间翻页不会重复或遗漏查询起点前已提交的事件**；查询期间新提交的事件可能留待下一页出现；取满 `limit` 条时返回 `next_cursor`，无更多事件时为 `null`。
- **明确拒绝**：缺少/错误服务密钥 `401`；未知快照 `404`（`snapshot not found`，已撤回快照的历史成功事件仍可查询）；缺参数/无时区时间戳/无法解析/`start > end`/`limit` 越界/游标非法或与本次（快照、时间范围）不一致，一律 `422`。
- 响应只含上述事件字段与分页元数据，**不返回原文、脱敏正文或任何凭证明文**。

### 调用示例

```bash
# 1. 维护侧按快照与时间范围查询第一页（每页 2 条）
curl -sS -G http://localhost:8000/snapshots/<SID>/read-events \
  -H "X-Service-Key: dev-service-key-change-me" \
  --data-urlencode "start=2026-10-01T00:00:00Z" \
  --data-urlencode "end=2026-10-02T00:00:00Z" \
  --data-urlencode "limit=2"
# => {"snapshot_id":"<SID>","start":"...","end":"...","limit":2,
#     "events":[{"event_id":"<EID1>","snapshot_id":"<SID>",
#                "occurred_at":"2026-10-01T05:00:00.123456Z",
#                "credential_kind":"token","chain_id":"<CHAIN>"},
#               {"event_id":"<EID2>","snapshot_id":"<SID>",
#                "occurred_at":"2026-10-01T05:01:00.654321Z",
#                "credential_kind":"bundle","chain_id":"<CHAIN>"}],
#     "next_cursor":"<不透明游标>"}
# 同一令牌泄露换发后，新旧凭证的成功读取 chain_id 相同；事件载荷中找不到任何凭证明文。

# 2. 携带游标翻下一页
curl -sS -G http://localhost:8000/snapshots/<SID>/read-events \
  -H "X-Service-Key: dev-service-key-change-me" \
  --data-urlencode "start=2026-10-01T00:00:00Z" \
  --data-urlencode "end=2026-10-02T00:00:00Z" \
  --data-urlencode "limit=2" \
  --data-urlencode "cursor=<上一页 next_cursor>"
# => 末页 next_cursor 为 null

# 3. 非法请求
# 无时区时间戳 / start>end / limit 越界 / 游标伪造或与参数不一致 => 422
# 未知快照 => 404 {"detail":"snapshot not found"}；缺少服务密钥 => 401
```

完整端到端演示（成功/失败/清单外/撤回不产生事件、多次换发同链、游标分页、401/404/422 拒绝）：

```bash
./examples/demo_read_events.sh   # 可用 BASE_URL / SERVICE_KEY / ALICE_PW / BOB_PW 覆盖
```

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 受控复核用 `SELECT ... FOR UPDATE` 锁定申请行串行化并发批准/驳回，配合终态检查保证同一申请只有一个终态；
  - 公开读取的**有效性判定与次数消耗合并为同一条条件 `UPDATE ... WHERE 未撤销 AND 未到期 AND (不限次 OR reads_used < max_reads)`**，命中行即 `reads_used = reads_used + 1`：行锁串行化并发读取，成功次数绝不超过上限；撤销/到期不命中因而不消耗次数，且优先于“尚有剩余次数”；
  - 撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **快照永久撤回**：
  - `snapshots` 增加 `withdrawn_at` / `withdraw_reason`：`withdrawn_at` 为空即仍公开，非空为撤回终态；撤回原因仅维护侧返回，公开侧永不透露；
  - 撤回是一条条件 `UPDATE ... WHERE withdrawn_at IS NULL`，并发撤回只有一个首次结果，落选方读回首次时间与状态，重复撤回完全幂等；
  - 公开读取先 `SELECT ... FOR UPDATE` 锁定快照行再判定，撤回与读取按快照行锁串行化：撤回提交后开始的读取在计数 `UPDATE` 之前就收到模糊 403，**不返回正文也不消耗次数**；
  - 直接发布/受控批准在写令牌前以 `FOR UPDATE` 取快照并检查撤回状态，提交受控申请在写申请前检查；已撤回版本的重新公开一律 `409`，但撤回前已在审的申请仍可驳回；
  - 撤回不删除任何数据：历史脱敏正文、同记录其他版本快照、已批准申请记录全部留存，仅公开读取被关闭。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；令牌无效/撤销/到期/次数用尽/快照已撤回/与快照不匹配统一返回模糊的 `403 snapshot unavailable`；维护侧令牌查询与受控申请查询只返回元数据与计数，错误信息不含原文内容。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，并支持可选正整数 `max_reads` 读取次数上限；到期、撤销或次数用尽后读取一律拒绝。直接发布与受控批准共用同一套令牌规则、计数、查询与撤销接口。
- **受控发布**：
  - 申请行保存**冻结副本** `frozen_text`/`frozen_intervals`，复核批准基于副本做脱敏，历史版本行即使变化也不影响待审内容；
  - 批准/驳回先 `SELECT ... FOR UPDATE` 锁定申请行，把并发复核串行化：先提交者写入终态，后提交者看到非 `pending` 收到 `409`，配合 `status` 检查约束，保证同一申请只有一个终态；
  - 批准时**快照、新令牌、申请终态在同一事务提交**，任一异常整体回滚，不会留下无主或可读令牌；驳回不产生快照/令牌；
  - 个人凭据（HTTP Basic）与机构服务密钥（`X-Service-Key`）分离，角色为 `operator`/`reviewer`，申请人与复核人身份按用户名强制不同；
  - 既有数据库升级时启动会幂等补列 `records.controlled`（旧记录默认非受控）、`read_tokens.max_reads`/`reads_used`（既有令牌 `max_reads` 为空即继续不限次、已用为 0）、`publish_requests.token_max_reads` 与 `snapshots.withdrawn_at`/`withdraw_reason`（既有快照默认未撤回、继续可读），新表随 `create_all` 幂等创建。
- **多快照共享凭证**：
  - 新表 `read_bundle_requests`（冻结清单 `frozen_snapshot_ids`、共享 `max_reads`、到期策略、申请/复核人与终态）、`read_bundles`（随机 `credential`、共享 `max_reads`/`reads_used`、到期与撤销）与 `read_bundle_items`（凭证-快照绑定与提交顺序）随 `create_all` 幂等创建，不改动既有表；
  - 清单在**提交时冻结**为 JSONB 快照 ID 数组（去重保序，至少 2 项，附 `jsonb_array_length >= 2` 与额度正整数检查约束）；批准时对全部快照行 `SELECT ... FOR UPDATE` **重新核对**仍存在且未撤回，任一失效即回滚本次批准、申请保持待审（仍可驳回）；
  - 凭证、清单项与申请终态在**同一事务**提交，随机凭证一次性签发；驳回不产生凭证；
  - 公开读取沿用快照行锁先判定撤回，再把“凭证有效 + 快照在清单内（`EXISTS` 子查询命中 `read_bundle_items`）+ 未撤销 + 未到期 + `reads_used < max_reads`”与共享计数递增合并为**同一条条件 `UPDATE`**：行锁串行化跨快照并发读取，成功总次数绝不超过共享上限；清单外/失效/到期/撤销/耗尽均不命中因而不扣额度，统一模糊 `403`；
  - 清单内某快照撤回只在该快照的行锁阶段拒绝该项、不触碰共享计数，其他清单项不受影响；撤销凭证是独立的原子条件 `UPDATE`，提交后优先生效；
  - 共享凭证与单快照令牌是**两套独立计数与命名空间**：公开接口先按单快照令牌原子判定，未命中再按共享凭证判定，故既有令牌的响应、计数与 `/tokens` 查询行为完全不变。
- **读取凭证泄露换发**：
  - 新表 `credential_rotations`（`kind` 区分 token/bundle、原/新凭证、请求标识、首次响应快照 JSONB）随 `create_all` 幂等创建；`read_tokens` 与 `read_bundles` 各幂等补列 `rotated_at`（既有凭证默认空、继续可读）；
  - 换发在**同一事务**内：先按快照行（令牌为其快照；共享凭证按冻结清单全部快照、按 ID 排序）`FOR UPDATE` 加锁，再对凭证行 `FOR UPDATE` 加锁并以 `populate_existing()` 读取锁等待后的最新已提交版本——锁序与公开读取的“快照行锁 → 条件 UPDATE”一致，换发与读取**串行化而不死锁**，按事务先后结算已用次数；
  - 新凭证复制原范围（同快照 / 同冻结清单同顺序）、原 `expires_at`、`max_reads` 与 `reads_used`，旧凭证置 `rotated_at` 后提交即不能再读；公开读取的两条条件 `UPDATE` 均增加 `rotated_at IS NULL`，旧凭证的失败读取不计数；
  - `(old_credential, request_id)` 唯一约束保证相同请求重试幂等：命中既有换发记录即回放保存的首次响应（`replayed=true`），不生成新凭证；不同请求标识的并发换发在凭证行锁后看到非空 `rotated_at`，仅一次成功、其余 `409`；
  - 资格（未撤销/未到期/额度未耗尽）在凭证行锁内按锁后最新值判定，换发与读取并发时不会重复扣次或超支；共享清单内已撤回快照不阻止换发（不重新核对清单），该项按既有规则继续 403 且不扣额度。
- **成功读取审计**：
  - 新表 `read_events`（`id`、`snapshot_id`、`credential_kind`、`chain_id`、`occurred_at`，附类型检查约束与 `(snapshot_id, occurred_at, id)` 复合索引）随 `create_all` 幂等创建；`read_tokens`/`read_bundles` 各幂等补列 `chain_id`（既有凭证回填随机 UUID、各自成链，换发时继承同链）；
  - 公开读取的两条条件扣次 `UPDATE` 增加 `.returning(chain_id)`：命中（成功）即在**同一事务**插入事件并一起提交，未命中（任何失败）不插入；事件不含令牌/共享凭证/原文/脱敏正文；
  - `BEFORE UPDATE/DELETE` 行级触发器在数据库层拒绝事件变更与删除，事件写入后不可变；
  - 维护侧查询以服务密钥鉴权，先查快照存在性（未知 `404`，撤回快照历史可查），再校验带时区的半开区间 `[start, end)` 与游标（base64 编码的 JSON，绑定 snapshot/start/end，任何不一致 `422`），按 `(occurred_at, id) > 游标` 键集取 `limit + 1` 行判断下一页——两键均不可变，故并发写入下对起点前已提交事件无重复、无遗漏，查询期间新提交事件可留待下一页。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
# 可选：配置受控发布操作者（不配置则受控接口一律 401，其他功能不受影响）
export REVIEW_CREDENTIALS='[{"username":"alice","password":"alice-pw","roles":["operator"]},{"username":"bob","password":"bob-pw","roles":["reviewer"]}]'
uvicorn app.main:app --reload --port 8000

# 脱敏核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
# 凭据配置校验单元测试
python3 -m unittest tests.test_config -v
# 受控发布端到端集成测试：默认用 pgserver 内置 PostgreSQL 自动建临时库，
# 也可用 TEST_DATABASE_URL 指向已有数据库
python3 -m unittest tests.test_integration_api -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑（受控发布、令牌计数、快照撤回、多快照共享凭证申请-复核与共用额度、凭证泄露换发、成功读取审计与分页查询）
  models.py     # records / record_versions / snapshots(含撤回字段) / read_tokens / publish_requests / read_bundles(+requests/items) / credential_rotations / read_events(不可变审计事件，含换发链标识)
  redaction.py  # 区间校验、合并、替换（纯函数）
  schemas.py    # 请求/响应模型（含撤回、max_reads、多快照共享凭证与泄露换发模型）
  security.py   # 服务密钥与操作者个人凭据（Basic）鉴权、角色依赖
  config.py     # 环境变量配置（含 REVIEW_CREDENTIALS 校验）
  db.py         # 引擎与会话
examples/
  demo.sh             # 直接发布端到端调用示例
  demo_controlled.sh  # 受控发布（申请-复核）端到端调用示例
  demo_withdraw.sh    # 快照永久撤回端到端调用示例
  demo_bundles.sh     # 多快照共享凭证（申请-复核 + 共用额度）端到端调用示例
  demo_rotate.sh      # 读取凭证泄露换发（令牌/共享凭证统一入口）端到端调用示例
  demo_read_events.sh # 成功读取审计（同事务事件、换发链归一、游标分页）端到端调用示例
tests/
  test_redaction.py       # 脱敏逻辑单元测试
  test_config.py          # 操作者凭据配置校验单元测试
  test_integration_api.py # 受控/共享凭证/换发/成功读取审计/既有接口端到端集成测试（真实 PostgreSQL）
Dockerfile / docker-compose.yml
```
