"""PexisLink: open-source telemetry and anomaly-detection research.

``Sample``, ``read_samples``, ``analyze`` and ``Assessment`` are the frozen v0.1
API, kept for backward compatibility and as the benchmark comparator. The v0.2
pipeline lives in ``pexislink.ingest``, ``quality``, ``reference`` and ``detect``.
"""

__version__ = "0.2.0a0"

from .core import Assessment, Sample, analyze, read_samples  # noqa: E402
from .config import DetectorConfig  # noqa: E402
from .detect import RecordAssessment, assess_records  # noqa: E402
from .reference import build_reference  # noqa: E402

__all__ = [
    "Assessment", "Sample", "analyze", "read_samples",
    "DetectorConfig", "RecordAssessment", "assess_records", "build_reference",
    "__version__",
]
