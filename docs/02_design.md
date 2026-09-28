# Template Design Specification — SVC-C2-125 JAMemberInquiryIntakeAgent

> 承認済み提案（2026-07-29）に基づく設計書。
> 本設計書は設計フェーズの成果物であり、実装フェーズの指示書を兼ねる。
> SDK 実体確認: `agenticstar-agentcore==1.0.0`（2026-07-30 実機 introspection・§12）

---

## 1. AgentCore アーキテクチャ上の位置づけ

- **Agent Class**: `JAMemberInquiryIntakeAgent`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承。`AutonomousBaseGraph` ではない）
- **パターン**: DocGenerationAgent 系（非定型入力 → 構造化成果物の生成）
- **三層分離**:
  - **State**: flat TypedDict（`AgentState` を継承）。プリミティブと JSON 直列化可能型のみ。
    Pydantic / dataclass は msgpack 非互換のため禁止
  - **Node**: L1 継承（Template Method）。`execute(self, state) -> dict` のみ override し、
    **変更したキーだけを返す**（full state 返却は `node_history` / `error_log` の reducer を二重化する）
  - **Graph**: composition。`register_nodes()` で 3 スロット（pre_process / main / post_process）を埋める。
    `add_edges()` / `route()` は override しない（framework の固定 backbone に従う）

固定 5 ノード backbone のうち `initialize` / `finalize` は `super().register_nodes()` が自動注入する。

---

## 2. アーキテクチャ概要

### 2-1. ノード構成（outer graph）

| Node | 責務 | 入力 State | 出力 State | 継承 / override |
|------|------|-----------|-----------|----------------|
| initialize | framework 既定 | — | — | `InitializeNode`（既定・注入） |
| pre_process | 入力検証・正規化・intake メタ確定・`inquiry_id` 発行 | `user_input`（相談本文 str・S-2 マスク済） | `validated_input`, `intake_meta`, `status` | `FunctionNode` → `InquiryIntakeValidateNode` |
| main | 内側 domain workflow subgraph を実行（意図分解 → 抽出 → ランキング） | `validated_input`, `intake_meta` | `handoff_packets`, `coverage_gaps`, `degradation_reason`, `provenance_verified`, `status` | `GraphNode` → `IntakeWorkflowGraphNode` |
| post_process | 出力 envelope 組立・逐語照合の再検証・S-3 出力ゲート | 上記 | `formatted_output`, `status` | `FunctionNode` → `IntakePacketEnvelopeNode` |
| finalize | framework 既定 | — | — | `FinalizeNode`（既定・注入） |

### 2-2. データフロー

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max_retry=3)
                                         pre_process
```

`pre_process → main` は無条件辺。`route()` は **main の後**に `state["status"]` を読む
（`success` → post_process / `retry` → pre_process / その他 → finalize）。

### 2-3. 内側 domain workflow subgraph（main スロット）

Cat 2 標準構成として `main` に `GraphNode` を置き、その内側に 3 ステップの subgraph を持つ。
`GraphNode` は `BaseNode` 直系のため S-2/S-3 は既定 no-op（内側 `FunctionNode` の gate が効く）。

```
inner START → seasonal_context → decompose_extract → handoff_rank → inner END
                (決定論)             (LLM)              (決定論)
```

| Inner Node | 種別 | 責務 | 出力キー |
|---|---|---|---|
| `SeasonalContextNode` | 決定論 | **`extract_input()` の JSON envelope を parse し `canonical_text` を inner `validated_input` に復元**（§9-1）。版固定の routing policy / seasonal calendar を読み、業務日から季節ウィンドウと作物別 critical window を確定。`ranking_mode` / `policy_id` / `policy_version` / `policy_digest` / `seasonal_calendar_version` を固定 | `validated_input`, `seasonal_context`, `ranking_basis` |
| `IntentDecomposeExtractNode` | **LLM**（本 AG で LLM を使う唯一のノード） | `canonical_text` から ①意図配列（`type` / `source_span` / `span_offsets` / `confidence` / `related_intent_ids`）②意図別 `extracted_facts` ③`missing_information`（`missing_field` / `ask_target` / **`staff_action`＝職員のタスク**）を JSON 生成。`why_needed` は書かない（policy 定型文・§5-1） | `decomposed_intents`, `llm_degraded` |
| `HandoffRankNode` | 決定論 | 意図 → capability 写像（policy 由来の版固定テーブル）、`seasonal_priority` 付与、`rank` の決定論ソート、**逐語照合（span 検証）**、パケット組立 | `handoff_packets`, `coverage_gaps`, `provenance_verified` |

**LLM を Stage 2 の 1 ノードに閉じる理由**: 順位・優先度・capability 写像を LLM に決めさせない
（設計レビュー指摘「LLM はランキングポリシーを発明しない」への対応）。LLM が担うのは
**①意図の分解 ②事実の抽出 ③不足情報に対する職員タスクの言語化**のみで、いずれも規則化できない自然言語理解。
Stage 1 / Stage 3 は非 LLM のため単体テストで値レベルの検証ができる。

**`rationale` は決定論テンプレ文**（policy の写像ルール ID ＋ 意図 type ＋ 季節ウィンドウ名を埋める）。
LLM に順位の説明を書かせると「季節的に優先すべき」といった判断を発明しうるため、
順位の監査可能性を優先して機械生成に固定する。

---

## 3. State 定義（`src/schemas/state.py`）

`AgentState` を継承した flat TypedDict（`total=False`）。追加フィールドは以下のみ。

| Field | Type | 用途 | 必須 |
|-------|------|------|------|
| `intake_meta` | `dict` | `inquiry_id` / `source_channel` / `caller_ref`（認証済み `caller_id` 由来・§9-4） / `business_date` / `expected_policy_version` | ✅ |
| `seasonal_context` | `dict` | 季節ウィンドウ・作物別 critical window（policy 由来・決定論） | — |
| `ranking_basis` | `dict` | `policy_id` / `policy_version` / `seasonal_calendar_version` | — |
| `decomposed_intents` | `list[dict]` | LLM が出した意図配列（未検証の中間表現） | — |
| `handoff_packets` | `list[dict]` | 検証済みハンドオフパケット配列（出力の核） | — |
| `coverage_gaps` | `list[str]` | 抽出できなかった軸・逐語照合で落とした事実の理由 | — |
| `degradation_reason` | `str` | 縮退理由（`"+"` 結合。LLM 不在・ポリシー欠落など） | — |
| `provenance_verified` | `bool` | 逐語照合（span 検証）を通過したことの宣言（S-3 が必須検証） | ✅（post_process 時点） |

**State 制約（必須）:**
- flat TypedDict のみ（プリミティブ ＋ JSON 直列化可能型）
- JWT / API キー / credential を State に置かない（checkpoint DB 漏洩）
- `InvocationContext` は `config["configurable"]` 経由のみ。State に格納しない。
  ノード内では `InvocationContext.from_state(state)` を使い、直接構築しない
- Pydantic / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- ノードのインスタンス変数（`self._result` 等）に可変状態を持たない（`get_subgraph()` の
  subgraph キャッシュのみ例外）

---

## 4. I/O schema

### 4-1. 入力（`POST /invoke`）

```jsonc
{
  "input": "うちのハウスのトマトの下葉が枯れてきた、あと来週の防除機の予約と、去年の補助金の書類まだ出してないんだけど間に合う？",
  "session_id": "sess-abcdef",
  "context": {
    "source_channel": "chat",            // 任意。chat | line | phone | counter | unspecified
    "business_date": "2026-07-30",        // 任意。季節ウィンドウ判定の基準日（省略時は §4-1-bis の既定）
    "expected_policy_version": "2026-07"  // 任意。指定時のみ policy の実版と照合し不一致なら fail-closed
  }
}
```

### 4-1-bis. adapter → `intake_meta` の契約（実装契約・設計レビュー指摘対応）

**`context` オブジェクト自体を省略できる**（「貼り付けだけで動く」を成立させるため）。
一方 `intake_meta` は**グラフ内では全キーが揃っている前提**で扱う（§6-2 の検証は
adapter が既定値で埋めた**後**の値に対して行う）。両者の橋渡しを入口 adapter が担う。

| 入力 `context` のキー | 省略時に adapter が注入する既定値 | 検証 |
|---|---|---|
| `source_channel` | **`"unspecified"`**（allowlist の正規メンバー） | allowlist = `chat` / `line` / `phone` / `counter` / `unspecified`。範囲外は **422 reject** |
| `business_date` | **`business_timezone`（既定 `Asia/Tokyo`）における当日**を、**adapter 境界で 1 回だけ**確定 | `YYYY-MM-DD` 形式。不正は 422 reject |
| `expected_policy_version` | **注入しない**（`None` のまま＝版照合を行わない） | 与えられた場合のみ文字列として照合（§5-3） |
| `caller_ref` | 認証済み `caller_id` から導出（§9-4）。**クライアントからは受け取らない** | `^[A-Za-z0-9_-]{1,64}$` |
| `inquiry_id` | adapter が英字のみで発行（§9-3）。**クライアントからは受け取らない** | — |

**未知キー・型違いは 422 で reject する**（closed allowlist）。`context` に宣言外のキーがあれば
silently 無視せず拒否する（自己申告の値を素通しさせない・entry-adapter の確立原則と同型）。

🔴 **`business_date` を adapter 境界で 1 回だけ確定する理由**: 季節ウィンドウ（§5）は日付で決まる。
グラフ内の各ノードが「今日」を都度参照すると、①日付境界をまたぐリクエストで**同一実行内に
結果が変わる** ②**ホスト TZ が UTC のコンテナでは JST の日付と最大 9 時間ずれ**、同じ入力でも
季節判定が変わりうる。決定論性（同じ入力 → 同じ順位）を成立させるため、
**`business_date` は `intake_meta` に固定した値のみを参照し、グラフ内で `date.today()` 等を呼ばない**。
`business_timezone` は `config.yaml` の実行時パラメータ（既定 `Asia/Tokyo`）。

- **相談本文は `input`（＝ `user_input`・str）で渡す**。これにより framework の S-2 ゲートが
  `user_input` に対する PII 走査を自動実行する（§9）
- **入力形式は プレーンテキストのみ**。ファイル添付・PDF・Excel は v1 で受け付けない
  （パーサを持たない選択＝攻撃面を作らない。テンプレート共通の既定）
- 本文長は `max_inquiry_chars`（既定 4000）で上限。超過は検証エラーとして
  `status="error"` ＋理由を返す
- **`operator_id` は受け付けない**（§9-4）。受付職員の識別は認証済み `caller_id` から
  サーバ側で導出する。クライアント body 由来の識別子を provenance に載せない

### 4-2. 出力（`formatted_output`）

```jsonc
{
  "schema_version": "1.0.0",
  "inquiry_id": "inq-hzkfmbqa",               // 英字のみの opaque ref（§9-3）
  "text_basis": "canonical_text",              // span_offsets の座標基準（原文座標ではない・§9-1）
  "packets": [ /* 意図 1 件につき 1 パケット。下記 4-3 */ ],
  "coverage_gaps": ["parcel", "deadline"],
  "provenance_verified": true,
  "degradation_reason": ""                      // 縮退時のみ非空
}
```

### 4-3. ハンドオフパケット（承認済み提案 §12-bis のスキーマを実装形に確定）

> **`span_offsets` の単位（実装契約）**: **Unicode code point 単位・0-based・end-exclusive**
> （Python の `str` スライス `canonical_text[start:end]` と完全に一致する）。UTF-8 バイト数でも
> grapheme cluster 数でもない。座標基準テキストは §9-1 の `canonical_text`。
> 絵文字・結合文字・サロゲートペアを含む場合も code point 単位で数える
> （例: `"🌾" == canonical_text[k:k+1]` が成り立つ位置を `start=k` / `end=k+1` とする）。

下記は §4-1 の入力例（NFKC で変化しない・PII を含まない）に対する**実際に検証を通る**例。

```jsonc
{
  "packet_id": "pkt-abcdefgh",
  "intent": {
    "intent_id": "i-02",
    "type": "machinery_booking",              // policy の closed enum（未知値は other_general に正規化）
    "source_span": "来週の防除機の予約",
    "span_offsets": { "start": 22, "end": 31 },   // canonical_text[22:31] == source_span
    "confidence": "high",                     // high | medium | low
    "related_intent_ids": ["i-01"]
  },
  "routing": {
    "capability": "machinery_service",        // 受け手の職能（テンプレート ID ではない）
    "rationale": "意図 machinery_booking を写像ルール map-machinery により machinery_service へ振り分け（季節ウィンドウ: 夏季防除期）",
    "seasonal_priority": "high",              // critical | high | normal | low
    "rank": 2                                 // 同一相談内の相対順位（1 起点・一意・連番）
  },
  "ranking_basis": {
    "ranking_mode": "policy_applied",         // policy_applied | policy_unavailable（§5）
    "policy_id": "ja-routing-policy",
    "policy_version": "2026-07",
    "policy_digest": "sha256:9f2c…",          // 承認済み配布物の同一性（§5-2）
    "seasonal_calendar_version": "2026-summer",
    "mapping_rule_id": "map-machinery"
  },
  "extracted_facts": [
    { "field": "crop", "value": "トマト", "source_span": "ハウスのトマト",
      "span_offsets": { "start": 3, "end": 10 }, "confidence": "high" }
  ],
  "missing_information": [
    { "missing_field": "deadline",                          // §4-4 の enum（必須）
      "ask_target": "member",                               // member | internal_record（enum）
      "staff_action": "会員に防除機の使用希望日を確認する",     // 職員のタスク（LLM 生成・§6-3-8）
      "why_needed": "予約枠の確保に必要です" }                 // policy 由来の定型文（決定論・§5-1）
  ],
  "provenance": { "source_channel": "phone", "caller_ref": "staff-kojima" }
}
```

### 4-4. フィールド制約（S-3 が機械検証する）

| フィールド | 制約 | 根拠 |
|---|---|---|
| `intent.type` | **policy の closed enum ∪ universal 予約値 `other_general`**（`other_general` は policy に依存せず常に有効。policy 側に無くてもテンプレートが補う）。未知値は `other_general` に正規化し `coverage_gaps` に `unmapped_intent:<type>` を記録（LLM の生値を出力に載せない） | 自由文 type は診断・断定の裏口チャネルになる。policy 欠落時にも成立する値が必要（§5-4） |
| `intent.source_span` / `extracted_facts[].source_span` | `canonical_text` の literal 部分文字列 ＋ `span_offsets` 完全一致（§6-4） | 根拠なき意図・事実を出さない |
| `extracted_facts[].field` | allowlist: `crop` / `growth_stage` / `parcel` / `symptom` / `deadline` / `quantity` / `machine` / `program`（policy で拡張可能） | 宣言外 field を出さない |
| `extracted_facts[].value` | **既定は `value == source_span`（完全一致）**。例外は `deadline` / `quantity` のみで、**正規化器が当該 span から決定論的に導出した値と完全一致する場合だけ**正規形（日付 → ISO-8601、数量 → 数値＋単位）を許可する。LLM が書いた `value` はそのまま信用しない。正規化器の前段は **NFKC ＋ 漢数字変換 ＋ 日本語単位同義語の正規化**を必ず通す（`六十二リットル` / `６２Ｌ` が素通りする既知の穴）。ただし span が**否定・不確実性の表現**（`ではない` / `ではありません` / `かどうか` / `不明` / `未定` 等の文型）を含む場合は正規化せず **`value = source_span`** とする | **部分文字列を許すと否定を切り落とせる**: `source_span: "べと病ではない"` ＋ `value: "べと病"` が全検査を通り、否定された病名を症状として出力できてしまう（レビュー High 指摘）。完全一致を既定にすればこの経路は構造的に消える |
| `missing_information[].missing_field` | 上記 field allowlist ∪ **universal 予約値 `unspecified`**（必須） | タスクが「どの軸の不足か」に紐づくことを構造で担保。`unspecified` は縮退時の予約値（§5-4） |
| `missing_information[].ask_target` | enum: `member`（会員に確認する） / `internal_record`（JA 内部の記録・台帳を確認する）（必須） | 職員が「誰に当たればよいか」を機械可読にする。すべてを会員への聞き返しに寄せない |
| `missing_information[].staff_action` | LLM 生成（**核**）。**1 文のみ**（`。` を含まない）・**120 文字以内**・`missing_field` に対応する内容であること | **職員が取る確認アクション**を書く欄（会員へ読み上げる文ではない・§4-6）。文型 allowlist は課さない（§4-6 のユーザー裁定） |
| `missing_information[].why_needed` | **policy の `missing_field` → 定型文 写像から決定論生成**（LLM は書かない） | 「なぜ必要か」は業務ルールでありケース固有の創造性は不要。作文させると理由の捏造になる |
| `confidence` / `seasonal_priority` | enum 完全一致（`high\|medium\|low` / `critical\|high\|normal\|low`） | 確定値（`confirmed` 等）を作れない |
| `provenance.caller_ref` | 認証済み `caller_id` からサーバ側で導出。`^[A-Za-z0-9_-]{1,64}$`。S-3 の PII 再走査対象に含める | クライアント body 由来の識別子を載せない（§9-4） |

### 4-5. `routing.capability` の token カタログ

| 値 | 渡す先の業務 |
|---|---|
| `pest_disease_consultation` | 病害虫・防除の相談（営農指導員 / 防除担当）— **本 AG は診断しない** |
| `machinery_service` | 農機の予約・修理・整備 |
| `procurement` | 資材・種苗の購買 |
| `sales_shipment` | 出荷・販売・集荷の調整 |
| `subsidy_program` | 補助金・制度の案内（**受給資格判定はしない**） |
| `other_general` | 上記に当てはまらない一般相談 |

**型**: 厳格 enum ではなく **lower_snake_case の拡張可能 token ＋ v1 既知値カタログ**。
S-3 は「既知カタログ ∪ `^[a-z][a-z0-9_]{2,39}$` の形式」を検証する（形式違反のみ reject）。
token 追加はマイナー、既存 token の意味変更・削除はメジャー。
**受け手の契約**: 同一メジャーの受け手は未知 token を受理し人間キューへ送る（`other_general` 相当）。

LLM が出した `intent.type` が policy の写像テーブルに無い場合は `other_general` へ fail-safe し、
`coverage_gaps` に `unmapped_intent:<type>` を記録する（LLM の生値は出力に載せない）。

### 4-6. `staff_action` の位置づけと文型制約を課さない判断（2026-07-30 ユーザー裁定）

`missing_information[].staff_action` は **「担当職員が次に取る確認アクション」** を書く欄である。
**会員にそのまま読み上げる文ではない。**

```jsonc
// ✅ 正: 職員のタスクが分かる
{ "missing_field": "program", "ask_target": "member",
  "staff_action": "会員に前回の防除時期と使用薬剤を確認する", "why_needed": "総使用回数の残数確認に必要です" }
{ "missing_field": "parcel", "ask_target": "internal_record",
  "staff_action": "圃場台帳で該当ハウスの区画番号を照合する", "why_needed": "対象圃場の特定に必要です" }
