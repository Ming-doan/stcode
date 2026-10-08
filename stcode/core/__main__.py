"""`python -m stcode.core` — the daemon, same as `stcode-daemon`."""

import sys

from stcode.core.daemon.main import main

sys.exit(main())
