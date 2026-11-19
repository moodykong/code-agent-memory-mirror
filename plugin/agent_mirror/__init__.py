"""agent-mirror: give coding agents the same context on every machine.

The program links a user-owned git "store" (instructions, skills, memory,
project settings) into each agent's home folder, keeps it in sync with git,
moves bulk files through a "box", and renders cloud environment settings.

Python 3.8+ standard library only.
"""

import os

__version__ = "0.5.9"

# plugin/ (the folder that holds bin/, templates/ and this package)
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(os.path.abspath(__file__))))
TOOL_REPO = "https://github.com/moodykong/code-agent-memory-mirror"
DEFAULT_REF = "main"
STORE_SCHEMA = 1
