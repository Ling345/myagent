"""支持 ``python -m agentcode ...`` 的入口。"""

import sys

from agentcode.cli import main

if __name__ == "__main__":
    sys.exit(main())
