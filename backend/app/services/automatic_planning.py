"""有原文依据的偏好识别、收益目标分析与用款期限规划；不生成承诺收益。"""
from __future__ import annotations

import calendar
import math
import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION = "automatic-planning-v1"
NUMBER = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+|半)"


class AutomaticPlanningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narrative: str = Field(default="", max_length=2000)
    questionnaire_version: str | None = None
    risk_answers: dict[str, str] | None = None

    @field_validator("risk_answers")
    @classmethod
    def validate_answers(cls, value):
        from backend.app.risk_questionnaire import validate_answers
        return validate_answers(value) if value is not None else None


def _number(text):
    if text == "半":
        return .5
    digits = {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    if "百" in text:
        left, right = text.split("百", 1)
        tail = _number(right.lstrip("零")) if right.lstrip("零") else 0
        return digits.get(left, 1)*100 + (tail or 0)
    if "十" in text:
        left, right = text.split("十", 1)
        return digits.get(left, 1)*10 + digits.get(right, 0)
    if text in digits:
        return digits[text]
    try:
        return float(text)
    except ValueError:
        return None


def _add_months(day, months):
    year, month = divmod(day.year*12+day.month-1+months, 12)
    return date(year, month+1, min(day.day, calendar.monthrange(year,month+1)[1]))


def _negated(text, start):
    return bool(re.search(r"(?:不是|并非|不想|不愿|不要|没有|不追求|不希望|不期望|不算|不)[^，。；]{0,2}$", text[max(0,start-6):start]))


def extract_planning_inputs(text):
    """只取用户明确的偏好/目标；忽略历史收益、投资经历及被否定的目标。"""
    inputs, evidence, warnings = {}, [], []
    styles = []
    for label, pattern in (("保守倾向", r"不能亏本金|不能接受(?:任何)?(?:亏损|回撤)|本金安全|保本优先|保守|低风险"),
                           ("稳健倾向", r"稳健|稳妥|平衡型"), ("积极倾向", r"积极型|进取|激进|高风险")):
        for match in re.finditer(pattern, text):
            if not _negated(text, match.start()):
                styles.append(label)
                evidence.append({"field":"risk_preference", "source":"用户描述", "text":match[0]})
    if len(set(styles)) == 1:
        inputs["preference"] = styles[0]
    elif len(set(styles)) > 1:
        inputs["preference_conflict"] = True
        warnings.append("描述中包含不同风险偏好，请核对主要偏好；未擅自选择其中一个。")
    for match in re.finditer(r"(?:不能接受(?:任何)?(?:亏损|回撤)|不能亏本金|不接受亏损)", text):
        if _negated(text, match.start()):
            continue
        inputs["drawdown"] = 0.0
        evidence.append({"field":"drawdown", "source":"用户描述", "text":match[0]})
    rates = []
    pattern = r"(?:期望|预期|目标|希望|追求|要求)[^，。；]{0,8}?(?:年化|年收益|年回报)[^，。；\d-]{0,8}(-?\d+(?:\.\d+)?)\s*[%％](?:\s*[-–—~至到]\s*(-?\d+(?:\.\d+)?)\s*[%％])?"
    for match in re.finditer(pattern, text):
        if _negated(text, match.start()):
            continue
        low, high = float(match[1])/100, float(match[2])/100 if match[2] else float(match[1])/100
        if not -1 <= low <= high <= 5:
            warnings.append("收益目标区间无效或过大，请用明确百分比重新描述。")
            continue
        suffix = re.match(r"以上|及以上|以内|以下", text[match.end():])
        context = text[max(0,match.start()-4):match.end()] + (suffix[0] if suffix else "")
        if not match[2]:
            if re.search(r"不超过|至多|不高于|以内|以下", context):
                low = None
            elif re.search(r"至少|超过|不低于|以上", context):
                high = None
        rates.append((low,high))
        evidence.append({"field":"expected_return", "source":"用户描述", "text":match[0]})
    if rates and len(set(rates)) == 1:
        inputs["return_min"], inputs["return_max"] = rates[0]
    elif rates:
        inputs["return_conflict"] = True
        warnings.append("发现多个不同收益目标，未自动合并，请确认主要目标。")
    for match in re.finditer(r"无固定收益目标|没有固定收益(?:目标|要求)|不设收益目标", text):
        if not _negated(text, match.start()):
            inputs["no_fixed_return"] = True
            evidence.append({"field":"expected_return", "source":"用户描述", "text":match[0]})
    tolerances = []
    for match in re.finditer(r"(?:可接受|能承受|最多承受|最大(?:可接受)?|回撤上限)[^，。；\d]{0,10}(\d+(?:\.\d+)?)\s*[%％]", text):
        if _negated(text, match.start()):
            continue
        if re.search(r"过去|历史|曾经|上次", text[max(0,match.start()-6):match.start()]):
            continue
        if not any(word in match[0] for word in ("亏损", "损失", "回撤")):
            continue
        value = float(match[1])/100
        if 0 <= value <= 1:
            tolerances.append(value)
            evidence.append({"field":"drawdown", "source":"用户描述", "text":match[0]})
    if tolerances:
        candidates = set(tolerances + ([inputs["drawdown"]] if "drawdown" in inputs else []))
        if len(candidates) == 1:
            inputs["drawdown"] = tolerances[0]
        else:
            inputs.pop("drawdown", None)
            inputs["drawdown_conflict"] = True
            warnings.append("回撤边界存在矛盾，请核对；未擅自采用更宽松的损失额度。")
    durations = []
    pattern = rf"({NUMBER})(?:\s*(年|个月|月)?\s*[-–—~至到]\s*({NUMBER}))?\s*(年|个月|月)(半)?"
    for match in re.finditer(pattern, text):
        before = re.split(r"[，。；,;\n]", text[:match.start()])[-1][-14:]
        after = re.split(r"[，。；,;\n]", text[match.end():])[0][:22]
        if _negated(text, match.start()) or re.search(r"不打算|不计划|不准备", before):
            continue
        if re.search(r"过去|经验|已有|已经|投资了|从业", before) or re.match(r"(?:的)?(?:投资|交易)?经验", after):
            continue
        if not (re.search(r"计划|打算|投资|持有|期限|至少|超过", before) or re.match(r"后|内|以后|以内|以上|不动用|不用", after)):
            continue
        first, last = _number(match[1]), _number(match[3]) if match[3] else _number(match[1])
        if first is None or last is None:
            continue
        multiplier = 12 if match[4] == "年" else 1
        first_multiplier = 12 if (match[2] or match[4]) == "年" else 1
        low, high = first*first_multiplier, (last + (.5 if match[5] else 0))*multiplier
        if match[5] and not match[3]:
            low = high
        if not (1 <= low <= high <= 1200 and float(low).is_integer() and float(high).is_integer()):
            continue
        low, high = int(low), int(high)
        exact = not match[3]
        if re.match(r"(?:以?内)?(?:不动用|不用|无需用)", after) or re.match(r"以上", after) or re.search(r"(?:至少|超过)(?:投资|持有)?$", before):
            high, exact = None, False
        elif re.match(r"以内|内", after) and not re.match(r"内不|内无需|内不用", after):
            low, exact = 0, False
        kind = "用款时间" if re.search(r"买房|购房|用钱|用款|需要|教育|支出|偿债|结婚", before+after) else "拟投资期限"
        durations.append({"min":low,"max":high,"exact":exact,"kind":kind,"text":before+match[0]+after})
    if durations:
        inputs["durations"] = durations
        evidence.extend({"field":"horizon", "source":"用户描述", "text":item["text"], "kind":item["kind"]} for item in durations)
    for label, pattern, field in (("投资本金", r"(?:可投资(?:资金)?|计划投入|投资本金|本金(?:为|有))\s*(\d+(?:\.\d+)?)\s*(万元|万|元)", "capital"),
                                   ("目标金额", r"(?:目标金额(?:为)?|需要资金|到期需要)(\d+(?:\.\d+)?)\s*(万|万元|元)", "goal_amount")):
        amounts = []
        for match in re.finditer(pattern,text):
            if _negated(text, match.start()):
                continue
            value = float(match[1])*(10000 if match[2].startswith("万") else 1)
            if .01 <= value <= 1e12:
                amounts.append(value)
                evidence.append({"field":field,"source":"用户描述","text":match[0]})
        if len(set(amounts)) == 1:
            inputs[field] = amounts[0]
        elif amounts:
            warnings.append(f"发现多个不同{label}，未自动合并金额，请明确对应哪项计划。")
    return inputs, evidence, warnings


def build_automatic_planning(profile=None, *, narrative="", risk_answers=None, today=None):
    from backend.app.risk_questionnaire import QUESTION_BY_ID, assessment_is_current, local_today
    today = today or local_today()
    profile = profile or {}
    answers = risk_answers if risk_answers is not None else profile.get("risk_answers", {})
    inputs, evidence, warnings = extract_planning_inputs(narrative)
    plan = profile.get("financial_plan") or {}
    def answer(qid):
        selected = answers.get(qid)
        if selected:
            text = QUESTION_BY_ID[qid].options[ord(selected)-65]
            evidence.append({"field":qid,"source":"问卷","text":text})
            return selected, text
        return None, None
    risk_level = profile.get("risk_level")
    preference = inputs.get("preference")
    drawdown = inputs.get("drawdown", profile.get("max_drawdown"))
    q26, _ = answer("q26")
    q14, _ = answer("q14")
    q15, _ = answer("q15")
    if "drawdown" not in inputs and q26:
        drawdown = {"A":0, "B":.05, "C":.10, "D":.20, "E":None}[q26]
    if inputs.get("drawdown_conflict"):
        drawdown = None
    if preference is None and not inputs.get("preference_conflict"):
        if drawdown == 0 or q15 == "A" or q14 == "A":
            preference = "保守倾向"
        elif q14:
            preference = {"B":"稳健倾向", "C":"积极倾向", "D":"积极倾向"}[q14]
    if preference is None and profile.get("risk_description") and not inputs.get("preference_conflict"):
        preference = profile["risk_description"]
    missing = []
    if not preference:
        missing.append("请明确偏好本金安全、稳健还是愿意承担较大波动。")
    if not risk_level:
        missing.append("完成风险问卷后才能形成风险承受能力分档。")
    elif not assessment_is_current(profile, today=today):
        missing.append("风险测评待确认或已过期，请核对并保存当前问卷。")
    if drawdown is None:
        missing.append("请明确当前最大可接受回撤；未明确的损失边界不自动放宽。")
    low, high = inputs.get("return_min"), inputs.get("return_max")
    q27, _ = answer("q27")
    safety_first = q27 == "A"
    no_fixed_target = safety_first or bool(inputs.get("no_fixed_return"))
    if "return_max" not in inputs:
        if q27:
            low, high = {"A":(None,None),"B":(None,.05),"C":(.05,.10),"D":(.10,.20),"E":(.20,None)}[q27]
        elif profile.get("expected_annual_return") is not None:
            low = high = profile["expected_annual_return"]
        else:
            low, high = plan.get("expected_return_min"), plan.get("expected_return_max")
    if inputs.get("return_conflict"):
        low = high = None
        safety_first = False
        no_fixed_target = False
    elif inputs.get("no_fixed_return"):
        if "return_max" in inputs:
            warnings.append("描述同时包含无固定收益目标和具体收益目标，请核对主要目标。")
            low = high = None
            safety_first = False
            no_fixed_target = False
        else:
            low = high = None
    if low is None and high is None and not no_fixed_target:
        missing.append("请补充期望年化收益或明确本金安全优先、无固定收益目标。")
    durations = list(inputs.get("durations", []))
    if not durations or answers:
        for qid, kind, mapping in (("q11","拟投资期限",{"A":(0,12),"B":(12,60),"C":(None,None)}),
                                  ("q25","用款时间",{"A":(0,12),"B":(12,36),"C":(36,60),"D":(60,None),"E":(None,None)})):
            selected, text = answer(qid)
            if selected:
                minimum, maximum = mapping[selected]
                if minimum is not None or maximum is not None:
                    durations.append({"min":minimum,"max":maximum,"exact":False,"kind":kind,"text":text})
        if not durations and profile.get("horizon_months"):
            months = profile["horizon_months"]
            durations.append({"min":months,"max":months,"exact":True,"kind":"已记录期限","text":f"{months}个月"})
    finite = [item for item in durations if item["max"] is not None]
    horizon = min(finite, key=lambda item:(item["max"],item["exact"])) if finite else durations[0] if durations else None
    months = horizon["max"] if horizon else None
    exact_months = months if horizon and horizon["exact"] else None
    if horizon is None:
        missing.append("请补充最早用款时间，或计划持有的年数/月数。")
    if len({item["max"] for item in finite}) > 1:
        warnings.append("拟投资期限与用款时间不同，规划按更早的资金需求安排。")
    minimum_goal = low
    if minimum_goal is not None and (minimum_goal >= .10 and (drawdown is not None and drawdown <= .10 or risk_level in {"R1","R2"})
                                      or minimum_goal > .20 or months is not None and months <= 12 and minimum_goal >= .10):
        warnings.append("收益目标与期限或可接受损失之间存在张力，请优先复核目标和资金用途；不能通过提高目标保证收益。")
    warnings.extend(profile.get("financial_warnings") or [])
    if preference == "积极倾向" and drawdown == 0:
        warnings.append("积极偏好与零回撤要求存在矛盾，请先核对可接受损失边界。")
    if q27 == "A" and high is not None and high > 0:
        warnings.append("当前收益目标与问卷中的本金安全优先选择不同，请核对最新目标。")
    if "drawdown" in inputs and q26 and drawdown != {"A":0,"B":.05,"C":.10,"D":.20,"E":None}[q26]:
        warnings.append("描述中的回撤边界与已答问卷不同，本预览不会改写已保存的风险测评。")
    milestones = [{"when":"现在","date":today.isoformat(),"action":"区分近期必用资金、应急储备与可长期投资资金，核对目标及损失边界。"},
                  {"when":"每3个月复核","date":None,"action":"复核收入、负债、支出和目标变化；有变化时重新识别偏好与期限。"}]
    if exact_months:
        for offset, label, action in ((max(0,exact_months-6),"用款前6个月" if exact_months > 6 else "现在起安排用款","核对持有期、赎回到账与用款安排，逐步安排所需流动资金。"),
                                      (max(0,exact_months-1),"用款前1个月","确认到账时间和必要支出金额，避免到期时被迫变现。"),
                                      (exact_months,"计划用款日","复核资金可用性；日期由当前描述的明确月数换算。")):
            milestones.append({"when":label,"date":_add_months(today,offset).isoformat(),"action":action})
    elif horizon:
        milestones.extend([{"when":"最早用款前6个月","date":None,"action":"按实际用款日倒排流动性安排；当前为区间，不指定虚构日期。"},
                           {"when":"目标用款区间","date":None,"action":horizon["text"]}])
    scenario = None
    if inputs.get("capital") and high is not None and low == high and exact_months:
        value = inputs["capital"] * (1+high)**(exact_months/12)
        if math.isfinite(value):
            scenario = {"principal":inputs["capital"],"annual_assumption":high,"months":exact_months,
                        "ending_amount":round(value,2),"profit":round(value-inputs["capital"],2),
                        "basis":"仅按用户目标进行复利算术演示，不是收益预测，未计税费。"}
    required = None
    if inputs.get("capital") and inputs.get("goal_amount") and exact_months:
        required = (inputs["goal_amount"]/inputs["capital"])**(12/exact_months)-1
        if high is not None and required > high + 1e-8:
            warnings.append("按一次投入且无追加资金计算，目标金额所需的年化收益高于您描述的收益目标，请复核金额、期限或追加投入计划。")
    return {"rule_version":VERSION,"generated_on":today.isoformat(),"status":"needs_information" if missing else "ready",
            "risk_preference":{"label":preference or "待识别","capacity_level":risk_level,
                "capacity_label":profile.get("risk_description"),"confirmed_assessment":assessment_is_current(profile, today=today),
                "drawdown_limit":drawdown,"source":"用户明确描述" if inputs.get("preference") else "问卷或已记录资料"},
            "return_expectation":{"min_annual":low,"max_annual":high,"safety_first":safety_first,
                "label":("本金安全优先，无固定收益目标" if safety_first and low is None and high is None else
                         "无固定收益目标" if no_fixed_target and low is None and high is None else
                         f"{low:.1%}" if low is not None and low == high else
                         f"{low:.1%}–{high:.1%}" if low is not None and high is not None else
                         f"不超过{high:.1%}" if high is not None else f"超过{low:.1%}" if low is not None else "待补充"),
                "scenario":scenario,"required_annual_for_goal":required,
                "notice":"期望收益是个人目标，不能从风险档位推算成市场收益预测。"},
            "horizon_plan":{"label":horizon["text"] if horizon else "待补充","exact_months":exact_months,
                "upper_bound_months":months,"inputs":durations,"milestones":milestones},
            "evidence":evidence,"missing_information":list(dict.fromkeys(missing)),
            "warnings":list(dict.fromkeys(warnings)),"inputs":inputs}
