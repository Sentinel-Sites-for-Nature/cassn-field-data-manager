"""Load guards before any application import in test Python children."""
import os
import sys

try:
    from test_support.isolation import start_child
    start_child()
except BaseException:
    # Python normally prints and ignores sitecustomize errors. Fail closed.
    os.write(2, b'Test isolation startup failed; child process stopped.\n')
    os._exit(97)
