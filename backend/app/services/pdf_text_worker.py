"""Isolated, bounded PDF extraction process. No network and no instructions executed."""
from __future__ import annotations

import io
import json
import sys
import time

from pypdf import PdfReader

FOCUS = {
    'audit': ('审计意见', '无保留意见', '无法表示意见', '否定意见'),
    'regulatory': ('被调查处罚的事项', '处罚', '立案调查', '监管措施', '违法违规'),
    'disclosure': ('按规定披露定期报告', '及时披露', '延期披露', '信息披露违规', '定期报告', '未按规定披露'),
    'event': ('业绩预告', '权益分派', '股份回购', '股权质押', '限售', '重大事项'),
}


def main():
    started = time.monotonic()
    focus_groups = ({"financial": ("非经常性损益项目和金额", "主要会计数据", "合并利润表", "营业收入",
                                   "归属于上市公司股东的净利润", "扣除非经常性损益", "经营活动产生的现金流量净额")}
                    if sys.argv[1:] == ["financial"] else FOCUS)
    keywords = tuple(word for words in focus_groups.values() for word in words)
    data = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024 or not data.startswith(b'%PDF-'):
        return
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted or len(reader.pages) > 800:
        return
    indices = list(range(min(8, len(reader.pages))))
    # Prefer relevant outline sections when the publisher supplies bookmarks.
    def outline_pages(items):
        for item in items:
            if isinstance(item, list):
                yield from outline_pages(item)
            elif any(word in str(getattr(item, 'title', '')) for word in keywords):
                try:
                    destination = reader.get_destination_page_number(item)
                    yield from range(max(0, destination - 1), min(len(reader.pages), destination + 3))
                except (ValueError, TypeError, KeyError):
                    continue
    indices.extend(outline_pages(reader.outline))
    indices.extend(range(min(60, len(reader.pages))))
    indices.extend(range(max(0, len(reader.pages) - 60), len(reader.pages)))
    indices.extend(range(len(reader.pages)))
    indices = list(dict.fromkeys(indices))
    pages, identity = [], []
    for index in indices:
        if time.monotonic() - started > 9:
            break
        page = reader.pages[index]
        contents = page.get_contents()
        if contents is None or len(contents.get_data()) > 2 * 1024 * 1024:
            continue
        text = (page.extract_text() or '')[:16000]
        if index < 8:
            identity.append(text[:3000])
        if text.strip():
            pages.append({'page': index + 1, 'text':text})
    selected, seen = [], set()
    # Keep evidence for each dimension; audit boilerplate cannot take all slots.
    for focus, words in focus_groups.items():
        relevant = sorted((item for item in pages if any(word in item['text'] for word in words)),
            key=lambda item:(-(words[0] in item['text']), -sum(item['text'].count(word) for word in words), item['page']))
        for item in relevant[:6 if focus == 'financial' else 2]:
            offset = max(0, min(item['text'].find(word) for word in words if word in item['text']) - 1000)
            excerpt = item['text'][offset:offset+6000]
            key = (item['page'], excerpt)
            if key not in seen:
                seen.add(key)
                selected.append({'page':item['page'],'text':excerpt,'focus':focus})
    if not selected:
        selected = [{'page':item['page'],'text':item['text'][:6000]} for item in pages[:6]]
    sys.stdout.buffer.write(json.dumps({'identity': '\n'.join(identity), 'pages': selected[:8]},
                                      ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
