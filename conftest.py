"""测试会话的本地临时目录配置。

Streamlit 的 ``AppTest`` 会把待执行脚本写入 ``tempfile.mkdtemp()`` 创建的目录。
在只允许写入工作区的文件沙箱下，把临时目录整体放到仓库内，并让 ``mkdtemp``
直接在该目录下建普通子目录，测试才能在受限环境中真实执行。这里不改变任何
业务行为。
"""

import shutil
import tempfile
import uuid
from pathlib import Path

LOCAL_TEMP = Path(__file__).resolve().parent / ".tmp_pytest"
LOCAL_TEMP.mkdir(exist_ok=True)
tempfile.tempdir = str(LOCAL_TEMP)


def _local_mkdtemp(suffix=None, prefix=None, dir=None):
    """在本地临时目录下创建子目录，不依赖系统临时目录的写入权限。"""

    target = Path(dir or LOCAL_TEMP) / f"{prefix or 'tmp'}{uuid.uuid4().hex}{suffix or ''}"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


tempfile.mkdtemp = _local_mkdtemp


def pytest_sessionfinish(session, exitstatus):
    """测试结束后清理本次会话产生的临时文件，不留下构建垃圾。"""

    shutil.rmtree(LOCAL_TEMP, ignore_errors=True)
