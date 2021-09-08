from enum import Enum


class MetricAggregation(Enum):
    CONFUSION_MATRIX = "CONFUSION_MATRIX"
    ARITHMETIC_MEAN = "ARITHMETIC_MEAN"
    GEOMETRIC_MEAN = "GEOMETRIC_MEAN"
    HARMONIC_MEAN = "HARMONIC_MEAN"
    SUM = "SUM"

