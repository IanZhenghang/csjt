"""归因字段拼写兼容只接受已知别名，保留严格结构与类型校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from defect_agent.schemas import Counterfactual


def _counterfactual(**fields):
    return {
        "changed_condition": "驾驶员明确知晓辅助驾驶的使用边界",
        "expected_difference": "驾驶员可能更早介入",
        "reasoning_summary": "未经验证的反事实说明",
        **fields,
    }


@pytest.mark.parametrize("field_name", ["possible_confounders", "possible_conconfounders"])
def test_counterfactual_accepts_known_spelling_and_serializes_canonical_name(field_name):
    confounders = ["驾驶员对辅助驾驶边界的理解", "感知输入不完整"]
    result = Counterfactual.model_validate(_counterfactual(**{field_name: confounders}))

    assert result.possible_confounders == confounders
    for by_alias in (False, True):
        dumped = result.model_dump(mode="json", by_alias=by_alias)
        assert dumped["possible_confounders"] == confounders
        assert "possible_conconfounders" not in dumped


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_counterfactual_schema_advertises_only_canonical_name(mode):
    schema = Counterfactual.model_json_schema(mode=mode)

    assert "possible_confounders" in schema["properties"]
    assert "possible_conconfounders" not in schema["properties"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("field_name", ["possible_confounders", "possible_conconfounders"])
@pytest.mark.parametrize("value", ["不是列表", [123], None])
def test_counterfactual_known_spelling_still_validates_type(field_name, value):
    with pytest.raises(ValidationError) as exc_info:
        Counterfactual.model_validate(_counterfactual(**{field_name: value}))

    assert any(error["type"] in {"list_type", "string_type"} for error in exc_info.value.errors())


def test_counterfactual_rejects_unknown_fields_alongside_known_spelling():
    with pytest.raises(ValidationError) as exc_info:
        Counterfactual.model_validate(_counterfactual(
            possible_conconfounders=["感知输入不完整"],
            possible_confounder=["未知字段不能被忽略"],
        ))

    assert any(
        error["loc"] == ("possible_confounder",) and error["type"] == "extra_forbidden"
        for error in exc_info.value.errors()
    )


@pytest.mark.parametrize("alias_value", [["相同内容"], ["冲突内容"]])
def test_counterfactual_rejects_duplicate_field_spellings(alias_value):
    with pytest.raises(ValidationError) as exc_info:
        Counterfactual.model_validate(_counterfactual(
            possible_confounders=["相同内容"],
            possible_conconfounders=alias_value,
        ))

    assert any(
        error["loc"] == ("possible_conconfounders",) and error["type"] == "extra_forbidden"
        for error in exc_info.value.errors()
    )
