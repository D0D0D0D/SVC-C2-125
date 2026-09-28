"""実 LLM 1 回転チェック（`work/scripts/mrb_live_llm_check.py`）用の fixture。

ハーネスは `config["llm"]` に `BaseLLM` 準拠クライアントを注入して 1 回転し、
**LLM が実際に呼ばれたこと**（`assert llm.calls`）と**出力に中身があること**を検査する。
入力は当 AG の統合テスト（`tests/integration/test_intake_workflow.py`）が使う実データを流用する
ので、ここに検証専用の別経路を作り込まない。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def agent_config() -> dict:
    """ハーネスが `config["llm"]` を足す前提の構築 config（当 AG は他に必須キーを持たない）。"""
    return {}


@pytest.fixture
def case_payload() -> str:
    """当 AG の `/invoke` は生の問い合わせテキストを受ける（JSON パケットではない）。

    `tests/integration/test_intake_workflow.py` の `TEXT` と同一。複数意図を含むので
    LLM が意図分解を行ったかどうかが出力に現れる。
    """
    return "トマトの葉が枯れている。防除機の予約をしたい。補助金の書類を確認したい。"


@pytest.fixture
def live_input_context() -> dict:
    """当 AG は `intake_meta` を要求する（ハーネス既定の `{"mode": "invoke"}` では通らない）。"""
    return {
        "intake_meta": {
            # `_INQUIRY_ID` の形式に合わせる（統合テストと同形。合わないと S-3 が拒否する）
            "inquiry_id": "inq-livechek",
            "source_channel": "chat",
            "caller_ref": "staff-livecheck",
            "business_date": "2026-08-18",
        }
    }
