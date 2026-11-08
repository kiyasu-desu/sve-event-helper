"""抓取指定窗口的截图，用于判断页面是否真的渲染了内容。

为什么不看窗口标题：Taro/H5 单页应用的 document.title 由路由设置，
标题为 URL 并不等于页面空白——需要看像素。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.join(PROJECT_ROOT, "capture", "window_shot.png")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "login-betasystem"

found = []


def cb(hwnd, _l):
    if not user32.IsWindowVisible(hwnd):
        return True
    n = user32.GetWindowTextLengthW(hwnd)
    if n == 0:
        return True
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    if KEYWORD.lower() in buf.value.lower():
        found.append((hwnd, buf.value))
    return True


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
user32.EnumWindows(WNDENUMPROC(cb), 0)

if not found:
    print("未找到标题含 %r 的窗口" % KEYWORD)
    sys.exit(1)

hwnd, title = found[0]
print("窗口: %r (hwnd=%s)" % (title, hwnd))

# 取窗口客户区尺寸
rect = wt.RECT()
user32.GetClientRect(hwnd, ctypes.byref(rect))
w, h = rect.right - rect.left, rect.bottom - rect.top
print("客户区: %dx%d" % (w, h))

# 截图
hdc = user32.GetDC(hwnd)
memdc = gdi32.CreateCompatibleDC(hdc)
bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
gdi32.SelectObject(memdc, bmp)
user32.PrintWindow(hwnd, memdc, 0)  # PW_CLIENTONLY=1 会只抓客户区；0 抓整窗
gdi32.SelectObject(memdc, bmp)

# 取像素统计（判断是否纯白/纯黑 = 空白）
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

bi = BITMAPINFOHEADER()
bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
bi.biWidth = w
bi.biHeight = -h  # top-down
bi.biPlanes = 1
bi.biBitCount = 32
bi.biCompression = 0

buf = ctypes.create_string_buffer(w * h * 4)
got = gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
data = buf.raw
# 统计颜色分布
from collections import Counter
c = Counter()
for i in range(0, len(data), 4 * 97):  # 采样
    b, g, r = data[i], data[i + 1], data[i + 2]
    c[(r // 32 * 32, g // 32 * 32, b // 32 * 32)] += 1
top = c.most_common(5)
print("颜色分布（采样，RGB 量化到 32）:")
for col, n in top:
    print("   %s  %d" % (col, n))

white = sum(n for col, n in c.items() if col[0] >= 224 and col[1] >= 224 and col[2] >= 224)
total = sum(c.values())
print("近白占比: %.1f%%" % (100.0 * white / max(1, total)))

# 保存 PNG（用 PIL，若可用）
try:
    from PIL import Image
    img = Image.frombuffer("RGBA", (w, h), data, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(OUT)
    print("截图已保存: %s" % OUT)
except Exception as e:
    print("保存 PNG 失败（缺 Pillow?）: %s" % e)

gdi32.DeleteObject(bmp)
gdi32.DeleteDC(memdc)
user32.ReleaseDC(hwnd, hdc)
