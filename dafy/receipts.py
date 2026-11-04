"""操作结果由真实回执决定；沿用旧版执行声明校验，避免聊天承诺冒充完成。"""
import importlib.util
import re
from .paths import CODE

spec = importlib.util.spec_from_file_location("dafy_task_truth", CODE / "V1.2.4/task_truth.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def finalize(text, receipts, user_text):
    verified = []
    normalized = []
    for name, result in receipts:
        if name == "reminder" and isinstance(result, dict) and result.get("ok") is True:
            saved = result["saved"]
            verified.append("提醒已取消。" if saved["status"] == "cancelled" else "提醒已保存：" + saved["content"] + "；" + saved["next_time"] + "（" + saved["timezone"] + "）。编号：" + saved["id"])
        elif name == "manage_memory" and isinstance(result, dict) and result.get("ok") is True:
            verified.append("已保存记忆修正，受影响摘要将重新整理。" if result.get("corrected") else "应用记忆及登记的本机备份已清理；接入组件内部数据清理仍待完成，不能确认全部副本已经删除。")
        elif name == "teach_behavior" and isinstance(result, dict) and result.get("ok") is True:
            verified.append(result["receipt"])
        elif name == "share_memory_once" and isinstance(result, dict) and "state" in result:
            verified.append("已按这次允许分享一次指定内容。" if result.get("state") == "confirmed" else "分享尚未确认送达，不会更换发送编号重复发起。")
        elif name == "evolve_style" and isinstance(result, dict) and result.get("changed") is True and re.search(r"语气|风格|说话|表达|性格", user_text):
            verified.append("通用表达习惯已缓慢调整，保留原有核心身份。")
        if isinstance(result, list):
            normalized.append(("recall_memory" if name in {"reminder", "manage_memory"} else name, {"items": result}))
        else:
            normalized.append((name, result))
    if verified:
        return "\n".join(verified)
    checked = legacy.guard(text, normalized, user_text)
    if checked != text:
        return checked
    # 补齐新版任务中的主语表达，仍交给原校验器区分条件、否定、引用和真正执行声明。
    canonical = re.sub(r"(^|[，,。！？!?\n；;])(?:我|本鱼)(?:这边)?(?:已经|已)?(?:完成了?|弄好了|做好了|搞定了|处理好了|修好了|已启动|启动成功)",
                       r"\1已完成", text)
    guarded = legacy.guard(canonical, normalized, user_text)
    return guarded if guarded != canonical else text
