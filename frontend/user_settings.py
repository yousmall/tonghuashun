"""账号昵称、头像和个人设置界面。"""
from __future__ import annotations

import base64
from typing import Callable

import streamlit as st

from backend.app.user_appearance import default_display_name

# 本地矢量默认头像，不依赖外部图片服务。
DEFAULT_AVATAR = "data:image/svg+xml;base64," + base64.b64encode(b'''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect width="256" height="256" rx="128" fill="#eef1f6"/><circle cx="128" cy="94" r="43" fill="#8a99ad"/><path d="M43 236c0-53 34-86 85-86s85 33 85 86" fill="#8a99ad"/></svg>''').decode("ascii")


def account_session_data(user: dict) -> dict:
    """保留当前账号的展示资料和角色。"""
    return {
        "username": user["username"],
        "role": user.get("role", "user"),
        "display_name": user.get("display_name"),
        "avatar_data": user.get("avatar_data"),
    }


def display_name(user: dict) -> str:
    return user.get("display_name") or default_display_name(user.get("id", 0), user.get("username", ""))


def avatar_uri(user: dict | None = None) -> str:
    value = (user or {}).get("avatar_data")
    if isinstance(value, str) and value.startswith("data:image/png;base64,"):
        return value
    return DEFAULT_AVATAR


def render_user_settings(api_base: str, *, api_request: Callable) -> None:
    user = st.session_state.get("auth_user") or {}
    st.title("个人设置")
    if st.session_state.pop("account_settings_saved", False):
        st.success("个人设置已保存。")
    st.caption("自定义展示名称和头像；昵称留空后保存，会恢复为“用户 + 8位数字字母混合”。")
    st.image(avatar_uri(user), width=96)
    account = user.get("username", "")
    st.caption("登录账号：" + ("微信账号" if account.startswith("wx_") and len(account) == 43 else account))
    revision = st.session_state.get("account_settings_revision", 0)
    with st.form("account_settings_form"):
        name = st.text_input("展示名称", value=display_name(user), max_chars=40,
                             key=f"account_settings_name_{revision}")
        uploaded = st.file_uploader("上传头像", type=["png", "jpg", "jpeg", "webp"], max_upload_size=2,
                                    help="支持PNG、JPG和WebP，最大2MB；保存时会裁剪为正方形。",
                                    key=f"account_settings_avatar_{revision}")
        remove_avatar = st.checkbox("恢复默认头像", key=f"account_settings_remove_{revision}")
        submitted = st.form_submit_button("保存个人设置", type="primary")
    if not submitted:
        return
    if uploaded is not None and remove_avatar:
        st.warning("上传头像与恢复默认头像请选择一项。")
        return
    payload = {"display_name": name.strip()}
    if remove_avatar:
        payload["avatar_data"] = None
    elif uploaded is not None:
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(uploaded.name.rsplit(".", 1)[-1].lower())
        if mime is None:
            st.error("请选择PNG、JPG或WebP图片。")
            return
        payload["avatar_data"] = f"data:image/{mime};base64," + base64.b64encode(uploaded.getvalue()).decode("ascii")
    result = api_request(api_base, "PATCH", "/auth/me", payload)
    if result is None:
        return  # 通用 API 客户端已显示保存失败的具体原因。
    if isinstance(result, dict) and account and result.get("username") == account:
        st.session_state.auth_user = account_session_data(result)
        st.session_state.account_settings_revision = revision + 1
        st.session_state.account_settings_saved = True
        st.rerun()
    else:
        st.error("个人设置尚未保存，请重试。")
