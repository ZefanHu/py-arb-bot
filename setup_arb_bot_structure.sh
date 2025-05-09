#!/bin/bash

# 脚本：创建 arb_bot 项目目录结构
# 确保你在 py-arb-bot 仓库的根目录下，并且已经激活了 Conda 环境 arb_env

PROJECT_DIR="arb_bot" # 项目主目录名

# 检查 Conda 环境是否激活 (可选，但推荐)
if [ -z "$CONDA_DEFAULT_ENV" ] || [ "$CONDA_DEFAULT_ENV" != "arb_env" ]; then
    echo "警告：Conda 环境 'arb_env' 似乎未激活。"
    echo "建议先激活环境: conda activate arb_env"
    # read -p "是否继续创建目录结构? (y/n): " choice
    # if [ "$choice" != "y" ]; then
    #     exit 1
    # fi
fi

echo "在 $(pwd) 内创建项目目录: $PROJECT_DIR"

mkdir -p ${PROJECT_DIR}/config
mkdir -p ${PROJECT_DIR}/connectors
mkdir -p ${PROJECT_DIR}/core
mkdir -p ${PROJECT_DIR}/risk_mgmt # 缩短 risk_management
mkdir -p ${PROJECT_DIR}/utils
mkdir -p ${PROJECT_DIR}/state_fsm # 缩短 state_machine
mkdir -p ${PROJECT_DIR}/monitor   # 缩短 monitoring
mkdir -p ${PROJECT_DIR}/okx_sdk   # 用于存放OKX SDK文件
mkdir -p ${PROJECT_DIR}/logs      # 日志目录

touch ${PROJECT_DIR}/main.py
touch ${PROJECT_DIR}/connectors/__init__.py
touch ${PROJECT_DIR}/connectors/okx_conn.py # 缩短 okx_client.py
touch ${PROJECT_DIR}/core/__init__.py
touch ${PROJECT_DIR}/core/market_data.py # 缩短 market_data_handler.py
touch ${PROJECT_DIR}/core/strategy.py    # 缩短 arbitrage_strategy.py
touch ${PROJECT_DIR}/core/executor.py    # 缩短 order_executor.py
touch ${PROJECT_DIR}/risk_mgmt/__init__.py
touch ${PROJECT_DIR}/risk_mgmt/controls.py # 缩短 risk_controls.py
touch ${PROJECT_DIR}/utils/__init__.py
touch ${PROJECT_DIR}/utils/logger.py      # 缩短 logger_setup.py
touch ${PROJECT_DIR}/utils/helpers.py
touch ${PROJECT_DIR}/state_fsm/__init__.py
touch ${PROJECT_DIR}/state_fsm/handler.py  # 缩短 error_handler_fsm.py
touch ${PROJECT_DIR}/monitor/__init__.py
touch ${PROJECT_DIR}/monitor/status.py     # 缩短 basic_monitor.py

# 在项目根目录 (py-arb-bot) 创建 .gitignore 和 README.md
touch .gitignore
touch README.md

# 在 arb_bot 内部创建 requirements.txt 和特定 README
touch ${PROJECT_DIR}/requirements.txt
touch ${PROJECT_DIR}/README.md

echo "项目目录结构和初始文件创建完成。"
echo "项目路径: $(pwd)/${PROJECT_DIR}"
echo "下一步是填充配置文件、依赖文件，并将SDK文件放入 ${PROJECT_DIR}/okx_sdk/"

