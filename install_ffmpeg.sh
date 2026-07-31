#!/bin/bash
# install_ffmpeg.sh - FFmpeg 安装脚本

set -e

echo "========================================"
echo "FFmpeg 安装脚本"
echo "========================================"
echo

# 检查是否已安装
if command -v ffmpeg &> /dev/null; then
    echo "✅ ffmpeg 已安装"
    ffmpeg -version | head -1
    exit 0
fi

echo "ffmpeg 未找到，开始安装..."
echo

# 检查 Homebrew
if command -v brew &> /dev/null; then
    echo "✓ 使用 Homebrew 安装 ffmpeg..."
    brew install ffmpeg
    echo "✅ ffmpeg 安装完成"
    ffmpeg -version | head -1
    exit 0
fi

# Homebrew 未安装
echo "❌ Homebrew 未安装"
echo
echo "请选择安装方法："
echo
echo "方法 1 - 先安装 Homebrew（推荐）："
echo "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
echo "  brew install ffmpeg"
echo
echo "方法 2 - 手动下载 ffmpeg："
echo "  1. 访问: https://evermeet.cx/ffmpeg/"
echo "  2. 下载 ffmpeg.zip"
echo "  3. 运行: unzip ffmpeg.zip && sudo mv ffmpeg /usr/local/bin/"
echo
echo "方法 3 - 使用以下命令直接下载安装（需要管理员密码）："
echo "  curl -L -o /tmp/ffmpeg.zip https://evermeet.cx/ffmpeg/ffmpeg-7.1.zip"
echo "  unzip /tmp/ffmpeg.zip -d /tmp"
echo "  sudo mv /tmp/ffmpeg /usr/local/bin/"
echo "  sudo chmod +x /usr/local/bin/ffmpeg"
echo
