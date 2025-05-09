#!/bin/bash

# 脚本：安装 Miniconda 并创建 Conda 环境 arb_env

# 1. 下载 Miniconda 安装脚本
echo "正在下载 Miniconda 安装脚本..."
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh

# 2. 安装 Miniconda
echo "正在安装 Miniconda..."
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm -rf ~/miniconda3/miniconda.sh

# 3. 初始化 Conda (重要：这会修改你的 .bashrc 或 .zshrc)
echo "正在初始化 Conda..."
# 检查默认 shell 并相应地初始化
if [ -n "$BASH_VERSION" ]; then
    ~/miniconda3/bin/conda init bash
elif [ -n "$ZSH_VERSION" ]; then
    ~/miniconda3/bin/conda init zsh
else
    echo "无法检测到 Bash 或 Zsh。请手动运行 '~/miniconda3/bin/conda init <your_shell_name>'。"
    echo "然后关闭并重新打开终端，或 source 相应的 rc 文件。"
fi

echo "Miniconda 安装完成。"
echo "请关闭并重新打开你的终端，或者运行 'source ~/.bashrc' (如果使用bash) 或 'source ~/.zshrc' (如果使用zsh) 来使 Conda 命令生效。"
echo "然后，你可以运行以下命令来创建和激活新的 Conda 环境："
echo ""
echo "conda create --name arb_env python=3.10 -y"
echo "conda activate arb_env"
echo ""
echo "创建环境后，你可以通过 'conda env list' 查看所有环境，并通过 'conda deactivate' 退出当前环境。"

