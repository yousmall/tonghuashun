"""Keep a conversation panel's explicit open state across conditional widget cleanup."""
import streamlit as st


def _remember_open(key, saved_key):
    st.session_state[saved_key] = bool(st.session_state.get(key, False))


def persistent_expander(label, *, key, expanded=False, **kwargs):
    saved_key = key + '_open_state'
    if key not in st.session_state and saved_key in st.session_state:
        st.session_state[key] = bool(st.session_state[saved_key])
    panel = st.expander(label, key=key, expanded=expanded,
                        on_change=_remember_open, args=(key, saved_key), **kwargs)
    st.session_state[saved_key] = panel.open
    return panel