```

**文型 allowlist（疑問文型のみ許可）を課さない理由 — 前提が変わったため:**

初版は「職員が電話口で会員にそのまま読み上げる」想定で `question`（疑問文）としていた。
そのため「指示文・断定文を構造的に落とす」ために疑問文型 allowlist を置いた。
しかし実際の主要シナリオは以下であるとユーザーが確定した（2026-07-30）:

1. **主想定は電話口の即応ではなく、質問チャットを受けて AG を起動するケースがほとんど**
   （即応性が要求される場面ではない → 職員が出力を読んで整理する時間がある）
2. **LLM 出力は必ず職員が確認する。質問者（会員）に直接返る経路ではない**
   → 疑問形に包まれた診断示唆が会員に届くリスクは運用で避けられる
3. 求められる出力は「読み上げ文」ではなく **担当職員のタスクが分かる形**

疑問文型 allowlist は 1 の前提（読み上げ）に依存した制約であり、前提が変わった時点で
**核（職員が次に何をすべきかを LLM が言語化する）を狭めるだけの過剰防御**になる。
そのため撤去し、代わりに以下を安全境界とする:

| 残す安全境界 | 内容 |
|---|---|
| ① 診断・資格判定・予約確定の**フィールドが存在しない** | S-3 closed allowlist（§6-3-1/2）。AG が「診断した」という**構造的主張は作れない** |
| ② `missing_field` / `ask_target` の enum 必須 | 何の不足か・誰に当たるかは機械可読で、自由文に依存しない |
| ③ 1 文のみ（`。` を含まない）・120 文字以内 | 事実節と裁定節を 1 欄に同居させる書き方を防ぐ（複文で判断を混ぜ込む既知の回避への対処） |
| ④ PII 再走査（§6-3-7） | 自由文経路の PII を再チェック |
| ⑤ `why_needed` は policy 定型文（決定論） | 「なぜ必要か」の捏造を防ぐ |

**受容した残余リスク**: `staff_action` に診断的・処方的な言い回し（例「べと病の疑いとして防除を
手配する」）が混じり得る。これは**職員が読む下書きの品質問題**であり、AG が診断を下す設計欠陥では
ない（①により診断はフィールドとして存在しない）。ユーザー裁定により受容（根拠 = 上記 1〜3）。
`docs/07_operation_guide.md` に「`staff_action` は職員向けの下書きであり、会員への回答文ではない」
ことを明記する。

---

## 5. ランキングガバナンス（版固定ポリシー入力）

季節的重要度・capability 写像・専門職キャパシティは、**承認済み・バージョン管理された顧客側の
配布物**として扱う。AG はこれを**入力**として読み、自動更新も学習もしない。

| 入力 | 配置 | 参照 |
|---|---|---|
| routing policy（意図 enum・capability 写像・写像ルール ID・優先度規則・`why_needed` 定型文） | 顧客がデプロイ時に配置 | `config.yaml: routing_policy_path` |
| seasonal calendar（作物別 critical window・季節ウィンドウ名） | 同上 | `config.yaml: seasonal_calendar_path` |

テンプレートは**最小のサンプル**を同梱する（`config/routing_policy.sample.yaml` /
`config/seasonal_calendar.sample.yaml`・MR-B で作成）。サンプルは「利用者が自環境の承認済み
ポリシーに置き換える前提の例」であり、網羅を目的としない（保守アセット最小化）。

### 5-1. policy / calendar の schema（MR-B の実装契約）

```yaml
# config/routing_policy.sample.yaml
policy_id: "ja-routing-policy"
policy_version: "2026-07"                 # 顧客の承認単位
intent_types:                             # intent.type の closed enum（LLM はこの中から選ぶ）
  - id: "pest_symptom_report"
    capability: "pest_disease_consultation"
    mapping_rule_id: "map-pest"
    base_priority: "high"                 # critical | high | normal | low
  - id: "machinery_booking"
    capability: "machinery_service"
    mapping_rule_id: "map-machinery"
    base_priority: "normal"
  # …（procurement / sales_shipment / subsidy_program / other_general）
missing_field_reasons:                    # missing_field -> why_needed 定型文（決定論生成）
  deadline: "予約枠の確保に必要です"
  parcel: "対象圃場の特定に必要です"
  # …（crop / growth_stage / symptom / quantity / machine / program）
priority_escalation:                      # 季節ウィンドウとの相互作用
  - when_intent_has_field: "symptom"
    and_crop_in_critical_window: true
    escalate_to: "critical"
```

```yaml
# config/seasonal_calendar.sample.yaml
seasonal_calendar_version: "2026-summer"
windows:
  - name: "夏季防除期"
    from: "06-01"
    to: "09-15"
    crops_in_critical_window: ["トマト", "きゅうり", "なす"]
```

### 5-2. 順位の決定論導出（実装契約）

1. **`seasonal_priority`** = policy の `base_priority` に `priority_escalation` を適用した結果
   （エスカレーション条件はすべて policy 側の宣言。コードに業務条件を書かない）
2. **`rank`** = 以下の **sort key を辞書順**に適用した昇順。**1 起点・一意・連番**:
   ① `seasonal_priority` の序数（`critical`=0 / `high`=1 / `normal`=2 / `low`=3）
   ② `deadline` として抽出された日付の近さ（抽出できた意図が先。同日なら次のキーへ）。**来週・今月末等の相対表現は AG が日付を推定しないため正規化せず、rank に影響しない**
   ③ **`intent.span_offsets.start` の昇順**（tie-break の最終手段。原文の出現位置＝決定論）
   → **LLM の配列順は順位に使わない**（LLM が実質的に順位を決めることを防ぐ）
3. **`policy_digest`** = policy ファイルの正規化バイト列の `sha256`（`"sha256:<hex>"`）。
   同一性の記録であり、署名検証ではない（署名は顧客の配布基盤の責務）

### 5-3. 版の照合と縮退

- `expected_policy_version` が指定され実版と一致しない場合は **fail-closed**
  （`status="error"` ＋ `error_log` に不一致を記録。誤った版で振り分けを出さない）
- policy / calendar が**欠落または読み込み失敗**の場合は縮退する。このとき
  **`ranking_basis` に予約値を宣言する**（存在しない承認済み根拠を偽装しない）:

```jsonc
"ranking_basis": {
  "ranking_mode": "policy_unavailable",
  "policy_id": "unavailable",
  "policy_version": "none",
  "policy_digest": "none",
  "seasonal_calendar_version": "none",
  "mapping_rule_id": "fallback-unranked"
}
```

  - 全意図の `capability` は `other_general`、`seasonal_priority` は `normal`、
    `rank` は **`intent.span_offsets.start` の昇順**（LLM の配列順を使わない）
  - `degradation_reason="routing_policy_unavailable"` を立てて `status="success"` を維持
    （職員が意図分解と質問だけでも使えることを優先）
  - **S-3 は `ranking_mode` の値で検査を分岐する**（§6-3-5）。`policy_applied` なら policy snapshot と
    完全一致、`policy_unavailable` なら上記予約値のみ許可。これにより「縮退の偽装」も
    「S-3 での不要な失敗」も起こらない

### 5-4. 縮退時の閉じた fallback contract（policy 欠落 / LLM 不在の両方）

policy が無い状態では `intent.type` の enum も `why_needed` の定型文写像も参照できない。そのため
**policy に依存しない予約値だけで構成された閉じた contract** を定義し、S-3 はこの mode に限り
**予約値の完全一致**を許可する（§6-3-5）。

| 場面 | `ranking_mode` | パケットの内容 |
|---|---|---|
| **policy / calendar 欠落**（`routing_policy_unavailable`） | `policy_unavailable` | LLM が分解した意図ごとに 1 パケット。`intent.type` は **`other_general`**（universal 予約値）／`capability` は `other_general`／`seasonal_priority` は `normal`／`rank` は `span_offsets.start` 昇順／`extracted_facts` は逐語照合を通ったものだけ（policy 非依存）／`missing_information` は **予約 1 件**（下記） |
| **LLM 不在 / 呼び出し失敗**（`llm_unavailable`）※policy はある | `policy_applied` | **fallback パケット 1 件**: `intent.type="other_general"`（policy enum にも必ず存在）／`source_span = canonical_text` 全体・`span_offsets = {start: 0, end: len(canonical_text)}`（逐語照合を必ず通る）／`extracted_facts = []`／`missing_information` は policy の `other_general` 用定型文 1 件 |
| **両方**（policy 欠落 ＋ LLM 不在） | `policy_unavailable` | 上記の合成。`missing_information` は予約 1 件 |

**予約 `missing_information` 1 件（policy 非依存・S-3 は完全一致で検査）:**

```jsonc
{ "missing_field": "unspecified",
  "ask_target": "member",
  "staff_action": "会員に用件の内訳と優先したい件を確認する",
  "why_needed": "用件の切り分けに必要です" }
