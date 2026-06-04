"""员工 onboarding 引导插件 · 飞书卡片按钮交互式入职流程。

流程（杨总 + PM 5/19 上午定的）：
1. 插件定期扫描 EmployeeStore，主动私聊未完成 onboarding 的员工
2. 新员工搜「巅池-Agent小助手」加联系人 → 私聊也可手动触发
3. 机器人推【部门选择卡】（按钮）→ 员工点击
4. 推【角色选择卡】→ 员工点击
5. 推【姓名输入提示】→ 员工发文字
6. 信息存 employees.db
7. 推【学习清单卡】→ 5 节教程
8. 学完点「申请测试」→ 5 题 quiz
9. 全对通过 → 标 onboarded + 推内测群邀请 / 自动拉群
10. 答错 → 标 quiz_failed，提示复习后重做

设计要点：
- 状态存 employee.preferences['_onboarding']
- 卡片回调走 __card_action__ 前缀消息（lark_adapter 转发）
- 跟 concierge_plugin 共用 EmployeeStore（不重建）
"""
