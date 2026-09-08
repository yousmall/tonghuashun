"""标准化数据提供者协议与本地快照实现。"""

from .base import DataProvider, SnapshotProvider
from .iwencai import CompositeProvider, IwencaiSkillHubProvider

__all__ = ["CompositeProvider", "DataProvider", "IwencaiSkillHubProvider", "SnapshotProvider"]