```

- `degradation_reason` は該当する理由を `"+"` で結合（例 `"routing_policy_unavailable+llm_unavailable"`）
- **fallback パケットの span を `canonical_text` 全体にする理由**: §6-4 の逐語照合を必ず通す必要が
  あるため（span を持たないパケットは drop されて出力が空になる）。「原文全体が根拠」＝
  「分解できなかった」ことの正直な表現でもある
- `other_general` / `unspecified` は **テンプレート組み込みの universal 予約値**であり、policy 側に
  無くてもテンプレートが補う。policy 適用時も `other_general` は必須 intent enum として扱う

> **設計原則の出所**: 基準表・統制表の類（振り分けルール・季節カレンダー）は KB に蓄積して
> AG が育てるものではなく、**所有者・版・承認を伴う配布物＝版固定入力**として扱う
> （確立済みパターン。AG が判定基準を自動更新しないことが監査要件）。

---

## 6. セキュリティ設計（S-1 〜 S-5）

### 6-1. S-1 Trust Gate

| Node | `required_trust_level` | 根拠 |
|---|---|---|
| `InquiryIntakeValidateNode`（pre_process） | `VERIFIED_EXTERNAL` | PII を含む会員相談本文を扱う |
| `IntakeWorkflowGraphNode`（main / GraphNode） | `VERIFIED_EXTERNAL` | **明示宣言必須**（`GraphNode` 既定 ANONYMOUS のままだと内側 S-1 と不整合になる既知バグ） |
| `SeasonalContextNode`（inner） | `VERIFIED_EXTERNAL` | ポリシー入力の読み込み |
| `IntentDecomposeExtractNode`（inner） | `VERIFIED_EXTERNAL` | LLM 呼び出し（コスト発生）・本文処理 |
| `HandoffRankNode`（inner） | `VERIFIED_EXTERNAL` | 本文 span の照合 |
| `IntakePacketEnvelopeNode`（post_process） | `VERIFIED_EXTERNAL` | 出力組立 |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` のみ（文字列 enum・int 比較不可）。
全 `FunctionNode` サブクラスでクラス本体に明示宣言する（暗黙の ANONYMOUS 継承は不可）。

### 6-2. S-2 Input Security Gate

`FunctionNode._security_gate_input()` は `@final`。**`_extra_security_gate_input(state)` フックのみ**で拡張する。

framework 既定の PII 走査対象は `('user_input', 'validated_input', 'llm_response')` の **str 型フィールド**
（実機確認済・§12）。相談本文を `user_input`（str）で受けることで既定走査が必ず効く。

`InquiryIntakeValidateNode._extra_security_gate_input()` で追加する検証:

1. 本文の非空・長さ上限（`max_inquiry_chars`）
2. **テキスト品質ゲート**（文字化け・貼り付け事故を reject）: ①制御文字比率 ②置換文字
   （U+FFFD）比率 ③**可読文字比率**（日本語・英数・句読点以外が一定割合を超えたら reject）。
   ファイル入力口を持たない設計でも、**バイナリ断片や Base64 をテキストとして貼り付けられると
   同じ事故が起きる**ため、経路封鎖だけに頼らず入力テキスト自体をゲートする（過去レビューの指摘）
3. `intake_meta` の必須キー検証（`inquiry_id` / `source_channel` / `caller_ref` / `business_date` が
   揃っていること・`source_channel` が allowlist 内か）。**これは adapter が既定値で埋めた後の値に
   対する検証**であり、クライアントに `context` を必須化するものではない（§4-1-bis）。
   欠落は adapter 実装の不具合を意味するので `status="error"`（fail-closed）
4. `business_date` の形式（`YYYY-MM-DD`）と、指定された場合の `expected_policy_version` の形式検証

`execute()` 側では **NFKC 正規化 → `shared.security.detect_pii` の明示的な再実行 → マスク**を行い
`canonical_text` を確定する（§9-1 の手順 2〜4）。これは S-2 hook ではなく `execute()` の責務
（hook は `execute()` の前に走るため、正規化後の PII を潰せない）。

> **自前の日本語 PII サニタイザは実装しない**（テンプレート共通の既定）。`detect_pii` は日本語氏名を検知しない
> 既知欠陥があるが、辞書ベースの自前補完は抜け漏れと過検知だけが残るため入れない。
> 残余リスクと責務分界は §9-2 / `docs/07_operation_guide.md` に明記する。

### 6-3. S-3 Output Security Gate

`FunctionNode._security_gate_output()` は `@final`（既定は result の **top-level str 値のみ** credential 走査）。
**`_extra_security_gate_output(result)` フックのみ**で拡張する。
このフックは **`state` を受け取らない**（実機確認済）ため、判定に必要な情報は `execute()` が
`formatted_output` に**宣言**しておく。

`IntakePacketEnvelopeNode._extra_security_gate_output()` の検証（すべて fail-closed で `raise`）:

1. **出力構造の closed allowlist** — top-level・`packets[]`・`intent`・`routing`・`ranking_basis`・
   `extracted_facts[]`・`missing_information[]`・`provenance` の各階層で**宣言外キーを全 reject**
2. **禁止フィールドの構造排除** — 診断確定（`diagnosis` / `pest_identification` / `confirmed_disease`）・
   受給資格判定（`eligibility` / `eligible` / `subsidy_approved`）・予約確定（`booking_confirmed` /
   `reservation_id`）・確定割当（`assigned_to` / `assignee`）は**どの階層にも存在してはならない**
3. **`routing.capability` の形式検証**（既知カタログ ∪ `^[a-z][a-z0-9_]{2,39}$`）
4. **`seasonal_priority` / `confidence` / `intent.type` / `missing_field` の enum 完全一致**
   （`intent.type` と `missing_field` は policy の closed enum。未知値は reject）
5. **`ranking_basis` の検査は `ranking_mode` で分岐**（§5-3）。照合相手は **`execute()` の申告ではなく、
   ノードに compile 時注入された不変の policy snapshot `self._policy_snapshot`**
   （`policy_id` / `policy_version` / `policy_digest` / `seasonal_calendar_version` /
   `mapping_rule_ids` / `intent_type` enum / `missing_field` enum。読み取り専用の設定であり
   可変状態ではない）:
   - `policy_applied` → `ranking_basis` の各値が **snapshot と完全一致**すること
     （`mapping_rule_id` は snapshot の `mapping_rule_ids` に含まれること）。
     snapshot 自体が未ロード（policy 欠落）なのに `policy_applied` を主張していたら reject
   - `policy_unavailable` → §5-3 の**予約値と完全一致**であること。かつ snapshot が未ロードで
     あること（policy があるのに `policy_unavailable` を偽装していたら reject）
   - `ranking_mode` 自体が欠落・未知値なら reject
   併せて **`rank` の正整数・一意・1 起点の連番**を検査する（順位の欠番・重複を通さない）。
   **`rank` の値そのものの再計算（sort key の適用）は `canonical_text` と policy の両方が必要な
   ため `execute()` 側の決定論検証で行う**（§6-4-6）。hook は形式と snapshot 一致のみを担う
6. **逐語照合の再計算（`provenance_verified` は証跡ではない）** —
   `execute()` が result に **private verification basis**（`_verification_basis`: 座標基準の
   `canonical_text`）を載せ、**hook 側で全 `source_span` / `span_offsets` を再計算して照合**する。
   `provenance_verified` は「execute が照合した」という**申告に過ぎず**、単独では証明にならない
   （bool だけを検査すると `execute()` が未検証パケット ＋ `true` を返して通る fail-open になる）。
   `_verification_basis` が欠落している場合も reject（検算不能な出力を通さない）。
   **hook 内の処理順序（実装契約）**: ①`result` から `_verification_basis` を取り出して
   全 span を再計算・照合 ②`_verification_basis` を **`pop` して除去** ③その後に
   `formatted_output` の closed allowlist（本節 1・2）を検証する。
   → private key は allowlist 検証の**前に消える**ので、宣言外キーとして自らを reject しない
7. **自由文フィールドの PII 再走査** — `missing_information[].staff_action` / `.why_needed` /
   `extracted_facts[].value` / `intent.source_span` / `routing.rationale` /
   `provenance.caller_ref` を `_FREE_TEXT_PATHS`（`*` = list 展開）で**再帰的に**列挙して
   `detect_pii` を適用。主フィールドだけ検査すると脇の自由文が漏れる（他テンプレートで実証済の裏口）
8. **`staff_action` の形式検査** — **1 文のみ**（`。` を含まない）・**120 文字以内**・非空。
   `missing_field` / `ask_target` が enum に一致すること。
   **文型 allowlist（疑問文型のみ許可）は課さない**（§4-6 のユーザー裁定。`staff_action` は
   会員への読み上げ文ではなく職員向けタスクであり、疑問文限定は核を狭めるだけの過剰防御）。
   1 文制約は「事実節と裁定節を 1 欄に同居させる」書き方を防ぐために残す
9. **`value` の原文由来検証** — 正規化後（NFKC）の `value` が正規化後の `source_span` に
   含まれること。自由作文の `value`（例: 「べと病と診断済み」）を通さない
10. **出力量上限**（§11-2 の値）— `packets` 件数 ≤ `max_intents`、意図あたり
    `extracted_facts` ≤ `max_facts_per_intent`、`missing_information` ≤ `max_questions_per_intent`、
    各文字列長 ≤ 上限、`formatted_output` の JSON 直列化サイズ ≤ `max_output_bytes`。
    超過は `execute()` 側で**決定論的に truncate**（rank 昇順で残す・落とした件数を
    `coverage_gaps` に `truncated:<field>:<n>` として記録）し、hook は上限超過を reject

> **denylist は書かない**（テンプレート共通の既定）。「補助金は受給できます」のような断定語彙の禁止リストは
> 言い換えで原理的に閉じず、保守負担と網羅性の誤った期待だけが残る。非結論性は**構造**で担保する:
> ①closed allowlist（判定フィールドを生成不能にする）②禁止キーの再帰排除
> ③`confidence` / `seasonal_priority` / `intent.type` / `missing_field` を enum に固定
> （確定値・自由文 type を作れない）④**`value` は原文由来のみ・`why_needed` は policy 定型文のみ**
> ＝LLM が自由に文字列を書ける経路を `staff_action` の 1 箇所に絞り、そこは
> 「診断・資格判定フィールドが存在しない」構造 ＋ `missing_field` / `ask_target` enum ＋ 1 文制約で
> 囲う（疑問文型 allowlist は §4-6 のユーザー裁定により課さない）。
> S-3 の位置づけは**製品安全の緩和策であり、法令遵守や業務判定の正しさを確立しない**
> （基準の妥当性検証は顧客側の責務）。残余リスクは `docs/07_operation_guide.md` に明記する。

### 6-4. 逐語照合（provenance）

核の出力保証。`HandoffRankNode`（inner・決定論）で実施し、`IntakePacketEnvelopeNode.execute()` で
再計算し、さらに S-3 hook が `_verification_basis` を使って**もう一度再計算**する（§6-3-6）。

