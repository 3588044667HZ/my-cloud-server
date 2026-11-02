"""终端生成配对码（与后台生成等价）。

用法：
  python scripts/gen_pair_code.py            # 生成并打印一个新配对码
  python scripts/gen_pair_code.py --count 3  # 生成 3 个
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.bootstrap import init_core_db  # noqa: E402
from core.auth import generate_pair_code  # noqa: E402
from core.db import close_local  # noqa: E402


def main():
    count = 1
    if '--count' in sys.argv:
        try:
            count = max(1, int(sys.argv[sys.argv.index('--count') + 1]))
        except (IndexError, ValueError):
            count = 1

    init_core_db()
    for i in range(count):
        code = generate_pair_code()
        print(code if count == 1 else f'{i + 1}: {code}')
    close_local()


if __name__ == '__main__':
    main()
