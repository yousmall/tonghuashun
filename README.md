# 问策智投 MV

当前已实现主协调智能体的第一版：识别用户意图、检查已确认画像、生成可执行 Task DAG、并行调度专业智能体、事实核验，并以合规审核作为最终闸门。

## 启动 API

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.app.main:app --reload --port 8000
```

启动后访问 `http://127.0.0.1:8000/api/v1/health`，预期返回 `{"status":"ok"}`。
接口文档位于 `http://127.0.0.1:8000/docs`。

## 运行测试

```powershell
python -m pytest -q
```

主协调智能体位于 `backend/app/agents/coordinator.py`。当前示例专业智能体只使用调用方传入的 `FactRecord`，不会产生或伪装实时市场数据。
