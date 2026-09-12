"""POSIX fleet maintenance components; no automatic activation or authority."""
import os

if os.name != "posix":
    raise ImportError("Fleet maintenance adapters require a POSIX host")