0. 🔴 **`source_span` の長さ下限**: `len(source_span) >= min_span_chars`（既定 **2**）かつ
   `span_offsets.end > span_offsets.start` であること。**空文字・1 文字の span を許すと
   `"" in canonical_text` / `canonical_text[0:0] == ""` が常に真になり、照合そのものが
   vacuous（何も検証していない）になる**（他テンプレートで実際に指摘された穴）。
   下限未満は照合不合格として扱う（drop ＋ `coverage_gaps` 記録）
1. `intent.source_span` / `extracted_facts[].source_span` が **`canonical_text`（§9-1）** の
   **literal な部分文字列**であること
2. `canonical_text[span_offsets.start : span_offsets.end] == source_span` が成り立つこと
   （code point 単位・0-based・end-exclusive。同語が複数回出る場合の同定）
3. 不合格の事実は**出力から drop** し、`coverage_gaps` に `unverified_span:<field>` を記録
   （黙って落とさない）
4. 意図の span が不合格の場合は、その意図を縮退させず**意図ごと drop** し、`coverage_gaps` に
   記録する（根拠のない意図を振り分け候補にしない）
5. 全意図が drop された場合は `handoff_packets=[]` ＋ `degradation_reason="no_verifiable_intent"` で
   `status="success"`（職員が原文を見て対応する運用に戻す）
6. **`rank` の再計算**: `IntakePacketEnvelopeNode.execute()` は §5-2 の sort key を
   `canonical_text` と policy snapshot から**再適用**し、パケットの `rank` が一致することを検証する
   （不一致なら `status="error"`）。S-3 hook は state / canonical_text の両方を必要とする再計算を
   行えないため、値の正しさは execute 側、形式（正整数・一意・連番）と snapshot 一致は hook 側で
   二段に分担する

### 6-5. S-4 Audit Logging

`emit_trace_event(event_type, payload, state)`（`shared.utils.audit_logger`）を
**各ノードの `execute()` 内で最低 1 回**呼ぶ。`node_start` / `node_complete` / `node_error` は
framework が `__call__()` で自動発火するため**重複させない**。

| Node | ドメインイベント | payload（PII を含めない） |
|---|---|---|
| pre_process | `inquiry_intake_validated` | `inquiry_id` / `source_channel` / 本文長 / 品質ゲート結果 |
| main（GraphNode） | `intake_workflow_completed` | `inquiry_id` / 意図数 / 縮退有無 |
| inner seasonal | `seasonal_context_resolved` | `policy_id` / `policy_version` / 季節ウィンドウ名 |
| inner LLM | `intent_decomposition_completed` | 意図数 / LLM 使用有無 / fallback 有無 |
| inner rank | `handoff_ranking_completed` | パケット数 / drop 数 / `mapping_rule_id` 一覧 |
| post_process | `handoff_packets_emitted` | パケット数 / `provenance_verified` / `coverage_gaps` 件数 |
| pre_process（検証 reject） | `inquiry_intake_validation_rejected` | 検証理由の件数（本文・値は載せない） |
| inner LLM（部品完了） | `llm_draft_stage_complete` | cited 件数 |
| post_process（envelope 完了） | `post_process_formatted` | mode / gate_kind |

🔴 **失敗・縮退・drop の経路にも必ずドメインイベントを出す**（成功経路だけに置くと、
「静かに落とした」事象が監査に残らない）:

| 事象 | イベント | payload |
|---|---|---|
| 逐語照合の不合格で fact / intent を drop | `provenance_span_rejected` | `inquiry_id` / 対象（intent or field）/ 理由（`too_short` / `not_found` / `offset_mismatch`） |
| S-3 の reject（closed allowlist 違反・enum 違反・上限超過） | `output_gate_rejected` | 違反種別 / 対象キーのパス（**値は載せない**） |
| policy 版不一致で fail-closed | `policy_version_mismatch` | 期待版 / 実版 |
| 縮退（policy 欠落 / LLM 不在 / 全 intent drop） | `degradation_applied` | `degradation_reason` / `ranking_mode` |
| 出力量上限による truncate | `output_truncated` | 対象フィールド / 落とした件数 |
| 上流 `status="error"` のパススルー | `upstream_error_passthrough` | 直前ノード名 |
| 逐語照合の再計算で不一致を検出（S-3 hook 内） | `provenance_violation` | 違反箇所のパス（**span テキストは載せない**） |

**payload に相談本文・span テキストを載せない**（監査ログは PII 保存先ではない）。

### 6-6. S-5 / 供給網・入口認証

- credential を `src/` に置かない。**本 AG は起動時必須の secret を持たない**（§11-1・
  `requires.secrets: []`）。将来 secret が必要になった場合は `ctx.secrets.require("KEY")` で取得し
  `config/agent.yaml` の `requires.secrets` に宣言する。`os.environ` は使わない
  （唯一の例外＝入口 adapter の `INVOKE_AUTH_TOKEN`。`InvocationContext` 成立前の呼び出し元認証）
- LLM の認証情報は `config["llm"]` に注入されるクライアントが保持する（注入する側の責務）。
  テンプレートは API キーを読まない・State に載せない
- `pyproject.toml` の依存は全て `==` 完全ピン（`requires-python` のみ例外）
- 入口 adapter（`src/api/server.py`）は scaffold seed に準拠し、**`/invoke` のみ**を公開する。
  `/ingest`・`/feedback`・`/resume` は**持たない**（蓄積なし・HITL なし）。
  standalone token（`INVOKE_AUTH_TOKEN`）が開けるのは read-only な `/invoke` のみで、
  middleware 由来の識別属性が存在する場合はそれを優先する（bearer による権限昇格の封鎖）

---

## 7. HITL 設計 — 非採用

判定基準は「**グラフ内 suspend の必然性**」（「人間の判断が要るか」で決めると必ず過剰採用になる）。

| 問い | 本 AG |
|---|---|
| ① AG 自身が承認・可否の判定をするか | **しない**（振り分け候補の提示のみ・確定割当は職員） |
| ② 検証失敗を fail-closed で正常終了できないか | **できる**（不足情報は「質問」として `status="success"` で返す） |
| ③ 人間側の応答が数分で返るか | **返らない**（職員 → 会員の聞き返しは数時間〜数日） |
| ④ 非同期経路で人間関与を担保できないか | **できる**（聞き返し結果を持って再 invoke） |

→ `hitl.enabled: false` / `memory_enabled: false`。checkpointer を持たない。

**副次効果**: PB-7・`/resume` 認証・inner/outer checkpointer 2 系統という事故多発箇所を回避でき、
STG の `deploy-stg` も `awaiting_human` 留置にならず `overall: PASS` を目指せる。

`tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` は **exact-name で残す**
（scaffold 同梱・`gate-scaffold-integrity` の必須ファイル）。中身は `hitl.enabled` false により
auto-skip される（PB-7 = 非 HITL のため auto-waived）。

---

## 8. 永続化・蓄積 — 持たない

本 AG は**一次インテーク**に責務を閉じ、KB / DB / ベクタストアを持たない。

- 相談履歴の蓄積・過去相談との照合は本 AG のスコープ外（承認済み提案のスコープ境界）
- したがって tenant/org スコープの fail-closed 統治・`scoped-retrieval` 部品・
  `accumulation-ledger` 部品はいずれも**不適用**
- 出力パケットの保存先は呼び出し元（JA の既存ワークフロー / チケットシステム）の責務
- `memory_enabled: false` のため LangGraph checkpointer を生成しない
  → **checkpoint への本文永続化が構造的に発生しない**

---

## 9. PII・センシティブの取り扱い

### 9-1. `canonical_text` — 座標基準の確定手順（実装契約・最重要）

`span_offsets` は **`canonical_text`** に対する code point オフセットである。
**顧客側の原文（マスク前・正規化前）に対する座標ではない。**

**`canonical_text` の定義（この順序で 1 度だけ確定し、以後どのノードも変更しない）:**

```
1. framework S-2（pre_process の __call__）が user_input の PII を [MASKED] に置換   … 自動
2. pre_process.execute() が NFKC 正規化を適用                                        … テンプレ実装
3. pre_process.execute() が shared.security.detect_pii を明示的に再実行し、
   検出分を [MASKED] に置換（NFKC 後に初めて検出可能になる PII を潰す）              … テンプレ実装
   ※ 置換は **検出 span の後方から前方へ**（`for f in reversed(findings)`）行う。
     前方から置換すると後続の offset がずれる（framework の実装と同じ作法）
4. 3 の結果を canonical_text とし、validated_input に格納する（str）
```

**ステップ 3 が必須である理由（この設計の要）**: framework の S-2 は `execute()` の**前**に走る。
そのため「全角数字の電話番号」のように **NFKC 正規化後に初めて `detect_pii` にマッチする PII** は、
pre_process の S-2 では検出されず `validated_input` に生で残る。すると**後段の
`FunctionNode`（post_process）の S-2 が `validated_input` を再走査してそこで初めてマスクする**ため、
`main` が見たテキストと `post_process` が見たテキストが**食い違い、全 span が照合不合格になって
drop される**。ステップ 3 で先に潰しておけば、後段の S-2 再走査は「検出ゼロ＝無変更」となり
**冪等**になる。

**冪等性を MR-B のテストで固定する**（受入基準 §15-11）:
`detect_pii(canonical_text) == []` かつ、pre_process 後の各ノードの `__call__` 前後で
`state["validated_input"]` が**バイト同一**であることを assert する（変異検査つき）。

**inner subgraph 境界での取り扱い**: `GraphNode.extract_input()` は**単一の str** を inner の
`user_input` に渡すだけで、outer State を自動移送しない（実機確認済）。したがって:

- `extract_input()` は `{"canonical_text": ..., "intake_meta": ...}` の **JSON 文字列**を返す
  （`seasonal_context` は inner の先頭ノードが policy から作るものであり、outer の main 実行前には
  存在しないため envelope に入れない）
- **inner の最初のノード（`SeasonalContextNode`）が envelope を parse し、`canonical_text` を
  inner state の `validated_input`（str）に復元する**
- **`span_offsets` は `canonical_text` の内部座標のみを基準とする。JSON envelope のキー・
  メタデータ・引用符を絶対に数えない**（LLM プロンプトには `canonical_text` 本文のみを渡し、
  「この文字列の先頭を 0 とする」ことを明示する）
- inner の各 `FunctionNode` の S-2 も `user_input`（= envelope 文字列）と `validated_input`
  （= 復元した canonical_text）を再走査するが、ステップ 3 により**無変更**である

出力には `text_basis: "canonical_text"` を明示宣言し、呼び出し元の誤用を防ぐ。
呼び出し元が原文にハイライトを戻す必要がある場合は、`source_span` の文字列一致で
再探索する運用とする（MR-B で `docs/07_operation_guide.md` に記載する）。

### 9-2. 生本文の out-of-state 化（payload_ref）を採らない判断

テンプレート共通の既定では機微本文は入口 adapter で `payload_ref` 化して State から追い出すが、本 AG では**採らない**。

1. `memory_enabled: false` で checkpointer を持たないため、**checkpoint への本文永続化が発生しない**
   （payload_ref 化の主目的が構造的に消える）
2. 本 AG の**価値の核が「原文根拠付きの抽出」**であり、`source_span`（原文断片）は出力に必ず載る。
   本文の out-of-state 化は核と両立しない
3. 代わりに **framework S-2 の PII マスクを本文そのものに効かせ**（§9-1）、AG が読むテキスト・
   出力に載る span の両方をマスク済みに統一する

**残余リスク（正直に記載）**: `detect_pii` は日本語氏名を検知しない既知欠陥があるため、
「山田さんの圃場」のような記述はマスクされずに `source_span` に載り得る。また、電話番号等の
連絡先 PII も日本語に隣接すると detector の単語境界が成立せず検知されない場合がある。
自前の日本語氏名・連絡先辞書は入れない（テンプレート共通の既定）。AG は多層防御の一枚であり、呼び出し元
システム側での保存・表示制御が併用される前提を `docs/07_operation_guide.md` の責務分界に明記する。

### 9-3. 識別子の発行規則

`inquiry_id` / `packet_id` は **英字のみ**（`inq-` / `pkt-` ＋ 小文字英字 8 桁）で発行する。

理由: `uuid4()` 等で**数字を含む** ref を発行すると、その ref が `user_input` / `validated_input` /
`llm_response` に載った瞬間に S-2 の `my_number_jp` / `credit_card` パターンにマッチして
`[MASKED]` へ**破壊的に書き換えられる**（他テンプレートで実測された本番障害）。

### 9-4. 受付職員の識別子はクライアントから受け取らない

`provenance.caller_ref`（受付職員の記録）は **認証済み `caller_id`（`InvocationContext.caller_id`）
からサーバ側で導出**する。クライアント body の `context` で `operator_id` を受け付けない。

理由: クライアント供給の識別子は**未走査の PII チャネル**になる。形式検査（メール・電話形式の拒否）
だけでは日本語氏名（例: `"山田太郎"`）を止められず、それが `provenance` に載って出力される。
本文について認めている日本語 PII の残余リスク（§9-2）とは異なり、これは**設計で消せる不要な新規経路**。

