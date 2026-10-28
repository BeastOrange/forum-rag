# 本地语义检索与性能验证

上一批修复已合并并推送至 `main`：`3c4a004`。本轮新增代码保留在 `lody/semantic-performance`。人工相关性标注尚未完成，因此暂不能报告人工准确率，也不能认定余弦阈值已经校准。

## 运行独立语义检索

语义候选由完整离线向量矩阵产生，不依赖 FTS 命中；混合模式再按 RRF 60 合并词法候选。默认词法入口保持兼容，新增 `search-semantic` CLI 和 `search_semantic_forum` MCP 工具供显式选择。当前独立语义路径覆盖论坛；文档和知识页仍使用原检索路径。

模型使用 FastEmbed 0.8.0、ONNX Runtime 1.25.1、NumPy 2.4.4、CPUExecutionProvider，固定多语 MiniLM 的 ONNX 版本。下载准备与索引构建是显式操作，查询强制离线加载，失败不会静默回退到 hash。

- 仓库：`qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`
- revision：`faf4aa4225822f3bc6376869cb1164e8e3feedd0`
- ONNX SHA-256：`634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99`
- 权重：235,052,644 字节；向量：384 维、mean pooling、L2 归一化，无查询/正文前缀。
- 全量索引：45,927 个一级片段，矩阵约 67 MiB。模型和索引留在忽略的 `.cache` 内，不纳入 Git。

准备好带文件哈希的 `model-spec.json` 和本地模型后，在数据库副本上构建：

```bash
PYTHONPATH=src python scripts/build_semantic_index.py \
  --db /path/to/copy.sqlite3 --model-dir .cache/models/minilm \
  --index-dir .cache/semantic-full --batch-size 16 --threads 1
```

索引绑定源库路径和文件状态，修改来源后须显式重建。不要复制索引后仅修改数据库路径。每个请求固定完整 generation；源库、模型或产物不匹配时拒绝旧索引。查询只编码 query，不现场计算文档向量。

## 区分行为验证和准确率

[真实模型小样本](evaluation/real-model-independent.json)中的三条英文查询，在词法零命中时均召回对应中文主题。它证明模型通道独立工作，是合成行为验证，不能用作人工准确率。

[原有回归集](evaluation/phase2-smoke.json)的 44 个已知目标全部召回，16 个负例全部返回空，60 条查询零错误。这是来源构造的回归集，相关主题并非穷尽标注。

人工审核模板及合并候选池保持 `pending`。审核者须阅读来源，填写真实相关性等级、姓名或标识及日期。未知判断保持 null，不能作为不相关；开发集选阈值后，须冻结阈值再评估测试集。当前 `min_score=0.5` 仅为未校准默认值，分数不代表正确概率。

## 按实测优化性能

剖析首先发现每次查询全表来源指纹和重复文件哈希的成本；热查询现在复用已验证的只读矩阵，并通过文件状态变化触发失效。词法路径采用有界纯 hash 特征缓存及仅适用于自有连接的候选缓存；旧事务快照不复用跨连接结果。

128 个真实片段的编码实验中，单线程约 70 个片段/秒，2、4、8 线程约 53、62、61，因此全量构建选择单线程，未将增加线程当作必然提速。

[词法优化前](evaluation/lexical-load-before.json)和[优化后](evaluation/lexical-load-after.json)均为相同副本上的 512 次请求：单并发吞吐约 7.1→13.6 请求/秒，8 并发 p95 约 989→785 毫秒，均零错误。它们是固定查询池的进程内测量，包含缓存逐步变暖，不能外推生产 SLA。

[真实 MCP 语义测量](evaluation/semantic-mcp-load.json)覆盖 1/2/4/8 并发，每档 128 请求，零错误且重复结果一致；首次调用约 1.06 秒，热态 p95 约 30/15/30/77 毫秒。[直调持续测试](evaluation/semantic-direct-soak.json)每档 1,000 次，共 4,000 次，零错误且重复结果一致。不同任务曾有时间重叠，这些描述性数据不用于严格因果性能比较；最终修复后另做顺序复测。

