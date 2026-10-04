"""原19题风险评分，加8题财务规划补充问卷及适当性分类。

题目来自用户提供的方正证券截图。官网只核实了分类阈值及匹配规则，
未公开本版逐选项分值；下面的分值为本平台规则，不是方正官方评分。
期限、经验和损失偏好保留原始区间，不能转换成用户未填写的精确数值。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

LEGACY_QUESTIONNAIRE_VERSION = "fangzheng-19-platform-v1"
QUESTIONNAIRE_VERSION = "platform-27-v2"
SCORING_NOTICE = "前19题依据所提供的方正证券界面，后8题为本平台财务规划补充题；评分和配置约束采用本平台规则，非方正证券官方测评。"
CLASSIFICATION_URL = "https://www.foundersc.com/invEduSuitMagMethodSolve/55722_3.jhtml"
MATCHING_URL = "https://www.foundersc.com/fzhtml/sdxgl/6/E/D/26WH5HP.html"
RISK_NAMES = ("保守型", "谨慎型", "稳健型", "积极型", "激进型")


@dataclass(frozen=True)
class Question:
    id: str
    title: str
    options: tuple[str, ...]
    points: tuple[float, ...]

    @property
    def labels(self) -> dict[str, str]:
        return {chr(65 + i): f"{chr(65 + i)}. {text}" for i, text in enumerate(self.options)}


BASE_QUESTIONS = (
    Question("q01", "您的主要收入来源是：", (
        "工资、劳务报酬", "生产经营所得", "利息、股息、转让证券等金融性资产收入",
        "出租、出售房地产等非金融性资产收入", "无固定收入"), (5, 4, 3, 3, 0)),
    Question("q02", "最近您家庭预计进行证券投资的资金占家庭现有总资产（不含自住、自用房产及汽车等固定资产）的比例是：", (
        "70%以上", "50%–70%", "30%–50%", "10%–30%", "10%以下"), (0, 1, 2, 4, 5)),
    Question("q03", "您是否有尚未清偿的数额较大的债务，如有，其性质是：", (
        "没有", "有，住房抵押贷款等长期定额债务", "有，信用卡欠款、消费信贷等短期信用债务", "有，亲朋之间借款"), (5, 3, 0, 1)),
    Question("q04", "您可用于投资的资产数额（包括金融资产和不动产）为：", (
        "不超过50万元人民币", "50万–300万元（不含）人民币", "300万–1000万元（不含）人民币", "1000万元人民币以上"), (0, 2, 4, 5)),
    Question("q05", "您的最高学历是：", ("高中或以下", "大学专科", "大学本科", "硕士及以上"), (0, 2, 4, 5)),
    Question("q06", "您的投资经验可以被概括为：", ("基本没有", "一般", "丰富", "专业"), (0, 2, 4, 5)),
    Question("q07", "有一位投资者一个月内做了15笔交易（同一品种买卖各一次算一笔），您认为这样的交易频率：", (
        "太高了", "偏高", "正常", "偏低"), (0, 2, 4, 5)),
    Question("q08", "过去一年时间内，您购买的不同产品或接受的不同服务（含同一类型的不同产品或服务）的数量是：", (
        "5个以下", "6至10个", "11至15个", "16个以上"), (0, 2, 4, 5)),
    Question("q09", "您有多少年投资股票、基金、外汇、金融衍生产品等风险投资品的经验：", (
        "8年以上", "5–8年", "2–5年", "少于2年", "没有经验"), (5, 4, 3, 1, 0)),
    Question("q10", "如果您曾经从事过金融市场投资，在交易较为活跃的月份，平均月交易额大概是多少：", (
        "10万元以内", "10万元–30万元", "30万元–100万元", "100万元以上", "从未从事过金融市场投资"), (1, 2, 4, 5, 0)),
    Question("q11", "您用于证券投资的大部分资金不会用作其它用途的时间段为：（提示：建议您结合自身投资行为审慎填写。您的拟投资期限之后将作为您购买产品或接受服务时重要适当性匹配要素。）", (
        "0到1年", "1到5年", "无特别要求"), (0, 0, 0)),
    Question("q12", "您打算投资于哪些种类的投资品种：", (
        "固定收益凭证，债券、货币市场基金、债券型基金或其它固定收益类产品",
        "股票、混合型基金、偏股型基金、股票型基金等权益类投资品种；股票质押式回购、约定购回式证券交易、股权激励融资等融资类业务；第1选项所有品种",
        "融资融券、期货、期权；商品及金融衍生产品类资产管理产品等衍生品类金融产品；第2选项所有品种",
        "结构化金融产品、场外衍生品等复杂或高风险金融产品或业务；其他金融产品或业务；第3选项所有品种"), (0, 0, 0, 0)),
    Question("q13", "假设有两种不同的投资：投资A预期获得5%的收益，有可能承担非常小的损失；投资B预期获得20%的收益，但有可能面临25%甚至更高的亏损。您将您的投资资产分配为：", (
        "全部投资于A", "大部分投资于A", "两种投资各一半", "大部分投资于B", "全部投资于B"), (0, 2.5, 5, 7.5, 10)),
    Question("q14", "当您进行投资时，您的首要目标是：", (
        "尽可能保证本金安全，不在乎收益率比较低", "产生一定的收益，可以承担一定的投资风险",
        "产生较多的收益，可以承担较大的投资风险", "实现资产大幅增长，愿意承担很大的投资风险"), (0, 3, 7, 10)),
    Question("q15", "您认为自己能承受的最大投资损失是多少？", (
        "尽可能保证本金安全", "一定的投资损失", "较大的投资损失", "损失可能超过本金"), (0, 3, 7, 10)),
    Question("q16", "您打算将自己的投资回报主要用于：", (
        "改善生活", "个体生产经营或证券投资以外的投资行为", "履行扶养、抚养或赡养义务", "本人养老或医疗", "偿付债务"), (4, 5, 2, 1, 0)),
    Question("q17", "您的年龄是：", ("18–30岁", "31–40岁", "41–50岁", "51–60岁", "18以下或60以上"), (4, 5, 4, 2, 0)),
    Question("q18", "以下描述中何种符合您的实际情况：", (
        "现在或此前曾从事金融、经济或财会等与金融产品投资相关的工作超过两年",
        "已取得金融、经济或财会等与金融产品投资相关专业学士以上学位",
        "取得证券从业资格、期货从业资格、注册会计师证书（CPA）或注册金融分析师证书（CFA）中的一项及以上",
        "我不符合以上任何一项描述"), (5, 5, 5, 0)),
    Question("q19", "您的家庭年收入：", ("大于100万", "51–100万", "21–50万", "5–20万", "小于5万"), (5, 4, 3, 1, 0)),
)
SUPPLEMENTARY_QUESTIONS = (
    Question("q20", "您本次计划投入的投资资金总额（不含日常生活资金及已有应急储备）为：", (
        "不足5万元", "5万元至不足20万元", "20万元至不足50万元", "50万元至不足100万元", "100万元及以上"), (0, 0, 0, 0, 0)),
    Question("q21", "未来12个月必须用于生活、大额支出或到期偿债的资金，占上述投资资金的比例为：", (
        "没有此类支出", "不超过10%", "超过10%但不超过30%", "超过30%但不超过50%", "超过50%"), (0, 0, 0, 0, 0)),
    Question("q22", "在上述投资资金之外，您已有的可随时动用的应急储备能覆盖家庭必要支出的时间为：", (
        "不足1个月", "1个月至不足3个月", "3个月至不足6个月", "6个月及以上"), (0, 0, 0, 0)),
    Question("q23", "您家庭每月必须偿还的债务，占稳定月收入的比例为：", (
        "没有还款负担", "不超过20%", "超过20%但不超过40%", "超过40%但不超过60%", "超过60%，或稳定收入不足以覆盖还款"), (0, 0, 0, 0, 0)),
    Question("q24", "上述投资资金的首要用途或规划目标是：", (
        "日常备用或短期支出", "购房或其他大额消费", "教育支出", "养老安排", "长期财富积累"), (0, 0, 0, 0, 0)),
    Question("q25", "为实现上述目标，您最早需要动用这笔投资资金的时间为：", (
        "1年以内", "超过1年但不超过3年", "超过3年但不超过5年", "5年以上", "暂未明确"), (0, 0, 0, 0, 0)),
    Question("q26", "您能接受投资组合从阶段高点回落的最大幅度（回撤）为：", (
        "不能接受回撤", "不超过5%", "不超过10%", "不超过20%", "可接受20%以上的回撤，暂不设明确上限"), (0, 0, 0, 0, 0)),
    Question("q27", "您的期望年化收益范围为（仅记录目标，不代表能够实现）：", (
        "本金安全优先，没有固定收益目标", "不超过5%", "超过5%但不超过10%", "超过10%但不超过20%", "超过20%"), (0, 0, 0, 0, 0)),
)
QUESTIONS = BASE_QUESTIONS + SUPPLEMENTARY_QUESTIONS
QUESTION_BY_ID = {q.id: q for q in QUESTIONS}
PRODUCT_GROUPS = ("固定收益类", "权益类", "混合类", "融资及衍生品类", "复杂或高风险类")


def validate_answers(answers: dict[str, str]) -> dict[str, str]:
    for key, choice in answers.items():
        if key not in QUESTION_BY_ID or choice not in QUESTION_BY_ID[key].labels:
            raise ValueError(f"无效问卷答案：{key}={choice}")
    return answers


def answer_issues(answers: dict[str, str]) -> list[str]:
    issues = []
    if answers.get("q03") == "A" and answers.get("q16") == "E":
        issues.append("第3题选择没有债务，与第16题偿付债务不一致，请核对。")
    if answers.get("q09") == "E" and answers.get("q10") not in (None, "E"):
        issues.append("第9题选择没有经验，与第10题交易额不一致，请核对。")
    if answers.get("q10") == "E" and answers.get("q09") not in (None, "E"):
        issues.append("第10题选择从未投资，与第9题投资年限不一致，请核对。")
    if answers.get("q03") == "A" and answers.get("q23") not in (None, "A"):
        issues.append("第3题选择没有债务，与第23题存在还款负担不一致，请核对。")
    return issues


def local_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def risk_band(score: float) -> int:
    return 1 if score < 20 else 2 if score < 37 else 3 if score < 54 else 4 if score < 83 else 5


def evaluate_answers(answers: dict[str, str], *, today: date | None = None) -> dict:
    validate_answers(answers)
    if len(answers) != len(QUESTIONS):
        raise ValueError(f"请完成全部{len(QUESTIONS)}道题后再提交。")
    issues = answer_issues(answers)
    if issues:
        raise ValueError("；".join(issues))
    score = round(sum(q.points[ord(answers[q.id]) - 65] for q in QUESTIONS), 2)
    # 明确偏向本金安全时采用保守档；不把定性损失转换成虚构的百分比。
    band = 1 if answers["q15"] == "A" or answers["q26"] == "A" else risk_band(score)
    assessed = today or local_today()
    try:
        expires = assessed.replace(year=assessed.year + 2)
    except ValueError:
        expires = assessed.replace(year=assessed.year + 2, day=28)
    groups = PRODUCT_GROUPS[:(1, 3, 4, 5)[ord(answers["q12"]) - 65]]
    horizon_label, horizon_min, horizon_max = (("0到1年", 0, 12), ("1到5年", 12, 60), ("无特别要求", None, None))[ord(answers["q11"]) - 65]
    values = {
        "questionnaire_version": QUESTIONNAIRE_VERSION, "risk_answers": dict(answers),
        "questionnaire_details": {q.title: q.options[ord(answers[q.id]) - 65] for q in QUESTIONS},
        "risk_score": score, "risk_level": f"R{band}", "investor_category": f"C{band}",
        "risk_description": RISK_NAMES[band - 1], "suitable_product_levels": [f"R{i}" for i in range(1, band + 1)],
        "preferred_product_types": list(groups), "investment_horizon_label": horizon_label,
        "horizon_min_months": horizon_min, "horizon_max_months": horizon_max,
        "loss_tolerance_label": QUESTION_BY_ID["q15"].options[ord(answers["q15"]) - 65],
        "experience_label": QUESTION_BY_ID["q09"].options[ord(answers["q09"]) - 65],
        "target": QUESTION_BY_ID["q16"].options[ord(answers["q16"]) - 65],
        "assessed_on": assessed, "valid_until": expires,
        "scoring_method": "platform-19-v1", "scoring_notice": SCORING_NOTICE,
    }
    score_groups = (
        ("财务基础", ("q01", "q02", "q03", "q04", "q19")),
        ("知识与经验", ("q05", "q06", "q09", "q18")),
        ("交易行为", ("q07", "q08", "q10")),
        ("收益与损失偏好", ("q13", "q14", "q15")),
        ("资金用途与年龄", ("q16", "q17")),
    )
    values["scoring_breakdown"] = [{
        "dimension": name, "question_ids": list(qids),
        "points": sum(QUESTION_BY_ID[qid].points[ord(answers[qid]) - 65] for qid in qids),
        "maximum": sum(max(QUESTION_BY_ID[qid].points) for qid in qids),
    } for name, qids in score_groups]
    from backend.app.profile_guidance import build_profile_guidance
    values.update(build_profile_guidance(answers, values))
    return values


def assessment_is_current(profile, *, today: date | None = None) -> bool:
    """兼容旧档案；新问卷必须完整、有期限且尚未过期。"""
    get = profile.get if isinstance(profile, dict) else lambda key, default=None: getattr(profile, key, default)
    if not get("confirmed", False):
        return False
    if not get("questionnaire_version"):
        return True
    if get("questionnaire_version") != QUESTIONNAIRE_VERSION:
        return False
    answers = get("risk_answers", {})
    try:
        validate_answers(answers)
        if len(answers) != len(QUESTIONS) or answer_issues(answers):
            return False
        valid_until = date.fromisoformat(str(get("valid_until")))
        assessed_on = date.fromisoformat(str(get("assessed_on")))
        return assessed_on <= (today or local_today()) <= valid_until
    except (TypeError, ValueError):
        return False
