"""Render small charts with their initial data, before Vega computes domains.

Streamlit 1.62 runs Arrow-backed charts once before inserting their datasets
(upstream #16614). Keep data local to a layer to avoid that empty first pass.
Use public chart APIs; do not change Altair's process-global transformers.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from hashlib import sha256

import streamlit as st


def _embed_frames(chart):
    import altair as alt
    import pandas as pd
    chart = chart.copy(deep=True)
    data = getattr(chart, "data", alt.Undefined)
    if isinstance(data, pd.DataFrame):
        # to_json preserves ISO dates and maps missing/nonfinite numbers to null.
        records = json.loads(data.to_json(orient="records", date_format="iso"))
        name = "inline_" + sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()[:16]
        chart.data = alt.InlineData(values=records, name=name)
    for attr in ("layer", "hconcat", "vconcat", "concat"):
        children = getattr(chart, attr, alt.Undefined)
        if children is not alt.Undefined:
            setattr(chart, attr, [_embed_frames(child) for child in children])
    return chart


def _valid(value, kind):
    if kind == "quantitative":
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    if kind == "temporal":
        if not isinstance(value, (str, date, datetime)):
            return False
        try:
            if isinstance(value, str):
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except (TypeError, ValueError, OverflowError):
            return False
    return value is not None


def _clean_data(node, inherited=None):
    data = node.get("data", inherited)
    children = node.get("layer") or []
    if children:
        for child in children:
            # Each layer can have a different set of required fields.
            if "data" not in child and isinstance(data, dict):
                child["data"] = {**data, "values": list(data.get("values", []))}
            _clean_data(child, data)
        node["layer"] = [child for child in children if child.get("layer") or (child.get("data") or {}).get("values")]
        node.pop("data", None)
        return
    if not isinstance(data, dict) or "values" not in data:
        return
    encodings = node.get("encoding", {})
    required = [encoding for channel, encoding in encodings.items()
                if channel in {"x", "y", "x2", "y2", "theta", "size"}
                and isinstance(encoding, dict) and "field" in encoding]
    rows = [row for row in data["values"] if isinstance(row, dict) and all(
        _valid(row.get(encoding["field"]), encoding.get("type")) for encoding in required)]
    # One dated point cannot describe a line; keep this explicit rather than
    # displaying an empty chart.
    mark = node.get("mark", {})
    mark = mark.get("type") if isinstance(mark, dict) else mark
    if mark in {"line", "area"} and len(rows) < 2:
        rows = []
    node["data"] = {**data, "values": rows}


def chart_spec(chart):
    """Return a Vega-Lite spec whose datasets exist on its first view run."""
    spec = _embed_frames(chart).to_dict()
    _clean_data(spec)
    # Keep presentation at the top level; selection parameters stay on their
    # original unit to avoid duplicate signals on line+point layers. A nested
    # layer holds the data so Streamlit does not extract it as delayed Arrow.
    top_keys = {"$schema", "config", "width", "height", "padding", "autosize",
                "background", "title", "usermeta"}
    outer = {key: value for key, value in spec.items() if key in top_keys}
    outer["layer"] = [{key: value for key, value in spec.items() if key not in top_keys}]
    return outer


def render_chart(chart, *, width="stretch", key=None):
    spec = chart_spec(chart)
    def has_data(node):
        return bool((node.get("data") or {}).get("values")) or any(
            has_data(child) for child in node.get("layer", []))
    if not has_data(spec):
        st.caption("可绘制的有效数据不足，暂不绘制图表。")
        return
    return st.vega_lite_chart(spec=spec, width=width, key=key)


def _series_frame(data, x, y, color):
    import pandas as pd
    frame = pd.DataFrame(data)
    if isinstance(y, (list, tuple)):
        frame = frame.melt(id_vars=[x], value_vars=list(y), var_name="指标", value_name="数值")
        return frame, "数值", "指标"
    return frame, y, color


def render_line(data, *, x, y, color=None, width="stretch", key=None):
    import altair as alt
    frame, value, color = _series_frame(data, x, y, color)
    chart = alt.Chart(frame).mark_line().encode(
        x=alt.X(field=x, type="temporal", title=x),
        y=alt.Y(field=value, type="quantitative", title=value, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip(field=x, type="temporal", format="%Y-%m-%d"),
                 alt.Tooltip(field=value, type="quantitative", format=",.4f")],
    )
    if color:
        chart = chart.encode(color=alt.Color(field=color, type="nominal", title=None))
    return render_chart(chart, width=width, key=key)


def render_bar(data, *, x, y, horizontal=False, width="stretch", key=None):
    import altair as alt
    frame, value, color = _series_frame(data, x, y, None)
    label = alt.Y(field=x, type="nominal", title=None, sort=None) if horizontal else alt.X(field=x, type="nominal", title=None, sort=None)
    amount = alt.X(field=value, type="quantitative", title=value) if horizontal else alt.Y(field=value, type="quantitative", title=value)
    chart = alt.Chart(frame).mark_bar(cornerRadiusEnd=3).encode(
        x=amount if horizontal else label, y=label if horizontal else amount,
        tooltip=[alt.Tooltip(field=x, type="nominal"), alt.Tooltip(field=value, type="quantitative")],
    )
    if color:
        chart = chart.encode(color=alt.Color(field=color, type="nominal", title=None))
    return render_chart(chart, width=width, key=key)
