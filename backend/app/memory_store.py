from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Union

from pydantic import TypeAdapter

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


@dataclass
class _ThreadState:
    thread: ThreadMetadata
    items: List[ThreadItem]


class SQLiteStore(Store[dict[str, Any]]):
    """SQLite-backed persistent store compatible with the ChatKit server interface."""

    def __init__(self, db_path: str | None = None) -> None:
        # Set default database path
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / "data" / "chatkit.db")

        self.db_path = db_path

        # Ensure the directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

        # In-memory cache for performance (per-user caches)
        self._threads: Dict[str, _ThreadState] = {}

        # Attachments intentionally unsupported; use a real store that enforces auth.

    def _init_db(self) -> None:
        """Initialize SQLite database schema."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Table for thread metadata
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
            """)

            # Table for thread items
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS thread_items (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
                )
            """)

            # Index for faster queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_thread_items_thread_id
                ON thread_items(thread_id, created_at)
            """)

            conn.commit()
        finally:
            conn.close()

    def _load_thread_from_db(self, thread_id: str) -> _ThreadState | None:
        """Load a specific thread from database."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Load thread metadata
            cursor.execute("SELECT data FROM threads WHERE id = ?", (thread_id,))
            row = cursor.fetchone()
            if not row:
                return None

            thread_data = json.loads(row[0])
            # Remove 'items' field if present (for backwards compatibility with old data)
            thread_data.pop('items', None)
            thread = ThreadMetadata.model_validate(thread_data)

            # Load items for this thread
            cursor.execute(
                "SELECT data FROM thread_items WHERE thread_id = ? ORDER BY created_at",
                (thread_id,)
            )
            items = []
            for (item_json,) in cursor.fetchall():
                item_data = json.loads(item_json)
                # Reconstruct the appropriate ThreadItem subclass
                item = self._deserialize_thread_item(item_data)
                items.append(item)

            return _ThreadState(thread=thread, items=items)
        finally:
            conn.close()

    def _deserialize_thread_item(self, data: Dict[str, Any]) -> ThreadItem:
        """Deserialize a thread item from JSON data using Pydantic's discriminated union."""
        # Use TypeAdapter with a Union of all ThreadItem types
        # Pydantic will automatically use the 'type' discriminator field
        adapter = TypeAdapter(
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
        # Check cache first
        state = self._threads.get(thread_id)
        if not state:
            # Load from database
            state = self._load_thread_from_db(thread_id)
            if not state:
                raise NotFoundError(f"Thread {thread_id} not found")
            # Cache it
            self._threads[thread_id] = state
        return state.thread.model_copy(deep=True)

    async def save_thread(self, thread: ThreadMetadata, context: dict[str, Any]) -> None:
        state = self._threads.get(thread.id)
        if state:
            state.thread = thread.model_copy(deep=True)
        else:
            self._threads[thread.id] = _ThreadState(
                thread=thread.model_copy(deep=True),
                items=[],
            )

        # Persist to a database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            # Exclude 'items' field when saving to ensure we only save ThreadMetadata
            thread_dict = thread.model_dump(exclude={'items'})
            thread_json = json.dumps(thread_dict, default=str)
            created_at = thread.created_at or datetime.utcnow()
            updated_at = datetime.utcnow()

            cursor.execute("""
                INSERT OR REPLACE INTO threads (id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (thread.id, thread_json, created_at, updated_at))

            conn.commit()
        finally:
            conn.close()

    async def load_threads(
        self,
        limit: int,
        after: str | None,
        order: str,
        context: dict[str, Any],
    ) -> Page[ThreadMetadata]:
        # Load threads directly from database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, data FROM threads")

            threads = []
            for thread_id, thread_json in cursor.fetchall():
                thread_data = json.loads(thread_json)
                # Remove 'items' field if present (for backwards compatibility)
                thread_data.pop('items', None)
                thread = ThreadMetadata.model_validate(thread_data)
                threads.append(thread)
        finally:
            conn.close()

        # Sort threads
        threads.sort(
            key=lambda t: t.created_at or datetime.min,
            reverse=(order == "desc"),
        )

        # Handle pagination
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
        self._threads.pop(thread_id, None)

        # Delete from database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            # Items are deleted automatically via CASCADE
            conn.commit()
        finally:
            conn.close()

    # -- Thread items ----------------------------------------------------
    def _items(self, thread_id: str) -> List[ThreadItem]:
        state = self._threads.get(thread_id)
        if state is None:
            state = _ThreadState(
                thread=ThreadMetadata(id=thread_id, created_at=datetime.utcnow()),
                items=[],
            )
            self._threads[thread_id] = state
        return state.items

    async def load_thread_items(
        self,
        thread_id: str,
        after: str | None,
        limit: int,
        order: str,
        context: dict[str, Any],
    ) -> Page[ThreadItem]:
        items = [item.model_copy(deep=True) for item in self._items(thread_id)]
        items.sort(
            key=lambda item: getattr(item, "created_at", datetime.utcnow()),
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
        self._items(thread_id).append(item.model_copy(deep=True))

        # Persist to a database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            item_json = item.model_dump_json()
            created_at = getattr(item, "created_at", datetime.utcnow())

            cursor.execute("""
                INSERT OR REPLACE INTO thread_items (id, thread_id, data, created_at)
                VALUES (?, ?, ?, ?)
            """, (item.id, thread_id, item_json, created_at))

            conn.commit()
        finally:
            conn.close()

    async def save_item(self, thread_id: str, item: ThreadItem, context: dict[str, Any]) -> None:
        items = self._items(thread_id)
        for idx, existing in enumerate(items):
            if existing.id == item.id:
                items[idx] = item.model_copy(deep=True)
                return
        items.append(item.model_copy(deep=True))

        # Persist to a database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            item_json = item.model_dump_json()
            created_at = getattr(item, "created_at", datetime.utcnow())

            cursor.execute("""
                INSERT OR REPLACE INTO thread_items (id, thread_id, data, created_at)
                VALUES (?, ?, ?, ?)
            """, (item.id, thread_id, item_json, created_at))

            conn.commit()
        finally:
            conn.close()

    async def load_item(self, thread_id: str, item_id: str, context: dict[str, Any]) -> ThreadItem:
        for item in self._items(thread_id):
            if item.id == item_id:
                return item.model_copy(deep=True)
        raise NotFoundError(f"Item {item_id} not found")

    async def delete_thread_item(
        self, thread_id: str, item_id: str, context: dict[str, Any]
    ) -> None:
        items = self._items(thread_id)
        self._threads[thread_id].items = [item for item in items if item.id != item_id]

        # Delete from database
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM thread_items WHERE id = ?", (item_id,))
            conn.commit()
        finally:
            conn.close()

    # -- Files -----------------------------------------------------------
    # These methods are not currently used but required to be compatible with the Store interface.

    async def save_attachment(
        self,
        attachment: Attachment,
        context: dict[str, Any],
    ) -> None:
        raise NotImplementedError(
            "MemoryStore does not persist attachments. Provide a Store implementation "
            "that enforces authentication and authorization before enabling uploads."
        )

    async def load_attachment(
        self,
        attachment_id: str,
        context: dict[str, Any],
    ) -> Attachment:
        raise NotImplementedError(
            "MemoryStore does not load attachments. Provide a Store implementation "
            "that enforces authentication and authorization before enabling uploads."
        )

    async def delete_attachment(self, attachment_id: str, context: dict[str, Any]) -> None:
        raise NotImplementedError(
            "MemoryStore does not delete attachments because they are never stored."
        )
