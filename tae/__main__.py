"""python -m tae 快捷入口（等价 python -m tae.cli.main）。"""

from .cli.main import main

if __name__ == "__main__":
    import sys

    sys.exit(main())
