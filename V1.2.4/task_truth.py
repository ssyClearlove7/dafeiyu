"""工具回执约束：任务事实由执行器提供，不以聊天自述充当执行证据。"""
import re

INSTRUCTIONS = """
真实性要求优先于人设和聊天效果：
只报告实际工具回执、当前任务记录或已读取资料能够证明的事实。
历史聊天中你自己说过“正在做”“已完成”不构成证据，不能接着圆。
未调用工具就不能声称查过、启动、正在执行或已经完成，也不能用“这就去做”替代执行。
任务启动、运行、部分完成、失败和完成必须区分；被问到具体任务时按实际记录回答。
数量、时间、文件名和进度不得猜测；不确定的事实明确说不确定，推测必须标明。
图片或语音没有识别结果时不能假装看懂或听懂；工具失败要报告失败。
没有执行能力的操作如实说明，不能承诺在回复后自行工作或稍后提醒。
虚构角色的外貌和性格可以用于聊天，但不能虚构真实操作、资料来源或执行结果。
日常聊天只回应当前话题；历史任务记录不能作为主动插播任务进度的理由。
举例、假设、引用、表达观点和建议不是工具执行声明，不要用执行记录话术代替正常聊天。
历史里的内部校验话术不作为回复范例，不要复读；始终回应眼前的具体问题。
对方明确要求上网找例子或查证时，使用 web_search 后结合当前论点回答并给出来源；
工具不可用或失败时简短说明具体原因，不复读内部校验规则。
"""


def receipt_reply(name, result):
    """对改变状态的操作和进度查询，直接使用执行器结果。"""
    if not isinstance(result, dict):
        return None
    if name in {"update_sticker_library", "sticker_update_status", "delete_stickers"}:
        import sticker_skill
        return sticker_skill.result_reply(name, result)
    if name == "install_skill":
        if result.get("error"):
            return "技能安装未完成：" + result["error"][:100]
        if result.get("status") in ("installed", "already_installed"):
            return "技能%s已安装；包内脚本未执行。" % result.get("name", "")
        return "没有确认技能安装成功，不能按已安装回报。"
    if name == "evolve_persona":
        return "演化人设已保存。" if result.get("ok") is True else "演化人设没有保存成功。"
    return None


def guard(output, receipts, user_text="", rewrite=None):
    verified = [receipt_reply(name, result) for name, result in receipts]
    verified = [text for text in verified if text]
    if verified:
        return "\n".join(verified)
    # 按分句检查明确的执行声明，不能跨过观点、举例或他人的话去拼接主谓。
    operation = r"(?:搜(?:索|集)?|查(?:找|询|阅)?|下载|更新|补充|入库|收藏|保存|删(?:除)?|清空|安装|修复|修改|发给|发送|提醒|设(?:置|好)|创建|执行|运行|处理|挑(?:选)?|整理|安排)"
    actor = r"(?:本鱼|我)(?:这边)?"
    phase = r"(?:已经|已|刚刚|刚才|早就|正在|还在|一直在|马上|这就|现在就|稍后会|待会会)"
    beneficiary = r"(?:帮你|给你|替你)?"
    pattern = re.compile(r"(?:" + actor + phase + r"|^" + phase + r")" + beneficiary + operation
                         + r"|" + actor + beneficiary + operation + r"(?:过|了|完了|好了|完毕|成功|完成|到了)"
                         + r"|^" + operation + r"(?:完了|好了|完毕|成功|完成)")
    task = bool(re.search(r"任务|进度|表情|图库|技能|安装|修复|更新|提醒", user_text, re.I))
    # 引文只是讨论材料；只检查引文之外的自述。
    prose = re.sub(r'“[^”]*”|「[^」]*」|‘[^’]*’|"[^"\n]*"', "", output)
    claims = []
    for clause in re.split(r"[，,。！？!?\n；;：:]", prose):
        clause = clause.strip()
        if re.search(r"没有|未|没法|不能|不会|不支持|无法|不确定|不知道|不清楚|建议|如果|假如|假设|比如|例如|举个例子|要是|[吗么]$", clause):
            continue
        claims.extend(m[0] for m in pattern.finditer(clause))
        if task and re.match(r"(?:已完成|完成了|弄好了|做好了|搞定了|处理好了|还在干|没偷懒|正在进行|已启动|启动成功)", clause):
            claims.append(clause)
    internal_refusal = re.search(r"没有可核验的执行记录|不能说已经做过或还在进行", output)
    if not claims and not internal_refusal:
        return output
    # 查询/读取回执只证明相应的读取，不能证明安装、删除等变更。
    proven_reads = {"web_search": r"搜|查", "find_skills": r"搜|查|找", "read_skill": r"查|读", "recall_memory": r"查|找"}
    def proven(claim):
        if re.search(r"下载|更新|补充|入库|收藏|保存|删|清空|安装|修复|修改|发送|发给|提醒|设置|创建|执行|运行", claim):
            return False
        return any(name in proven_reads and isinstance(result, dict) and not result.get("error")
                   and re.search(proven_reads[name], claim) for name, result in receipts)
    if claims and not internal_refusal and all(proven(claim) for claim in claims):
        return output
    if rewrite is not None:
        revised = rewrite()
        if revised and revised != output:
            return guard(revised, receipts, user_text)
    failures = [r.get("error") for _, r in receipts if isinstance(r, dict) and r.get("error")]
    if failures:
        return "工具执行失败，没有确认完成：" + str(failures[-1])[:100]
    return "这件事还没实际执行，暂时没有结果。"
