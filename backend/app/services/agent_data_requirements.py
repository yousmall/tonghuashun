"""专业 agent 只能请求与职责相关的问财只读能力。"""
from backend.app.models import ResearchCapability

C = ResearchCapability
AGENT_DATA_CAPABILITIES = {
    "market": (C.MACRO, C.QUOTE, C.NEWS, C.RESEARCH_REPORT),
    "industry": (C.INDUSTRY, C.NEWS, C.RESEARCH_REPORT),
    "security": (C.QUOTE, C.FINANCIAL, C.BASIC_INFO, C.COMPANY_OPERATIONS,
                 C.SHAREHOLDER_EQUITY, C.EVENT, C.INSTITUTIONAL_RESEARCH,
                 C.NEWS, C.RESEARCH_REPORT, C.ANNOUNCEMENT, C.CONVERTIBLE),
    "fund": (C.FUND, C.QUOTE, C.NEWS, C.ANNOUNCEMENT),
    "portfolio": (C.QUOTE, C.FINANCIAL, C.FUND, C.NEWS, C.ANNOUNCEMENT),
}


def agent_requirements(results):
    """服务端再次校验职责；只接受业务枚举，不接受模型的方法、URL 或参数。"""
    selected = {}
    for result in results:
        allowed = AGENT_DATA_CAPABILITIES.get(result.agent_id, ())
        capabilities = [cap for cap in result.data_requirements if cap in allowed]
        if capabilities:
            selected[result.agent_id] = list(dict.fromkeys([
                *selected.get(result.agent_id, []), *capabilities]))
    return selected
