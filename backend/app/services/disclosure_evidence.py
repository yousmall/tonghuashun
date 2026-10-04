"""Extract narrowly explicit report statements; missing text remains unknown."""
from __future__ import annotations

import hashlib
import json
import re

from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import FactRecord
from backend.app.services.disclosure_reader import allowed_disclosure_url

PATTERNS = {
    'audit_opinion': {
        'unqualified': r'审计意见[\s|：:]*((?:标准)?无保留意见)',
        'qualified': r'审计意见[\s|：:]*保留意见',
        'adverse': r'审计意见[\s|：:]*否定意见',
        'disclaimer': r'审计意见[\s|：:]*无法表示意见',
    },
    'regulatory_status': {
        'explicitly_clear': r'(?:报告期内[，,\s]*公司[，,\s]*(?:未受到(?:任何)?(?:行政处罚|监管处罚)|不存在(?:被行政处罚|受到行政处罚)的情形)|是否存在被调查处罚的事项\s*(?:□\s*是\s*)?√\s*否)',
        'penalty': r'报告期内[，,\s]*公司[，,\s]*(?:受到|被处以|被给予)[^。；\n]{0,40}(?:行政处罚|监管处罚)',
    },
    'disclosure_status': {
        'timely': r'(?:报告期内[，,\s]*公司[，,\s]*(?:及时披露(?:了)?(?:各项)?定期报告|不存在未按规定披露定期报告的情形)|报告期内是否按规定披露定期报告\s*√\s*是)',
        'delayed': r'报告期内[，,\s]*公司[，,\s]*(?:未按规定披露定期报告|延迟披露(?:了)?定期报告)',
    },
}


def derive_disclosure_assessments(facts, *, entity, code, now):
    """Report-scoped governance only; never turn search absence into clearance.

    A full assessment requires all three statements in the same official PDF.
    Partial criteria still remain visible to the semantic evidence assessor.
    """
    groups = {}
    for doc in facts:
        if (doc.field != 'announcement_excerpt' or doc.entity != entity or doc.entity_code != code
                or not isinstance(doc.value, str) or not doc.period or not doc.source_url
                or not allowed_disclosure_url(doc.source_url) or not fact_is_current(doc, now)):
            continue
        key = (doc.source_url, doc.period)
        found = groups.setdefault(key, {})
        for criterion, labels in PATTERNS.items():
            for label, pattern in labels.items():
                for match in re.finditer(pattern, doc.value):
                    # Advisory/example text is not a statement of reported fact.
                    if any(word in doc.value[max(0,match.start()-12):match.start()] for word in ('如果','假设','例如','应当')):
                        continue
                    if criterion == 'audit_opinion' and '内部控制' in doc.value[max(0,match.start()-20):match.start()]:
                        continue
                    found.setdefault(criterion, []).append((label, match.group(), doc))
    output = []
    for (url, period), found in groups.items():
        items, parents = {}, []
        for criterion, records in found.items():
            if len({label for label,quote,doc in records}) != 1:
                continue
            label, quote, doc = max(records, key=lambda record:record[2].snapshot_time)
            items[criterion] = {'label':label,'evidence_id':doc.fact_id,'quote':quote}
            parents.append(doc)
            identity = json.dumps([entity,code,criterion,items[criterion]],ensure_ascii=False,sort_keys=True)
            output.append(FactRecord(fact_id='DISCLOSURE-CRITERION-'+hashlib.sha256(identity.encode()).hexdigest()[:24],
                entity=entity,entity_code=code,field=criterion+'_evidence',value=items[criterion],
                period=period,snapshot_time=doc.snapshot_time,source_url=url,quality=doc.quality*.9,
                source_id='DERIVED_EXPLICIT_DISCLOSURE_V1',derived_from=[doc.fact_id],
                derivation_rule='EXPLICIT_REPORT_STATEMENT_V1: exact quoted statement; report scope only'))
        if set(items) == set(PATTERNS):
            value = {'rubric':'GOVERNANCE_V1','complete':True,'scope':'same disclosed report period',**items}
            identity = json.dumps([entity,code,url,period,value],ensure_ascii=False,sort_keys=True)
            output.append(FactRecord(fact_id='DISCLOSURE-ASSESS-'+hashlib.sha256(identity.encode()).hexdigest()[:24],
                entity=entity,entity_code=code,field='governance_assessment',value=value,period=period,
                snapshot_time=min(doc.snapshot_time for doc in parents),source_url=url,
                quality=min(doc.quality for doc in parents)*.9,source_id='DERIVED_EXPLICIT_DISCLOSURE_V1',
                derived_from=sorted({doc.fact_id for doc in parents}),
                derivation_rule='EXPLICIT_REPORT_GOVERNANCE_V1: all three exact statements from same official report; report scope only'))
    return list({fact.fact_id:fact for fact in output}.values())
