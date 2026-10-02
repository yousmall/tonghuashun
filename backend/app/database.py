"""MySQL 持久化层：用户、会话与消息均在这里集中读写。"""

from __future__ import annotations

import os
import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from threading import Event
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import Float, case, cast, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import hash_password
from backend.app.risk_questionnaire import local_today


class DatabaseUnavailable(RuntimeError):
    """数据库未配置或暂时无法使用。"""


class ProfileVersionConflict(ValueError):
    """保存画像时客户端版本已经落后。"""


class ProfileDailyLimit(ValueError):
    """同一账号的新风险测评每天只保存一次。"""


class UsernameExists(ValueError):
    """注册账号已存在。"""


class WatchlistItemExists(ValueError):
    """同一账号已收藏相同类型和名称的标的。"""


class WatchlistCapacityExceeded(ValueError):
    """单个账号的自选标的超过产品上限。"""


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    conversations: Mapped[list["ConversationRow"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AdminRoleRow(Base):
    """独立权限表，旧用户表无需修改；公开注册永远不会写入这里。"""
    __tablename__ = "admin_roles"
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class WechatIdentityRow(Base):
    """微信身份只绑定普通用户；不与用户名相同的管理员账号自动合并。"""

    __tablename__ = "wechat_identities"
    openid: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)


class IwencaiCallRow(Base):
    """每次真实 HTTP 尝试一条记录，包含重试与空结果。"""
    __tablename__ = "iwencai_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    endpoint: Mapped[str] = mapped_column(String(80))
    skill_id: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    fact_count: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    user: Mapped[UserRow] = relationship(back_populates="conversations")
    messages: Mapped[list["MessageRow"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class ProfileRow(Base):
    """每个账号一份已确认画像；退出或换标签页后仍可恢复。"""

    __tablename__ = "profiles"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_user_conversation_id", "user_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    conversation: Mapped[ConversationRow] = relationship(back_populates="messages")


class ConsultationRow(Base):
    """与用户消息一一对应的轻量统计，不在统计排序中携带大 JSON。"""
    __tablename__ = "consultation_stats"
    __table_args__ = (Index("ix_consultation_domain_topic", "domain", "topic"),)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(40), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WatchlistRow(Base):
    """账号级自选列表；不依赖 Streamlit 临时会话。"""

    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "target", "asset_type", name="uq_watchlist_user_target_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target: Mapped[str] = mapped_column(String(60), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class Database:
    """可显式关闭的数据库门面，便于旧版规则模式和测试继续运行。"""

    def __init__(self, url: str | None, auth_secret: str | None) -> None:
        self.url = url
        self.auth_secret = auth_secret
        self.engine = None
        self.session_factory: sessionmaker[Session] | None = None
        self.initialization_error: str | None = None
        if url:
            options: dict[str, Any] = {"pool_pre_ping": True}
            if url.startswith("sqlite") and ":memory:" in url:
                options.update({"connect_args": {"check_same_thread": False}, "poolclass": StaticPool})
            elif url.startswith("mysql"):
                options["pool_recycle"] = 1800
            self.engine = create_engine(url, **options)
            self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def from_env(cls) -> "Database":
        url = os.getenv("MYSQL_URL")
        if not url:
            host = os.getenv("MYSQL_HOST")
            user = os.getenv("MYSQL_USER")
            password = os.getenv("MYSQL_PASSWORD")
            database = os.getenv("MYSQL_DATABASE")
            if all((host, user, password, database)):
                port = os.getenv("MYSQL_PORT", "3306")
                url = (
                    f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
                    f"@{host}:{port}/{quote_plus(database)}?charset=utf8mb4"
                )
        return cls(url, os.getenv("WENCE_AUTH_SECRET"))

    @property
    def configured(self) -> bool:
        return bool(
            self.engine is not None
            and self.session_factory is not None
            and self.auth_secret
            and len(self.auth_secret) >= 32
        )

    def initialize(self, *, backfill: bool = True) -> None:
        if not self.engine:
            return
        try:
            Base.metadata.create_all(self.engine)
            # create_all 不会为已存在的表补建索引。
            for table in (ConversationRow.__table__, MessageRow.__table__):
                for index in table.indexes:
                    if index.name in {"ix_conversations_user_updated", "ix_messages_user_conversation_id"}:
                        index.create(self.engine, checkfirst=True)
            if backfill:
                self._backfill_consultations()
            self.initialization_error = None
        except SQLAlchemyError as exc:
            self.initialization_error = type(exc).__name__

    def backfill_consultations(self, stop_event: Event | None = None) -> None:
        """表结构就绪后可在后台补齐统计，不阻塞应用接收请求。"""
        if self.engine is not None and self.initialization_error is None:
            self._backfill_consultations(stop_event)

    def _backfill_consultations(self, stop_event: Event | None = None) -> None:
        """幂等补齐历史消息的轻量统计；缺少元数据时保留为未分类。"""
        assert self.session_factory is not None
        while stop_event is None or not stop_event.is_set():
            with self.session_factory.begin() as session:
                rows = session.execute(select(
                    MessageRow.id, MessageRow.user_id, MessageRow.created_at,
                    cast(MessageRow.payload["_analytics"]["domain"].as_string(), String(40)),
                    cast(MessageRow.payload["_analytics"]["topic"].as_string(), String(200)),
                    func.substr(MessageRow.content, 1, 200),
                ).outerjoin(ConsultationRow, ConsultationRow.message_id == MessageRow.id).where(
                    MessageRow.role == "user", ConsultationRow.message_id.is_(None),
                ).limit(200)).all()
                if not rows:
                    return
                session.add_all([ConsultationRow(message_id=message_id, user_id=user_id,
                                                 created_at=created, domain=domain or "unknown",
                                                 topic=topic or content or "未命名主题")
                                 for message_id, user_id, created, domain, topic, content in rows])

    def require_ready(self) -> None:
        if not self.configured:
            raise DatabaseUnavailable("尚未完整配置 MySQL 或 WENCE_AUTH_SECRET")
        if self.initialization_error:
            raise DatabaseUnavailable("MySQL 暂时不可用，请检查连接配置")

    def create_user(self, username: str, password_hash: str, *, admin: bool = False) -> dict[str, Any]:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            if session.scalar(select(UserRow.id).where(UserRow.username == username)) is not None:
                raise UsernameExists("该账号已存在")
            row = UserRow(username=username, password_hash=password_hash)
            session.add(row)
            try:
                if admin:
                    session.flush()
                    session.add(AdminRoleRow(user_id=row.id))
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if session.scalar(select(UserRow.id).where(UserRow.username == username)) is not None:
                    raise UsernameExists("该账号已存在") from exc
                raise
            session.refresh(row)
            return {**self._user_dict(row), "role": "admin" if admin else "user"}

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.scalar(select(UserRow).where(UserRow.username == username))
            return self._user_with_role(session, row, include_password=True) if row else None

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            return self._user_with_role(session, row) if row else None

    def get_or_create_wechat_user(self, openid: str) -> dict[str, Any]:
        """按微信 openid 原子绑定独立账号；并发回调收敛到同一身份。"""
        self.require_ready()
        assert self.session_factory is not None
        if not openid or len(openid) > 128:
            raise ValueError("微信身份无效")
        username = "wx_" + hashlib.sha256(openid.encode("utf-8")).hexdigest()[:40]
        try:
            with self.session_factory.begin() as session:
                identity = session.get(WechatIdentityRow, openid)
                if identity is None:
                    row = UserRow(username=username, password_hash=hash_password(secrets.token_urlsafe(48)))
                    session.add(row)
                    session.flush()
                    session.add(WechatIdentityRow(openid=openid, user_id=row.id))
                else:
                    row = session.get(UserRow, identity.user_id)
                    if row is None:
                        raise ValueError("微信绑定账号不存在")
                result = self._user_with_role(session, row)
            return result
        except IntegrityError:
            with self.session_factory() as session:
                identity = session.get(WechatIdentityRow, openid)
                if identity is None:
                    raise
                row = session.get(UserRow, identity.user_id)
                if row is None:
                    raise ValueError("微信绑定账号不存在")
                return self._user_with_role(session, row)

    def save_profile(
        self, user_id: int, payload: dict[str, Any], version: int,
        expected_version: int | None = None,
    ) -> None:
        """写入或更新账号的已确认画像。"""

        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(ProfileRow).where(ProfileRow.user_id == user_id).with_for_update()
            )
            current_version = int(row.version) if row is not None else 1
            if expected_version is not None and expected_version != current_version:
                raise ProfileVersionConflict("投资偏好已在其他页面更新，请重新打开投资偏好后再确认。")
            if (row is not None and payload.get("questionnaire_version")
                    and row.payload.get("questionnaire_version")
                    and str(row.payload.get("assessed_on")) == str(local_today())):
                raise ProfileDailyLimit("今日已保存风险测评，每日只能保存一次，请明日重新测评。")
            if row is None:
                session.add(ProfileRow(user_id=user_id, payload=payload, version=version))
            else:
                row.payload = payload
                row.version = version
                row.updated_at = datetime.now(timezone.utc)

    def get_profile(self, user_id: int) -> dict[str, Any] | None:
        """读取账号已保存的画像；没有保存过时返回 None。"""

        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.get(ProfileRow, user_id)
            if row is None:
                return None
            return {"payload": dict(row.payload), "version": int(row.version)}

    def list_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        """按最近添加顺序返回账号自选，不暴露 user_id。"""

        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            rows = session.scalars(
                select(WatchlistRow)
                .where(WatchlistRow.user_id == user_id)
                .order_by(WatchlistRow.created_at.desc(), WatchlistRow.id.desc())
            ).all()
            return [self._watchlist_dict(row) for row in rows]

    def add_watchlist_item(self, user_id: int, target: str, asset_type: str) -> dict[str, Any]:
        """新增账号自选；最多 50 条，并把并发重复写转换成可读业务错误。"""

        self.require_ready()
        assert self.session_factory is not None
        try:
            with self.session_factory.begin() as session:
                existing = session.scalar(
                    select(WatchlistRow.id).where(
                        WatchlistRow.user_id == user_id,
                        WatchlistRow.target == target,
                        WatchlistRow.asset_type == asset_type,
                    )
                )
                if existing is not None:
                    raise WatchlistItemExists("该标的已在自选中")
                count = session.scalar(
                    select(func.count(WatchlistRow.id)).where(WatchlistRow.user_id == user_id)
                )
                if int(count or 0) >= 50:
                    raise WatchlistCapacityExceeded("自选标的最多保存 50 个，请先移除不再关注的标的")
                row = WatchlistRow(user_id=user_id, target=target, asset_type=asset_type)
                session.add(row)
                session.flush()
                result = self._watchlist_dict(row)
            return result
        except IntegrityError as exc:
            raise WatchlistItemExists("该标的已在自选中") from exc

    def remove_watchlist_item(self, user_id: int, item_id: int) -> bool:
        """只允许删除当前账号自己的自选项。"""

        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            row = session.scalar(
                select(WatchlistRow).where(
                    WatchlistRow.id == item_id,
                    WatchlistRow.user_id == user_id,
                )
            )
            if row is None:
                return False
            session.delete(row)
            return True

    def save_exchange(
        self,
        user_id: int,
        conversation_id: str | None,
        query: str,
        request_payload: dict[str, Any],
        answer: str,
        response_payload: dict[str, Any],
    ) -> str:
        """在同一事务中保存一次用户提问和助手回答。"""
        self.require_ready()
        assert self.session_factory is not None
        resolved_id = conversation_id or str(uuid.uuid4())
        with self.session_factory.begin() as session:
            conversation = session.get(ConversationRow, resolved_id)
            if conversation and conversation.user_id != user_id:
                resolved_id = str(uuid.uuid4())
                conversation = None
            if not conversation:
                conversation = ConversationRow(
                    id=resolved_id, user_id=user_id, title=query.strip()[:200] or "新对话"
                )
                session.add(conversation)
            conversation.updated_at = datetime.now(timezone.utc)
            user_message = MessageRow(conversation_id=resolved_id, user_id=user_id,
                                      role="user", content=query, payload=request_payload)
            session.add_all([user_message, MessageRow(conversation_id=resolved_id, user_id=user_id,
                                                     role="assistant", content=answer, payload=response_payload)])
            session.flush()
            metadata = request_payload.get("_analytics") or {}
            session.add(ConsultationRow(message_id=user_message.id, user_id=user_id,
                                        domain=metadata.get("domain") or "unknown",
                                        topic=metadata.get("topic") or query[:200] or "未命名主题",
                                        created_at=user_message.created_at))
        return resolved_id

    def list_conversations(
        self, user_id: int, limit: int = 50, offset: int = 0, search: str = "",
    ) -> list[dict[str, Any]]:
        self.require_ready()
        assert self.session_factory is not None
        # 只对当前页的会话计数，避免先聚合该账号全部消息。
        count_query = (
            select(func.count(MessageRow.id))
            .where(MessageRow.user_id == user_id, MessageRow.conversation_id == ConversationRow.id)
            .correlate(ConversationRow)
            .scalar_subquery()
        )
        # 把“每个会话的最后一条消息”做成相关子查询并随列表一次取回，避免
        # 历史记录达到 50 条时产生 1 + 50 次数据库往返。
        last_message_query = (
            select(MessageRow.content)
            .where(
                MessageRow.conversation_id == ConversationRow.id,
                MessageRow.user_id == user_id,
            )
            .order_by(MessageRow.id.desc())
            .limit(1)
            .correlate(ConversationRow)
            .scalar_subquery()
        )
        query = (
            select(ConversationRow, count_query.label("message_count"), last_message_query.label("last_message"))
            .where(ConversationRow.user_id == user_id)
        )
        if search.strip():
            slash = chr(92)
            escaped = search.strip().replace(slash, slash * 2).replace("%", slash + "%").replace("_", slash + "_")
            query = query.where(ConversationRow.title.like(f"%{escaped}%", escape=slash))
        with self.session_factory() as session:
            rows = session.execute(
                query.order_by(ConversationRow.updated_at.desc(), ConversationRow.id.desc())
                .offset(offset).limit(limit)
            ).all()
            results = []
            for conversation, message_count, last_message in rows:
                results.append(
                    {
                        "id": conversation.id, "title": conversation.title,
                        "created_at": self._utc(conversation.created_at),
                        "updated_at": self._utc(conversation.updated_at),
                        "message_count": int(message_count or 0), "last_message": last_message,
                    }
                )
            return results

    def get_conversation(
        self, user_id: int, conversation_id: str, before_id: int | None = None, limit: int = 40,
    ) -> dict[str, Any] | None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            conversation = session.scalar(
                select(ConversationRow).where(
                    ConversationRow.id == conversation_id, ConversationRow.user_id == user_id
                )
            )
            if not conversation:
                return None
            query = select(MessageRow).where(
                MessageRow.conversation_id == conversation_id, MessageRow.user_id == user_id
            )
            if before_id is not None:
                query = query.where(MessageRow.id < before_id)
            newest = session.scalars(query.order_by(MessageRow.id.desc()).limit(limit + 1)).all()
            has_more = len(newest) > limit
            messages = list(reversed(newest[:limit]))
            return {
                "id": conversation.id, "title": conversation.title,
                "created_at": self._utc(conversation.created_at),
                "updated_at": self._utc(conversation.updated_at),
                "has_more": has_more,
                "next_before_id": messages[0].id if has_more and messages else None,
                "messages": [
                    {
                        "id": message.id, "role": message.role, "content": message.content,
                        "payload": message.payload, "created_at": self._utc(message.created_at),
                    }
                    for message in messages
                ],
            }

    def rename_conversation(self, user_id: int, conversation_id: str, title: str) -> bool:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            row = session.scalar(select(ConversationRow).where(
                ConversationRow.id == conversation_id, ConversationRow.user_id == user_id,
            ))
            if row is None:
                return False
            row.title = title
            return True

    def admin_users(self, online: dict[int, int], *, since: datetime | None = None,
                    search: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """全部注册账号分页展示，在线账号始终优先；聚合查询避免逐用户读取。"""
        self.require_ready()
        assert self.session_factory is not None
        filters = [MessageRow.role == "user"]
        if since:
            filters.append(MessageRow.created_at >= since)
        counts = select(
            MessageRow.user_id.label("user_id"), func.count().label("consultations"),
            func.count(func.distinct(MessageRow.conversation_id)).label("conversations"),
            func.max(MessageRow.created_at).label("last_consultation"),
        ).where(*filters).group_by(MessageRow.user_id).subquery()
        users = select(UserRow, AdminRoleRow.user_id, counts.c.consultations,
                       counts.c.conversations, counts.c.last_consultation).outerjoin(
            AdminRoleRow, AdminRoleRow.user_id == UserRow.id,
        ).outerjoin(counts, counts.c.user_id == UserRow.id)
        conditions = []
        if search.strip():
            slash = chr(92)
            escaped = search.strip().replace(slash, slash * 2).replace("%", slash + "%").replace("_", slash + "_")
            conditions.append(UserRow.username.like(f"%{escaped}%", escape=slash))
        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(UserRow).where(*conditions)) or 0
            rows = session.execute(users.where(*conditions).order_by(
                case((UserRow.id.in_(list(online)), 0), else_=1), UserRow.created_at.desc(), UserRow.id.desc(),
            ).offset(offset).limit(limit)).all()
            return {"total": total, "items": [
                {**self._user_dict(user), "role": "admin" if admin_id is not None else "user",
                 "online": user.id in online, "online_sessions": online.get(user.id, 0),
                 "consultations": int(count or 0), "conversations": int(conversations or 0),
                 "last_consultation": self._utc(last) if last else None}
                for user, admin_id, count, conversations, last in rows
            ]}

    def record_iwencai_call(self, event: dict[str, Any]) -> None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            session.add(IwencaiCallRow(**event))

    def admin_statistics(self, *, since: datetime | None = None, user_id: int | None = None) -> dict[str, Any]:
        """每轮已保存咨询计一次；旧记录缺少领域时明确归入未分类。"""
        from backend.app.services.admin import DOMAIN_LABELS

        self.require_ready()
        assert self.session_factory is not None
        domain = ConsultationRow.domain
        topic = ConsultationRow.topic
        conditions = []
        if since:
            conditions.append(ConsultationRow.created_at >= since)
        if user_id is not None:
            conditions.append(ConsultationRow.user_id == user_id)
        grouped = select(domain.label("domain"), topic.label("topic"),
                         func.count().label("count"), func.count(func.distinct(ConsultationRow.user_id)).label("users"))
        grouped = grouped.where(*conditions).group_by(domain, topic).subquery()
        ranked = select(grouped, func.row_number().over(
            partition_by=grouped.c.domain, order_by=(grouped.c.count.desc(), grouped.c.topic.asc()),
        ).label("rank")).subquery()
        with self.session_factory() as session:
            total, active_users = session.execute(select(
                func.count(), func.count(func.distinct(ConsultationRow.user_id)),
            ).select_from(ConsultationRow).where(*conditions)).one()
            domain_counts = dict(session.execute(select(domain, func.count()).where(*conditions).group_by(domain)).all())
            top = session.execute(select(ranked).where(ranked.c.rank <= 5).order_by(ranked.c.domain, ranked.c.rank)).mappings().all()
            daily = session.execute(select(func.date(ConsultationRow.created_at), func.count()).where(*conditions)
                                    .group_by(func.date(ConsultationRow.created_at)).order_by(func.date(ConsultationRow.created_at))).all()
            return {
                "consultations": total, "consulting_users": active_users,
                "domains": [{"domain": key, "label": label, "count": int(domain_counts.get(key, 0)),
                             "top": [{"topic": item["topic"], "count": item["count"], "users": item["users"]}
                                     for item in top if item["domain"] == key]}
                            for key, label in DOMAIN_LABELS.items()],
                "daily": [{"date": str(day), "count": count} for day, count in daily],
            }

    def admin_recent_consultations(self, user_id: int, since: datetime | None = None) -> list[dict[str, Any]]:
        self.require_ready()
        assert self.session_factory is not None
        conditions = [MessageRow.role == "user", MessageRow.user_id == user_id]
        if since:
            conditions.append(MessageRow.created_at >= since)
        with self.session_factory() as session:
            rows = session.execute(select(MessageRow.content, MessageRow.created_at,
                                          MessageRow.payload["_analytics"]["domain"].as_string())
                                   .where(*conditions).order_by(MessageRow.id.desc()).limit(50)).all()
            return [{"query": content, "created_at": self._utc(created), "domain": domain or "unknown"}
                    for content, created, domain in rows]

    def admin_iwencai_statistics(self, since: datetime | None = None) -> dict[str, Any]:
        self.require_ready()
        assert self.session_factory is not None
        conditions = [IwencaiCallRow.created_at >= since] if since else []
        c = IwencaiCallRow
        aggregates = [func.count().label("total"),
                      func.sum(case((c.status.in_(["success", "empty"]), 1), else_=0)).label("successful"),
                      func.sum(case((c.status == "failed", 1), else_=0)).label("failed"),
                      func.sum(case((c.status == "empty", 1), else_=0)).label("empty"),
                      func.sum(case((c.attempt > 0, 1), else_=0)).label("retries"),
                      func.avg(c.duration_ms).label("average_ms"), func.sum(c.fact_count).label("facts")]
        def clean(row):
            return {key: round(float(value), 2) if key == "average_ms" and value is not None else int(value or 0)
                    for key, value in row.items()}
        with self.session_factory() as session:
            summary = clean(session.execute(select(*aggregates).where(*conditions)).mappings().one())
            grouped = session.execute(select(c.endpoint, c.skill_id, *aggregates).where(*conditions)
                                      .group_by(c.endpoint, c.skill_id).order_by(func.count().desc(), c.skill_id)).mappings().all()
            errors = session.execute(select(c.error_type, c.status_code, func.count()).where(
                *conditions, c.status == "failed",
            ).group_by(c.error_type, c.status_code).order_by(func.count().desc())).all()
            daily = session.execute(select(func.date(c.created_at), func.count(),
                                           func.sum(case((c.status == "failed", 1), else_=0)))
                                    .where(*conditions).group_by(func.date(c.created_at)).order_by(func.date(c.created_at))).all()
            first = session.scalar(select(func.min(c.created_at)))
            summary.update({
                "by_interface": [{"endpoint": row["endpoint"], "skill_id": row["skill_id"],
                                  **clean({k: v for k, v in row.items() if k not in {"endpoint", "skill_id"}})} for row in grouped],
                "errors": [{"error_type": kind, "status_code": code, "count": count} for kind, code, count in errors],
                "daily": [{"date": str(day), "count": count, "failed": int(failed or 0)} for day, count, failed in daily],
                "first_recorded_at": self._utc(first) if first else None,
            })
            return summary

    @classmethod
    def _user_with_role(cls, session: Session, row: UserRow, include_password: bool = False) -> dict[str, Any]:
        return {**cls._user_dict(row, include_password), "role": "admin" if session.get(AdminRoleRow, row.id) else "user"}

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @classmethod
    def _user_dict(cls, row: UserRow, include_password: bool = False) -> dict[str, Any]:
        result = {"id": row.id, "username": row.username, "created_at": cls._utc(row.created_at)}
        if include_password:
            result["password_hash"] = row.password_hash
        return result

    @classmethod
    def _watchlist_dict(cls, row: WatchlistRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "target": row.target,
            "asset_type": row.asset_type,
            "created_at": cls._utc(row.created_at),
        }
