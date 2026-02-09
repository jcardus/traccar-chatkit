from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Union
from uuid import uuid4

from chatkit.store import NotFoundError, Store
from chatkit.types import (
    AssistantMessageItem,
    Attachment,
    ClientToolCallItem,
    EndOfTurnItem,
    HiddenContextItem,
    Page,
    TaskItem,
    ThreadItem,
    ThreadMetadata,
    UserMessageItem,
    WidgetItem,
    WorkflowItem,
)
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter

from .traccar import invoke


@dataclass
class _ThreadState:
    thread: ThreadMetadata
    items: List[ThreadItem]


_pool: AsyncConnectionPool | None = None


async def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        conninfo = os.environ["DATABASE_URL"]
        _pool = AsyncConnectionPool(conninfo=conninfo, min_size=1, max_size=10, open=False)
        await _pool.open()
    return _pool


class NeonStore(Store[dict[str, Any]]):
    """Neon Postgres-backed persistent store compatible with the ChatKit server interface."""

    def __init__(self) -> None:
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        pool = await _get_pool()
        async with pool.connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    data TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS thread_items (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_thread_items_thread_id
                ON thread_items(thread_id, created_at)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_threads_user_id
                ON threads(user_id)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS html_reports (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    thread_id TEXT,
                    url TEXT NOT NULL,
                    image_url TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_html_reports_user_id
                ON html_reports(user_id)
            """)
            await conn.execute("""
                ALTER TABLE html_reports ADD COLUMN IF NOT EXISTS image_url TEXT
            """)
        self._initialized = True

    def _get_user_email_from_traccar(self, context: dict[str, Any]) -> str | None:
        """Get user email from Traccar session."""
        try:
            request = context.get("request")
            if not request:
                return None
            session = invoke("get", "session", "", request)
            return session.get("email") if session else None
        except Exception as e:
            print(f"Failed to get user from Traccar: {e}")
            return None

    def _deserialize_thread_item(self, data: Dict[str, Any]) -> ThreadItem:
        """Deserialize a thread item from JSON data using Pydantic's discriminated union."""
        adapter: TypeAdapter[
            Union[
                UserMessageItem,
                AssistantMessageItem,
                ClientToolCallItem,
                HiddenContextItem,
                EndOfTurnItem,
                TaskItem,
                WidgetItem,
                WorkflowItem,
            ]
        ] = TypeAdapter(
            Union[
                UserMessageItem,
                AssistantMessageItem,
                ClientToolCallItem,
                HiddenContextItem,
                EndOfTurnItem,
                TaskItem,
                WidgetItem,
                WorkflowItem,
            ]
        )
        return adapter.validate_python(data)

    # -- Thread metadata -------------------------------------------------
    async def load_thread(self, thread_id: str, context: dict[str, Any]) -> ThreadMetadata:
        await self._ensure_schema()
        pool = await _get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT data FROM threads WHERE id = %s", (thread_id,)
            )
            row = await cur.fetchone()
            if not row:
                raise NotFoundError(f"Thread {thread_id} not found")
            thread_data = json.loads(row[0])
            thread_data.pop("items", None)
            return ThreadMetadata.model_validate(thread_data)

    async def save_thread(self, thread: ThreadMetadata, context: dict[str, Any]) -> None:
        await self._ensure_schema()
        pool = await _get_pool()
        thread_dict = thread.model_dump(exclude={"items"})
        thread_json = json.dumps(thread_dict, default=str)
        created_at = thread.created_at or datetime.now(timezone.utc)
        updated_at = datetime.now(timezone.utc)
        user_email = self._get_user_email_from_traccar(context)

        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO threads (id, user_id, data, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at
                """,
                (thread.id, user_email, thread_json, created_at, updated_at),
            )

    async def load_threads(
        self,
        limit: int,
        after: str | None,
        order: str,
        context: dict[str, Any],
    ) -> Page[ThreadMetadata]:
        await self._ensure_schema()
        user_email = self._get_user_email_from_traccar(context)

        if not user_email:
            return Page(data=[], has_more=False, after=None)

        pool = await _get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, data FROM threads WHERE user_id = %s", (user_email,)
            )
            rows = await cur.fetchall()

        threads = []
        for thread_id, thread_json in rows:
            thread_data = json.loads(thread_json)
            thread_data.pop("items", None)
            thread = ThreadMetadata.model_validate(thread_data)
            threads.append(thread)

        threads.sort(
            key=lambda t: t.created_at or datetime.min,
            reverse=(order == "desc"),
        )

        if after:
            index_map = {thread.id: idx for idx, thread in enumerate(threads)}
            start = index_map.get(after, -1) + 1
        else:
            start = 0

        slice_threads = threads[start : start + limit + 1]
        has_more = len(slice_threads) > limit
        slice_threads = slice_threads[:limit]
        next_after = slice_threads[-1].id if has_more and slice_threads else None
        return Page(
            data=slice_threads,
            has_more=has_more,
            after=next_after,
        )

    async def delete_thread(self, thread_id: str, context: dict[str, Any]) -> None:
        await self._ensure_schema()
        pool = await _get_pool()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM thread_items WHERE thread_id = %s", (thread_id,))
            await conn.execute("DELETE FROM threads WHERE id = %s", (thread_id,))

    # -- Thread items ----------------------------------------------------
    async def load_thread_items(
        self,
        thread_id: str,
        after: str | None,
        limit: int,
        order: str,
        context: dict[str, Any],
    ) -> Page[ThreadItem]:
        await self._ensure_schema()
        pool = await _get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT data FROM thread_items WHERE thread_id = %s ORDER BY created_at",
                (thread_id,),
            )
            rows = await cur.fetchall()

        items = []
        for (item_json,) in rows:
            item_data = json.loads(item_json)
            item = self._deserialize_thread_item(item_data)
            items.append(item.model_copy(deep=True))

        items.sort(
            key=lambda item: getattr(item, "created_at", datetime.now(timezone.utc)),
            reverse=(order == "desc"),
        )

        if after:
            index_map = {item.id: idx for idx, item in enumerate(items)}
            start = index_map.get(after, -1) + 1
        else:
            start = 0

        slice_items = items[start : start + limit + 1]
        has_more = len(slice_items) > limit
        slice_items = slice_items[:limit]
        next_after = slice_items[-1].id if has_more and slice_items else None
        return Page(data=slice_items, has_more=has_more, after=next_after)

    async def add_thread_item(
        self, thread_id: str, item: ThreadItem, context: dict[str, Any]
    ) -> None:
        await self._ensure_schema()
        pool = await _get_pool()
        item_json = item.model_dump_json()
        created_at = getattr(item, "created_at", datetime.now(timezone.utc))

        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO thread_items (id, thread_id, data, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    created_at = EXCLUDED.created_at
                """,
                (item.id, thread_id, item_json, created_at),
            )

    async def save_item(self, thread_id: str, item: ThreadItem, context: dict[str, Any]) -> None:
        await self.add_thread_item(thread_id, item, context)

    async def load_item(self, thread_id: str, item_id: str, context: dict[str, Any]) -> ThreadItem:
        await self._ensure_schema()
        pool = await _get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT data FROM thread_items WHERE id = %s AND thread_id = %s",
                (item_id, thread_id),
            )
            row = await cur.fetchone()
            if not row:
                raise NotFoundError(f"Item {item_id} not found")
            item_data = json.loads(row[0])
            return self._deserialize_thread_item(item_data).model_copy(deep=True)

    async def delete_thread_item(
        self, thread_id: str, item_id: str, context: dict[str, Any]
    ) -> None:
        await self._ensure_schema()
        pool = await _get_pool()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM thread_items WHERE id = %s", (item_id,))

    # -- HTML reports ----------------------------------------------------
    async def save_html_report(self, user_id: str | None, thread_id: str, url: str, image_url: str | None = None) -> None:
        await self._ensure_schema()
        pool = await _get_pool()
        report_id = uuid4().hex
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO html_reports (id, user_id, thread_id, url, image_url, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (report_id, user_id, thread_id, url, image_url, datetime.now(timezone.utc)),
            )

    # -- Files -----------------------------------------------------------
    async def save_attachment(
        self,
        attachment: Attachment,
        context: dict[str, Any],
    ) -> None:
        raise NotImplementedError(
            "NeonStore does not persist attachments. Provide a Store implementation "
            "that enforces authentication and authorization before enabling uploads."
        )

    async def load_attachment(
        self,
        attachment_id: str,
        context: dict[str, Any],
    ) -> Attachment:
        raise NotImplementedError(
            "NeonStore does not load attachments. Provide a Store implementation "
            "that enforces authentication and authorization before enabling uploads."
        )

    async def delete_attachment(self, attachment_id: str, context: dict[str, Any]) -> None:
        raise NotImplementedError(
            "NeonStore does not delete attachments because they are never stored."
        )
