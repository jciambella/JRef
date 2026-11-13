"""JRef — Journal Reference Manager: local, file-first academic reference manager for LaTeX workflows.

The import package keeps its historical name ``bibmanager``; the product name is ``APP_NAME``.
"""

__version__ = "0.7.0"

APP_NAME = "JRef"
APP_SUBTITLE = "Journal Reference Manager"
APP_AUTHOR = "JC"
APP_AUTHOR_EMAIL = "j.c@outlook.com"
# Earlier product names, newest first: settings folders, QSettings scopes and installations to take over.
LEGACY_APP_NAMES = ("JBibManager", "BibManager")  # 0.5.x, <= 0.4
LEGACY_APP_NAME = LEGACY_APP_NAMES[-1]  # kept for callers written against 0.5
