"""MySQL 持久化层：用户、会话与消息均在这里集中读写。"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool


class DatabaseUnavailable(RuntimeError):
    """数据库未配置或暂时无法使用。"""


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


class ConversationRow(Base):
    __tablename__ = "conversations"

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

    def initialize(self) -> None:
        if not self.engine:
            return
        try:
            Base.metadata.create_all(self.engine)
            self.initialization_error = None
        except SQLAlchemyError as exc:
            self.initialization_error = type(exc).__name__

    def require_ready(self) -> None:
        if not self.configured:
            raise DatabaseUnavailable("尚未完整配置 MySQL 或 WENCE_AUTH_SECRET")
        if self.initialization_error:
            raise DatabaseUnavailable("MySQL 暂时不可用，请检查连接配置")

    def create_user(self, username: str, password_hash: str) -> dict[str, Any]:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            if session.scalar(select(UserRow.id).where(UserRow.username == username)) is not None:
                raise UsernameExists("该账号已存在")
            row = UserRow(username=username, password_hash=password_hash)
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if session.scalar(select(UserRow.id).where(UserRow.username == username)) is not None:
                    raise UsernameExists("该账号已存在") from exc
                raise
            session.refresh(row)
            return self._user_dict(row)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.scalar(select(UserRow).where(UserRow.username == username))
            return self._user_dict(row, include_password=True) if row else None

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory() as session:
            row = session.get(UserRow, user_id)
            return self._user_dict(row) if row else None

    def save_profile(self, user_id: int, payload: dict[str, Any], version: int) -> None:
        """写入或更新账号的已确认画像。"""

        self.require_ready()
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            row = session.get(ProfileRow, user_id)
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
            session.add_all(
                [
                    MessageRow(
                        conversation_id=resolved_id, user_id=user_id, role="user",
                        content=query, payload=request_payload,
                    ),
                    MessageRow(
                        conversation_id=resolved_id, user_id=user_id, role="assistant",
                        content=answer, payload=response_payload,
                    ),
                ]
            )
        return resolved_id

    def list_conversations(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        self.require_ready()
        assert self.session_factory is not None
        count_query = (
            select(MessageRow.conversation_id, func.count(MessageRow.id).label("message_count"))
            .group_by(MessageRow.conversation_id).subquery()
        )
        # 把“每个会话的最后一条消息”做成相关子查询并随列表一次取回，避免
        # 历史记录达到 50 条时产生 1 + 50 次数据库往返。
        last_message_query = (
            select(MessageRow.content)
            .where(
                MessageRow.conversation_id == ConversationRow.id,
                MessageRow.user_id == user_id,
            )
            .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
            .limit(1)
            .correlate(ConversationRow)
            .scalar_subquery()
        )
        with self.session_factory() as session:
            rows = session.execute(
                select(
                    ConversationRow,
                    count_query.c.message_count,
                    last_message_query.label("last_message"),
                )
                .join(count_query, count_query.c.conversation_id == ConversationRow.id, isouter=True)
                .where(ConversationRow.user_id == user_id)
                .order_by(ConversationRow.updated_at.desc()).limit(limit)
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

    def get_conversation(self, user_id: int, conversation_id: str) -> dict[str, Any] | None:
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
            messages = session.scalars(
                select(MessageRow).where(
                    MessageRow.conversation_id == conversation_id, MessageRow.user_id == user_id
                )
            ).all()
            # 排序在 Python 侧完成：消息 payload 可能较大，交给数据库 filesort 会
            # 触发 "Out of sort memory"（MySQL 默认 sort_buffer_size 256KB）。
            messages.sort(key=lambda message: (self._utc(message.created_at), message.id))
            return {
                "id": conversation.id, "title": conversation.title,
                "created_at": self._utc(conversation.created_at),
                "updated_at": self._utc(conversation.updated_at),
                "messages": [
                    {
                        "id": message.id, "role": message.role, "content": message.content,
                        "payload": message.payload, "created_at": self._utc(message.created_at),
                    }
                    for message in messages
                ],
            }

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
