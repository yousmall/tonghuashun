"""Deploy local BGE ONNX model and persist attributed project education documents.
Usage: .venv/Scripts/python.exe scripts/deploy_rag.py [--download-model] [--root PATH]
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.services.rag import RagStore, DocumentUpload, SearchRequest, MODEL, PUBLIC, DEFAULT_ROOT
from backend.app.services.local_consultation import CONCEPTS

INDICATORS = {
    'MACD': 'MACD使用快慢指数移动平均线的差值DIF及其信号线DEA观察趋势与动量。柱状值常用2乘以DIF减DEA。金叉、死叉是滞后信号，震荡行情可能反复失效，需要结合价格、成交量与风险约束。',
    'RSI': 'RSI相对强弱指标比较观察期内上涨与下跌幅度，数值通常处于0到100。高位或低位区间只表示相对动量，并不保证立刻反转；强趋势下可能长期处于超买或超卖区间。',
    'KDJ': 'KDJ随机指标通过收盘价在近期最高最低价范围内的位置计算RSV，再平滑得到K、D以及J。J可以超出0到100，短周期信号易受噪声影响，交叉不能独立证明买卖时点。',
    'BOLL': '布林带BOLL由移动平均中轨与标准差构成的上下轨组成。带宽描述波动变化，触及上轨不必然下跌，跌破下轨也不必然反弹；需要结合趋势和样本区间。',
    'ATR': 'ATR平均真实波幅使用当日高低差、最高价与前收盘差、最低价与前收盘差的最大值，再进行平滑。ATR衡量波动程度，不表示涨跌方向，也不保证止损价格能够成交。',
    'OBV': 'OBV能量潮按收盘涨跌对成交量作正负累计，用于观察量价配合。指标数值受起始点和成交量口径影响，背离只是待核实线索，不能保证趋势反转。',
}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(os.getenv('WENCE_RAG_PATH') or DEFAULT_ROOT))
    parser.add_argument('--download-model',action='store_true')
    args=parser.parse_args()
    root=args.root.resolve(); root.mkdir(parents=True,exist_ok=True)
    if args.download_model:
        os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
        from fastembed import TextEmbedding
        TextEmbedding(MODEL,cache_dir=str(root/'models'),threads=2,providers=['CPUExecutionProvider'])
    store=RagStore(root)
    # Offline inference verifies that all tokenizer/ONNX files are present.
    store.vectors(['验证本地中文金融知识检索'],query=True)
    previous = json.loads((root/'deployment.json').read_text(encoding='utf-8')) if (root/'deployment.json').is_file() else {}
    seed_date = previous.get('seed_date') or datetime.now(timezone.utc).date().isoformat()
    ids=[]
    for title,text in {**CONCEPTS,**INDICATORS}.items():
        result=store.add(PUBLIC,DocumentUpload(title=title+'投资基础知识',as_of=seed_date,text=text),kind='education')
        ids.append(result['id'])
    result=store.search('deployment-check',SearchRequest(query='如何衡量历史从高点下跌的最大幅度',top_k=3),general_only=True)
    if not any('最大回撤' in item['title'] for item in result['matches']):
        raise RuntimeError('中文语义召回验收未通过')
    manifest={'model':MODEL,'dimension':512,'seed_date':seed_date,'seed_documents':len(ids),
              'seed_ids':ids,'seed_source':'项目自有CONCEPTS与指标解释，不是实时金融数据',
              'deployed_at':datetime.now(timezone.utc).isoformat()}
    (root/'deployment.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'ready',**store.status('deployment-check'),
                      'database':str(store.path),'top_matches':[item['title'] for item in result['matches']]},ensure_ascii=False))


if __name__=='__main__':
    main()
