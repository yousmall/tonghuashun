# 本地RAG部署与资料保存

## 已部署组件

- 中文向量模型：`BAAI/bge-small-zh-v1.5`，FastEmbed/ONNX CPU推理，512维；模型来源为FastEmbed登记的Qdrant模型镜像。
- 原文、元数据、文本分块、float32向量、FTS5词项索引、检索审计：本机SQLite，无需额外启动向量数据库进程。
- 初始资料：项目自有6份基础概念解释与6份技术指标说明。明确标记为基础知识，不能当作实时行情或官方财报。
- 默认资料根目录：`data/rag/`，已加入Git忽略。环境变量`WENCE_RAG_PATH`可指定独立数据盘，`WENCE_RAG_ENABLED=false`可关闭。

## 部署/迁移

```powershell
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe scripts/deploy_rag.py --download-model
```

首次下载需要访问Hugging Face模型镜像。部署脚本使用真实本地向量完成中文语义召回验收，验收成功后写入`deployment.json`。已缓存模型时可去掉`--download-model`离线重复执行，不会重复导入相同版本的知识资料。运行时使用`local_files_only=True`，不会隐式下载。

目录结构：

```text
data/rag/
  knowledge.sqlite3       # documents/chunks/settings/retrievals及FTS5
  documents/              # 以账号+元数据+内容哈希命名的原始TXT/MD/PDF
  models/                 # 中文ONNX模型和tokenizer本地缓存
  deployment.json         # 模型、维度、初始资料版本和部署时间
```

备份时停止服务后复制整个目录，或使用SQLite备份API并同时保存原文与模型目录；运行中不要只复制主SQLite文件而漏掉WAL。更换向量模型或维度时，使用新目录重新建库，旧索引会拒绝混用。

## 深入研究

深入研究会自动检索已入库资料，检索记录随咨询历史保存。资料导入、手动检索和启停可通过下文所列的登录接口执行。

## 召回和证据边界

按当前账号、资产类型、精确标的代码/名称预过滤，目标不明确时只检索无标的的通用资料。股票代码后缀在同一资产类型内规范化，避免误查同代码的基金/指数。每次在最近12000个符合范围的分块上计算余弦相似度，同时执行FTS5词项召回，合并前60名并设置相似度阈值，默认最多返回6份不同资料；达到候选上限会显式记录。分块为340字符、重叠40字符，记录原文偏移，PDF文本带页码标记。检索记录保存查询哈希与实际命中依据，不额外保存原始提问文本。

供应商事实的值、ID、快照时间、报告期、单位、来源链接原样保留；过期记录留在库内但不能参与本轮回答。用户资料以`USER_SUPPLIED:RAG`来源的资料摘录呈现，默认有效期90天；基础知识365天，从资料日期计算，不按查询时间续期。相同标的资料允许并列引用，原有金融指标仍按自身时效核验。知识片段是参考数据，不能改写模型指令、风险画像或授权范围，不能自行提供规则计算所需价格或财务数值。

自动入库仅面向深度/兼容fast研究，保存最多40条本次新取数、可追溯、有效且非派生的供应商事实，不自动索引持仓、交易记录、用户画像、任意仓库文件或历史回答。向量检索并不替代当前外部数据获取。无命中或本地模型不可用时明确记录，不伪装为成功。

## API与验证

所有知识库管理API均要求登录，由服务端账号决定权限。导入支持UTF-8 TXT、Markdown、可提取文字的PDF或直接提交正文，文件与正文二选一。单份资料最多5MB、100页、10万字符，同一账号下的相同内容和元数据会去重。

- `GET /api/v1/rag/status`：当前账号可见资料数和部署状态。
- `GET /api/v1/rag/documents`：本人最近100份资料。
- `POST /api/v1/rag/documents`：导入正文或Base64文件。
- `POST /api/v1/rag/search`：查询个人资料与共享基础知识。
- `PATCH /api/v1/rag/documents/{id}`：停用/恢复本人资料。

`tests/test_rag.py`使用离线确定性向量验证持久化、账号隔离、标的范围、上传限制、旧行情剔除、模型维度一致性、模型输入切片、咨询风险门槛；部署脚本另用真实BGE模型验证中文语义召回。它们不能证明真实模型幻觉率下降幅度，或未经测试的外部行情及大模型调用已经成功。

模型资料：[FastEmbed支持的模型](https://qdrant.github.io/fastembed/examples/Supported_Models/)、[FastEmbed文档](https://qdrant.tech/documentation/embeddings/fastembed/)。

## 真实验收补充

深入研究在计算向量相似度前，会排除本轮已有有效数据的同一标的标量指标，避免反复入库的历史指标挤占召回名额；新闻、公告等多条并列证据仍保留候选资格。过期的本轮输入不参与这项排除。审计中的`already_available_exclusions`记录跳过的候选分块数。

模型调用记录中的`input_rag_fact_ids`记录实际进入该次模型输入的文档摘录编号，不以知识库的召回数量冒充实际模型输入。严格真实验收还要求所有分项调用和最终复核成功，不能只有意图识别成功而分项回退规则。REVIEW仍按真实证据缺项、过期或冲突保留，技术链路通过不代表投资判断获准使用。

DeepSeek V4.1等V4小版本沿用项目已有V4的自动思考控制协议，显式enabled/disabled/omit设置优先；auto在结构化调用中关闭思考，避免默认思考占用输出预算导致JSON截断。协议依据：[DeepSeek官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。更换`.env`中的模型后应重启后端，并以readiness返回的实际模型和真实模型调用记录核对。
