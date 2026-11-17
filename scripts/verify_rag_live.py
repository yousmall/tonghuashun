"""Real localhost HTTP + configured model/provider acceptance. Never logs credentials.
Writes artifacts under outputs/testing; QA imports are explicitly labelled test data.
"""
from __future__ import annotations
import base64
import argparse
from datetime import datetime,timedelta,timezone
from io import BytesIO
import json
from pathlib import Path
import secrets
import sys
from time import perf_counter
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.app.risk_questionnaire import QUESTIONNAIRE_VERSION,QUESTIONS
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--reuse-qa',action='store_true')
parser.add_argument('--output',type=Path,default=ROOT/'outputs/testing/rag-live-20261117')
ARGS=parser.parse_args()
OUT=ARGS.output.resolve()
PRIVATE=ROOT/'.tmp_test/rag-live-credentials.json'
OUT.mkdir(parents=True,exist_ok=True); PRIVATE.parent.mkdir(parents=True,exist_ok=True)
report={'created_at':datetime.now(timezone(timedelta(hours=8))).isoformat(), 'transport':'real_localhost_http',
        'fake_encoder':False,'mock_model':False,'mock_provider':False,'checks':[],'analyses':[]}

def save():
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

def check(name, passed, **details):
    row={'name':name,'passed':bool(passed),**details};report['checks'].append(row);save()
    print(json.dumps(row,ensure_ascii=True),flush=True)


