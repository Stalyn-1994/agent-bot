"""RAG Memory Plugin for Hermes Agent.

Production-grade Retrieval-Augmented Generation memory system with:
- Hybrid TF-IDF + Neural search (sqlite-vec + sentence-transformers)
- Auto-capture hooks (pre_llm_call, post_llm_call)
- Peer/Session model with namespace isolation
- Query caching and performance optimization
- Zero-configuration setup with graceful fallback

Plugin Entry Point
------------------
Install via pip and Hermes auto-discovers via ``hermes_agent.plugins`` entry point::

    pip install rag-memory-plugin
    pip install rag-memory-plugin[neural]  # With sentence-transformers

Configuration
-------------
Plugin config lives under ``rag_memory:`` in Hermes ``config.yaml``::

    plugins:
      rag_memory:
        enabled: true
        mode: hybrid              # tfidf | neural | hybrid
        auto_capture: true
        cache_enabled: true
        cache_ttl: 300           # 5 minutes
        max_results: 10

Migration
---------
Migrate existing data from ~/rag-system::

    rag-memory migrate-from-legacy

Example
-------
>>> from rag_memory import RAGCore
>>> rag = RAGCore()
>>> rag.add_document("Hermes is an AI agent", namespace="test")
>>> results = rag.search("AI agent", namespace="test", limit=5)
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "register",
    "plugin_name",
    "plugin_version",
]

# Plugin metadata (required by Hermes plugin system)
plugin_name = "rag-memory"
plugin_version = "1.0.0"

# --------------------------------------------------------------------------- #
# Import core after version check (allows soft imports)
# --------------------------------------------------------------------------- #

try:
    from rag_memory.core import RAGCore
    from rag_memory.plugin import register
except ImportError:
    # Graceful degradation during development/testing
    RAGCore = None  # type: ignore
    register = None  # type: ignore
