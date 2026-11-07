"""Entry point: python3 -I -B /srv/moa/ops-bot/run.py <command> (see opsbot/main.py).

-I keeps PYTHONPATH, the user's site-packages and the working directory out
of sys.path; this file puts back only its own folder.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from opsbot.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