[真实模型生命周期验证](evaluation/real-lifecycle.json)使用可丢弃的小库：普通写事务期间读取成功；8 线程 64 次请求跨原子 generation 切换结果一致；排他锁约 5 秒后返回既有 locked 状态；已提交修改使旧索引被拒绝。

所有数据库写入压力仅落在副本。RSS 的脚本统计只包含父进程，MCP 子进程内存未包含；首次请求不是操作系统冷缓存。人工质量、大规模知识页表现及跨机器负载不能由这些实验推出。

## 最终放行记录

独立审查发现 WAL 辅助文件自失效、模型加载缓存竞态、编码期间读锁和缓存无界四项问题。修复、限定复审和最终套件结果在完成后追加；在此之前不宣称本轮放行。

最终源码的全套测试为 **105 passed in 74.63s**，见 [套件日志](evaluation/phase2-final-tests.txt)。同一全量索引通过新代码的来源指纹、模型和产物哈希校验，返回 `existing`，未重新编码。

[修复后的真实 MCP 语义压测](evaluation/semantic-mcp-final.json)各档 1,000 请求，合计 4,000 次，全部零错误、零超时且重复结果一致：

| 并发 | 吞吐（请求/秒） | p95（毫秒） |
| --- | ---: | ---: |
| 1 | 103.1 | 13.9 |
| 2 | 147.1 | 18.5 |
| 4 | 173.9 | 29.5 |
| 8 | 136.4 | 81.7 |

该轮未并行启动其他压力任务或全库构建；轻量测试及审查可能同时运行。首次模型请求约 1.11 秒。8 并发吞吐低于 4 并发，更多线程不会必然提升吞吐；模型查询当前串行化推理以控制资源竞争。表中数值是本机固定查询池结果，不能作为生产承诺。

主检出原有 parser、storage、测试及保留的空 docs 文件逐项 SHA-256 校验一致。本轮未修改这些用户改动。

人工审核材料已准备：共 14 条查询、158 项待判定候选，见 [可读候选池](evaluation/human-review-pool.md)及[标签 JSON](evaluation/human-review-pool.json)。[评测工具试运行](evaluation/relevance-pending-report.json)明确输出 provisional；没有人工判断时，指标不能作为质量验收。合并候选包含不同模式各自命中的内容，审核时应验证结论适用范围和原文上下文。

文档格式命令 `yarn prettier --write` 已尝试，但此 Python 仓库没有 package.json，未安装额外格式依赖。Python 编译及 `git diff --check` 均通过。

[最终混合 MCP 测量](evaluation/hybrid-mcp-final.json)覆盖每档 128 次、共 512 次请求，零错误、零超时、重复结果一致。1/2/4/8 并发的 p95 为 196/254/392/745 毫秒，包含词法候选获取及排序成本，不能用纯语义数据代替。

审核模板中的两条待确认负例，在部分模式下仍有返回（中文：lexical 5、semantic 2、hybrid 5；英文：各 5）。这些不是已完成的人工负例判断，但足以说明当前阈值和自由表达检索尚需质量核验。语义入口保持显式选择，不用合成行为测试宣称精准度已达标。

限定复审已完成：四项 blocker 全部 **ADDRESSED**，修复范围内未发现新的重要问题。Reviewer 实际执行 9 项限定回归（15 项未选），另检查 identity LRU 容量及顺序；使用真实模型分别查询已有全量 semantic/hybrid 索引均返回 ready，源状态及产物签名不变。Reviewer 核对了 105 项通过的日志，没有重复运行全套。

代码、运行与并发验证已完成；人工准确率和阈值校准仍等待真实人工标签。本轮工作尚未提交或合入 main。
