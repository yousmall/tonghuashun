"""Read only provider-returned official disclosure PDFs, with no user-supplied URL execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import httpx

from backend.app.models import FactRecord
from backend.app.services.provider_errors import ProviderCallError

PDF_HOSTS = frozenset({'static.cninfo.com.cn', 'www.cninfo.com.cn', 'dataclouds.cninfo.com.cn',
    'www.sse.com.cn', 'static.sse.com.cn', 'www.szse.cn', 'www.bse.cn', 'disclosure.szse.cn', 'www.neeq.com.cn'})


def canonical_disclosure_url(url):
    """Upgrade only explicitly allowlisted publisher PDF links, never arbitrary HTTP."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == 'http' and parsed.hostname in PDF_HOSTS and parsed.port in {None, 80}
                and not parsed.username and not parsed.password and parsed.path.lower().endswith('.pdf')):
            return parsed._replace(scheme='https', netloc=parsed.hostname).geturl()
    except ValueError:
        pass
    return url


def allowed_disclosure_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in PDF_HOSTS and parsed.port in {None, 443}
                and not parsed.username and not parsed.password and parsed.path.lower().endswith('.pdf'))
    except ValueError:
        return False


async def extract_pdf(data):
    process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('pdf_text_worker.py')),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=12)
        if process.returncode or len(output) > 200000:
            return {}
        return json.loads(output.decode('utf-8')) if output else {}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def read_disclosure(source, *, code, name, client=None, extractor=extract_pdf):
    try:
        return await _read_disclosure(source, code=code, name=name, client=client, extractor=extractor)
    except ProviderCallError as error:
        if (client is not None or source.entity != name or not allowed_disclosure_url(source.source_url or '')
                or error.code not in {'DOCUMENT_TIMEOUT', 'DOCUMENT_NETWORK_ERROR', 'DOCUMENT_HTTP_ERROR', 'DOCUMENT_NOT_PDF'}):
            raise
        from backend.app.services.official_disclosures import resolve_official_mirror
        mirror = await resolve_official_mirror(source, code=code, name=name)
        if mirror is None or mirror.source_url == source.source_url:
            raise error
        excerpts = await _read_disclosure(mirror, code=code, name=name, extractor=extractor)
        return [mirror, *excerpts] if excerpts else []


async def _read_disclosure(source, *, code, name, client=None, extractor=extract_pdf):
    if (not source.source_url or not source.period or not allowed_disclosure_url(source.source_url)
            or source.entity != name or (source.entity_code and str(source.entity_code).split('.')[0] != code)):
        return []
    owned = client is None
    client = client or httpx.AsyncClient(timeout=8, follow_redirects=False,
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/pdf'})
    try:
        url, data = source.source_url, bytearray()
        for hop in range(3):
            async with client.stream('GET', url) as response:
                if response.is_redirect:
                    destination = urljoin(str(response.url), response.headers.get('location', ''))
                    if not allowed_disclosure_url(destination):
                        raise ProviderCallError('公告跳转地址不在官方PDF范围内。', code='DOCUMENT_REDIRECT_REJECTED')
                    if hop == 2:
                        raise ProviderCallError('公告跳转超过读取预算。', code='DOCUMENT_REDIRECT_LIMIT')
                    url = destination
                    continue
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8 * 1024 * 1024:
                        raise ProviderCallError('公告文件超过读取预算。', code='DOCUMENT_TOO_LARGE')
                break
        if not data.startswith(b'%PDF-'):
            raise ProviderCallError('公告地址未返回PDF正文。', code='DOCUMENT_NOT_PDF')
        result = await extractor(bytes(data))
        identity = result.get('identity', '')
        # A link labelled as this security is insufficient when the PDF itself
        # identifies a different company. Require identity in its opening pages.
        compact = re.sub(r'\s+', '', identity)
        if not re.search(r'(?<!\d)' + re.escape(code) + r'(?!\d)', compact) and name not in compact:
            return []
        facts = []
        for page in result.get('pages', [])[:8]:
            text = str(page.get('text', ''))
            if not text.strip():
                continue
            digest = hashlib.sha256(f'{source.fact_id}:{page["page"]}:{text}'.encode()).hexdigest()[:24]
            facts.append(FactRecord(fact_id='PDF-' + digest, entity=name, entity_code=code,
                field='announcement_excerpt', value=text, period=source.period,
                observation_date=source.observation_date,
                source_url=source.source_url, source_field=f'公告PDF第{page["page"]}页原文节选',
                snapshot_time=source.snapshot_time, source_id='OFFICIAL_DISCLOSURE_PDF_V1', quality=source.quality,
                derived_from=[source.fact_id], derivation_rule='PDF_TEXT_V1: bounded original-page extraction; partial coverage'))
        return facts
    except httpx.TimeoutException as exc:
        raise ProviderCallError('公告原文读取超时。', code='DOCUMENT_TIMEOUT') from exc
    except httpx.HTTPStatusError as exc:
        raise ProviderCallError('公告原文暂不可读取。', code='DOCUMENT_HTTP_ERROR',
                                status_code=exc.response.status_code) from exc
    except httpx.RequestError as exc:
        raise ProviderCallError('公告原文网络连接失败。', code='DOCUMENT_NETWORK_ERROR') from exc
    finally:
        if owned:
            await client.aclose()
