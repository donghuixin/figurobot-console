#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figurobot 本地记忆库（SQLite + FTS5）
=====================================
存三类信息，跨会话持久化：
  1. 对话历史 conversation —— 最近 N 轮上下文
  2. 用户画像 profile     —— 长期稳定的用户信息（姓名、偏好等）
  3. 语义记忆 facts       —— 值得记住的事实（供跨会话检索）

用法：
    from memory_store import MemoryStore
    m = MemoryStore("/userdata/data/robot_memory/robot.db")
    m.add_turn("user", "我叫小明")
    m.add_turn("assistant", "你好小明")
    facts = m.retrieve_facts("小明")
    m.set_profile("user_name", "小明")
"""
import os
import sqlite3
import threading
from datetime import datetime


class MemoryStore:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "robot_memory.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS conversation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts  TEXT DEFAULT (datetime('now','localtime')),
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts   TEXT DEFAULT (datetime('now','localtime')),
                fact TEXT NOT NULL UNIQUE
            );
            """)

    # ---- 对话历史 ----
    def add_turn(self, role, content):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO conversation(role, content) VALUES(?, ?)",
                      (role, content))

    def recent_history(self, limit=20):
        """返回最近 N 轮对话，旧→新，形如 [(role, content), ...]"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT role, content FROM conversation "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [(r["role"], r["content"]) for r in reversed(rows)]

    # ---- 用户画像 ----
    def set_profile(self, key, value):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO profile(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, str(value)))

    def get_profile(self, key):
        with self._conn() as c:
            r = c.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def all_profile(self):
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM profile").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- 语义记忆（facts） ----
    def add_fact(self, fact):
        fact = fact.strip()
        if not fact:
            return
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO facts(fact) VALUES(?)", (fact,))

    def retrieve_facts(self, query, limit=5):
        """中文关键词检索相关记忆（LIKE 子串匹配，适合个人机器人的小规模事实库）。"""
        query = (query or "").strip()
        with self._conn() as c:
            if query:
                # 对查询里的每个关键词分别做子串匹配，取并集
                rows = []
                seen = set()
                for term in query.split():
                    for r in c.execute(
                        "SELECT fact FROM facts WHERE fact LIKE ? ORDER BY id DESC LIMIT ?",
                        (f"%{term}%", limit)):
                        if r["fact"] not in seen:
                            seen.add(r["fact"])
                            rows.append(r["fact"])
                return rows[:limit]
            rows = c.execute(
                "SELECT fact FROM facts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [r["fact"] for r in rows]

    def all_facts(self):
        with self._conn() as c:
            rows = c.execute("SELECT fact FROM facts ORDER BY id DESC").fetchall()
        return [r["fact"] for r in rows]

    def clear_facts(self):
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM facts")


if __name__ == "__main__":
    # 自测
    m = MemoryStore("/tmp/robot_memory_test.db")
    m.add_turn("user", "我叫小明，喜欢跳舞")
    m.add_turn("assistant", "你好小明")
    m.set_profile("user_name", "小明")
    m.add_fact("用户叫小明")
    m.add_fact("用户喜欢跳舞")
    print("画像:", m.all_profile())
    print("最近对话:", m.recent_history())
    print("检索'小明':", m.retrieve_facts("小明"))
    print("检索'跳舞':", m.retrieve_facts("跳舞"))
    print("自测通过")
