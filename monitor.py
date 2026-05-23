import json
from pathlib import Path
from console_shared import console

def analyze_logs(log_dir):
    """分析日志并打印统计摘要"""
    log_path = Path(log_dir)
    if not log_path.exists() or not log_path.is_dir():
        console.print("无日志数据。", highlight=False)
        return
        
    logs = list(log_path.glob("*.jsonl"))
    if not logs:
        console.print("无日志数据。", highlight=False)
        return
        
    total_tasks = 0
    failed_tasks = 0
    total_attempts = 0
    requirements_failed = {}
    
    for f in logs:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            total_tasks += 1
            is_pass = data.get("test_result") == "pass"
            if not is_pass:
                failed_tasks += 1
                req = data.get("requirement", "")
                requirements_failed[req] = requirements_failed.get(req, 0) + 1
            total_attempts += data.get("attempts", 0)
        except Exception:
            continue
            
    if total_tasks == 0:
        console.print("无有效日志数据。", highlight=False)
        return
        
    fail_rate = (failed_tasks / total_tasks) * 100
    avg_attempts = total_attempts / total_tasks
    
    console.print("\n=== 日志统计摘要 ===", highlight=False)
    console.print(f"总任务数: {total_tasks}", highlight=False)
    console.print(f"失败率: {fail_rate:.1f}%", highlight=False)
    console.print(f"平均尝试次数: {avg_attempts:.1f}", highlight=False)
    
    if requirements_failed:
        sorted_fails = sorted(requirements_failed.items(), key=lambda x: x[1], reverse=True)[:3]
        console.print("\n最常失败的需求:", highlight=False)
        for req, count in sorted_fails:
            short_req = req[:50] + "..." if len(req) > 50 else req
            console.print(f"- ({count}次) {short_req}", highlight=False)
    console.print("==================\n", highlight=False)

def watch_errors(log_dir):
    """监控最新日志，检测连续失败"""
    log_path = Path(log_dir)
    if not log_path.exists() or not log_path.is_dir():
        return
        
    logs = sorted(log_path.glob("*.jsonl"), reverse=True)
    if len(logs) < 2:
        return
        
    try:
        last_log = json.loads(logs[0].read_text(encoding="utf-8"))
        prev_log = json.loads(logs[1].read_text(encoding="utf-8"))
        
        if last_log.get("test_result") != "pass" and prev_log.get("test_result") != "pass":
            if last_log.get("requirement") == prev_log.get("requirement"):
                console.print(f"\n[监控警告] 检测到同一任务连续失败！\n建议检查需求描述或人工介入：{last_log.get('requirement')[:60]}...", style="bold red")
    except Exception:
        pass