- `caller_ref` は `^[A-Za-z0-9_-]{1,64}$` を満たす非 PII の運用 ID に限定（違反は S-3 で reject）
- S-3 の PII 再走査パス（§6-3-7）に `provenance.caller_ref` を含める
- entry-adapter 部品の確立原則「クライアント header 由来の scope を信用しない」と同型の判断

---

## 10. 適用部品一覧（`parts/`）

MR-B では `parts/tools/apply_part.py` で写経適用し、`verify_part.py` で IDENTICAL / ADAPT を確認する。
部品の import identity は `src.<part>` に統一する（top-level import は非 editable 経路で壊れる）。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.3 | `src/nodes/main_node.py`（`IntakeWorkflowGraphNode`） | `subgraph_factory` = inner `IntakeWorkflowGraph` の生成 factory（lazy＋キャッシュ）／`_parent_config` = `llm` / policy・calendar のパスと解析済み内容 / volume caps / `llm_temperature` / `llm_max_tokens`／`extract_input` = **`{"canonical_text", "intake_meta"}` の JSON 文字列**（`canonical_text` は §9-1 の座標基準そのまま・inner 先頭ノードが parse して復元する）／`mode_specs` = 単一モード `invoke`（`gate_kind="handoff_packets"`・昇格 field は `handoff_packets` / `coverage_gaps` / `degradation_reason` / `provenance_verified`）／`error_strategy` = `propagate`（部分結果を返さない） |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | `format_invoke` = §4-2 の envelope／`required_fields_by_mode` = `invoke`: `schema_version` / `inquiry_id` / `text_basis` / `packets` / `provenance_verified`／`gate_kind_by_mode` = `invoke` → `handoff_packets`／ingest・feedback 系の hook は未使用（単一モード） |
| `llm-draft-stage` | 0.1.0 | inner `IntentDecomposeExtractNode` | `config['llm']` 注入シーム（モデル ID・具体クライアントを `src/` に書かない）／`build_prompt` = 意図分解＋事実抽出＋質問生成の JSON 出力指示（capability を LLM に決めさせない）／`build_fallback` = 単一 `other_general` 意図・facts 空・定型質問 1 件 ＋ `degradation_reason="llm_unavailable"`／`build_evidence` = span 照合対象（`source_span` / `span_offsets`）の受け渡し／`parse_json_object` = §4-3 の schema 検証へ拡張 |
| `evidence-gate` | 0.1.0（strict 逐語版） | inner `HandoffRankNode` ＋ post_process の再検証 | `ProvenanceContract` の各キー名を span 照合仕様へ: `gate_kind="handoff_packets"`／照合対象は「検索レコード ID」ではなく **`validated_input` テキストへの literal 部分文字列 ＋ offsets 一致**／不合格は drop ＋ `coverage_gaps` 記録 |
| `entry-adapter` | 0.3.0 | `src/api/server.py` | **AUTH 部分のみ適用**。`verified trust gate`・`INVOKE_AUTH_TOKEN`（read-only `/invoke` 限定）・client header 由来 scope の禁止・middleware present 判定・予約 caller_id プレフィックス。**scope 分離（tenant/org fail-closed）は不適用**（§8 のとおり KB を持たないため。部品変数名をドメイン実体と誤読しない） |

**不適用と判断した部品**: `payload-store` / `transient-payload-store`（§9-2）・`scoped-retrieval`・
`ingest-row-pipeline`・`ingest-file-sanitizer`（ファイル入力口を持たない）・`feedback-intake`・
`accumulation-ledger`（§8）。

---

## 11. config 設計値

### 11-1. `config/agent.yaml`（静的マニフェスト）

```yaml
agent:
  id: "SVC-C2-125"
  name: "JAMemberInquiryIntakeAgent"
  version: "0.1.0"
  category: "Cat 2"
  industry: "SVC"
  base_type: "AgentBaseGraph"          # L1 直継承（DocGenerationAgent パターン）
  module: "src.graph.graph"
  class: "JAMemberInquiryIntakeAgent"
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    extras: []
    secrets: []                        # 起動時に必須な秘密は無い（下記の理由）
```

> **`requires.secrets` を空にする理由（レビュー High 指摘への対応）**:
> `requires.secrets` は「**起動時に存在しなければ起動できない秘密**」の宣言欄であり、
> ここに載せた鍵は起動時に `ctx.secrets` の provider で解決される必要がある。
> 本 AG の LLM は **`config["llm"]` 注入シーム**（呼び出し側が構築済みクライアントを注入する）で
> あり、**未注入なら deterministic fallback に落ちて動作する**設計（§16-2）。
> したがって LLM API キーは「起動時に必須な秘密」ではない。
> ここに `LLM_API_KEY` を宣言すると、鍵が provider に無い環境（STG の provisional mode 等）で
> **起動そのものが失敗し、宣言した fallback 経路を一度も実行できない**という矛盾が生じる
> （他テンプレートで実際に起動不能事故が起きている型）。
> LLM クライアントの認証情報は**注入する側（呼び出し元 / デプロイ構成）の責務**とする。
> DB / KB を持たないため DSN 系の宣言も無い（§8）。

#### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は Marketplace 専用の注入側である。通常の HTTP / Registry 経路における
上記 fallback 契約は変更しない一方、Marketplace 経路では Azure OpenAI を必須とし、
`AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` の欠落・空・空白を
`provision_secrets()` で起動時に拒否する。runner は `compile()` の後に `provision_secrets()` を呼ぶため、
compile 時点では認証情報を持たない `_AzureMarketplaceLLM` facade を `config["llm"]` に注入する。
facade は各 `complete()` / `stream()` 呼び出しで invocation-bound provider から接続値を解決し、
`AzureOpenAIClient` もそのメソッド内で import する。接続値と client はインスタンスに保持しない。

Marketplace scope は vendored entry adapter の `SCOPE_KEYS` と `STG_DEFAULT_SCOPE_ENVS` だけから組み立てる。
宣言の型・キー完全一致・重複・環境変数名、および各 scope 値の欠落・空・空白を起動時に fail closed とする。
本 AG は永続 KB / DB を持たないため現在の `SCOPE_KEYS == ()` は意図した宣言であり、ダミー scope は作らない。

runner の `input_context` はマージせず破棄し、adapter が `intake_meta`（ランダムな `inquiry_id`、
`source_channel=unspecified`、PII を含まない固定 `caller_ref=marketplace`、設定 timezone で確定した
`business_date`）と宣言済み scope だけから再構築する。これにより `conversation_history` や将来追加される
runner key が S-2 の PII / injection 検査を通らず State や checkpoint に入る経路を作らない。問い合わせ本文は
従来どおり唯一 `user_input` に入り、S-2、NFKC 後の PII 再検査、S-3 の順に通る。Marketplace では
`memory_enabled=false`、`hitl.enabled=false`、`ctx.hitl_allowed=false` に固定し、Graph 自身の
`mode=invoke` 以外（書込・resume）を公開しない。

### 11-2. `config/config.yaml`（実行時パラメータ）

```yaml
max_retry: 3
timeout_s: 45                  # 1 相談あたり LLM 1 往復（分解＋抽出＋質問生成を 1 回で出す）

memory_enabled: false          # HITL 非採用（§7）。checkpointer を生成しない
hitl:
  enabled: false

# --- domain: intake ---
max_inquiry_chars: 4000        # チャット・電話転記の実運用上限を想定した初期値
business_timezone: "Asia/Tokyo"  # business_date 省略時の既定日を決める TZ（§4-1-bis）

# --- domain: output volume caps（§6-3-10。超過は決定論 truncate + coverage_gaps 記録）---
max_intents: 8                 # 1 相談あたりの意図数上限
max_facts_per_intent: 8        # 意図あたりの抽出事実の上限
max_questions_per_intent: 5    # 意図あたりの不足情報（staff_action）件数の上限
min_span_chars: 2              # source_span の下限（空/1 文字は照合が vacuous になる・§6-4-0）
max_span_chars: 200            # source_span / value の 1 件あたり文字数上限
max_question_chars: 120        # staff_action / why_needed の 1 件あたり文字数上限
max_output_bytes: 65536        # formatted_output の JSON 直列化サイズ上限（UI / audit 保護）

# --- domain: ranking policy（版固定入力・§5）---
routing_policy_path: "config/routing_policy.sample.yaml"
seasonal_calendar_path: "config/seasonal_calendar.sample.yaml"

# --- domain: LLM ---
# 実 LLM は config["llm"] で注入する（モデル ID / 具体クライアントを src/ に書かない）。
# 未注入・呼び出し失敗の**いずれの場合も** deterministic fallback に落ちる（§16-2）。
llm_temperature: 0.0           # 抽出タスクのため決定論寄り
llm_max_tokens: 4096           # LLM 応答の上限（過大な JSON 応答による処理・表示負荷を防ぐ）
```

運用パラメータ（`max_inquiry_chars` / 各 volume cap / `timeout_s` / `llm_temperature` /
`llm_max_tokens`）は**テンプレート利用者が自環境で調整する前提の初期値**。

---

## 12. SDK 1.0.0 実機スパイク結果（2026-07-30 実施）

設計が依拠する API を `agenticstar-agentcore==1.0.0` の実 wheel で introspection し確認した。
**設計書の記述はすべてこの実体に合わせている（推測で書いていない）。**

| 確認項目 | 実体 | 設計への反映 |
|---|---|---|
| `InvocationContext` の実フィールド | `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed`。**`tenant_id` / `org_id` は存在しない** | テナントキーを ctx から取らない設計にした（本 AG は §8 のとおりそもそも不要） |
| `AgentBaseGraph.get_output()` | `{"output": state.get("formatted_output") or state.get("result"), "status", "trace_id", "correlation_id", "node_history"}` | 最終出力は **`formatted_output` に畳み込む**（カスタム State 欄は透過しない） |
| `AgentStatus` の値 | `pending` / `success` / `retry` / `error` / `timeout` / `awaiting_human` / `cancelled` | `status` は `AgentStatus.<X>.value`（文字列）で返す |
| `TrustLevel` の値 | `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL`（文字列 enum・int 比較不可） | §6-1 のとおり宣言 |
| `FunctionNode._security_gate_input` | `@final`。既定 PII 走査対象は `_PII_SCAN_FIELDS = ('user_input','validated_input','llm_response')` の **str 型のみ**。検出時は `[MASKED]` に**置換** | 相談本文を `user_input`（str）で受ける設計に確定。`span_offsets` の座標基準を §9-1 に明記 |
| `FunctionNode._security_gate_output` | `@final`。既定は result の **top-level str 値のみ** credential 走査（ネストは見ない）。`_extra_security_gate_output(result)` は **`state` を受け取らない** | ネストの自由文は §6-3-7 で自前再帰スキャン。判定に必要な情報は `execute()` が `formatted_output` に宣言（§6-3-6） |
| `GraphNode` の abstract | `get_subgraph` / `extract_input` / `merge_output` の 3 つ | §2-3 / §10 のとおり実装 |
| PII / credential 検出器の import パス | **`shared.security.detect_pii` / `detect_credentials`**。`framework.security` は **wheel に存在しない**（配布ドキュメントの記載は実体と異なる） | 実装は `shared.security` を使う（自前 regex を書かない＝substrate reuse） |
| `emit_trace_event` | `(event_type: str, payload: dict, state: dict) -> None`（`shared.utils.audit_logger`） | §6-5 のとおり 3 引数で呼ぶ |
| `shared.services.llm` | `anthropic_client` / `base_llm` / `bedrock_client` / `openai_client` が実在 | LLM は `config["llm"]` 注入シームのみ。具体クライアントを `src/` に書かない |

---

## 13. Framework Utilization

### Shared Components Used
- [x] `InvocationContext`（`config["configurable"]` 経由・`InvocationContext.from_state(state)` を使用）
- [ ] ConnectionPolicy（外部サービス接続を持たないため不使用）
- [x] `SecurityViolationError`（S-2/S-3 の違反時）
- [x] **S-2**: `_extra_security_gate_input()` — §6-2
- [x] **S-3**: `_extra_security_gate_output()` — §6-3
- [x] **S-4**: `emit_trace_event()` — §6-5（全ノード最低 1 回）

### Composition Pattern
- **Pattern**: `GraphNode`（inner subgraph）。`RemoteAgentNode` は**持たない**
- **Composition target**: 内側 `IntakeWorkflowGraph`（本テンプレート内・外部 AG を呼ばない）
- **Error propagation strategy**: `propagate`（inner エラー時に部分結果を返さない）

### Import Isolation Confirmation
- [x] Level 0（`agenticstar`）を import しない
- [x] import 対象は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレートを import しない（部品は写経で取り込む）

---

## 14. Design Decision Record

### 14-0. 設計フェーズのレビュー指摘 先回りセット照合

> 実施 2026-07-30。**該当なしも記載する**（照合したのか飛ばしたのかを後から判別できるようにするため）。
> 4 点が開いていたので同じ改版内で閉じた（外部モデルの敵対レビュー 2 ラウンドでは出なかった本プロジェクト固有の指摘）。

