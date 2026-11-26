"""启动翻译主流程并输出仓库定位结果。"""

import argparse
import json

from localization.config import load_config

from .localization_step import localize_repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    # 仓库和语言由同一份配置确定，翻译对仍由定位阶段校验。
    config = load_config()
    dataset = config["dataset"]
    pair = f"{dataset['source_lang']}_to_{dataset['target_lang']}"
    # 定位接口的异常直接传到命令行，失败时由 Python 返回非零退出码。
    result = localize_repository(config, pair, dataset["repository"], config["output_dir"])
    # 命令行将结构化结果输出到终端，程序调用方可直接使用定位函数的返回值。
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
