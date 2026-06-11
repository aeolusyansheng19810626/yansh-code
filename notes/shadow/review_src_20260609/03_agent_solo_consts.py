# agent.py: solo 4 常量
# 行号 137-140
_SOLO_SOFT_LIMIT     = 120       # 主 loop 工具调用轮次上限（连续 context，复杂多文件任务需大轮数）
_SOLO_TOKEN_BUDGET   = 600_000   # token 增量软提醒阈值（超过注入一次收敛提示；硬熔断交给 --max-cost）
_SOLO_NO_PROGRESS_CAP = 6        # 连续 N 轮无写编辑先注提醒，2N 轮熔断（agent 级，区别于逐文件 no_progress）
_SOLO_GATE_MAX_ROUNDS = 8        # 外部 test gate 回灌最大轮数
