"""Resolve an official PDF mirror by exact issuer, title and publication date."""
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import httpx

from backend.app.models import FactRecord
from backend.app.services.return_expectation import _date
from backend.app.services.provider_errors import ProviderCallError


def normalized_title(title, name):
    title = html.unescape(re.sub('<[^>]+>', '', str(title)))
    title = re.sub(r'[\s：:（）()－-]', '', title)
    while title.startswith(name):
        title = title.removeprefix(name)
    return title


async def resolve_official_mirror(source, *, code, name, client=None):
    if source.field != 'announcement' or not isinstance(source.value, str) or not _date(source):
        return None
    stamp = datetime.fromisoformat(_date(source))
    owned = client is None
    client = client or httpx.AsyncClient(timeout=6, follow_redirects=False,
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.cninfo.com.cn/'})
    try:
        endpoint = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
        async with client.stream('POST', endpoint, data={'pageNum': '1', 'pageSize': '30',
            'column': 'sse' if code.startswith(('60', '68')) else 'szse', 'tabName': 'fulltext',
            'searchkey': name, 'seDate': f'{(stamp-timedelta(days=1)).date()}~{(stamp+timedelta(days=1)).date()}',
            'sortName': 'time', 'sortType': 'desc', 'isHLtitle': 'false'}) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ProviderCallError('官方公告检索超过读取预算。', code='DOCUMENT_LOOKUP_TOO_LARGE')
        import json
        payload = json.loads(data)
        matches = []
        for row in (payload.get('announcements') or [])[:30]:
            if (str(row.get('secCode')) != code or row.get('secName') != name
                    or normalized_title(row.get('announcementTitle'), name) != normalized_title(source.value, name)):
                continue
            try:
                observed = datetime.fromtimestamp(float(row['announcementTime']) / 1000, timezone.utc).astimezone(
                    timezone(timedelta(hours=8))).date()
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            if observed.isoformat() != _date(source):
                continue
            path = row.get('adjunctUrl', '')
            if not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF', path, re.I):
                continue
            url = urljoin('https://static.cninfo.com.cn/', path)
            matches.append(FactRecord(fact_id='CNINFO-' + hashlib.sha256(url.encode()).hexdigest()[:24],
                entity=name, entity_code=code, field='announcement', value=row['announcementTitle'],
                period='REC-' + hashlib.sha256(url.encode()).hexdigest()[:16], observation_date=observed,
                snapshot_time=source.snapshot_time, source_url=url, source_id='CNINFO_OFFICIAL_DISCLOSURE',
                source_field='巨潮官方检索：证券、标题、发布日期一致的原文', quality=.9))
        return matches[0] if len({f.source_url for f in matches}) == 1 else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise ProviderCallError('官方公告检索暂不可用。', code='DOCUMENT_LOOKUP_UNAVAILABLE') from None
    finally:
        if owned:
            await client.aclose()
