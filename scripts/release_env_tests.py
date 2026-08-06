"""在尽量接近发布构建机的条件下跑全量测试。

构建机和开发机有两处差异，各自都放过过一个真 bug：

1. 解释器是 Python 3.11，开发机通常更新。依赖对象生命周期的写法（典型是拿
   id() 当身份用）在不同版本的内存分配器下表现可以完全相反——本地全绿、
   构建机当场失败。解释器由 run_release_env_tests.sh 负责挑，这里不管。
2. 构建机上没有装 Microsoft Excel。任何真去拉起 Excel 的测试在本地有 Office
   的机器上悄悄通过，到了构建机必然 ApplicationNotFoundError。所以这里把
   create_excel_app 换成直接报错——测试要么自己把它挡掉，要么就是不该依赖它。

必须用 if __name__ == "__main__" 守住：test_settings_persistence 会 spawn 子进程，
子进程会重新导入 __main__，没有守卫就会递归地再跑一遍整个测试套件。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _block_real_excel() -> None:
    import core.excel_automation as excel_automation
    import core.task_runner as task_runner

    def _refuse(*_args, **_kwargs):
        raise RuntimeError(
            "测试尝试拉起本机 Microsoft Excel。发布构建机上没有装 Office，"
            "这里必然失败——请在用例里把 create_excel_app 挡掉。"
        )

    # 两处都要换：task_runner 是 from ... import 进来的，改模块源头不影响它已绑定的名字。
    excel_automation.create_excel_app = _refuse
    task_runner.create_excel_app = _refuse


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))
    _block_real_excel()
    suite = unittest.TestLoader().discover(str(REPO_ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
