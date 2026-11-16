"""Initial chart data and malformed observations must survive the UI boundary."""
import json

import altair as alt
import pandas as pd
from streamlit.testing.v1 import AppTest

from frontend.charts import chart_spec


def _datasets(node):
    if (node.get("data") or {}).get("values"):
        yield node["data"]["values"]
    for child in node.get("layer", []):
        yield from _datasets(child)


def test_browser_receives_initial_data_instead_of_a_delayed_arrow_dataset():
    app = AppTest.from_string("""
import pandas as pd
import altair as alt
from frontend.charts import render_chart
render_chart(alt.Chart(pd.DataFrame({'名称':['甲','乙'],'数值':[2.,3.]})).mark_bar().encode(x='名称:N',y='数值:Q'))
""").run()
    assert not app.exception
    chart = app.get("vega_lite_chart")[0].proto
    assert not chart.datasets and not chart.data.data
    rows = list(_datasets(json.loads(chart.spec)))
    assert rows == [[{"名称": "甲", "数值": 2.}, {"名称": "乙", "数值": 3.}]]


def test_invalid_dates_and_nonfinite_values_never_reach_plot_data():
    frame = pd.DataFrame({"日期": ["bad", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
                          "数值": [5., float("inf"), float("nan"), 100., 101.]})
    spec = chart_spec(alt.Chart(frame).mark_line().encode(x="日期:T", y="数值:Q"))
    assert list(_datasets(spec)) == [[{"日期": "2026-11-12", "数值": 100.}, {"日期": "2026-11-13", "数值": 101.}]]
    # Input evidence stays untouched; filtering is only for plotting.
    assert len(frame) == 5 and frame.iloc[1]["数值"] == float("inf")


def test_empty_and_single_point_lines_show_a_notice_instead_of_empty_axes():
    app = AppTest.from_string("""
import pandas as pd
import altair as alt
from frontend.charts import render_chart
for rows in [[],[{'日期':'2026-11-10','数值':1.}]]:
    render_chart(alt.Chart(pd.DataFrame(rows,columns=['日期','数值'])).mark_line().encode(x='日期:T',y='数值:Q'))
""").run()
    assert not app.exception and not app.get("vega_lite_chart")
    assert len(app.caption) == 2 and all("有效数据不足" in c.value for c in app.caption)


def test_layered_price_and_ma_keep_separate_data_and_interactions():
    frame = pd.DataFrame({"日期": ["2026-11-10", "2026-11-11"], "收盘": [100., 101.], "均线": [99., 100.]})
    price = alt.Chart(frame[["日期", "收盘"]]).mark_line(point=True).encode(x="日期:T", y="收盘:Q")
    ma = alt.Chart(frame[["日期", "均线"]]).mark_line().encode(x="日期:T", y="均线:Q")
    spec = chart_spec(alt.layer(price, ma).resolve_scale(y="shared"))
    assert len(list(_datasets(spec))) == 2
    assert list(_datasets(spec))[1][-1]["均线"] == 100.
    interactive = chart_spec(price.interactive())
    assert "params" not in interactive
    assert interactive["layer"][0]["params"][0]["bind"] == "scales"


def test_multiseries_native_replacements_preserve_group_names_and_values():
    app = AppTest.from_string("""
from frontend.charts import render_line, render_bar
render_line([{'日期':'2026-11-10','请求':2,'失败':1},{'日期':'2026-11-11','请求':3,'失败':0}],x='日期',y=['请求','失败'])
render_bar([{'月份':'2026-11','买入':2,'卖出':1}],x='月份',y=['买入','卖出'])
""").run()
    assert not app.exception
    charts = app.get("vega_lite_chart")
    assert len(charts) == 2
    rows = list(_datasets(json.loads(charts[0].proto.spec)))[0]
    assert {(row["指标"], row["数值"]) for row in rows} == {("请求", 2), ("请求", 3), ("失败", 1), ("失败", 0)}
