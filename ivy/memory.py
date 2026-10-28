import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

MEMORY_PATH = Path(__file__).resolve().parent.parent / "memory.json"
_lock = threading.Lock()

class MemoryStore:
    def __init__(self, path: Path = MEMORY_PATH):
        self.path = path
        self._data = {"memories": []}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
                if "memories" not in self._data:
                    self._data = {"memories": []}
            except Exception:
                self._data = {"memories": []}
        else:
            self._data = {"memories": []}

    def _save(self):
        with _lock:
            self.path.write_text(json.dumps(self._data, indent=2))

    def add(self, text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        text = text.strip()
        if not text:
            return {"error": "Empty memory."}
        
        # Deduplication check
        for m in self._data["memories"]:
            if m["text"].lower() == text.lower():
                m["updated"] = datetime.now().isoformat(timespec="seconds")
                self._save()
                return m
        
        entry = {
            "id": "m_" + uuid.uuid4().hex[:8],
            "text": text,
            "tags": tags or [],
            "created": datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        self._data["memories"].append(entry)
        self._save()
        return entry

    def all(self) -> List[Dict[str, Any]]:
        return list(self._data["memories"])

    def recent(self, n: int = 20) -> List[Dict[str, Any]]:
        return self._data["memories"][-n:]

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        query_terms = set(re.findall(r"\w+", query.lower()))
        if not query_terms:
            return []
        
        scored = []
        for m in self._data["memories"]:
            haystack = (m["text"] + " " + " ".join(m.get("tags", []))).lower()
            hits = sum(1 for t in query_terms if t in haystack)
            if hits:
                scored.append((hits, m))
        
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:limit]]

    def forget(self, memory_id: str) -> bool:
        before = len(self._data["memories"])
        self._data["memories"] = [
            m for m in self._data["memories"] if m["id"] != memory_id
        ]
        if len(self._data["memories"]) < before:
            self._save()
            return True
        return False

    def clear(self) -> int:
        """Delete every memory. Returns how many were removed."""
        count = len(self._data["memories"])
        self._data["memories"] = []
        self._save()
        return count

_store = None
def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