def main():
    base='http://127.0.0.1:8000/api/v1'
    clients=[]
    def request(c,method,path,body=None,expected=200):
        response=c.request(method,base+path,json=body)
        if response.status_code!=expected:
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:350]}')
        return response.json()
    try:
        c=httpx.Client(timeout=900,trust_env=False);clients.append(c)
        ready=request(c,'GET','/readiness');report['readiness']=ready;save()
        check('real_dependencies_configured',ready.get('mysql_ready') and ready.get('third_party_llm_configured') and ready.get('iwencai_skillhub_configured'))
        check('unauthenticated_access',c.get(base+'/rag/status').status_code==401)
        stamp=datetime.now().strftime('%m%d_%H%M%S_')+secrets.token_hex(2)
        existing=json.loads(PRIVATE.read_text(encoding='utf-8')) if ARGS.reuse_qa and PRIVATE.exists() else None
        credentials=existing['primary'] if existing else {'username':'qa_rag_'+stamp,'password':secrets.token_urlsafe(24)}
        peer_credentials=existing['peer'] if existing else {'username':'qa_rag_peer_'+stamp,'password':secrets.token_urlsafe(24)}
        auth=request(c,'POST','/auth/login' if existing else '/auth/register',credentials);c.headers['Authorization']='Bearer '+auth['access_token']
        PRIVATE.write_text(json.dumps({'primary':credentials,'peer':peer_credentials},ensure_ascii=False),encoding='utf-8')
        report['test_account']=credentials['username'];report['test_user_id']=auth['user']['id'];save()
        peer=httpx.Client(timeout=60,trust_env=False);clients.append(peer)
        peer_auth=request(peer,'POST','/auth/login' if existing else '/auth/register',peer_credentials);peer.headers['Authorization']='Bearer '+peer_auth['access_token']
        answers=dict(zip((q.id for q in QUESTIONS),'ACABDC CACCB BCCBC CDB CADAECDC'.replace(' ',''),strict=True))
        old_profile=request(c,'GET','/profile').get('profile') or {}
        assessment=request(c,'POST','/profile/confirm',{'profile':{'questionnaire_version':QUESTIONNAIRE_VERSION,'risk_answers':answers,'version':old_profile.get('version',1),'confirmed':False}})
        profile={'confirmed':True,'version':assessment['version']}
        report['profile_confirmed']=True
        status=request(c,'GET','/rag/status');report['initial_rag_status']=status
        check('real_bge_model_ready',status.get('ready') and status.get('dimension')==512,model=status.get('model'))
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        doc={'title':'QA贵州茅台现金流来源核验说明（测试资料，非公告）','entity':'贵州茅台','entity_code':'600519.SH','asset_type':'stock',
             'as_of':today,'text':'本资料由自动化验收编写，只验证知识库检索和来源标注，不是贵州茅台的财务报告。现金流风险分析需要核对经营活动现金流、利润质量、负债和报告期。应回到本轮真实财报和公告核验数字，缺少证据时明确说明缺口。不得将测试资料当成公司现状或投资建议。'}
        start=perf_counter();added=request(c,'POST','/rag/documents',doc)
        check('text_import_real_embeddings',bool(added.get('id')),seconds=round(perf_counter()-start,3),chunks=added.get('chunks'))
        check('idempotent_reimport',not request(c,'POST','/rag/documents',doc).get('indexed'))
        md='# QA检索接口验收\n\n'+('这是知识库系统验收用Markdown资料，不是实际投资结论。资料原文应保留来源和日期，检索结果需要定位到原文片段。\n'*25)
        md_added=request(c,'POST','/rag/documents',{'title':'QA Markdown分块与重叠验收','as_of':today,'filename':'qa.md','content_base64':base64.b64encode(md.encode()).decode()})
        md_meta=next((row for row in request(c,'GET','/rag/documents') if row['id']==md_added['id']),{})
        check('markdown_chunking',(md_added.get('chunks') or md_meta.get('chunks',0))>1,
              chunks=md_added.get('chunks') or md_meta.get('chunks'))
        from reportlab.pdfgen import canvas
        pdf=BytesIO();page=canvas.Canvas(pdf);page.drawString(60,740,'QA RAG PDF import: provenance and original document retention.');page.save()
        pdf_added=request(c,'POST','/rag/documents',{'title':'QA PDF原文导入验收','as_of':today,'filename':'qa.pdf','content_base64':base64.b64encode(pdf.getvalue()).decode()})
        pdf_meta=next((row for row in request(c,'GET','/rag/documents') if row['id']==pdf_added['id']),{})
        check('pdf_text_extraction',(pdf_added.get('chunks') or pdf_meta.get('chunks',0))>0)
        query={'query':'贵州茅台现金流风险需要如何核验来源与报告期','entity':'贵州茅台','entity_code':'600519','asset_type':'stock'}
        start=perf_counter();found=request(c,'POST','/rag/search',query)
        check('hybrid_retrieval_real_bge',any(row['document_id']==added['id'] for row in found['matches']),seconds=round(perf_counter()-start,3))
        (OUT/'retrieval.json').write_text(json.dumps(found,ensure_ascii=False,indent=2),encoding='utf-8')
        public=request(c,'POST','/rag/search',{'query':'如何衡量历史从高点下跌的最大幅度','top_k':1})
        check('chinese_semantic_paraphrase',any('最大回撤' in row['title'] for row in public['matches']))
        peer_docs=request(peer,'GET','/rag/documents');peer_search=request(peer,'POST','/rag/search',query)
        check('cross_account_isolation',not peer_docs and not any(row['document_id']==added['id'] for row in peer_search['matches']))
        check('cross_account_update_denied',peer.patch(base+'/rag/documents/'+added['id'],json={'enabled':False}).status_code==404)
        check('client_cannot_set_owner',c.post(base+'/rag/documents',json={**doc,'owner':peer_auth['user']['id']}).status_code==422)
        check('wrong_asset_excluded',not any(row['document_id']==added['id'] for row in request(c,'POST','/rag/search',{**query,'asset_type':'fund'})['matches']))
        request(c,'PATCH','/rag/documents/'+added['id'],{'enabled':False})
        check('disabled_document_excluded',not any(row['document_id']==added['id'] for row in request(c,'POST','/rag/search',query)['matches']))
        request(c,'PATCH','/rag/documents/'+added['id'],{'enabled':True})
        check('document_restore',any(row['document_id']==added['id'] for row in request(c,'POST','/rag/search',query)['matches']))
        stale={**doc,'title':'QA过期资料应排除','as_of':(datetime.now(timezone.utc).date()-timedelta(days=100)).isoformat()}
        stale_added=request(c,'POST','/rag/documents',stale)
        stale_query=request(c,'POST','/rag/search',query)
        check('stale_source_date_excluded',stale_added['id'] not in {row['document_id'] for row in stale_query['matches']} and stale_query['freshness_rejections']>0)
        # Known malformed files use test payloads; the live endpoint must reject them.
        check('invalid_file_rejected',c.post(base+'/rag/documents',json={'title':'QA bad','as_of':today,'filename':'bad.exe','content_base64':'eA=='}).status_code==422)
        cid='qa-rag-live-'+uuid4().hex;report['conversation_id']=cid;report['imported_ids']=[added['id'],md_added['id'],pdf_added['id'],stale_added['id']];save()
        common={'profile':profile,'research_mode':'deep','research_direction':'个股研究','research_target':'600519',
                'auto_fetch':True,'use_rag':True,'context_compression':True,'use_conversation_history':True,'conversation_id':cid}
        for index,question in enumerate(['分析下贵州茅台','继续分析它的现金流风险，结合本轮可核验资料说明缺口，不要编造数字']):
            payload={**common,'query':question}
            if index:
                # Explicit QA context tests compression with the real model. This is not fabricated market evidence.
                payload['context_messages']=[{'role':'user' if i%2==0 else 'assistant','content':('上下文验收：继续研究贵州茅台600519，资料日期和现金流数字必须核验，历史回答不能当作本轮事实。'*40)+f'第{i}段'} for i in range(10)]
            start=perf_counter();response=c.post(base+'/portfolio/analyze',json=payload);elapsed=perf_counter()-start
            (OUT/f'analysis-{index+1}.json').write_text(response.text,encoding='utf-8')
            if response.status_code!=200:
                check(f'deep_round_{index+1}_completed',False,http_status=response.status_code,seconds=round(elapsed,3),detail=response.text[:250]);continue
            advice=response.json();acquisition=advice.get('data_acquisition') or {}
            summary={'round':index+1,'query':question,'seconds':round(elapsed,3),'http_status':response.status_code,
                     'compliance':advice.get('compliance',{}).get('status'),'response_scope':advice.get('response_scope'),
                     'fact_count':len(advice.get('facts',[])),'source_ids':sorted({f['source_id'] for f in advice.get('facts',[])}),
                     'rag':{key:value for key,value in (advice.get('rag') or {}).items() if key!='matches'},
                     'rag_matches':len((advice.get('rag') or {}).get('matches',[])),
                     'context_compression':advice.get('context_compression'),'timings_ms':advice.get('timings_ms'),
                     'model_calls':advice.get('model_calls',[]),'acquisition':acquisition,'agents':[{'id':a.get('agent_id'),'status':a.get('status'),'facts_used':len(a.get('facts_used',[]))} for a in advice.get('agent_results',[])]}
            report['analyses'].append(summary);save()
            check(f'deep_round_{index+1}_completed',bool(advice.get('conclusion') and advice.get('risk_conclusion') and advice.get('intent')=='security_research' and advice.get('model_calls') and not all(call.get('status')=='failed' for call in advice['model_calls'])),seconds=round(elapsed,3),compliance=summary['compliance'])
            check(f'deep_round_{index+1}_used_rag',advice.get('rag',{}).get('status')=='searched' and summary['rag_matches']>0)
            check(f'deep_round_{index+1}_real_provider_facts',any(not f['source_id'].upper().startswith(('USER','PROJECT','DEMO','DERIVED','RAG')) for f in advice.get('facts',[])))
            specialist_ids={row['agent_id'] for row in advice.get('agent_results',[])}
            specialist_calls=[call for call in advice.get('model_calls',[]) if call.get('purpose') in specialist_ids]
            check(f'deep_round_{index+1}_specialists_really_completed',
                  bool(specialist_ids) and specialist_ids<={call.get('purpose') for call in specialist_calls}
                  and all(call.get('status')=='completed' for call in specialist_calls)
                  and all(row.get('details',{}).get('engine')!='rule_fallback' for row in advice.get('agent_results',[])))
            check(f'deep_round_{index+1}_all_model_calls_completed',bool(advice.get('model_calls'))
                  and all(call.get('status')=='completed' for call in advice['model_calls']))
            rag_passage_ids={row['fact']['fact_id'] for row in advice.get('rag',{}).get('matches',[])
                             if row['fact'].get('field') in {'rag_document_excerpt','rag_education_excerpt'}}
            sent_rag_ids={fact_id for call in specialist_calls if call.get('status')=='completed'
                          for fact_id in call.get('input_rag_fact_ids',[])}
            check(f'deep_round_{index+1}_retrieved_passages_sent_to_model',bool(rag_passage_ids & sent_rag_ids),
                  sent_rag_fact_count=len(sent_rag_ids))
            if index:
                audit=advice.get('context_compression') or {}
                check('same_chat_history_and_compression',audit.get('history_status')=='loaded' and audit.get('compressed_characters',999999)<=6000 and audit.get('compressed'))
        history=request(c,'GET','/history/'+cid)
        (OUT/'history.json').write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf-8')
        messages=history.get('messages',[])
        answers=[m for m in messages if m.get('role')=='assistant']
        check('same_conversation_two_rounds_persisted',len(answers)==2 and all(m.get('payload',{}).get('rag') for m in answers),messages=len(messages))
        final_status=request(c,'GET','/rag/status');report['final_rag_status']=final_status
        check('new_provider_data_persisted',any((a.get('rag') or {}).get('stored_facts',0)>0 for a in report['analyses']),documents=final_status['documents'],chunks=final_status['chunks'])
        # Compare instantaneous mode on the same live data snapshot; its timer excludes prior data acquisition.
        current_facts=advice.get('facts',[]) if report['analyses'] else []
        start=perf_counter();instant=request(c,'POST','/portfolio/analyze',{**common,'query':'分析下贵州茅台','research_mode':'instant','facts':current_facts,'conversation_id':'qa-rag-instant-'+uuid4().hex});instant_seconds=perf_counter()-start
        check('instant_path_retained',instant_seconds<3 and instant.get('rag',{}).get('status')=='skipped',seconds=round(instant_seconds,3),timing_scope='localhost request only, prior quote acquisition excluded')
        report['instant_seconds']=round(instant_seconds,3)
        request(peer,'POST','/auth/logout');report['peer_session_closed']=True
        request(c,'POST','/auth/logout');report['api_session_closed']=True
    except Exception as exc:
        report['fatal_error']={'type':type(exc).__name__,'message':str(exc)[:800]};save()
        print(json.dumps({'fatal_error':report['fatal_error']},ensure_ascii=True),flush=True)
    finally:
        for c in clients:c.close()
        report['passed']=not report.get('fatal_error') and all(row['passed'] for row in report['checks']);save()
        print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'report':str(OUT/'report.json')},ensure_ascii=True),flush=True)


if __name__=='__main__':
    main()
    raise SystemExit(0 if report['passed'] else 1)
