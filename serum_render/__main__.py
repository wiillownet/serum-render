"""`python -m serum_render` — same entry point as the `serum-render` script.

Exists because the console script is not reliably locatable from a GUI:
there is no useful PATH when an app is launched from Finder, and Windows
venvs place it in `Scripts/` with a `.exe` suffix. On Windows that stub
also spawns a child interpreter which can survive a kill
(pypa/distlib#175); going through `-m` removes that process level.
"""
from .cli import app

app()