| # | 先回り項目 | 該当 | 対処 |
|---|---|---|---|
| 1-① | provenance: **逐語引用の長さ下限**（空文字は `"" in text` が常に真＝照合が vacuous） | 🔴 該当 | §6-4-0 に `min_span_chars`（既定 2）＋ `end > start`。§15-3-③ に下限を外す変異検査 |
| 1-② | provenance: claim→source の数値/属性照合 | ✅ 対応済 | `value == source_span` 完全一致（§4-4）。`staff_action` 本文の数値は照合対象外＝§16-8 に残余リスク明記 |
| 1-③ | provenance: **双方向**（claims→入力 ＋ 本文→claims の網羅性） | ⚠️ 部分該当 | 散文本文を持たず出力は構造化パケットのみ。網羅性の機械保証は不可能なため `coverage_gaps` で明示（§16-9） |
| 1-④ | 抽出は **NFKC ＋ 漢数字 ＋ 単位同義語** | 🔴 該当 | §4-4 の `deadline`/`quantity` 正規化 extractor の前段に必須化 |
| 1-⑤ | `kind` は allowlist（未知値で「空でない」と主張させない） | ✅ 対応済 | `field` / `intent.type` / `capability` / `missing_field` / `ask_target` すべて enum / allowlist |
| 1-⑥ | PB に「**実在するが無関係な span を引用**」ケース | 🔴 該当 | §15-3-b を新設（照合は通るため機械検出不能であることを明示し `span_context_mismatch` を警告記録） |
| 2 | 永続 KB の統治契約（tenant + org 両方 fail-closed 等） | — 該当なし | KB / DB を持たない（§8）。`scoped-retrieval` / `accumulation-ledger` も不適用 |
| 3 | PoC 検証計画を proposal に | — 該当なし | 「Proposal に PoC 計画・買い手実名は書かない」という確定方針が優先 |
| 4 | 継承欄に構成要素を混ぜない | ✅ 対応済 | §1 は `L1 Base: AgentBaseGraph` のみ。`GraphNode` は §2-3 / §13 の Composition Pattern に分離 |
| 5 | 外部ソース確認は**実施日**を明記 | ✅ 対応済 | 外部 API 依存ゼロ。SDK 実機スパイクは §12 に「2026-07-30 実施」 |
| 6 | 入力の allowlist ＋ **品質ゲート**（経路封鎖だけでは足りない） | 🔴 該当 | §6-2 に**可読文字比率**を追加（バイナリ断片・Base64 をテキストとして貼られる事故は経路封鎖で防げない） |
| 追加 | **S-4 を成功経路だけに置かない** | 🔴 該当 | §6-5 に失敗・縮退・drop 経路のイベント表を新設（6 種） |
| S-3 裏口 | allowlist の免除条件に `.*` | — 該当なし | 免除条件を持たない（全キー allowlist） |
| S-3 裏口 | クライアント由来 `payload_ref` を信用 | — 該当なし | `payload_ref` 不使用。クライアント由来値は形式検証＋`operator_id` は受け取らない（§9-4） |
| S-3 裏口 | `_extra_security_gate_output(result)` は `state` を受け取らない | ✅ 対応済 | §6-3-6 の `_verification_basis` 方式 |
| S-3 裏口 | 禁止ガードが主フィールド 1 本 | ✅ 対応済 | `_FREE_TEXT_PATHS` の再帰適用（§6-3-7） |
| 実値化 | `agent.yaml` `base_type`/`class`・README | ✅ 対応済 | `AgentBaseGraph` / `JAMemberInquiryIntakeAgent` / `Inherits: AgentBaseGraph (L1 direct)` |

> **教訓**: 敵対レビュー 2 ラウンド（High 9 / Medium 8）を通しても、**span 長さ下限は出なかった**
> （照合ロジック自体は正しく見えるため）。本プロジェクト固有の過去指摘は外部モデルの代替にならないので、
> この照合は **docs 起草前**に当てる。

### 14-1. 設計判断（Option A / B と裁定）


| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 固定パイプライン（分解 → 抽出 → ランキング）。終了を AG が決めない＝Cat 3 ではない |
| Composition pattern | main に `GraphNode`（inner subgraph） | 3 スロットに直接ノードを置く | **A** | Cat 2 標準。3 ステップを inner に閉じることで outer の 3 スロット制約と独立に段構成を持てる |
| ランキングの決定主体 | LLM が優先度を判断 | 版固定ポリシーから決定論導出 | **B** | 設計レビュー指摘「LLM はランキングポリシーを発明しない」。順位の監査可能性が核 |
| `rationale`（順位の説明） | LLM が散文で書く | 決定論テンプレ文 | **B** | LLM に書かせると判断を発明しうる。写像ルール ID ＋ 季節ウィンドウ名を埋める機械生成に固定 |
| HITL | D6 `interrupt()` を採用 | 非採用 | **B** | §7 の 4 問すべて否定。聞き返しは数時間〜数日で D6 不適合・再 invoke で担保 |
| 蓄積（KB/DB） | 相談履歴を蓄積 | 持たない | **B** | 承認済み提案のスコープは一次インテーク。蓄積はスコープ外 |
| 生本文の扱い | `payload_ref` で out-of-state 化 | S-2 マスク済み本文を State に載せる | **B** | §9-2（checkpointer 無し＋核の出力が原文断片を含む） |
| `span_offsets` の基準 | 顧客原文座標 | マスク済み正規化テキスト座標 | **B** | S-2 の `[MASKED]` 置換で長さが変わり原文座標は必ずずれる（§9-1） |
| S-3 の主防御 | 断定語 denylist | 出力構造の closed allowlist | **B** | denylist は言い換えで閉じない（テンプレート共通の既定）。判定フィールドを構造的に生成不能にする |
| capability の型 | 厳格 enum | 拡張可能 token ＋ 既知カタログ | **B** | 厳格 enum だと受け手が未知値をフォールバック前に弾き、契約が成立しない（承認済み提案 §12-bis） |
| 未知 `intent.type` | error | `other_general` へ fail-safe ＋ `coverage_gaps` 記録 | **B** | 職員が使えることを優先。黙って落とさない |
| ポリシー欠落時 | fail-closed（error） | 縮退（意図分解と質問のみ返す） | **B** | 分解と質問だけでも現場価値がある。`degradation_reason` で明示 |
| `expected_policy_version` 不一致 | 警告して続行 | fail-closed（error） | **B** | 誤った版の振り分けを出す方が危険 |
| 入力形式 | テキスト＋ファイル | テキストのみ | **B** | パーサを持たない＝攻撃面を作らない（テンプレート共通の既定） |
| `canonical_text` の確定 | S-2 のマスク結果をそのまま使う | **execute で NFKC ＋ `detect_pii` 再実行して確定** | **B** | S-2 は execute 前に走るため NFKC 後に初めて検出可能な PII が残り、後段 S-2 の再マスクで座標がずれて全 span が drop される（レビュー High・§9-1） |
| `provenance_verified` の位置づけ | S-3 が bool を検証すれば足りる | **hook が `_verification_basis` で全 span を再計算** | **B** | bool 単独は `execute()` の自己申告で fail-open（レビュー High・§6-3-6） |
| `extracted_facts[].value` | LLM の自由文 / `source_span` の部分文字列を許可 | **`value == source_span` 完全一致（`deadline`・`quantity` のみ決定論正規化）** | **B** | 部分文字列を許すと `"べと病ではない"` → `"べと病"` と**否定を切り落とせる**（レビュー R2 High・§4-4） |
| policy 欠落・LLM 不在時のパケット | 個別に縮退記述 | **§5-4 の閉じた fallback contract（universal 予約値）** | **B** | policy 由来 enum / 定型文が参照できない状態では schema を満たせず、span 無しの fallback は逐語照合で drop される（レビュー R2 High） |
| S-3 の policy 実在性検証 | hook が「実在値であること」を検査 | **hook は compile 時注入の policy snapshot と完全一致を検査。値の再計算は execute** | **B** | hook は state を受け取れないため「実在性」を検査できない。snapshot 照合なら state 不要（レビュー R2 High・§6-3-5 / §6-4-6） |
| 不足情報欄の位置づけ | 会員への読み上げ文（`question`・疑問文型 allowlist） | **担当職員のタスク（`staff_action`）＋ `ask_target` enum。文型 allowlist なし** | **B** | 2026-07-30 ユーザー裁定。主想定はチャット経由で即応性が要らず、LLM 出力は必ず職員が確認するため会員に直接届かない。疑問文限定は「読み上げ」前提に依存した過剰防御で核を狭めるだけ（§4-6） |
| `why_needed` | LLM が書く | **policy の `missing_field` → 定型文写像から決定論生成** | **B** | 「なぜ必要か」は業務ルール。作文させると理由の捏造になる |
| `intent.type` | LLM の自由文 token | **policy の closed enum（未知は `other_general` 正規化）** | **B** | 自由文 type は診断・断定の裏口。capability は受け手契約のため拡張可能 token を維持 |
| policy 欠落時の `ranking_basis` | 空欄 or 実在値を偽装 | **`ranking_mode: policy_unavailable` ＋ 予約値** | **B** | 縮退と S-3 の必須値検査が衝突していた（レビュー High・§5-3 / §6-3-5） |
| `requires.secrets` | `LLM_API_KEY` を宣言 | **空（`[]`）** | **B** | 起動時必須にすると鍵の無い環境で起動不能になり、宣言した fallback 経路を実行できない（レビュー High・§11-1） |
| 受付職員の識別子 | クライアント body の `operator_id` | **認証済み `caller_id` から導出（`caller_ref`）** | **B** | クライアント供給値は未走査の PII チャネル（レビュー Medium・§9-4） |
| 順位の tie-break | LLM の配列順 | **`span_offsets.start` 昇順** | **B** | LLM が実質的に順位を決めるのを防ぐ（§5-2） |

### 14-2. 実装で確定した差分（MR-B・2026-07-30）

MR-B の実装・独立レビュー・**実 LLM 1 回転**を通じて設計を更新した箇所。**実装が正しいので design を
同じ改版で直した**（乖離を残すとレビューで弾かれる）。

| # | 差分 | 経緯 |
|---|---|---|
| 1 | S-4 ドメインイベントを **4 種追加**（`inquiry_intake_validation_rejected` / `post_process_formatted` / `llm_draft_stage_complete` / `provenance_violation`） | 実装が検証 reject・envelope 完了・部品 draft 完了・逐語照合違反を記録していた。design §6-5 の表に追記 |
| 2 | S-3 の `provenance_verified` 検査を **`is True` 厳格化** | 独立レビューで **`False` が素通り**していた（欠落は allowlist で落ちるが False は通る）。design §6-3-6 は「欠落・False なら raise」と要求していたので実装を design に合わせた。**False / 欠落 / 文字列 `"true"` すべて BLOCK** を実機確認 |
| 3 | PII マスクの残余リスクに **電話番号（日本語隣接）**を追記 | framework `detect_pii` の `phone_jp` 末尾 `\b` が日本語隣接で成立せず `detect_pii("連絡先は090-1234-5678です。") == []`。既知の「数値ガードは単位隣接で無効化」と同型。**自前補完は実装せず**（テンプレート共通の既定）限界を固定するテストで可視化（§9-2 / §16-7） |
| 4 | LLM プロンプトに **schema の実体と allowlist を明示**（`extracted_facts` / `missing_information` の要素形・enum 許容値・`staff_action` の制約） | **実 LLM 1 回転で検出**: 初版は schema が空配列（`"extracted_facts": []`）しか示さず、実 LLM は空を返して fallback 予約値で埋まっていた（意図分解しか動かない状態）。`FakeLlm` は完成品を返すため pytest 259 緑・CI 全緑・自己レビューをすべてすり抜けていた。**プロンプト contract テスト**（`build_prompt()` の戻り値文字列を検査・4 変異で有効性確認）を追加して再発を防ぐ |
| 5 | `deadline` / `quantity` の決定論正規化器を実装 | `value != source_span` を一律 drop していたため、設計が許可する ISO deadline / 数値＋単位 quantity が rank に到達しなかった。正規化器が span と `business_date` から導出した値との一致のみを許可する。相対日付は推定せず literal のままとし、rank に影響させない |

> **この 5 件はいずれも「テストが緑」では出ない**。②は本番 config でゲートを直接叩いて、④は実 LLM を
> 注入シームに刺して初めて出た。モック前提のテストは**プロンプトが構造化出力を導けるか**を
> 構造的に検証できない。

### 14-3. MR-B 人間判断項目の裁定（2026-07-30）— **判断項目ゼロ**

正典 `mr_judgment_criteria.md` §1 の MR-B 表（B-1〜B-5）を当てた結果、**🔴 として人間に問う項目は無かった**
（先行テンプレート CandidateFitAndPerformancePotentialReportAgent の「判断項目ゼロの型」— 判断項目を無理に立てず理由付きで「該当なし」と明記して進める）。

