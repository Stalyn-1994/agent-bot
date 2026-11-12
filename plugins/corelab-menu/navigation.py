"""Persist only screen identifiers, never messages, examination lists or patient details."""
import json
import sqlite3
import time
from contextlib import closing
from .screens import SCREENS

class Navigation:
    def __init__(self, path):
        self.path=path

    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(self.path,timeout=3)
        conn.execute("CREATE TABLE IF NOT EXISTS screens (bot INTEGER, chat INTEGER, thread INTEGER, history TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(bot,chat,thread))")
        return conn

    def read(self, bot, chat, thread):
        with closing(self.connect()) as conn:
            row=conn.execute("SELECT history,updated FROM screens WHERE bot=? AND chat=? AND thread=?",
                (bot,chat,thread or 0)).fetchone()
        if not row or time.time()-row[1]>7200:
            return ["principal"]
        history=json.loads(row[0])
        if not isinstance(history,list) or not history or not all(s in SCREENS for s in history):
            raise ValueError("Estado de navegacion invalido")
        return history[-12:]

    def save(self, bot, chat, thread, history):
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO screens VALUES(?,?,?,?,?)",
                    (bot,chat,thread or 0,json.dumps(history[-12:]),time.time()))
                conn.execute("DELETE FROM screens WHERE updated<?",(time.time()-7200,))
