# -*- coding: utf-8 -*-
import os
import re
import subprocess
import sys

'''
原始镜像备份
const PLAYWRIGHT_CDN_MIRRORS = ['https://playwright.azureedge.net', 'https://playwright-akamai.azureedge.net', 'https://playwright-verizon.azureedge.net'];
'''

def install_playwright():
    """
    安装 Playwright 库。
    使用清华大学的 PyPI 镜像源进行安装。
    """
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "playwright", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
    except subprocess.CalledProcessError as e:
        handle_error(f"安装过程中出现错误: {e}")
    except Exception as e:
        handle_error(f"发生了一个意外错误: {e}")

def install_chromium():
    """
    安装 Playwright 的 Chromium 浏览器。
    """
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    except subprocess.CalledProcessError as e:
        handle_error(f"安装过程中出现错误: {e}")
    except Exception as e:
        handle_error(f"发生了一个意外错误: {e}")

def handle_error(message):
    """
    处理错误并打印错误信息。
    可以在这里添加其他错误处理逻辑，比如发送通知、记录日志等。

    :param message: 错误信息
    """
    print(message)

def find_specific_directory(start_path, target_directory):
    """
    从起始路径递归查找特定名称的目录。

    :param start_path: 起始查找的目录路径
    :param target_directory: 要查找的目录名称
    :return: 符合条件的目录路径列表
    """
    matched_directories = []

    for root, dirs, files in os.walk(start_path):
        for dir_name in dirs:
            full_path = os.path.join(root, dir_name)
            if full_path.endswith(target_directory):
                matched_directories.append(full_path)

    return matched_directories

def read_and_modify_file():
    """
    读取并修改 Playwright 的 CDN 镜像配置文件。
    """
    # 获取当前Python可执行文件所在的目录
    directory = os.path.dirname(sys.executable)
    # 获取上一级目录
    parent_directory = os.path.dirname(directory)

    target_directory = r'playwright\driver\package\lib\server\registry'
    matched_dirs = find_specific_directory(parent_directory, target_directory)

    if not matched_dirs:
        print(f"未找到目录: {target_directory}")
        return

    if len(matched_dirs) > 1:
        print(f"找到多个匹配的目录: {matched_dirs}")
        return

    directory = matched_dirs[0]
    # 定义 index.js 文件的路径
    index_js_path = os.path.join(directory, 'index.js')

    # 检查文件是否存在
    if not os.path.exists(index_js_path):
        print(f"未找到文件: {index_js_path}")
        return

    try:
        # 打开并读取文件内容
        with open(index_js_path, 'r', encoding='utf-8') as file:
            content = file.read()

        # 定义要匹配的模式
        pattern = r"const PLAYWRIGHT_CDN_MIRRORS = \['https://[^']+?(?:', 'https://[^']+?)*'\];"
        # 使用正则表达式查找匹配的字符串
        match = re.search(pattern, content)

        # 如果未找到匹配的字符串，直接返回
        if not match:
            print("未找到匹配的字符串")
            return

        # 打印找到的原始镜像
        print("找到原始镜像：")
        print(match.group())

        # 替换匹配到的字符串
        new_content = re.sub(pattern,
                             "const PLAYWRIGHT_CDN_MIRRORS = ['https://registry.npmmirror.com/-/binary/playwright'];",
                             content)
        # 打印替换后的镜像
        print("替换后的镜像：")
        match2 = re.search(pattern, new_content)
        print(match2.group())

        # 将修改后的内容写回文件
        with open(index_js_path, 'w', encoding='utf-8') as file:
            file.write(new_content)

    except Exception as e:
        handle_error(f"读取文件时出错: {e}")

if __name__ == "__main__":
    """
    主函数入口。
    依次执行安装 Playwright、读取并修改配置文件、安装 Chromium 的操作。
    """
    # # 安装 Playwright
    # install_playwright()
    # 读取并修改 Playwright 的 CDN 镜像配置文件
    read_and_modify_file()
    # 安装 Chromium
    install_chromium()
