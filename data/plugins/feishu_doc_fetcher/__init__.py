"""飞书云文档 / Wiki 直读工具（给 LLM 调）。

之前 LLM 看到飞书 URL 时只能编"由于技术限制无法访问飞书内部链接"，
现在给它一个真工具 `fetch_feishu_doc(url)`，自动识别 wiki / docx /
doc / sheet URL，调飞书 API 把内容拉回来作为工具结果给 LLM。

支持的 URL 格式：
  https://*.feishu.cn/wiki/{node_token}    → wiki node, 自动解到 docx
  https://*.feishu.cn/docx/{doc_token}     → docx 直读
  https://*.feishu.cn/docs/{doc_token}     → 旧版 doc

配置来源：data/feishu_whitelist.yaml 的 feishu.app_id / app_secret
（跟 feishu_resource_plugin 同源，不另搞凭证）
"""
