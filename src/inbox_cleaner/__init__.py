"""Inbox Cleaner — safely fetch, index, group, and clean a Gmail inbox.

The package is organized around a strict safety boundary: the read/analysis
path (auth, store, sync, classify) only ever uses the read-only Gmail scope.
The write path (executor) is a separate component, added in a later phase.
"""

__version__ = "0.1.0"
