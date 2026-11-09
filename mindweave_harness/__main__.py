"""支持通过 ``python -m mindweave_harness`` 启动 MindWeave Harness。"""

import argparse
from pathlib import Path

from mindweave_harness.adapters.cli.main import main
from mindweave_harness.config import ConfigError


def _parse_arguments() -> argparse.Namespace:
    """解析 MindWeave Harness 命令行启动参数。"""
    parser = argparse.ArgumentParser(description="启动 MindWeave Harness 交互会话")
    parser.add_argument(
        "--config",
        type=Path,
        help="指定 TOML 运行配置文件，默认读取工作区下的 mindweave_harness.toml",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_arguments()
    try:
        main(arguments.config)
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}") from error
