"""账号展示资料的持久化、身份隔离与页面集成。"""
import base64
import re
from io import BytesIO
from random import Random

from fastapi.testclient import TestClient
from PIL import Image
from streamlit.testing.v1 import AppTest

from backend.app import main as main_module
from backend.app.database import Database
from backend.app.session_pool import SessionThreadPool
from backend.app.user_appearance import default_display_name
from frontend.risk_assessment import result_html
from frontend.user_settings import DEFAULT_AVATAR


def image_data():
    output = BytesIO()
    image = Image.frombytes("RGB", (420, 180), Random(127).randbytes(420 * 180 * 3))
    image.save(output, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


def test_appearance_api_persists_isolates_and_resets_without_changing_login(monkeypatch, request, tmp_path):
    url = "sqlite+pysqlite:///" + (tmp_path / "accounts.db").as_posix()
    database = Database(url, "test-secret-that-is-longer-than-thirty-two-characters")
    database.initialize()
    monkeypatch.setattr(main_module, "database", database)
    pool = SessionThreadPool()
    monkeypatch.setattr(main_module, "session_thread_pool", pool)
    request.addfinalizer(pool.shutdown)
    request.addfinalizer(database.engine.dispose)
    with TestClient(main_module.app) as client:
        def register(name):
            response = client.post("/api/v1/auth/register", json={"username": name, "password": "strong-pass-123"})
            assert response.status_code == 200
            body = response.json()
            return body["user"], {"Authorization": "Bearer " + body["access_token"]}
        first, headers = register("appearance-a")
        other, other_headers = register("appearance-b")
        default = first["display_name"]
        assert re.fullmatch(r"用户(?=[a-z0-9]{0,7}[a-z])(?=[a-z0-9]{0,7}[0-9])[a-z0-9]{8}", default)
        assert default != other["display_name"]
        assert client.patch("/api/v1/auth/me", json={"display_name": "未登录"}).status_code == 401
        response = client.patch("/api/v1/auth/me", headers=headers,
                                json={"display_name": "  小林  ", "avatar_data": image_data()})
        assert response.status_code == 200
        updated = response.json()
        assert updated["display_name"] == "小林" and updated["username"] == first["username"]
        assert updated["role"] == "user"
        assert len(updated["avatar_data"].encode("ascii")) > 65535
        with Image.open(BytesIO(base64.b64decode(updated["avatar_data"].split(",")[1]))) as image:
            assert image.size == (256, 256) and image.format == "PNG"
        assert client.get("/api/v1/auth/me", headers=other_headers).json() == other
        login = client.post("/api/v1/auth/login", json={"username": first["username"], "password": "strong-pass-123"})
        assert login.status_code == 200 and login.json()["user"] == updated
        reopened = Database(url, database.auth_secret)
        reopened.initialize()
        try:
            assert reopened.get_user(first["id"])["avatar_data"] == updated["avatar_data"]
            assert reopened.get_user(first["id"])["display_name"] == "小林"
        finally:
            reopened.engine.dispose()
        assert client.patch("/api/v1/auth/me", headers=headers, json={"display_name": "新昵称"}).json()["avatar_data"] == updated["avatar_data"]
        for invalid in ({"display_name": "x" * 41}, {"avatar_data": "data:image/svg+xml;base64,PHN2Zz4="},
                        {"avatar_data": "data:image/png;base64,invalid"}, {"user_id": other["id"]}, {"role": "admin"}):
            assert client.patch("/api/v1/auth/me", headers=headers, json=invalid).status_code == 422
        reset = client.patch("/api/v1/auth/me", headers=headers, json={"display_name": " ", "avatar_data": None}).json()
        assert reset["display_name"] == default and reset["avatar_data"] is None


def test_default_name_is_stable_for_existing_accounts_and_mixed():
    for i in range(100):
        name = default_display_name(i, f"old-user-{i}")
        assert name == default_display_name(i, f"old-user-{i}")
        assert len(name[2:]) == 8 and name[2:].isalnum()
        assert any(c.isdigit() for c in name[2:]) and any(c.isalpha() for c in name[2:])


def test_risk_result_uses_current_avatar_and_escapes_nickname():
    profile = {"investor_category": "C3"}
    avatar = "data:image/png;base64,dGVzdA=="
    html = result_html(profile, {"display_name": '<img src=x onerror="alert(1)">', "avatar_data": avatar})
    assert avatar in html and DEFAULT_AVATAR not in html
    assert '&lt;img' in html and '<img src=x' not in html
    assert DEFAULT_AVATAR in result_html(profile, {})


def test_settings_saves_an_uploaded_avatar_and_restores_it_on_login():
    jpeg = image_data()
    script = '''
import base64
from io import BytesIO
from unittest.mock import patch

import streamlit as st
from backend.app.user_appearance import normalize_avatar
from frontend import streamlit_app as ui
from frontend.user_settings import render_user_settings

ui.init_session()
st.session_state.setdefault('test_stored_user', {
    'id': 1, 'username': 'test1', 'role': 'user', 'display_name': '用户a1234567', 'avatar_data': None,
})
if not st.session_state.get('auth_user'):
    with patch.object(ui, 'prefetch_research_board_data'):
        ui.complete_login({
            'user': st.session_state.test_stored_user,
            'access_token': 'isolated-token',
            'session_beacon_token': 'isolated-beacon',
        })
file = None
if not st.session_state.test_stored_user['avatar_data']:
    file = BytesIO(base64.b64decode(JPEG_DATA.split(',', 1)[1]))
    file.name = 'avatar.jpg'
def api(base, method, path, payload):
    assert method == 'PATCH' and path == '/auth/me'
    assert payload['avatar_data'] == JPEG_DATA
    st.session_state.test_stored_user.update(
        display_name=payload['display_name'], avatar_data=normalize_avatar(payload['avatar_data']),
    )
    return dict(st.session_state.test_stored_user)
with patch('frontend.user_settings.st.file_uploader', return_value=file):
    render_user_settings('http://localhost', api_request=api)
st.button('模拟退出', on_click=ui.reset_user_session)
'''
    app = AppTest.from_string(script.replace("JPEG_DATA", repr(jpeg))).run(timeout=15)
    assert not app.exception and app.text_input[0].value == "用户a1234567"
    app.text_input[0].set_value("自定义昵称")
    next(b for b in app.button if b.label == "保存个人设置").click().run(timeout=15)
    assert not app.exception and app.success and not app.error
    account = app.session_state["auth_user"]
    assert account["display_name"] == "自定义昵称"
    assert account["avatar_data"].startswith("data:image/png;base64,")
    assert "id" not in account
    next(b for b in app.button if b.label == "模拟退出").click().run(timeout=15)
    assert not app.exception and app.text_input[0].value == "自定义昵称"
    assert app.session_state["auth_user"] == account


def test_planning_has_its_own_page_and_risk_page_omits_planning_sections():
    script = '''
import streamlit as st
from unittest.mock import patch
from frontend import streamlit_app as ui
from backend.app.risk_questionnaire import evaluate_answers, QUESTIONS
ui.init_session()
answers = dict(zip((q.id for q in QUESTIONS), 'ACABDC CACCB BCCBC CDB CADAECDC'.replace(' ', '')))
st.session_state.profile.update(evaluate_answers(answers), confirmed=True)
st.session_state.risk_assessment_editing = False
st.session_state.auth_user = {'id': 1, 'username': 'test1'}
ui.page_profile('http://localhost')
'''
    risk = AppTest.from_string(script).run(timeout=15)
    assert not risk.exception and not risk.text_area
    text = "\n".join(item.value for item in risk.markdown)
    assert "评级依据与用户画像" in text
    assert "财务规划与资金安排" not in text and "根据资金约束调整的配置参考" not in text
    planning = AppTest.from_string(script.replace("ui.page_profile(", "ui.page_planning(")).run(timeout=15)
    assert not planning.exception
    assert [s.value for s in planning.subheader] == ["自动投资规划", "风险偏好识别", "收益预期分析", "投资期限规划", "个性化投资建议与资产配置"]
    assert planning.text_area and planning.table
