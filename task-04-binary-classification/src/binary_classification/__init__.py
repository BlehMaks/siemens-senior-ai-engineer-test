"""Reproducible binary classification pipeline."""

from binary_classification.analysis import (
    AnalysisReport,
    DataProfile,
    LeakageReport,
    analyze_training_frame,
    feature_group_ids,
    write_analysis,
)
from binary_classification.calibration import CalibrationMetrics, SigmoidCalibrator
from binary_classification.data import (
    DataContractError,
    JoinAudit,
    TrainingDataset,
    load_training_data,
)
from binary_classification.decision import DecisionMetrics, DecisionScenario
from binary_classification.modeling import (
    BinaryMetrics,
    CandidateMetrics,
    FeatureSchema,
    ThresholdChoice,
)

__all__ = [
    "AnalysisReport",
    "BinaryMetrics",
    "CalibrationMetrics",
    "CandidateMetrics",
    "DataContractError",
    "DataProfile",
    "DecisionMetrics",
    "DecisionScenario",
    "FeatureSchema",
    "JoinAudit",
    "LeakageReport",
    "SigmoidCalibrator",
    "ThresholdChoice",
    "TrainingDataset",
    "analyze_training_frame",
    "feature_group_ids",
    "load_training_data",
    "write_analysis",
]
