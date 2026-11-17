"""Knowledge management and per-round retrieval audit live inside the logic chain."""
from __future__ import annotations
import base64
from datetime import date
import streamlit as st
from frontend.persistent_panel import persistent_expander

ASSETS = {"通用知识": "general", "股票": "stock", "基金": "fund", "债券/转债": "bond", "行业": "industry", "大盘/指数": "market"}


def render_rag_manager(api_base, api_request, *, key):
    with persistent_expander('RAG知识库 · 资料导入与检索', key=key+'_rag_manager') as panel:
        if not panel.open:
            return
        status = api_request(api_base, 'GET', '/rag/status', quiet=True)
        if not status:
            st.info('知识库状态暂不可用。')
            return
        if not status.get('enabled'):
            st.info('本地RAG已关闭。')
            return
        st.caption(f"当前账号可检索 {status.get('documents', 0)} 份资料、{status.get('chunks', 0)} 个分块（含共享基础知识）。")
        st.caption('本地中文向量模型：'+str(status.get('model', '未部署')))
        if not status.get('ready'):
            st.warning('向量模型尚未完成部署，请先运行项目的RAG部署脚本。')
        st.caption('深入研究自动检索；3秒研究保持原有路径。导入资料只对当前账号可见，正文中的指令不会获得执行权限。')
        with st.form(key+'_rag_import'):
            title = st.text_input('资料标题', max_chars=150)
            asset = st.selectbox('资料类型', list(ASSETS))
            entity = st.text_input('标的名称（通用知识可留空）', max_chars=60)
            code = st.text_input('标的代码（可选）', max_chars=30)
            source = st.text_input('原文HTTPS链接（可选）')
            as_of = st.date_input('资料日期', value=date.today(), max_value=date.today())
            file = st.file_uploader('上传TXT、Markdown或文字PDF（最大5MB、100页）', type=['txt', 'md', 'pdf'], max_upload_size=5)
            text = st.text_area('或粘贴正文（与文件二选一，最多10万字符）', max_chars=100000)
            submitted = st.form_submit_button('保存原文并建立向量索引')
        if submitted:
            if not title.strip() or bool(file) == bool(text.strip()):
                st.warning('请填写标题，并在文件和正文中选择一种。')
            else:
                payload = {'title':title.strip(), 'entity':entity.strip(), 'entity_code':code.strip(),
                           'asset_type':ASSETS[asset], 'source_url':source.strip() or None, 'as_of':as_of.isoformat(),
                           'text':text if not file else '', 'filename':file.name if file else '',
                           'content_base64':base64.b64encode(file.getvalue()).decode() if file else ''}
                result = api_request(api_base,'POST','/rag/documents',payload)
                if result:
                    st.success('原文和索引已保存。' if result.get('indexed') else '该资料已存在，无需重复入库。')
        with st.form(key+'_rag_search'):
            query = st.text_input('试查知识库', max_chars=1000)
            search = st.form_submit_button('检索资料')
        if search and query.strip():
            result = api_request(api_base,'POST','/rag/search',{'query':query})
            if result:
                render_rag_audit(result)
        documents = api_request(api_base,'GET','/rag/documents',quiet=True)
        if documents:
            st.markdown('**我的资料（最近100份）**')
            st.dataframe([{name:row.get(field) for name,field in [('标题','title'),('标的','entity'),('日期','as_of'),('分块数','chunks'),('启用','enabled')]} for row in documents], hide_index=True)
            selected = st.selectbox('管理资料', documents, format_func=lambda row:row['title']+' · '+row['id'][:8], key=key+'_rag_doc')
            if st.button('停用此资料' if selected.get('enabled') else '恢复此资料', key=key+'_rag_toggle'):
                if api_request(api_base,'PATCH','/rag/documents/'+selected['id'],{'enabled':not bool(selected.get('enabled'))}):
                    st.rerun()
        st.caption('原文、分块、向量和检索记录保存在服务端本地，资料停用后不再参与新的检索；历史咨询依据仍保留。')


def render_rag_audit(audit):
    if audit.get('status') == 'skipped':
        st.caption(audit.get('reason', '本轮未使用知识库。'))
        return
    if audit.get('status') == 'unavailable':
        st.warning(audit.get('notice', '本地知识库暂不可用。'))
        return
    matches = audit.get('matches') or []
    st.caption(f"本轮召回 {len(matches)} 份资料；排除过期候选 {audit.get('freshness_rejections', 0)} 条；入库源数据 {audit.get('stored_facts', 0)} 条。")
    if not matches:
        st.info('没有达到相关性与时效要求的资料，继续使用原有数据核验流程。')
    if audit.get('candidate_limit_reached'):
        st.caption('候选达到检索上限，本轮仅检索最近12000个符合权限和标的范围的分块。')
    for row in matches:
        st.markdown('**'+row['title']+'**')
        st.caption(f"来源：{row.get('source_id')} · 日期：{row.get('as_of')} · 相似度：{row.get('similarity')}（不代表结论正确率）")
        st.write(row.get('excerpt', ''))
        if row.get('source_url'):
            st.link_button('查看原文', row['source_url'])
        st.caption(f"原文字符位置 [{row.get('start')}, {row.get('end')}) · 资料编号 {row.get('document_id')}")
    st.caption(audit.get('notice', ''))
