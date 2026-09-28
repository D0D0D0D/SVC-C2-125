# テスト仕様書

## 方針

テストは実ネットワーク、DB、KB、外部 API を使用しない。LLM は `config["llm"]` の fake に差し替える。PB-3 は本エージェントに外部サービスがないため、policy / calendar 入力ファイルの読込境界で代替する。HITL は無効なので PB-7 は exact-name scaffold を残し auto-skip とする。

## Framework compliance

| ID | 検証 | 期待値 | 結果 |
|---|---|---|---|
| TC-01 | State 契約 | flat TypedDict、credential / Context を保持しない | PASS |
| TC-02 | SecurityViolationError | S-3 の不正 span / 禁止キーを型付き例外で拒否 | PASS |
| TC-03 | credential 不在 | State と `src/` に credential を置かない | PASS |
| TC-04 | InvocationContext | adapter / framework state 経由のみ | PASS |
| TC-05 | S-4 | 各 execute の domain event、framework lifecycle の重複なし | PASS |
| TC-06 | S-2 | final gate の override が class 定義時に失敗 | PASS |
| TC-07 | S-3 | final gate の override が class 定義時に失敗 | PASS |
| TC-08 | `required_trust_level` declared and enforced — 専用の S-1 境界カバレッジは `node(state)`（`__call__` 経由）で privileged node を呼ぶこと | 不足 trust では **`execute()` に到達する前**に拒否。**`execute()` が呼ばれなかったことを証明する**（PB-6 `TestInvokeOrder::test_s1_denial_refuses_execution_before_execute` が `_PrivilegedTrustGateFixture` で `status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert）。`node.execute(state)` 直呼びは業務ロジック単体テストに限り可で TC-08 を満たさない | PASS |

## Proof of boundary

| ID | 境界 | 検証 | 結果 |
|---|---|---|---|
| PB-1 | audit | node ごとの domain trace event と失敗・縮退イベントを検証 | PASS |
| PB-2 | state serialization | State は JSON/msgpack 相当の primitive container のみ | PASS |
| PB-3 | policy file | policy / calendar 読込失敗は外部接続なしで fallback | PASS |
| PB-4 | import isolation | `src/` AST に Level 0 import がない | PASS |
| PB-5 | checkpoint | memory / HITL 無効、credential を State に置かない | PASS |
| PB-6 | invoke order | S-1 → S-2 → execute → S-3 の framework 順序 | PASS |
| PB-7 | HITL | `hitl.enabled: false` のため auto-waived | SKIP |

## 共通部品リフレッシュ接合部（`parts/TESTING.md` 対応）

| ID | 項目 | 本 AG での具体化 | 期待値 |
|---|---|---|---|
| G-1 / G-3 | Cat 2 mode merge / 実 graph 到達 | 単一 `invoke` mode で inner 3 stage を通し、`handoff_packets` / `coverage_gaps` / `degradation_reason` / `provenance_verified` だけを outer state へ昇格する | 他 mode の None 埋め・reducer 管理 field 混入なし。主要 packet が実 compiled graph の出力へ到達 |
| N-6 / N-7 | operation policy / 区画なし宣言 | 永続化・feedback・resume のない `/invoke` を `Operation.READ_INVOKE` として公開 `authenticate()` に渡す。`SCOPE_KEYS = ()` / `STG_DEFAULT_SCOPE_ENVS = {}` とし、判断根拠を `_ctx()` docstring に残す | token / middleware の verified caller のみ通過、scope は `{}`。private auth 関数を呼ばず、ダミー scope を要求しない |
| V-1 | malformed evidence fail-closed | `evidence_map` の `source_ids: ""`、非 dict claim、非 dict `asserted_attrs` を公開 `validate_provenance()` に渡す | 3 ケースすべて `SecurityViolationError`。不正 claim を無視しない |
| V-4 | 型付き finding 射影 | 本 AG は LLM draft を packet へ全 field 再構築し、最終 S-3 で exact-key/type/value を再検証するため `project_typed_finding()` は未選択 | LLM の未知 key / nested dict は顧客出力へ透過せず、既存 closed allowlist テストで拒否 |
| L-4 / L-5 | LLM 障害境界 / 例外文非露出 | `llm=None` と provider `TimeoutError` を対で実行し、契約どおり `complete(messages)` だけを実装する fake で正常系も実行する | 未注入だけ success + degradation。provider 障害は `status=error` / `error_code=llm_call_failed`、draft/fallback なし。例外自由文を state/trace に出さない |
| X-2 | 変異検査 | verified trust guard を外す、malformed claim guard を `continue` に戻す、LLM 呼出へ契約外 `timeout=` を足す、provider 例外を fallback に戻す変異を個別適用する | 対応する追加テストが各変異で赤になる |

## ドメイン受入基準

| 項目 | 値レベルの確認 | 結果 |
|---|---|---|
| 複数用件 | fake LLM の3意図が3パケット、rank 1..3 で出力 | PASS |
| 必須構造 | intent span / facts / missing task / capability / ranking basis を全 packet に確認 | PASS |
| 逐語照合 | offset 不一致、存在しない span、空・1文字 span を drop / reject | PASS |
| canonical text | 全角電話を NFKC 後に再走査し `[MASKED]`、後段で不変 | PASS |
| PII既知限界 | 日本語に隣接した電話番号は framework `detect_pii` が検知しない現状を固定（テンプレ側で補完しない） | PASS |
| S-3 closed allowlist | diagnosis / eligibility / booking_confirmed / assigned_to を reject | PASS |
| S-3 再計算 | `provenance_verified: true` でも verification basis の offset 改ざんを reject、basis は出力から除去 | PASS |
| 自由文境界 | value は source span 完全一致、staff_action は1文・120文字・enum を検証 | PASS |
| capability / rank | token 形式、priority、1始まり連番、offset tie-break を検証 | PASS |
| deadline / quantity 正規化 | 絶対日付は business_date 基準の ISO に、数量は数値＋単位に正規化。LLM値は span からの決定論導出値と一致する場合のみ許可 | PASS |
| 相対日付・否定 | 来週の ISO 偽装、過去月日、無関係日付、否定切り落としを reject。literal 相対日付は rank 非影響で許可 | PASS |
| deadline rank | 同一priorityの2意図で近い ISO deadline を先順位にする。正規化器を常にNoneにする変異で失敗 | PASS |
| policy version | 正常版との対照で mismatch を fail-closed | PASS |
| policy / LLM fallback | policy 欠落は closed fallback。LLM は未注入時だけ deterministic fallback、provider 失敗は `status=error` / `llm_call_failed` | PASS |
| volume cap | `max_intents` と `min_span_chars` を実際に変更し E2E 結果が変わる | PASS |
| adapter | context 省略、未知 key / 不正 channel / 日付を 422、startup config 注入を検証 | PASS |
| business date | Asia/Tokyo で入口固定、過去日を seasonal 判定に使用 | PASS |
| span context | 無関係だが実在する span は警告 `span_context_mismatch`（既知限界） | PASS |
| 実 LLM 1 回転（CI 対象外） | 実モデルを `config["llm"]` 注入シームに刺して実 invoke を 1 回 | **2026-07-31 再実施**（レビュー対応後）: 4 パケット・縮退なし・`staff_action` はすべて LLM 由来の具体タスク・**`quantity` の正規化が実 LLM 出力で発動**（`六十二リットル` → `62L`）。`deadline` は当該試行で LLM が fact として抽出せず正規化の発動は未観測（下記の限界を参照） | ✅ PASS |
| 実 LLM での `deadline` 正規化 | 絶対日付を含む相談で `deadline` fact が ISO 化され rank 第 2 キーに効くか | **FakeLlm 経路で実証済み**（`2026-08-10` が rank 1・`2026-09-20` が rank 2 / span 由来でない `2030-01-01` は `unverified_value:deadline` で drop）。実 LLM は当該試行で `deadline` fact を返さず未観測 | ⚠️ 実装は検証済・実 LLM での発動は抽出揺れに依存（未発動でも害なし＝出現順 tie-break に落ちる） |
| プロンプト contract（変異検査済み） | `build_prompt()` の戻り値に fact/task の要素形・allowlist・config 由来の字数上限が含まれることを検査 | 4 変異すべてで落ちることを確認: ①`extracted_facts` を空配列に戻す（初版の欠陥そのもの）②`allowlists` セクション削除 ③`allowlists.field` から 1 要素落とす ④字数上限を config 非依存の固定値にする | ✅ PASS |

## 実行結果

2026-07-30 に repo root で実行。

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 257 passed, 2 skipped, 11 errors（本サンドボックスは `scripts/test_stg_invoke_evidence.py` の loopback HTTPServer bind を許可しない。通常実行環境で要再確認） |
| `.venv/bin/python -m pytest tests -q` | 125 passed, 2 skipped |
| `.venv/bin/python -m pytest tests/proof_of_boundary/ -q` | 5 passed, 2 skipped |
| `.venv/bin/ruff check src tests` | All checks passed |

### 2026-08-14 共通部品リフレッシュ

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | **424 passed, 3 skipped, 0 errors**（loopback bind を許可する通常実行環境で実測）。サンドボックス内では `scripts/test_stg_invoke_evidence.py` の `HTTPServer.bind()` が `PermissionError` になり errors として計上されるが、これは環境制約であって AG のテスト失敗ではない。**件数は環境依存なので固定値として記録しない** |
| `.venv/bin/python -m pytest -q --ignore=scripts/test_stg_invoke_evidence.py` | 405 passed, 3 skipped |
| `.venv/bin/python -m pytest tests -q` | 210 passed, 2 skipped |
| `.venv/bin/ruff check src tests` | All checks passed |
| `.venv/bin/python scripts/check_manifest_schema.py config/agent.yaml` | Manifest schema PASS |
| `python3 parts/tools/verify_part.py <repo>` | 12 files verified, SUSPECT 0 |

## 実 LLM 1 回転（共通部品リフレッシュ後・2026-08-14）

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-14 |
| 使用 LLM | ローカル CLI（注入シーム `config["llm"]` 経由・**repo 外 cwd**。テンプレの `src/` は不変） |
| 入力 | 3 意図を含む実務相談文（共済の対象可否 / 追肥時期 / 座談会日程）。**単一意図のダミーでは中核（多意図の分解と振り分け）が走らない**ため別途こしらえた |
| 結果 | **PASS** — LLM 呼び出し 1 回・`status: success`・`packets` 3 件（意図ごとに分解されて担当へ振り分け）・出力は非空 |

**検収で捕まえた退行**: `scripts/stg_invoke_evidence.py` が scaffold 正本世代へ更新された一方、
対になる自己回帰テスト `scripts/test_stg_invoke_evidence.py` が**旧世代のまま残り**、
`agent_status_success` と `agent_invoke_responsive` の契約差で 2 件が赤になっていた
（`develop` では 12 passed＝リフレッシュ起因）。正本から写経して解消。
**実装と対になる scaffold 自己回帰テストは必ずセットで更新する。**

### 独立レビュー指摘の修正後・実 LLM 再検証（2026-08-14）

| 項目 | 内容 |
|---|---|
| 結果 | **2 回連続 PASS** — `status: success` / `packets` 3 件 / `degradation_reason` 空 |
| 何が変わったか | 修正前は **LLM が返す span の offset が全件不一致になり `no_verifiable_intent` に縮退する回**があった（再現性が LLM 依存）。LLM に offset を計算させず、逐語 `source_span` + `occurrence_index` を返させて **Python 側で offset を決定**する契約に変えた結果、連続で中核（多意図の分解と振り分け）が動作 |
| 検証強度 | 逐語一致は offset 一致より強い保証。本文に無い文字列・短すぎる span・不正な occurrence は drop され、Python 算出後も `_span_valid` と S-3 で再検証される |

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。
