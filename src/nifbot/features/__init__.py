"""ONE feature module shared by backtest, training and live (no duplicate logic).

Every feature at decision time ``t`` uses only data whose ``available_at <= t``.
``tests/unit/test_feature_leakage.py`` enforces this.
"""

from nifbot.features.build import FEATURE_COLUMNS, FeatureInputs, build_features

__all__ = ["FEATURE_COLUMNS", "FeatureInputs", "build_features"]
