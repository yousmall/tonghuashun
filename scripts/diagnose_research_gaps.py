"""Bounded read-only gap diagnostics; never print gateway credentials or headers."""
import asyncio
import argparse
import json
import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from backend.app.data_provider.iwencai import IwencaiSkillHubProvider, _find_record_lists
from backend.app.services.provider_errors import failure_summary


async def main(stage):
    provider = IwencaiSkillHubProvider.from_env()
    if provider is None:
        return {'configured': False}
    async def capture(response):
        await response.aread()
        try:
            payload = response.json()
        except ValueError:
            return
        rows = list(_find_record_lists(payload))
        # Only known research row fields. Gateway status/token envelopes are excluded.
        fields = ('证券代码', '股票代码', '指数代码', '证券简称', '股票简称', '指数简称',
                  'title', 'summary', 'publish_date', 'url', '评级日期', '研报日期', '研究机构',
                  '研报', '研报链接', '原文链接', '研报发布日期', '公告日期', 'stock_infos')
        print(json.dumps({'http': response.status_code, 'row_count': len(rows),
            'declared_count': payload.get('row_count') if isinstance(payload, dict) else None,
            'row_keys': [list(row)[:35] for row in rows[:2]],
            'samples': [{k: str(v)[:1000] for k, v in row.items()
                         if k in fields or any(t in k for t in ('目标价', '換手率', '换手率', '资金', '成交额', '营业收入', '所属同花顺'))}
                        for row in rows[:2]],
            'columns': [{k: c[k] for k in ('key', 'unit', 'timestamp') if k in c}
                        for c in payload.get('columns', [])[:12]] if isinstance(payload, dict) else []},
            ensure_ascii=False), flush=True)
    provider._client.event_hooks['response'] = [capture]
    try:
        if stage == 'reader':
            from backend.app.services.disclosure_reader import read_disclosure
            from backend.app.services.official_disclosures import resolve_official_mirror
            facts = await provider._comprehensive_search('贵州茅台 2025年度审计报告', channel='announcement', entity_hint='600519')
            source = next((f for f in facts if f.entity == '贵州茅台' and f.field == 'announcement' and '2025年年度报告' in f.value), None)
            if source is None:
                print(json.dumps({'status': 'no_matching_report'}), flush=True)
                return
            mirror = await resolve_official_mirror(source, code='600519', name='贵州茅台')
            print(json.dumps({'source_title': source.value, 'source_url': source.source_url,
                'mirror': mirror.source_url if mirror else None, 'date': str(source.observation_date)}, ensure_ascii=False), flush=True)
            if mirror:
                try:
                    excerpts = await read_disclosure(mirror, code='600519', name='贵州茅台')
                    print(json.dumps({'excerpts': len(excerpts), 'fields': dict(Counter(f.field for f in excerpts))}), flush=True)
                except Exception as exc:
                    print(json.dumps(failure_summary(exc)), flush=True)
            return
        if stage == 'official':
            import httpx
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post('https://www.cninfo.com.cn/new/hisAnnouncement/query',
                    data={'pageNum': '1', 'pageSize': '30', 'column': 'sse', 'tabName': 'fulltext',
                          'searchkey': '贵州茅台', 'seDate': '2026-04-01~2026-05-01', 'sortName': 'time',
                          'sortType': 'desc', 'isHLtitle': 'false'},
                    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.cninfo.com.cn/'})
                payload = response.json()
                print(json.dumps({'http': response.status_code, 'total': payload.get('totalAnnouncement'),
                    'announcements': [{k: r.get(k) for k in ('secCode', 'secName', 'announcementTitle', 'adjunctUrl', 'announcementTime')}
                        for r in (payload.get('announcements') or [])[:30]]}, ensure_ascii=False), flush=True)
            return
        jobs = [
            ('basic', lambda: provider.get_basic_info('600519')),
            ('target_simple', lambda: provider.query('贵州茅台 研报目标价', entity_hint='600519', skill_id='hithink-insresearch-query', limit=3)),
            ('target_reports', lambda: provider._comprehensive_search('贵州茅台 目标价', channel='report', entity_hint='600519')),
            ('industry_simple', lambda: provider.query('白酒行业 主力净买入额 成交额 营业收入同比增长率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
            ('industry_history', lambda: provider.query('白酒行业 近5个交易日换手率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
        ]
        if stage == 'details':
            jobs = [
                ('target_latest', lambda: provider.query('600519 研报目标价 研报日期', entity_hint='600519', skill_id='hithink-insresearch-query', limit=10)),
                ('industry_exact', lambda: provider.query('白酒 主力净买入额 成交额', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
                ('industry_finance', lambda: provider.query('白酒 营业收入同比增长率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
                ('history_exact', lambda: provider.query('白酒 2025年10月8日换手率 2025年10月9日换手率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
                ('annual', lambda: provider._comprehensive_search('600519 2025年年度报告 审计报告', channel='announcement', entity_hint='600519')),
            ]
        if stage == 'industry':
            jobs = [
                ('finance_index', lambda: provider.query('白酒行业 营业收入同比增长率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
                ('finance_code', lambda: provider.query('881273.TI 营业收入同比增长率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
                ('history_index', lambda: provider.query('白酒行业 2025年10月9日换手率 2025年10月10日换手率', entity_hint='白酒', skill_id='hithink-industry-query', limit=3)),
            ]
        if stage == 'daily':
            jobs = [('daily', lambda: provider.query('白酒行业 2025年10月9日换手率 2025年10月10日换手率 2025年10月13日换手率 2025年10月14日换手率 2025年10月15日换手率 2025年10月16日换手率 2025年10月17日换手率 2025年10月20日换手率 2025年10月21日换手率 2025年10月22日换手率', entity_hint='白酒', skill_id='hithink-industry-query', limit=1))]
        if stage == 'constituents':
            jobs = [('constituents', lambda: provider.query('白酒行业成分股 2026年中报营业收入 2025年中报营业收入 所属同花顺行业', entity_hint='白酒', skill_id='hithink-finance-query', limit=100))]
        if stage == 'cninfo':
            jobs = [('annual_cninfo', lambda: provider._comprehensive_search('贵州茅台 2025年年度报告 巨潮资讯网', channel='announcement', entity_hint='600519')),
                    ('annual_pdf', lambda: provider._comprehensive_search('贵州茅台 2025年年度报告 static.cninfo.com.cn', channel='announcement', entity_hint='600519'))]
        if stage == 'network':
            jobs = [('annual', lambda: provider._comprehensive_search('贵州茅台 最新年度报告', channel='announcement', entity_hint='600519'))]
        for label, run in jobs:
            print(json.dumps({'query_label': label}), flush=True)
            try:
                facts = await asyncio.wait_for(run(), 25)
                print(json.dumps({'label': label, 'fields': dict(Counter(f.field for f in facts)),
                    'entity_metadata': list({(f.entity, f.entity_code, f.period, f.unit) for f in facts})[:12]}, ensure_ascii=False), flush=True)
                if stage == 'network':
                    import httpx
                    urls = list(dict.fromkeys(f.source_url for f in facts if f.source_url and '600519' in f.source_url))[:3]
                    urls += [u.replace('static.sse.com.cn', 'www.sse.com.cn') for u in urls[:1]]
                    for url in urls:
                        for trust in (True, False):
                            try:
                                async with httpx.AsyncClient(timeout=12, trust_env=trust) as client:
                                    async with client.stream('GET', url) as response:
                                        prefix = await anext(response.aiter_bytes())
                                        print(json.dumps({'download_url': url, 'trust_env': trust, 'http': response.status_code,
                                            'pdf_magic': prefix.startswith(b'%PDF-'), 'content_type': response.headers.get('content-type'),
                                            'prefix': prefix[:70].decode('ascii', errors='replace')}), flush=True)
                            except Exception as exc:
                                print(json.dumps({'download_url': url, 'trust_env': trust, 'type': type(exc).__name__}), flush=True)
            except Exception as exc:
                print(json.dumps({'label': label, **failure_summary(exc)}, ensure_ascii=False), flush=True)
    finally:
        await provider.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', choices=['initial', 'details', 'industry', 'daily', 'network', 'constituents', 'cninfo', 'official', 'reader'], default='initial')
    asyncio.run(main(parser.parse_args().stage))