| # | 論点 | 裁定 | 根拠 |
|---|---|---|---|
| B-1 | 核心ロジックの名指し | **名指しのみ・業務妥当性は問わない**。人間が読むべき実物 = `config/routing_policy.sample.yaml`（`intent_types` の capability 写像・`base_priority`・`priority_escalation`）／`config/seasonal_calendar.sample.yaml`（夏季防除期 06-01〜09-15・critical window 作物）／`src/nodes/inner_nodes.py` の `HandoffRankNode` の sort key（priority 序数 → deadline → `span_offsets.start`） | 正典 §2-2「**提示対象リストは『サンプル・利用者が拡張可能』位置づけが既定。初期対象の選定自体を 🔴 で聞かない**」（AssetInspectionHistoryAdvisoryAgent / AquacultureEnvironmentalEarlyWarningAgent で 2 連続の同型指摘）。B-1 の趣旨は名指しであって業務判断の依頼ではない |
| B-2 | S-3 の lexicon・網羅性 | **既定適用**。denylist を書かない。非結論性は closed allowlist ＋ enum 固定 ＋ `value == source_span` で構造担保（実機で攻撃 20 種の BLOCK を確認） | 正典 §2-4（2026-07-27 Professional-Services Engagement Estimation-Phase Agent 裁定で既定化） |
| B-3 | 設計と実装の乖離 | **乖離ゼロ**。実装で確定した差分 5 件は §14-2 に記録し design を同 MR で更新済み | orchestrator_review_checklist §4 を両方向で機械確認 |
| B-4 | 未配線・placeholder | **既定適用（🔴 に立てない）**。LLM 未注入 → deterministic fallback、policy は sample。実配線時に解消する事項を `docs/07` に明記 | 正典 §2-7（2026-07-28 確定） |
| B-5 | PII サニタイザの方針 | **既定適用**。FW 標準 `detect_pii` のみ・自前補完なし。**日本語隣接で電話番号・email が非検知になる欠陥は既知**（2026-07-06 に記録し、framework 側へ報告済み）。テンプレ側は限界を固定するテストと docs 記載で対応し、**再報告・再起票はしない** | 正典 §2-4 / §1 B-5（2026-07-24 Outsourcing SLA Deviation RCA Rebuttal-Brief Agent 裁定で自前補完は不採用が既定） |

> **プロセス上の反省**: 当初 B-1 の業務妥当性と B-5 の request:coe 起票可否を 🔴 として提示したが、
> **どちらも正典で既定が決まっており聞くべきではなかった**（§2-2 は同型 2 連続の指摘を受けて明文化
> されていた項目で、これが 3 連続目）。既定が書かれている項目を 🔴 に上げるのは、判断の重複であり
> 聞かれる側の負担になる。**🔴 を立てる前に §2 の既定と memory の既知事項を先に当てる。**

### ユーザビリティ設計レビューで修正した点

エンドユーザー（JA 営農相談窓口の職員・営農指導員）のユースケースを想定して設計を見直し、
以下を修正した:

1. **入力を「相談本文の生テキスト 1 本」に最小化した** — 当初案では意図候補・作物マスタ等の
   構造化入力を呼び出し元に要求していたが、電話を受けながら入力する職員に構造化を求めるのは
   非現実的。**`context` オブジェクトごと省略可能**とし（既定値は adapter が §4-1-bis の契約どおり
   注入する）、**貼り付けだけで動く**ことを最低条件にした
2. **出力を「担当職員のタスクが分かる形」にした**（2026-07-30 ユーザー裁定で初版から変更）—
   `missing_information[]` は `missing_field`（何が不足か）＋ `ask_target`（会員に聞くか内部記録を
   見るか）＋ `staff_action`（職員が取る確認アクション）＋ `why_needed`（なぜ必要か）で構成し、
   職員が受け取ってそのまま次の作業に移れる形にした。初版は「会員にそのまま読み上げる疑問文」
   としていたが、主想定が電話口の即応ではなくチャット経由の非即応であり、出力は必ず職員が
   確認するため、読み上げ文である必要がない（§4-6）
3. **順位の説明を必ず付けた** — `routing.rationale` と `ranking_basis` を必須にし、職員が
   「なぜこの順番なのか」を会員・専門職に説明できるようにした（納得できない順位は職員が覆せる）
4. **縮退時も必ず何かを返す設計にした** — ポリシー欠落・LLM 不在・全 span 照合失敗でも
   `status="success"` を維持し、`degradation_reason` で「なぜ薄いのか」を返す。エラーで
   何も返らないと職員は AG を使わなくなる
5. **抽出できなかった軸を明示した** — `coverage_gaps` で「作物は取れたが圃場は不明」を返し、
   職員が AG の見落としを補える（沈黙して落とさない）

---

## 15. 受入基準（テストで検証する項目・`docs/03_test_spec.md` へ展開）

MR-B では以下を**値レベル**で検証する（キー名の有無だけを見るテストにしない）。

1. **縮退防止**: 3 用件を含む相談 1 件で、**3 パケットが別個に返る**（単一ラベルへ潰れない）
2. **各パケットの必須要素**: `intent`（type ＋ span）／`extracted_facts`（`source_span` ＋
   `span_offsets`）／`missing_information`／`routing.capability`／`ranking_basis` が全て埋まる
3. **逐語照合**: ①存在しない span を含む LLM 出力（fake LLM で注入）が **drop** され
   `coverage_gaps` に記録される ②`span_offsets` がずれた場合も drop される（変異検査）
   ③**空文字・1 文字の `source_span` が drop される**（下限を外す変異でテストが落ちることを確認 —
   照合が vacuous になる穴の回帰）④§4-3 の掲載例（`canonical_text[22:31]` / `[3:10]`）が
   **実際に照合を通る**ことを固定する
3-b. **「実在するが無関係な span」ケース**（過去レビュー指摘の相当物）: `canonical_text` に実在する
   別用件の span（例: `machinery_booking` の intent に `"ハウスのトマト"` を引用）は**逐語照合を
   通ってしまう**。機械的には検出できないため、①この限界を `docs/07_operation_guide.md` に明記する
   ②`intent.source_span` と `extracted_facts[].source_span` が**同一 intent 内で整合するか**
   （fact の span が intent の span と重複するか近傍か）を**警告レベル**で `coverage_gaps` に記録する
   ③受入テストにこのケースを置き、期待動作（照合は通るが `coverage_gaps` に
   `span_context_mismatch` が入る）を固定する
4. **S-3 closed allowlist**: ①baseline（正当な出力）が**通る** ②禁止キー
   （`diagnosis` / `eligibility` / `booking_confirmed` / `assigned_to`）が top-level ・
   `packets[]` 内・**ネスト内**のいずれに混入しても reject される
5. **S-3 の逐語再計算が真に効く（fail-open 検査）**: `execute()` が **未検証パケット ＋
   `provenance_verified: true`** を返すケースを注入し、S-3 hook が `_verification_basis` を
   使った再計算で **reject する**ことを証明する（bool を信じていないことの証明）。
   `_verification_basis` 欠落でも reject。かつ**正常時は `_verification_basis` が
   最終出力から消えている**ことを assert
6. **自由文の裏口封鎖**: ①`value: "べと病と診断済み"`（`source_span` と不一致）→ reject
   ②**否定の切り落とし**: `source_span: "べと病ではない"` ＋ `value: "べと病"` → reject
   （`value == source_span` 既定の要点。`deadline` / `quantity` の正規化例外も、否定・不確実性
   文型を含む span では正規化されないことを検証）
   ③`staff_action` が複文（`。` を含む）→ reject ④`staff_action` が 120 字超 → reject
   ⑤`missing_field` / `ask_target` が enum 外 → reject
   ⑥`intent.type` が policy enum ∪ `other_general` 外 → reject
   （疑問文型 allowlist は課さないため「疑問文でない」ことを理由に落とすテストは置かない・§4-6）
7. **capability 形式検証**: 未知だが形式適合の token は通り、形式違反（大文字・記号）は reject
8. **`rank` の健全性**: 1 起点・一意・連番。`seasonal_priority` 同値のときの tie-break が
   **`span_offsets.start` 昇順**であること（LLM の配列順を入れ替えても順位が変わらないことを検証）
8-b. **adapter → `intake_meta` 契約（§4-1-bis・設計レビュー指摘対応）**:
    ①**`context` を丸ごと省略した invoke が success** し、`source_channel == "unspecified"` /
    `business_date` = `business_timezone` の当日 / `expected_policy_version is None` になる
    ②`context` に**未知キー**（例 `operator_id`・`tenant_id`）を入れると **422 reject**
    ③`source_channel` が allowlist 外（例 `"fax"`）／型違い（数値）で **422 reject**
    ④`business_date` が不正形式（`"2026/07/30"`）で **422 reject**
    ⑤🔴 **TZ 非依存**: プロセス TZ を `UTC` に固定しても `business_date` が `Asia/Tokyo` 基準の
    当日になる（JST 早朝・深夜の境界時刻を freeze して検証）
    ⑥🔴 **グラフ内で日付を再計算しない**: `intake_meta["business_date"]` を過去日に差し替えた
    invoke で、季節ウィンドウがその過去日基準で決まる（`date.today()` を呼んでいたら失敗する変異検査）
9. **ポリシー版不一致**: `expected_policy_version` 不一致で `status="error"`（fail-closed）
10. **ポリシー欠落の縮退と S-3 の整合**: `status="success"` ＋
    `degradation_reason="routing_policy_unavailable"` ＋ `ranking_basis.ranking_mode ==
    "policy_unavailable"` ＋ **予約値**。この出力が S-3 を**通る**こと（縮退が S-3 で落ちない）。
    併せて **両方向の偽装を reject** すること: ①policy 未ロードなのに `policy_applied` を主張
    ②policy がロード済みなのに `policy_unavailable` の予約値を主張
11. **縮退 fallback contract（§5-4）の 3 ケース**: ①policy 欠落 ②LLM 不在 / 呼び出し失敗
    ③両方 — いずれも `status="success"` ＋ パケットが**逐語照合を通る**こと
    （fallback の `span_offsets` が `{0, len(canonical_text)}` で照合成功する）＋ 予約
    `missing_information` が S-3 の完全一致検査を通ること
11-b. **`rank` の execute 側再計算**（§6-4-6）: パケットの `rank` を人為的にずらした入力で
    `status="error"` になること（形式は正しいが値が sort key と不一致のケース）
12. **S-1 境界（TC-08）**: 全 privileged ノードを `node(state)`（`__call__` 経由）で呼び、
    trust 不足時に **`execute()` が呼ばれない**ことを証明する
13. **`canonical_text` の冪等性（§9-1 の要）**: ①**全角**電話番号（`０９０－１２３４－５６７８`）を
    含む相談で、pre_process 後に `detect_pii(canonical_text) == []` であること
    ②pre_process 以降の各ノードの `__call__` 前後で `state["validated_input"]` が**バイト同一**
    （後段 S-2 の再走査で書き換わらない）③その相談に対する `span_offsets` が整合すること。
    ステップ 3（NFKC 後の再マスク）を外す**変異検査**で、このテストが実際に落ちることを確認する
14. **inner 境界の座標基準**: `extract_input()` の JSON envelope を経由しても
    `span_offsets` が **`canonical_text` の内部座標**であること（envelope のキーや引用符を
    数えていないこと）を、envelope の前置部分の長さを変えた変異検査で固定する
15. **識別子の S-2 耐性**: 発行した `inquiry_id` / `packet_id` が `[MASKED]` に書き換えられない
    （英字のみ規則の回帰テスト）
16. **出力量上限（§6-3-10）**: 上限超過の LLM 応答（意図 20 件・fact 50 件・長大文字列）を注入し、
    ①`execute()` が rank 昇順で決定論 truncate する ②`coverage_gaps` に `truncated:*` が入る
    ③`formatted_output` が `max_output_bytes` 以内 ④上限超過の出力は hook が reject する
17. **PB-1〜PB-6**: 全ノード動的スキャン（`pkgutil.walk_packages`）で trust level 宣言と
    `emit_trace_event` 発火を検証（合成ノード 1 個への置換は不可）。PB-3 は外部サービスを
    持たないため「ポリシー入力ファイルの読み込み境界」で代替し、その旨を test_spec に明記
18. **PB-7**: `hitl.enabled: false` により auto-skip（非 HITL のため auto-waived）
19. **startup 統合テスト**: `with TestClient(app):` で `startup()` を実行し、manifest / config の
    値が service に届くことを assert（CI の `run-tests` は startup を一度も実行しないため）

### 実装時に必ず当てる既知パターン（実装前照合の結果・2026-07-30）

`pattern_catalog.md` の MR-B 系パターンを本テンプレートに照合した結果、以下を実装契約に加える。

