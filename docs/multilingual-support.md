# 多语言内容与无需翻译

本次仅调整翻译平台。启动器语言设置及其下拉布局需在另一个窗口完成启动器任务后处理，避免同时修改。

## 对外约定

- 默认目标语言仍为 `zh-Hans`；已发布客户端的请求与中文缓存继续有效。
- `capabilities.languages` 返回代码、名称和英文名称，`default_target_language` 返回默认值。当前支持简繁中文、英、日、韩、德、法、西、巴西葡萄牙、俄、乌克兰、印尼语。
- resolve、ensure、manifest、catalog 接受目标语言；语言别名先规范化，再参与缓存、任务去重及幂等处理。
- `mc-text-v1` 和项目身份不变；目标语言不参与原文哈希，不替换原文的 `source_language=und`。
- 网页“内容语言”只控制资源内容，导航界面仍为中文；选择保存在当前浏览器。切换时重新分页并丢弃旧页面迟到响应。

## 无需翻译

继续使用已有终态 `ready`，结果质量为 `original`，可按原有方式读取简介或下载并校验正文 artifact。新增 `translation_required=false` 与 `no_translation_reason`。若对应当前原文，`update.code=not_required`、`action=none`。客户端停止轮询，不显示“AI 译文”或失败状态。

| 原因 | 规则 | 模型开销 |
| --- | --- | --- |
| project_name | 标题或内容仅为项目原名 | 无 |
| empty_content | 空文本 | 无 |
| technical_content | 仅代码、版本、URL 等受保护内容 | 无 |
| already_target_language | 可信原站明确给出相同来源语言 | 无 |
| unchanged | 模型处理后整篇与输入一致 | 首次可能有，随后复用缓存 |

没有可靠语言信息时不依据字符直接跳过。日语汉字与繁简中文不能混判。纯原文结果同样按原文哈希和项目上下文存储；原文变更后重新判断。防滥用字段配额仍正常计数，与模型 Token 费用分开。

## 更新与性能

全文、分段、任务和目录状态按目标语言隔离；原文更新后只修复实际请求过的语言。目录只查询当前页的简介及正文就绪信息，不载入正文文本。搜索用原文全文索引与当前语言摘要全文索引，短查询使用转义 LIKE。语言独立的目录摘要在变更时删除，避免旧译文继续参与搜索。

采用原有 SQLite WAL、批量查询、带签名的游标分页与不可变 artifact 缓存。新增索引表以事务迁移，按现有标题上下文回填当前有效简介；保留现有译文 revision、artifact、中文策略版本。目录游标绑定规范语言，切换语言后旧游标返回 `cursor_expired`。

模型预算在模型请求前的预留环节拦截。需要模型的任务延后至预算恢复，本地无需翻译的内容与原文刷新仍可完成。

## 文档与验证

Web 文档增加“目标语言与无需翻译”，OpenAPI 同步字段和可选参数；Python、Java、C、C++、C# 示例接受第二个命令行参数指定目标语言。

后端测试覆盖语言与别名、默认兼容、任务去重、分语言搜索、目录隔离、各类原文结果、预算暂停、本地处理、更新补译、游标语言绑定、manifest、管理台原文结果和旧库迁移。浏览器测试覆盖 12 个语言选项、中文/日文/英文内容、偏好保存、原文提示、正文 HTML 安全和 320–2560 像素宽度。测试使用本地确定性模型，不产生模型费用。

运行：

```sh
PYTHONPATH=src python -m unittest discover -s tests -q
PYTHONPATH=src python scripts/browser_multilingual.py
python scripts/browser_regression.py
```

浏览器测试需要 Playwright 和 Microsoft Edge。
