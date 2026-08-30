import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from binary_classification.analysis import (
    analyze_training_frame,
    binary_target,
    feature_group_ids,
    write_analysis,
)
from binary_classification.data import load_training_data


def _approximate_floats(value: Any) -> Any:
    if isinstance(value, float):
        return pytest.approx(value, rel=1e-12, abs=1e-15)
    if isinstance(value, dict):
        return {key: _approximate_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_approximate_floats(item) for item in value]
    return value


def _analysis_frame(rows: int = 100) -> pd.DataFrame:
    target = ["n" if index % 2 == 0 else "y" for index in range(rows)]
    return pd.DataFrame(
        {
            "id": range(rows),
            "signal": target,
            "numeric": [index % 7 for index in range(rows)],
            "missing_signal": [None if label == "n" else "present" for label in target],
            "Class": target,
        }
    )


def test_binary_target_requires_the_declared_column() -> None:
    with pytest.raises(ValueError, match="must contain"):
        binary_target(pd.DataFrame({"not-Class": ["n", "y"]}))


def test_profile_captures_class_missingness_cardinality_and_duplicates() -> None:
    frame = _analysis_frame()
    frame.loc[99, ["signal", "numeric", "missing_signal"]] = frame.loc[
        97, ["signal", "numeric", "missing_signal"]
    ]

    report = analyze_training_frame(frame)

    assert report.profile.rows == 100
    assert report.profile.feature_count == 3
    assert report.profile.class_counts == {"n": 50, "y": 50}
    assert report.profile.missing_counts["missing_signal"] == 50
    assert report.profile.unique_counts["numeric"] == 7
    assert report.profile.duplicated_feature_rows > 0
    assert report.profile.duplicated_feature_groups > 0
    assert report.profile.max_feature_group_size > 1
    assert report.profile.conflicting_feature_groups == 0


def test_leakage_screen_quarantines_identifiers_and_deterministic_signals() -> None:
    report = analyze_training_frame(_analysis_frame())

    assert report.leakage.identifier_columns == ("id",)
    assert set(report.leakage.deterministic_target_columns) == {
        "missing_signal",
        "signal",
    }
    assert report.leakage.missingness_rate_gaps["missing_signal"] == 1.0
    assert report.leakage.single_feature_pr_auc["signal"] == pytest.approx(1.0)
    assert set(report.leakage.quarantined_columns) == {
        "id",
        "missing_signal",
        "signal",
    }


def test_unique_feature_is_treated_as_identifier_not_target_mapping() -> None:
    frame = _analysis_frame()
    frame["serial"] = [f"serial-{index}" for index in range(len(frame))]

    report = analyze_training_frame(frame)

    assert "serial" in report.leakage.identifier_columns
    assert "serial" not in report.leakage.deterministic_target_columns


def test_complete_duplicate_vectors_receive_the_same_split_group() -> None:
    frame = _analysis_frame(20)
    frame.loc[19, ["signal", "numeric", "missing_signal"]] = frame.loc[
        17, ["signal", "numeric", "missing_signal"]
    ]

    groups = feature_group_ids(frame)

    assert groups[19] == groups[17]
    assert groups[18] != groups[17]


def test_requires_target_and_identifier_columns() -> None:
    with pytest.raises(ValueError, match="must contain"):
        analyze_training_frame(pd.DataFrame({"feature": [1, 2]}))


def test_rejects_empty_training_frame() -> None:
    with pytest.raises(ValueError, match="training rows"):
        analyze_training_frame(pd.DataFrame(columns=["id", "Class"]))


@pytest.mark.parametrize(
    "labels",
    [
        ["n", "y", "maybe", "n"],
        ["n", "y", None, "n"],
        ["n", "n", "n", "n"],
    ],
)
def test_rejects_target_values_outside_declared_binary_contract(
    labels: list[object],
) -> None:
    frame = pd.DataFrame(
        {
            "id": range(4),
            "feature": range(4),
            "Class": labels,
        }
    )

    with pytest.raises(ValueError, match="Class must contain exactly"):
        analyze_training_frame(frame)


def test_literal_missing_category_remains_distinct_from_null() -> None:
    rows = 40
    labels = ["n" if index < rows // 2 else "y" for index in range(rows)]
    frame = pd.DataFrame(
        {
            "id": range(rows),
            "feature": [None if label == "n" else "__MISSING__" for label in labels],
            "group_anchor": [index % 5 for index in range(rows)],
            "Class": labels,
        }
    )

    report = analyze_training_frame(frame)

    assert "feature" in report.leakage.deterministic_target_columns
    assert report.leakage.single_feature_pr_auc["feature"] == pytest.approx(1.0)


def test_requires_enough_examples_in_each_class() -> None:
    frame = pd.DataFrame(
        {"id": [1, 2, 3], "feature": [1, 2, 3], "Class": ["n", "y", "y"]}
    )

    with pytest.raises(ValueError, match="at least two groups per class"):
        analyze_training_frame(frame)


def test_rejects_conflicting_targets_for_identical_feature_vectors() -> None:
    frame = pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "feature": ["same", "same", "other", "third"],
            "Class": ["n", "y", "n", "y"],
        }
    )

    with pytest.raises(ValueError, match="conflicting targets"):
        analyze_training_frame(frame)


def test_write_analysis_emits_stable_machine_readable_json(tmp_path: Path) -> None:
    part1 = tmp_path / "part1.csv"
    part2 = tmp_path / "part2.csv"
    part1.write_text(
        "BIB;COD;ERG;FAN;GJAH;LUK;MYR;NUS;PKD;RAS;id\n"
        "1;a;b;1;c;2;d;3;e;;0\n"
        "2;b;c;2;d;3;e;4;f;x;1\n"
        "3;a;b;3;c;4;d;5;e;;2\n"
        "4;b;c;4;d;5;e;6;f;x;3\n",
        encoding="utf-8",
    )
    part2.write_text(
        "SIS;TOK;UIN;VOL;WET;KAT;XIN;Class;id\n"
        "1;t;1;f;1;a;t;n;0\n"
        "2;f;2;t;0;b;f;y;1\n"
        "3;t;3;f;1;a;t;n;2\n"
        "4;f;4;t;0;b;f;y;3\n",
        encoding="utf-8",
    )
    output = tmp_path / "reports" / "profile.json"

    report = write_analysis(part1, part2, output)

    assert json.loads(output.read_text(encoding="utf-8")) == report.to_dict()


def test_committed_profile_matches_private_data_when_supplied() -> None:
    input_dir_value = os.environ.get("SIEMENS_TASK4_INPUT_DIR")
    if input_dir_value is None:
        pytest.skip("Set SIEMENS_TASK4_INPUT_DIR to reproduce the committed profile")
    input_dir = Path(input_dir_value)
    dataset = load_training_data(
        input_dir / "Training_part1.csv", input_dir / "Training_part2.csv"
    )
    report = analyze_training_frame(dataset.frame)
    profile_path = Path(__file__).parents[1] / "reports" / "data-profile.json"

    committed_profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert report.to_dict() == _approximate_floats(committed_profile)