| # | 既知パターン | 本テンプレへの適用 |
|---|---|---|
| 20 | **config が飾りになる**（宣言済みキーをコードが読まずモジュール定数を使う） | `min_span_chars` / `max_*` / `business_timezone` / `routing_policy_path` / `llm_temperature` / `llm_max_tokens` は**すべて `config.yaml` から読む**（モジュール定数はデフォルトのみ）。**X-1 テスト**: config の値を実際に変えて（`min_span_chars=50` / `business_timezone="UTC"` / `max_intents=1`）end-to-end invoke の挙動が変わることを assert。static grep では不十分 |
| 21 | **PB-1 / TC-05 は framework 自動イベントを見ると必ず空証明**（`node_start` / `node_complete` は `__call__` が必ず出すので、ドメイン emit を全削除しても緑） | 各 node module の `emit_trace_event` **binding を patch** して**ドメインイベントの発火を実測**する。§6-5 の成功系 6 種 ＋ 失敗・縮退系 6 種の**すべて**をテストで踏む。変異検査（emit を消すと落ちる）で有効性を確認 |
| 22 | **fail-closed テストは provisioned 対照実験とセット**（`pytest.raises` だけだと state 形不足の `KeyError` で緑になり境界を何も検証しない） | policy 版不一致・S-3 reject・trust 不足の各テストは「**正常に通る対照ケース**」と対で書く。`pytest.raises(Exception)` のような広い捕捉を書かない（型を特定する） |
| 23 | **vendored part の絶対 self-import が editable install に隠れる**（二重アイデンティティ） | 写経部品の `__init__.py` は**相対 import**。消費側と repo テストは **`from src.<part>` に統一**。PB テストは 2 本立て（①`src/` の AST スキャンで vendored パッケージの top-level import を検出 ②`repo/src` を sys.path から外したサブプロセスで `import src.graph.graph`）＋変異検査 |
| 24 | **部品版マーカーが実在しない組み合わせを名乗る** | `apply_part.py` 適用後に `verify_part.py` で IDENTICAL を確認し、`# PART:` マーカーの version と sha が**実在する組み合わせ**であることを `parts/` の PART.yaml で照合する |
| 25 | **写経部品の lint 違反はテンプレ側で塗らない** | `ruff check src tests` で写経部品由来の違反が出たら、**まず上流 `parts/` master を開いて確認**（上流で直っていれば同期して byte-identity を回復）。上流にも無ければ parts の MR を切る。ローカル修正は最後の手段 |
| 26 | **「静かに何もしない」バグは CI 緑をすり抜ける** | `ranking_mode` / `intent.type` / `missing_field` / `ask_target` のような**意味を持つフィールドの値や意味を変えたら、そのフィールドで分岐している全箇所を grep** する。新規テストは変異検査（修正前コードで赤くなるか）で有効性を確認してから信用する |
| 27 | **著者モデルは PB を「弱い等価物」に置換する**（全ノード動的スキャン → 合成ノード 1 個。緑で件数も増える） | PB テストが全面書き換えされていたら `grep -c "src.nodes"` 等で**覆う対象が変わっていないか数える**（push 前レビュー項目） |
| — | checkpoint スクラバ（allowlist） | **該当なし** — `memory_enabled: false` で checkpointer を持たない（§8） |
| — | 日本語氏名サニタイザ（辞書＋役割語の二枚重ね） | **該当なし** — 共通方針により自前補完は実装しない（§6-2 / §9-2）。残余リスクは運用ガイドへ |
| — | 永続 KB の統治契約・scope 分離 | **該当なし** — KB / DB を持たない（§8） |

### MR-B の文書側の完了条件（現時点で未作成のもの）

`docs/07_operation_guide.md` は scaffold の空欄のままである。MR-B で**以下 3 点を実記載する**
（本設計書が運用側へ渡す前提としているため、記載されるまで前提は成立していない）:

1. **日本語氏名 PII の残余リスクと責務分界**（§9-2）— AG は多層防御の一枚であり、呼び出し元
   システム側の保存・表示制御が併用される前提
2. **`staff_action` は職員向けタスクの下書きであり会員への回答文ではない**（§4-6 / §16-7）—
   そのまま会員へ転送しない運用であること
3. **`span_offsets` が原文座標でないこと**と、原文ハイライトに戻す場合の再探索運用（§9-1）
4. **policy / calendar の配布・版管理・障害時の責務**（§5）— 誰が承認し、欠落時に何が起きるか
   （縮退動作）／`expected_policy_version` の運用方法

併せて `config/routing_policy.sample.yaml` / `config/seasonal_calendar.sample.yaml`（§5-1 の schema）
も MR-B で作成する。

---

## 16. 既知の限界（`docs/07_operation_guide.md` にも記載する）

1. **日本語氏名および日本語隣接の連絡先 PII は検知されない場合がある** — framework `detect_pii`
   の既知欠陥。自前辞書は入れない（§9-2）。呼び出し元システム側の保存・表示制御が併用される前提
2. **LLM 未注入 / 呼び出し失敗時は決定論 fallback** — `config["llm"]` が注入されない環境
   （STG provisional 等）**および**注入済み LLM の呼び出しが失敗した場合の**両方**で、意図分解が
   単一 `other_general` に縮退し `degradation_reason="llm_unavailable"` を立てる。
   `requires.secrets` を空にしているのはこの経路を実行可能に保つため（§11-1）。
   実配線時に解消する事項として運用ガイドに明記する
3. **`span_offsets` は原文座標ではない**（§9-1）。呼び出し元が原文にハイライトを戻す場合は
   `source_span` の文字列一致で再探索する
4. **capability は振り分け候補**であって確定割当ではない。配送・担当者決定は運用側
5. **季節的重要度の妥当性は顧客ポリシーの妥当性に等しい** — AG はポリシーを検証しない。
   カレンダーの内容は JA 側の承認済み配布物であることが前提
6. **IPMとの接続は自動的には成立しない** — 本 AG は capability パケットを返す
   のみで下流を参照しない。接続には受け手側での採用またはアダプタの実装・検証が別途必要
7. **`staff_action` には診断的・処方的な言い回しが混じり得る（受容済みの残余リスク・
   2026-07-30 ユーザー裁定）** — 例: 「べと病の疑いとして防除を手配する」。
   `staff_action` は LLM が自由に書く唯一の欄であり、文型 allowlist を課さないため
   （§4-6）自然言語の言い換えを機械的に閉じることはできない。
   **受容の根拠**: ①主想定はチャット経由で即応性が要らず、**LLM 出力は必ず職員が確認する**
   （会員に直接届く経路ではない）②診断・資格判定・予約確定は**フィールドとして存在しない**
   （S-3 closed allowlist）ため AG が「診断した」という構造的主張は作れない
   ③`staff_action` は職員向けの下書きであり、会員への回答文ではない。
   運用側への申し送り: `docs/07_operation_guide.md` に「`staff_action` は職員向けタスクの下書き
   であって会員への回答文ではない・そのまま転送しない」ことを明記する（MR-B）
8. **`staff_action` に書かれた数値・固有名の忠実性は担保されない** — 逐語照合が保証するのは
   `source_span` / `extracted_facts[].value` の原文一致であって、**`staff_action` の本文に LLM が
   書いた数値（「3 日以内に」等）や名称は照合対象外**（既知: 引用の忠実性と本文の忠実性は
   別の性質）。期限・数量は `extracted_facts` の `deadline` / `quantity` を正とし、
   `staff_action` はあくまで職員向けの下書きとして扱う旨を運用ガイドに明記する
9-bis. **`deadline` による順位付けは LLM の抽出に依存する** — sort key の第 2 キー（`deadline` の近さ）が
   効くのは、LLM が `deadline` fact を返し、かつその span が**絶対日付**で正規化できた場合に限る。
   相対表現（来週・今月末）は §4-4 のとおり AG が推定しないため正規化されず、LLM が `deadline` を
   抽出しない試行もある（実 LLM 1 回転で実測）。**未発動でも害はない**（第 1 キーの `seasonal_priority` と
   第 3 キーの `span_offsets.start` で決定論的に順位が決まる）。
   正規化と順位反映の実装自体は検証済み（`docs/03_test_spec.md` の該当行）。
   **これ以上プロンプトを厚くして LLM に抽出を強制しない** — 期限表現は無限にあり、
   追い続けると「AG が日付を推定する」方向（事実主張の捏造）に倒れるため
9. **意味的な取り違えは機械検出できない** — 実在するが無関係な span を引用しても逐語照合は通る
   （§15-3-b）。`span_context_mismatch` の警告は補助であって保証ではない

## 共通部品リフレッシュの判断記録（2026-08-14）

| # | 論点 | 判断 | 反映先 |
|---|---|---|---|
| R-1 | 部品の版上げ | `cat2-graph-skeleton` 0.1.4 / `entry-adapter` **0.4.0** / `evidence-gate` **0.3.0** / `llm-draft-stage` **0.2.0**（`output-envelope` 0.1.2 は最新のため無変更） | `src/` 各部品（`verify_part` SUSPECT 0） |
| R-2 | **`SCOPE_KEYS` を `()` へ**（区画なしの明示宣言） | **既定適用**。この AG は KB も永続化も持たず、旧宣言 `("production_company_id", "project_id")` は**出自 AG（Shooting Plan Risk Advisory & Incident History Agent＝映像制作）の語彙が写経時に残ったもの**で、`src/` のどこからも参照されていなかった。entry-adapter v0.4.0 は空タプルを正式な「区画なし」宣言としてサポートし、USAGE.md も「区画が無いドメインでダミー scope を作るな」と定める。判断根拠は `parts/TESTING.md` N-7 に従い server の docstring に記載 | `src/entry_adapter/auth.py`・`src/api/server.py` |
| R-3 | entry-adapter の配線 | 独自実装 `_trusted_request()` を公開 API `authenticate(request, operation=Operation.READ_INVOKE)` に置換（**部品を vendored しながら本番経路が未配線**だった）。operation は endpoint 名でなく実際の副作用で判定 — この AG は DB/KB 書き込み・feedback・resume を持たない | `src/api/server.py` |
| R-4 | manifest | AgentRegistry 準拠の root-level 形式へ移行。runtime 候補キーは 0 件で既存 `config.yaml` は HEAD と byte 一致＝移設漏れなし | `config/agent.yaml` |
| R-5 | PB-7 の世代 | scaffold 正本（`config/config.yaml` を読む世代）へ写経し直し。旧世代は `agent.yaml` を読むため、hitl の置き場を移すと**将来 HITL を有効化しても永久に SKIPPED（偽の緑）**になる | `tests/proof_of_boundary/` `scripts/` |

### 独立レビュー R1 の反映（2026-08-14）

著者と別セッションのレビュアーによる敵対レビュー。**High 0 / Medium 3 / Low 2**。

| 深刻度 | 指摘 | 対応 |
|---|---|---|
| **Medium-1** | **実 LLM に offset 計算を任せている**ため、外すと intent ごと drop され `no_verifiable_intent` に縮退する（中核が空回り）。プロンプトは「code point offset」とだけ指示し、end-exclusive の明記も日本語の例も無く、例は `{start:0,end:0}` | **受入** — LLM 契約を逐語 `source_span` + `occurrence_index` に変更し、**Python 側で end-exclusive offset を決定**。実 LLM 2 回連続で packets 3 件・縮退なしを確認。変異検査で旧契約に戻すと 3 件赤 |
| **Medium-2** | provider 障害の `error_code` が実応答まで届かない（State にフィールドが無く inner→outer の射影で消失）。実測で `{'status':'error','error_code':None,'error_log':None}` ＝運用者に理由が伝わらない | **受入** — `error_code`/`error_type`/`error_log` を State・inner/outer graph・公開応答まで伝播。provider `TimeoutError` で `error_code == "llm_call_failed"` を検証する E2E を追加。変異検査で射影を外すと 1 件赤 |
| Medium-3 | `evidence-gate` が非 dict の `retrieved_records` を無視し、数値 ID を文字列化して受理（公開部品契約として不完全） | **この repo では対象外** — `EvidenceGateNode` は本 AG の実行経路で未使用であり、かつ**写経元 `parts/` 側の課題**。テンプレ側で直すと正本との byte 一致が壊れる（`verify_part` SUSPECT）。上流 parts の課題として記録 |
| Low-1 | caller_id 検査（type/nonempty/64 文字）を無効化しても全テスト緑 | **受入** — 空文字・非文字列・65 文字・前後空白・改行の 5 ケースで 403 を検証。変異検査で検査を外すと 2 件赤（他 3 件は認証層でも拒否されるため） |
| Low-2 | `docs/03` がサンドボックス依存の errors 件数を固定値で記録していた | **受入** — 環境依存である旨に改め、固定値の記録をやめた |

**指摘なし（レビュアーが裏取りした観点）**: `SCOPE_KEYS = ()` の妥当性（内外とも `memory_enabled=False`・checkpointer 全て `None`・DB/KB/store/write 経路なし・**同一 agent への並行 2 呼び出しで相談文と caller_ref の混線なし**を実測）／entry-adapter 置換で旧検査の脱落なし（誤 token 401・壊れた middleware identity 403）／`READ_INVOKE` の妥当性（副作用は LLM 呼び出しと監査ログのみ）。
