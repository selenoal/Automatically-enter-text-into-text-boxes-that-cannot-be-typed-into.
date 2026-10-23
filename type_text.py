# -*- coding: utf-8 -*-
"""
模拟真人逐字键入文本，用于禁止粘贴的输入框 / 代码框。

原理：调用 Windows SendInput 发送 Unicode 键盘事件，相当于你在键盘上一个个敲，
没有使用剪贴板，因此不受“禁止粘贴”限制。

代码框会自动补全右括号 / 引号时，用 -a 让程序在输入每个括号后补一个 Delete，
把编辑器自动补上的那个字符删掉，最终内容与文件完全一致。

用法：
    python type_text.py                     # 读取同目录 input.txt，倒计时 5 秒后开始
    python type_text.py -a -v               # 自动补全框专用：抵消自动右括号，并回读审查
    python type_text.py -a -v --fix         # 审查发现不一致时，清空重打一次
    python type_text.py -f 答案.txt -w 8 -d 0.03

倒计时期间请用鼠标点一下目标输入框，让光标停在里面。
录入过程中按 Esc 可随时中止。
"""

import argparse
import ctypes
import difflib
import os
import sys
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_RIGHT = 0x27
VK_A = 0x41
VK_C = 0x43
VK_DELETE = 0x2E

# 会触发编辑器自动补全的字符
AUTOCLOSE_CHARS = set("()[]{}'\"")

CF_UNICODETEXT = 13


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.restype = wintypes.BOOL
kernel32.GlobalLock.argtypes = (wintypes.HANDLE,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HANDLE,)
kernel32.GlobalUnlock.restype = wintypes.BOOL


def _send(*events):
    """events: (wVk, wScan, dwFlags)"""
    arr = (INPUT * len(events))()
    for item, (vk, scan, flags) in zip(arr, events):
        item.type = INPUT_KEYBOARD
        item.u.ki.wVk = vk
        item.u.ki.wScan = scan
        item.u.ki.dwFlags = flags
        item.u.ki.time = 0
        item.u.ki.dwExtraInfo = 0
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def type_unicode_unit(unit):
    _send((0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))


def press_vk(vk):
    _send((vk, 0, 0), (vk, 0, KEYEVENTF_KEYUP))


def press_ctrl(vk):
    _send(
        (VK_CONTROL, 0, 0),
        (vk, 0, 0),
        (vk, 0, KEYEVENTF_KEYUP),
        (VK_CONTROL, 0, KEYEVENTF_KEYUP),
    )


def esc_pressed():
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


def clipboard_text():
    """读取剪贴板纯文本，读不到返回 None。"""
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def type_content(text, delay, autoclose):
    """逐字录入。返回 False 表示被 Esc 中止。"""
    for ch in text:
        if esc_pressed():
            return False
        if ch == "\n":
            press_vk(VK_RETURN)
        elif ch == "\t":
            press_vk(VK_TAB)
        else:
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                type_unicode_unit(units[i] | (units[i + 1] << 8))
            # 编辑器会在我打 ( [ { " ' 后自动补一个右括号，立刻 Delete 掉它
            if autoclose and ch in AUTOCLOSE_CHARS:
                time.sleep(delay * 2)
                press_vk(VK_DELETE)
        time.sleep(delay)
    return True


def read_back(delay):
    """全选复制，读回输入框里的实际内容。"""
    before = clipboard_text()
    press_ctrl(VK_A)
    time.sleep(max(delay * 4, 0.12))
    press_ctrl(VK_C)
    time.sleep(max(delay * 6, 0.25))
    copied = clipboard_text()
    press_vk(VK_RIGHT)  # 取消全选，避免后续误操作覆盖内容
    if copied is None:
        return None
    if copied == before and copied != "":
        return None  # 复制没生效，多半是页面禁止了复制
    return copied


def report_diff(expected, actual):
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    print("审查结果：与源文件%s。" % ("完全一致" if expected == actual else "不一致"))
    if expected == actual:
        return
    if exp_lines == act_lines:
        print("  差异只在行尾空白或结尾换行。")
    else:
        diff = list(
            difflib.unified_diff(exp_lines, act_lines, "源文件", "录入结果", n=0, lineterm="")
        )
        shown = 0
        for line in diff:
            if line.startswith(("---", "+++")):
                continue
            print("  " + line)
            shown += 1
            if shown >= 12:
                print("  ...（差异较多，已省略）")
                break
    print("  可以用 Ctrl+Z 撤销，或加 --fix 让程序清空后重打一次。")


def load_text(args):
    if args.text is not None:
        return args.text
    path = args.file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "input.txt")
    if not os.path.isfile(path):
        sys.exit("找不到内容文件：%s" % path)
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    sys.exit("无法识别文件编码，请用 UTF-8 保存：%s" % path)


def main():
    parser = argparse.ArgumentParser(description="模拟逐字键入，绕过禁止粘贴的输入框")
    parser.add_argument("-f", "--file", help="内容文件路径（默认 input.txt）")
    parser.add_argument("-t", "--text", help="直接指定要录入的文本")
    parser.add_argument("-w", "--wait", type=float, default=5, help="开始前的倒计时秒数（默认 5）")
    parser.add_argument("-d", "--delay", type=float, default=0.015, help="每个字符间隔秒数（默认 0.015）")
    parser.add_argument(
        "-a",
        "--autoclose",
        action="store_true",
        help="输入框会自动补全右括号/引号时使用，逐个抵消自动补上的字符",
    )
    parser.add_argument(
        "-v",
        "--verify",
        action="store_true",
        help="录入后全选复制、读回内容并与源文件逐字比对",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="配合 -v：审查发现不一致时，清空输入框并重打一次",
    )
    args = parser.parse_args()

    text = load_text(args)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        sys.exit("内容为空，没有可录入的文本。")

    print("即将录入 %d 个字符。请立刻点击目标输入框，让光标停在框内……" % len(text))
    for i in range(int(args.wait), 0, -1):
        print("  %d 秒后开始（按 Esc 取消）" % i, end="\r", flush=True)
        for _ in range(10):
            if esc_pressed():
                print("\n已取消。")
                return
            time.sleep(0.1)
    print("\n开始录入…")

    if not type_content(text, args.delay, args.autoclose):
        print("\n已按 Esc 中止录入。")
        return
    print("录入完成，共 %d 个字符。" % len(text))

    if not args.verify:
        return

    time.sleep(0.3)
    actual = read_back(args.delay)
    if actual is None:
        print("审查失败：读不到输入框内容（页面可能禁止复制）。请肉眼核对。")
        return

    report_diff(text, actual)
    if actual == text or not args.fix:
        return

    print("准备清空输入框并重打一次…")
    press_ctrl(VK_A)
    time.sleep(0.2)
    press_vk(VK_DELETE)
    time.sleep(0.3)
    if not type_content(text, args.delay, args.autoclose):
        print("\n已按 Esc 中止录入。")
        return
    time.sleep(0.3)
    actual = read_back(args.delay)
    if actual is None:
        print("重打完成，但无法读回核对，请肉眼确认。")
        return
    report_diff(text, actual)


if __name__ == "__main__":
    main()
