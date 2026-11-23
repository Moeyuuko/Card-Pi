"""Card Pi's 320×170 launcher, workspace picker and conversation screen."""
from collections import deque
from contextlib import ExitStack
from datetime import datetime
from math import ceil
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import signal
from threading import Event, Thread
from time import monotonic

from PIL import Image, ImageDraw, ImageFont

from .device_screen import (
    BACKSPACE, DOWN, ENTER, ESC, KEY_F, KEY_X, LEFT, RIGHT, TAB, UP, KeyPress, keyboard_fds,
    open_display, read_keys,
)
from .i18n import installation_message, translate
from .ime import Pinyin, PinyinOptions
from .markdown import message_blocks, wrap_runs
from .native import NativePi, TerminalProcess
from .pi import (
    CONFIG, HOME, RPC, detect, install, load_settings, message_text,
    purge_session, restore_session, save_settings, session_list, trash_session, validate_pi,
)
from .version import VERSION

TICKER_PERIOD = 4
HEADER_TICKER_PERIOD = 8
TICKER_SLIDE = 0.35

PAGE_UP, PAGE_DOWN = 104, 109
BG, PANEL, TEXT, DIM, ACCENT = "#10151c", "#1c2936", "#edf2f7", "#a3b4c5", "#85e6b9"
WIDTH, HEIGHT = 320, 170
CHAT_REPEAT_KEYS = {UP, DOWN, PAGE_UP}
LIST_STEPS = {UP: -1, DOWN: 1, KEY_F: -1, KEY_X: 1, PAGE_UP: -6, PAGE_DOWN: 6}
LIST_PAGES = {
    "home", "workspace", "browse", "sessions", "trash", "delete_session", "purge_session", "chat_menu",
    "models", "settings", "pi_settings", "language", "display_settings", "display_choice", "input_settings",
    "input_choice", "dialog_select", "dialog_confirm",
}
TEXT_FIELDS = {"path", "binary", "rename", "dialog_input", "dialog_editor"}
PATH_FIELDS = {"path", "binary"}
PATH_PUNCTUATION = set("/\\.~_-")
TERMINAL_SIZES = (8, 9, 11)
DEFAULT_TERMINAL_SIZE = 9
CTRL_Q = 16
ON_OFF = ((True, "开启"), (False, "关闭"))
DISPLAY_SETTINGS = {
    "render_markdown": ("Markdown 渲染", True, ON_OFF),
    "chat_font_size": ("聊天字号", 13, tuple((size, f"{size} px") for size in (11, 13, 15))),
    "chat_line_spacing": ("聊天行距", "compact", (("compact", "紧凑"), ("relaxed", "宽松"))),
    "terminal_size": ("终端字号", DEFAULT_TERMINAL_SIZE, tuple((size, f"{size} px") for size in TERMINAL_SIZES)),
}
INPUT_SETTINGS = {
    "startup": ("默认输入", ((False, "英文"), (True, "中文拼音"))),
    "script": ("输出字形", (("simplified", "简体"), ("traditional", "繁體"))),
    "scheme": ("拼音方案", (("full", "全拼"), ("double", "自然码双拼"))),
    "candidates": ("每页候选", ((3, "3"), (4, "4"), (5, "5"))),
    "punctuation": ("标点样式", (("ascii", "英文标点"), ("chinese", "中文标点"))),
    "width": ("字符宽度", (("half", "半角"), ("full", "全角"))),
    "fuzzy": ("模糊拼音", ON_OFF),
    "completion": ("拼音补全", ON_OFF),
    "sentences": ("整句候选", ON_OFF),
    "learning": ("词频学习", ON_OFF),
}



def mono_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


class Screen:
    def __init__(self):
        paths = (
            "/usr/share/APPLaunch/share/font/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
        )
        for path in paths:
            try:
                self.font = ImageFont.truetype(path, 13, index=2)
                self.small = ImageFont.truetype(path, 12, index=2)
                break
            except OSError:
                continue
        else:
            self.font = self.small = ImageFont.load_default()
        self.terminal_fonts = {}
        for size in TERMINAL_SIZES:
            font = mono_font(size)
            cjk = self.small.font_variant(size=size) if isinstance(self.small, ImageFont.FreeTypeFont) else font
            self.terminal_fonts[size] = font, cjk, ceil(font.getlength("M")), size + 3
        self.chat_fonts = {}
        for size, _ in DISPLAY_SETTINGS["chat_font_size"][2]:
            body = self.font.font_variant(size=size)
            try:
                bold = ImageFont.truetype(str(body.path).replace("Regular.ttc", "Bold.ttc"), size, index=2)
            except (AttributeError, OSError, ValueError):
                bold = body
            self.chat_fonts[size] = body, bold, mono_font(size)

    def message_lines(self, role, text, size, markdown):
        body, bold, mono = self.chat_fonts[size]

        def font_for(style, text):
            if style in {"code", "code_block"} and text.isascii():
                return mono
            return bold if style in {"heading", "strong"} else body

        for index, block in enumerate(message_blocks(text, markdown and role in {"user", "assistant"})):
            if index == 0 and role == "user":
                block = [("> ", "body"), *block]
            for line in wrap_runs(block, WIDTH - 16, font_for):
                yield role, line

    def terminal_dimensions(self, size):
        _, _, cell_width, cell_height = self.terminal_fonts[size]
        return (WIDTH - 10) // cell_width, 149 // cell_height

    def clip(self, text, width, font=None, tail=False):
        font = font or self.font
        text = " ".join(text.splitlines())
        if font.getlength(text) <= width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            part = text[-middle:] if tail else text[:middle]
            if font.getlength(part + "…") > width:
                hi = middle - 1
            else:
                lo = middle
        return ("…" + (text[-lo:] if lo else "")) if tail else text[:lo] + "…"

    def lines(self, text, width=300):
        for paragraph in text.expandtabs(4).splitlines() or [""]:
            position = 0
            while len(paragraph) - position > 300 or self.font.getlength(paragraph[position:]) > width:
                section = paragraph[position:position + 300]
                fit = self.clip(section, width) if self.font.getlength(section) > width else section
                end = max(1, len(fit) - (fit != section))
                if " " in fit and not fit.startswith(" "):
                    boundary = fit.rfind(" ")
                    if boundary > end // 2:
                        end = boundary
                yield paragraph[position:position + end]
                position += end
            yield paragraph[position:]

    def ticker(self, image, box, pages, now, *, right=False, background=PANEL, period=TICKER_PERIOD):
        x, y, width, height = box
        index, elapsed = divmod(now, period)
        index = int(index) % len(pages)
        offset = round(height * max(0, elapsed - period + TICKER_SLIDE) / TICKER_SLIDE)
        if len(pages) == 1:
            offset = 0
        strip = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(strip)
        for step in range(2 if offset and len(pages) > 1 else 1):
            label = self.clip(pages[(index + step) % len(pages)], width, self.small)
            draw.text((width - 1 if right else 0, height // 2 + step * height - offset),
                      label, font=self.small, fill=DIM, anchor="rm" if right else "lm")
        image.paste(strip, (x, y))

    def render(self, app):
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        if app.page == "history":
            draw.text((313, 8), app.tr("Esc 返回"), font=self.small, fill=DIM, anchor="rm")
            self.transcript(draw, app.history_lines, y=16, row_height=app.chat_row_height(),
                            visible=app.history_rows(), scroll=app.scroll)
            return image
        if app.page == "native":
            self.terminal(draw, app.native, app.terminal_size(), app.tr("Pi 原生命令   Ctrl+Q 返回 Card Pi"))
            return image
        if app.page == "core_update":
            self.terminal(draw, app.updater, app.terminal_size(), app.update_status, show_cursor=not app.update_finished)
            return image
        draw.rectangle((0, 0, WIDTH, 24), fill=PANEL)
        version = "Card Pi " + VERSION.replace("~", " ")
        now = app.ticker_time
        clock = datetime.now().strftime("%m-%d %H:%M")
        header = [version, f"{clock}  {app.battery_label()}"]
        width = ceil(max(self.small.getlength(label) for label in header)) + 2
        self.ticker(image, (314 - width, 0, width, 24), header, now, right=True, period=HEADER_TICKER_PERIOD)
        title = self.clip(app.title(), 296 - width)
        draw.text((7, 12), title, fill=ACCENT, font=self.font, anchor="lm")
        if app.page == "chat":
            self.chat(image, draw, app)
        else:
            self.menu(draw, app)
        return image

    def terminal(self, draw, terminal, size, label, *, show_cursor=True):
        screen = terminal.screen
        font, cjk, cell_width, cell_height = self.terminal_fonts[size]
        colors = {"black": "#10151c", "red": "#ff8c89", "green": ACCENT,
                  "yellow": "#eed18c", "blue": "#8bb9f8", "magenta": "#d6adf5",
                  "cyan": "#8cddd9", "white": TEXT, "brown": "#eed18c"}
        for row in range(screen.lines):
            cells = screen.buffer[row]
            y = row * cell_height
            for column in range(screen.columns):
                cell = cells[column]
                if cell.data in {"", " "} and not cell.reverse and cell.bg == "default":
                    continue
                x = 5 + cell_width * column
                fg = colors.get(cell.fg, "#" + cell.fg if len(cell.fg) == 6 else TEXT)
                bg = colors.get(cell.bg, "#" + cell.bg if len(cell.bg) == 6 else BG)
                if cell.reverse:
                    fg, bg = bg, fg
                if bg != BG:
                    draw.rectangle((x, y, x + cell_width - 1, y + cell_height - 1), fill=bg)
                if cell.data not in {"", " "}:
                    draw.text((x, y + 1), cell.data, fill=fg,
                              font=font if cell.data.isascii() else cjk)
        cursor = screen.cursor
        if show_cursor and not cursor.hidden and 0 <= cursor.y < screen.lines and 0 <= cursor.x < screen.columns:
            x, y = 5 + cursor.x * cell_width, (cursor.y + 1) * cell_height - 2
            draw.line((x, y, x + cell_width - 2, y), fill=ACCENT)
        self.footer(draw, label)

    def footer(self, draw, label):
        draw.rectangle((0, 149, WIDTH, HEIGHT), fill=PANEL)
        draw.text((7, 159), self.clip(label, 306, self.small), font=self.small,
                  anchor="lm", fill=DIM)

    def menu(self, draw, app):
        if app.page == "install":
            draw.text((9, 41), app.tr("安装官方 Pi · 将联网下载"), font=self.font, fill=TEXT)
            draw.text((9, 63), app.tr("pi.dev · 无兼容 Node 时下载 ARM64 发布包"),
                      font=self.small, fill=DIM)
            for i, line in enumerate(list(self.lines(app.notice))[:3]):
                draw.text((9, 88 + i * 16), line, font=self.font, fill=TEXT)
            if not app.working:
                draw.text((9, 137), app.tr("OK 安装 · Esc 返回"), font=self.small, fill=DIM)
                draw.rectangle((0, 149, WIDTH, HEIGHT), fill=PANEL)
                for x, label in ((161, app.tr("6 安装")), (217, app.tr("7 指定 Pi")), (273, app.tr("8 重查"))):
                    draw.text((x, 157), label, font=self.small, anchor="mm", fill=TEXT)
                    draw.line((x, 166, x, 169), fill=ACCENT)
                return
            label = app.tr("Esc 取消安装   长按 Esc 退出")
        elif app.page == "check":
            draw.text((9, 49), app.tr("正在检查 Pi…"), font=self.font, fill=TEXT)
            label = app.tr("Esc 返回")
        elif app.page in TEXT_FIELDS:
            self.field_input(draw, app)
            return
        elif app.page == "models" and not app.rows:
            line = app.notice or app.tr("正在读取可用模型…")
            for index, text in enumerate(list(self.lines(line))[:5]):
                draw.text((8, 35 + 17 * index), text, font=self.font, fill=TEXT)
            label = app.tr("Esc 返回")
        elif app.page == "detail":
            lines = app.detail_lines(self)
            for i, line in enumerate(lines[app.offset:app.offset + 7]):
                draw.text((8, 29 + i * 16), line, font=self.font, fill=TEXT)
            label = app.tr("↑↓ 滚动   Fn+L/M 翻页   Esc 返回")
        else:
            rows = app.rows
            row_height, visible = (17, 7) if app.page == "home" else (19, 6)
            start = max(0, app.selected - visible + 1)
            for i, (label, _) in enumerate(rows[start:start + visible], start):
                y = 28 + (i - start) * row_height
                if i == app.selected:
                    draw.rectangle((4, y, 315, y + row_height - 1), fill=PANEL)
                draw.text((9, y + row_height // 2), self.clip(label, 301), font=self.font, anchor="lm",
                          fill=ACCENT if i == app.selected else TEXT)
            if app.notice and not rows:
                draw.text((9, 40), self.clip(app.notice, 300), font=self.font, fill=DIM)
            label = app.tr("↑↓ 选择   OK 确认   Esc 返回")
            if app.page == "workspace":
                label = app.tr("↑↓ 选择   OK 确认   7 路径   Esc 返回")
            elif app.page == "sessions":
                label = app.tr("OK 打开   7 改名   8 删除   Esc 返回")
            elif app.page == "trash":
                label = app.tr("OK 恢复   8 永久删除   Esc 返回")
            elif app.page in {"home", "chat_menu"}:
                label = app.tr("↑↓ 选择   OK 确认   长按 Esc 退出")
        self.footer(draw, label)

    def transcript(self, draw, lines, y, row_height, visible, scroll=0):
        start = max(0, len(lines) - visible - scroll)
        palette = {"heading": ACCENT, "strong": "#fff1b9", "em": "#d7bfff",
                   "link": "#8bb9f8", "code": "#eed18c", "code_block": "#8cddd9", "marker": DIM}
        for index, (role, line) in enumerate(lines[start:start + visible]):
            x, top = 8, y + row_height * index
            color = ACCENT if role == "user" else DIM if role == "tool" else TEXT
            if any(style == "code_block" for _, style, _, _ in line):
                draw.rectangle((6, top, WIDTH - 7, top + row_height - 1), fill=PANEL)
            for text, style, font, width in line:
                draw.text((x, top), text, font=font, fill=palette.get(style, color))
                if style == "link":
                    draw.line((x, top + font.size + 1, x + width - 1, top + font.size + 1), fill=palette["link"])
                x += width

    def field_input(self, draw, app):
        draw.text((9, 40), self.clip(app.notice, 300, self.small), font=self.small, fill=DIM)
        draw.rectangle((6, 65, 313, 92), fill=PANEL)
        draw.text((10, 78), app.input_mode(), font=self.small, anchor="lm", fill=ACCENT)
        self.input_text(draw, app.field, app.field_cursor, 272, (36, 78), anchor="lm")
        composition = app.composition()
        if composition[0]:
            self.candidates(draw, app, composition, y=101)
            hint = app.tr("空格/数字选字   Esc 清除拼音")
        else:
            hint = app.ime_notice or app.tr("OK 确认   Esc 取消   Ctrl+空格 中英")
        self.footer(draw, hint)

    def candidates(self, draw, app, composition, y):
        preedit, candidates, highlight = composition
        draw.text((7, y + 8), self.clip(preedit, 300, self.small), font=self.small, anchor="ls", fill=DIM)
        slot = (WIDTH - 8) // app.ime.options.candidates
        for i, candidate in enumerate(candidates):
            x = 4 + i * slot
            if i == highlight:
                draw.rectangle((x, y + 12, x + slot - 2, y + 27), fill="#344955")
            draw.text((x + 1, y + 23), self.clip(f"{i + 1} {candidate}", slot - 3, self.small),
                      font=self.small, anchor="ls", fill=TEXT)

    def input_text(self, draw, text, cursor, width, position, *, anchor=None):
        before = self.clip(text[:cursor], width, self.font, tail=True)
        remaining = max(0, width - self.font.getlength(before))
        after = self.clip(text[cursor:], remaining) if remaining >= self.font.getlength("…") else ""
        visible = before + after
        x, y = position
        draw.text(position, visible, font=self.font, anchor=anchor, fill=TEXT)
        caret_x = x + self.font.getlength(before)
        _, top, _, bottom = draw.textbbox((x, y), "国Ag", font=self.font, anchor=anchor)
        draw.line((caret_x, top, caret_x, bottom - 1), fill=TEXT, width=1)

    def chat(self, image, draw, app):
        preedit, candidates, highlight = app.composition()
        prompt_y = 132 if preedit else 145
        self.transcript(draw, app.chat_lines(self), y=26, row_height=app.chat_row_height(),
                        visible=app.chat_rows(preedit))
        draw.rectangle((0, prompt_y - 14, WIDTH, HEIGHT), fill=PANEL)
        baseline = prompt_y + 7
        draw.text((6, baseline), app.input_mode(), font=self.small, anchor="ls", fill=ACCENT)
        self.input_text(draw, app.draft, app.cursor, 280, (33, baseline), anchor="ls")
        if preedit:
            self.candidates(draw, app, (preedit, candidates, highlight), y=142)
        else:
            hint = app.ime_notice or app.notice or (app.tr("生成中  Esc 停止  Tab 菜单") if app.busy else
                                                    app.tr("↑ 上次  Ctrl+空格 中英  Tab"))
            history = app.tr("Fn+L 聊天记录")
            draw.text((313, 155), history, font=self.small, fill=ACCENT, anchor="ra")
            width = 299 - self.small.getlength(history)
            pages = [hint] if app.ime_notice or app.busy else [hint, *app.usage_labels()]
            self.ticker(image, (7, 155, int(width), 15), pages, app.ticker_time)


class App:
    def __init__(self):
        self.ticker_time = 0
        self.stats = {}
        self.battery = None
        self.next_battery_read = 0
        self.settings = load_settings()
        self.screen = Screen()
        self.results = SimpleQueue()
        self.working = False
        self.core = self.rpc = self.ime = self.native = self.updater = None
        self.update_finished = False
        self.update_status = ""
        self.model = None
        self.cwd = self.session = self.session_file = None
        self.session_name = ""
        self.install_cancel = Event()
        self.install_thread = None
        self.page = "check"
        self.rows = []
        self.selected = 0
        self.offset = 0
        self.notice = ""
        self.field = ""
        self.field_cursor = 0
        self.draft = ""
        self.cursor = 0
        self.input_history = deque(maxlen=100)
        self.recalled_inputs = ()
        self.scroll = 0
        self.history_lines = ()
        self.workspace_target = "open"
        self.busy = False
        self.chinese = False
        self.ime_pending = False
        self.ime_notice = ""
        self.input_settings_back = "settings"
        self.input_setting_key = None
        self.display_from_chat = False
        self.display_setting_key = None
        self.messages = []
        self.wrapped_messages = []
        self.wrapped_count = 0
        self.wrapped_layout = None
        self.wrapped_stream = None
        self.tools = deque(maxlen=12)
        self.stream = {}
        self.pending = {}
        self.drafts = {}
        self.dialogs = deque()
        self.dialog = None
        self.active = True
        self.check()

    def tr(self, text):
        return translate(text, self.settings.get("language"))

    def background(self, action, call):
        if action != "ime":
            self.working = True

        def run():
            try:
                self.results.put((action, call(), ""))
            except Exception as exc:
                self.results.put((action, None, str(exc)))
        worker = Thread(target=run, daemon=True)
        worker.start()
        return worker

    def check(self, path=None):
        self.page = "check"
        candidate = self.settings.get("core", "") if path is None else path
        self.background("check", lambda: detect(candidate) if path is None else detect_only(candidate))

    def update(self):
        changed = False
        for _ in range(100):
            try:
                action, result, error = self.results.get_nowait()
            except Empty:
                break
            changed = True
            if action == "progress":
                self.notice = installation_message(result, self.settings.get("language"))
                continue
            if action != "ime":
                self.working = False
            if action == "core_update":
                self.update_finished = True
                if result:
                    self.core = result
                    self.update_status = f"Pi {result[1].removeprefix('v')} · {self.tr('Esc 返回')}"
                else:
                    self.updater.stream.feed(("\r\n" + error + "\r\n").encode("utf-8"))
                    self.update_status = self.tr("无法确认 Pi 版本 · Esc 返回")
            elif action == "ime":
                self.ime_pending = False
                self.ime = result
                self.ime_notice = self.tr("拼音初始化失败：{}").format(error) if error and self.chinese else ""
                self.chinese = self.chinese and bool(result)
                if (self.chinese and (self.page == "chat" or self.page in TEXT_FIELDS)
                        and result.options != self.pinyin_options()):
                    self.enable_pinyin()
            else:
                if action == "check" and result:
                    self.core, error = result
                elif action == "install":
                    self.core = result
                if self.core:
                    self.settings["core"] = self.core[0]
                    self.store()
                    self.home()
                else:
                    self.page = "install"
                    self.notice = installation_message(error, self.settings.get("language")) if error else self.tr("未检测到 Pi。按安装开始。")
        if self.native:
            changed = self.native.update() or changed
            if self.native.process.poll() is not None:
                self.finish_native()
                changed = True
        if self.updater and not self.update_finished and not self.working:
            changed = self.updater.update() or changed
            status = self.updater.process.poll()
            if status is not None:
                self.updater.update()
                changed = True
                if status == 0:
                    self.update_status = self.tr("正在检查 Pi 版本…")
                    path = self.core[0]
                    self.background("core_update", lambda: validate_pi(path))
                else:
                    self.update_finished = True
                    self.update_status = self.tr("Pi 升级失败（{}）· Esc 返回").format(status)
        if self.rpc:
            for _ in range(200):
                try:
                    event = self.rpc.events.get_nowait()
                except Empty:
                    break
                self.event(event)
                changed = True
                if not self.rpc:
                    break
        return changed

    def store(self):
        try:
            save_settings(self.settings)
        except OSError:
            self.notice = self.tr("设置保存失败")

    def list_page(self, page, rows):
        self.page = page
        self.rows = rows
        self.selected = 0
        self.notice = ""

    def home(self):
        self.list_page("home", [
            (self.tr("选择项目目录"), "workspace"),
            (self.tr("打开默认目录"), "default"),
            (self.tr("继续最近项目"), "recent"),
            (self.tr("会话管理"), "manage"),
            (self.tr("Pi 原生命令 / 登录"), "native"),
            (self.tr("设置"), "settings"),
            (self.tr("升级 Pi"), "core_update"),
        ])

    def preferences(self):
        default = self.settings.get("default_directory")
        locale = "English" if self.settings.get("language") == "en" else self.tr("简体中文")
        self.list_page("settings", [
            (self.tr("界面语言 · {}").format(locale), "language"),
            (self.tr("输入法设置"), "input"),
            (self.tr("Pi 设置"), "pi_settings"),
            (self.tr("显示设置"), "display"),
            (self.tr("默认目录 · {}").format(Path(default).name or default) if default else self.tr("设置默认目录"), "directory"),
            (self.tr("清除默认目录"), "clear"),
        ])

    def terminal_size(self):
        return self.display_value("terminal_size")

    def display_value(self, key):
        _, default, choices = DISPLAY_SETTINGS[key]
        value = self.settings.get(key)
        return value if type(value) is type(default) and any(value == option for option, _ in choices) else default

    def display_preferences(self, from_chat, *, focus=None):
        self.display_from_chat = from_chat
        rows = []
        for key, (label, _, choices) in DISPLAY_SETTINGS.items():
            name = dict(choices)[self.display_value(key)]
            rows.append((f"{self.tr(label)} · {self.tr(name)}", key))
        self.list_page("display_settings", rows)
        if focus is not None:
            self.selected = list(DISPLAY_SETTINGS).index(focus)

    def display_choices(self, key):
        self.display_setting_key = key
        rows = [(self.tr(label), value) for value, label in DISPLAY_SETTINGS[key][2]]
        self.list_page("display_choice", rows)
        self.selected = next(i for i, (_, value) in enumerate(rows) if value == self.display_value(key))

    def chat_row_height(self):
        return self.display_value("chat_font_size") + (6 if self.display_value("chat_line_spacing") == "relaxed" else 3)

    def chat_rows(self, preedit):
        return ((118 if preedit else 131) - 26) // self.chat_row_height()

    def history_rows(self):
        return (HEIGHT - 16) // self.chat_row_height()

    def pi_settings(self, from_chat, *, focus=None):
        self.pi_settings_from_chat = from_chat
        rows = [
            (self.tr("常规设置"), "/settings"),
            (self.tr("模型选择"), "/model"),
            (self.tr("思考强度"), "/thinking"),
            (self.tr("模型轮换范围"), "/scoped-models"),
            (self.tr("登录服务商"), "/login"),
            (self.tr("Pi 核心信息"), "core"),
        ]
        self.list_page("pi_settings", rows)
        if focus:
            self.selected = next((i for i, (_, action) in enumerate(rows) if action == focus), 0)

    def open_pi_setting(self, action):
        if action == "core":
            self.detail(f"Pi {self.core[1]}\n{self.core[0]}", "pi_settings")
        elif action == "/model" and self.pi_settings_from_chat:
            self.models(back="pi_settings")
        else:
            self.start_native(from_chat=self.pi_settings_from_chat, command=action)

    def pinyin_options(self):
        return PinyinOptions.from_settings(self.settings)

    def input_value(self, key, options):
        if key == "startup":
            return bool(self.settings.get("chinese_input"))
        return getattr(options, key)

    def input_settings(self, *, focus=None):
        options = self.pinyin_options()
        rows = []
        for key, (label, choices) in INPUT_SETTINGS.items():
            if key == "fuzzy" and options.scheme == "double":
                continue
            value = dict(choices)[self.input_value(key, options)]
            rows.append((f"{self.tr(label)} · {self.tr(value)}", key))
        rows.append((self.tr("恢复输入法默认"), "reset"))
        self.list_page("input_settings", rows)
        if focus:
            self.selected = next((i for i, (_, key) in enumerate(rows) if key == focus), 0)

    def input_choices(self, key):
        self.input_setting_key = key
        if key == "reset":
            self.list_page("input_choice", [(self.tr("取消"), False), (self.tr("恢复默认"), True)])
            return
        _, choices = INPUT_SETTINGS[key]
        self.list_page("input_choice", [(self.tr(label), value) for value, label in choices])
        current = self.input_value(key, self.pinyin_options())
        self.selected = next(i for i, (value, _) in enumerate(choices) if value == current)

    def save_input_choice(self, value):
        key = self.input_setting_key
        if key == "reset":
            if not value:
                self.input_settings(focus=key)
                return
            self.settings.pop("pinyin", None)
            self.settings.pop("chinese_input", None)
        elif key == "startup":
            self.settings["chinese_input"] = value
        else:
            stored = self.settings.get("pinyin")
            if not isinstance(stored, dict):
                stored = {}
                self.settings["pinyin"] = stored
            stored[key] = value
        self.store()
        self.input_settings(focus=key)

    def workspace(self):
        recent = self.settings.get("recent", [])
        rows = [(str(path), Path(path)) for path in recent
                if isinstance(path, str) and Path(path).is_dir()]
        self.list_page("workspace", [(self.tr("浏览文件夹"), None), *rows])

    def browse(self, directory):
        try:
            children = sorted((path for path in directory.iterdir()
                               if not path.name.startswith(".") and path.is_dir()),
                              key=lambda path: path.name.casefold())
        except OSError as exc:
            self.detail(str(exc), "workspace")
            return
        self.browsing = directory
        self.list_page("browse", [(self.tr("在此目录启动"), directory), ("../", directory.parent)] +
                       [(path.name + "/", path) for path in children])

    def project(self, path):
        try:
            path = path.expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError(self.tr("请选择文件夹"))
        except (OSError, RuntimeError, ValueError) as exc:
            self.detail(str(exc), "workspace")
            return
        if self.workspace_target == "default":
            self.settings["default_directory"] = str(path)
            self.store()
            self.workspace_target = "open"
            self.preferences()
            return
        self.cwd = path
        old = self.settings.get("recent", [])
        self.settings["recent"] = [str(path), *(x for x in old if x != str(path))][:12]
        self.store()
        self.sessions()

    def sessions(self):
        extras = self.settings.get("session_files", {})
        paths = session_list(self.cwd, extras.get(str(self.cwd)))
        self.list_page("sessions", [(self.tr("新建会话"), None), (self.tr("会话回收站"), "trash")] +
                       [(title, path) for _, title, path in paths])

    def trash(self):
        extras = self.settings.get("session_files", {})
        paths = session_list(self.cwd, extras.get(str(self.cwd)), trashed=True)
        self.list_page("trash", [(title, path) for _, title, path in paths])
        if not self.rows:
            self.notice = self.tr("回收站为空")

    def confirm_delete(self, path, *, permanent=False):
        title = self.rows[self.selected][0]
        page = "purge_session" if permanent else "delete_session"
        action = "永久删除" if permanent else "移到回收站"
        self.list_page(page, [
            (title, None), (self.tr("取消"), "cancel"), (self.tr(action), path),
        ])
        self.selected = 1

    def change_session_file(self, path, restore=False, *, permanent=False):
        back = "trash" if restore or permanent else "sessions"
        try:
            if permanent:
                purge_session(path)
            elif restore:
                restore_session(path)
            else:
                trash_session(path)
        except (OSError, ValueError) as exc:
            self.detail(str(exc), back)
            return
        if back == "trash":
            self.trash()
        else:
            self.sessions()

    def detail(self, text, back):
        self.detail_text = text
        self.detail_back = back
        self.offset = 0
        self.page = "detail"

    def detail_lines(self, screen):
        return list(screen.lines(self.detail_text[-8000:]))

    def edit(self, page, text="", notice=""):
        if self.ime:
            self.ime.clear()
        if not self.ime_pending:
            self.ime_notice = ""
        self.page, self.field, self.notice = page, text, notice
        self.field_cursor = len(text)
        if self.chinese:
            self.enable_pinyin()

    def title(self):
        return {
            "check": self.tr("Card Pi  /  核心检查"), "install": self.tr("Card Pi  /  安装"),
            "home": f"Pi {self.core[1].removeprefix('v')}" if self.core else "Pi",
            "workspace": self.tr("项目目录"), "browse": str(getattr(self, "browsing", "")),
            "path": self.tr("输入工作目录"), "binary": self.tr("输入 Pi 可执行文件路径"),
            "sessions": self.tr("会话  /  {}").format(self.cwd.name if self.cwd else ""),
            "chat": f"{'●' if self.busy else '○'}  {self.cwd.name if self.cwd else ''}  /  {self.session_name}",
            "models": self.tr("切换模型"), "settings": self.tr("设置"), "language": self.tr("界面语言"),
            "input_settings": self.tr("输入法设置 · Rime 中州韵"),
            "pi_settings": self.tr("Pi 设置"),
            "display_settings": self.tr("显示设置"),
            "display_choice": self.tr(DISPLAY_SETTINGS[self.display_setting_key][0]) if self.display_setting_key else "",
            "input_choice": self.tr(INPUT_SETTINGS.get(self.input_setting_key, ("恢复输入法默认",))[0]),
            "purge_session": self.tr("永久删除？无法恢复"),
            "trash": self.tr("会话回收站"), "delete_session": self.tr("删除会话？"), "chat_menu": self.tr("当前会话"), "rename": self.tr("重命名会话"), "detail": self.tr("详情"),
            "dialog_input": self.tr("Pi 扩展输入"), "dialog_editor": self.tr("Pi 扩展编辑"),
            "dialog_select": self.tr("Pi 扩展选择"), "dialog_confirm": self.tr("Pi 扩展确认"),
        }[self.page]

    def start(self, session, *, chinese=None):
        self.stop()
        self.session = session
        self.session_file = None
        self.session_name = self.tr("新会话")
        self.clear_messages()
        self.stats = {}
        self.model = None
        self.tools.clear()
        self.stream.clear()
        self.pending.clear()
        self.draft = self.drafts.get((self.cwd, session), "")
        self.cursor, self.scroll = len(self.draft), 0
        self.notice = self.tr("正在启动 Pi…")
        try:
            self.rpc = RPC(self.core, self.cwd, session)
            self.page = "chat"
            self.busy = True
            self.send("get_state")
            self.send("get_messages")
            self.chinese = bool(self.settings.get("chinese_input")) if chinese is None else chinese
            if self.chinese:
                self.enable_pinyin()
            else:
                self.ime_notice = ""
        except OSError as exc:
            self.stop()
            self.detail(str(exc), "sessions")

    def stop(self):
        if self.rpc:
            key = self.cwd, Path(self.session_file) if self.session_file else self.session
            if self.draft:
                self.drafts[key] = self.draft
            else:
                self.drafts.pop(key, None)
            if self.session is None and self.session_file:
                self.drafts.pop((self.cwd, None), None)
            self.rpc.close()
            self.rpc = None
        if self.ime:
            self.ime.clear()
        self.busy = False
        self.dialog = None
        self.dialogs.clear()
        self.pending.clear()

    def start_native(self, from_chat, *, command=None):
        if from_chat and self.busy:
            self.page = "chat"
            self.notice = self.tr("请先停止当前任务，再打开 Pi 原生命令")
            return
        self.native_return_chat = from_chat
        self.native_setting = command
        self.native_cwd = self.cwd if from_chat else HOME
        self.native_original = Path(self.session_file) if from_chat and self.session_file else None
        self.native_draft = self.draft if from_chat else ""
        self.native_chinese = self.chinese
        self.stop()
        try:
            cols, rows = self.screen.terminal_dimensions(self.terminal_size())
            self.native = NativePi(self.core, self.native_cwd, self.native_original,
                                   command=command, cols=cols, rows=rows)
        except (OSError, ImportError) as exc:
            if from_chat:
                self.restore_native_session(self.native_cwd, self.native_original)
                self.notice = str(exc)
            else:
                self.detail(str(exc), "pi_settings" if command else "home")
            return
        self.page = "native"

    def start_core_update(self):
        path, _, env = self.core
        cols, rows = self.screen.terminal_dimensions(self.terminal_size())
        try:
            self.updater = TerminalProcess([path, "update"], HOME, env, cols=cols, rows=rows)
        except (OSError, ImportError) as exc:
            self.detail(str(exc), "home")
            return
        self.updater.stream.feed(b"$ pi update\r\n")
        self.update_finished = False
        self.update_status = self.tr("正在升级 Pi…")
        self.page = "core_update"

    def restore_native_session(self, cwd, session):
        self.cwd = cwd
        if session is not None and not session.is_file():
            session = None
        self.start(session, chinese=self.native_chinese)

    def finish_native(self):
        terminal = self.native
        self.native = None
        if not terminal:
            return
        report = terminal.close() or {}
        if not self.native_return_chat:
            if self.native_setting:
                self.pi_settings(False, focus=self.native_setting)
            else:
                self.home()
            return
        cwd = Path(report.get("cwd") or self.native_cwd)
        name = report.get("session")
        session = Path(name) if name else self.native_original
        self.restore_native_session(cwd, session)
        # An empty Pi session may get a new filename when interactive mode opens it.
        if cwd == self.native_cwd and (session == self.native_original or
                                       not self.native_original or not self.native_original.is_file()):
            self.draft = self.native_draft
            self.cursor = len(self.draft)
        if self.native_setting and self.page == "chat":
            self.pi_settings(True, focus=self.native_setting)

    def models(self, *, back="chat_menu"):
        if not self.rpc or self.busy:
            self.page = "chat"
            self.notice = self.tr("请在 Pi 空闲时切换模型")
            return
        self.models_back = back
        self.list_page("models", [])
        self.send("get_available_models")

    def send(self, command, **fields):
        if not self.rpc:
            return
        try:
            ident = self.rpc.send(command, **fields)
        except OSError as exc:
            self.busy = False
            self.notice = str(exc)
            return
        self.pending[ident] = (command, fields)

    def prompt(self):
        if self.busy or not self.draft.strip():
            return
        if not self.rpc:
            self.notice = self.tr("Pi 已退出，请从会话列表重新打开")
            return
        self.notice = ""
        self.busy = True
        self.scroll = 0
        self.recalled_inputs = ()
        self.send("prompt", message=self.draft)

    def battery_label(self):
        now = monotonic()
        if now >= self.next_battery_read:
            self.next_battery_read = now + 15
            self.battery = None
            for supply in Path("/sys/class/power_supply").glob("*"):
                try:
                    if (supply / "type").read_text().strip() == "Battery":
                        percent = int((supply / "capacity").read_text())
                        if 0 <= percent <= 100:
                            self.battery = f"{percent}%"
                            break
                except (OSError, ValueError):
                    continue
        label = "Battery" if self.settings.get("language") == "en" else "电量"
        return f"{label} {self.battery or '—'}"

    def usage_labels(self):
        en = self.settings.get("language") == "en"
        tokens = self.stats.get("tokens") or {}
        context = self.stats.get("contextUsage") or {}
        window = context.get("contextWindow") or (self.model or {}).get("contextWindow")

        def count(value):
            if value is None:
                return "—"
            return f"{value / 1000:.3g}k" if value >= 1000 else str(value)

        pages = []
        if tokens:
            read, write = count(tokens.get("cacheRead")), count(tokens.get("cacheWrite"))
            pages.append(f"Cache R {read} W {write}" if en else f"缓存 读 {read} 写 {write}")
            used = f"↑{count(tokens.get('input'))} ↓{count(tokens.get('output'))}"
            cost = self.stats.get("cost")
            pages.append(used if cost is None else f"{used}  ${cost:.3f}")
        if window:
            label = "Context" if en else "上下文"
            used = count(context.get("tokens"))
            percent = context.get("percent")
            suffix = "" if percent is None else f" {percent:.0f}%"
            pages.append(f"{label} {used}/{count(window)}{suffix}")
        return pages

    def response(self, event):
        command, fields = self.pending.pop(event.get("id"), ("", {}))
        if command == "get_session_stats":
            self.stats = (event.get("data") or {}) if event.get("success") else {}
            return
        if not event.get("success"):
            self.notice = str(event.get("error") or self.tr("Pi 命令失败"))
            if command == "set_model":
                self.page = "chat"
            if command in {"prompt", "abort", "get_state", "get_messages"}:
                self.busy = False
            return
        data = event.get("data") or {}
        if command == "get_state":
            self.model = data.get("model")
            self.session_file = data.get("sessionFile")
            self.session_name = data.get("sessionName") or self.tr("新会话")
            self.busy = bool(data.get("isStreaming") or data.get("isCompacting"))
            self.send("get_session_stats")
            if self.notice == self.tr("正在启动 Pi…"):
                self.notice = "" if data.get("model") else self.tr("按 / 打开 Pi 原生命令，再执行 /login")
            if self.session_file:
                files = self.settings.setdefault("session_files", {})
                if files.get(str(self.cwd)) != self.session_file:
                    files[str(self.cwd)] = self.session_file
                    self.store()
        elif command == "get_messages":
            self.clear_messages()
            for message in data.get("messages", []):
                self.append_message(message)
        elif command == "get_available_models" and self.page == "models":
            current = self.model or {}
            models = sorted(data.get("models", []), key=lambda model: (model["provider"], model["id"]))
            self.rows = []
            for index, model in enumerate(models):
                provider, ident = model["provider"], model["id"]
                active = (provider, ident) == (current.get("provider"), current.get("id"))
                self.rows.append((f"{'* ' if active else ''}{provider}/{ident}", model))
                if active:
                    self.selected = index
            if not self.rows:
                self.notice = self.tr("无可用模型。返回对话，按 / 执行 /login。")
        elif command == "set_model":
            self.model = data
            self.stats.pop("contextUsage", None)
            self.send("get_session_stats")
            self.page = "chat"
            self.notice = self.tr("已切换到 {}").format(f"{fields['provider']}/{fields['modelId']}")
        elif command == "prompt":
            value = fields["message"]
            self.remember_input(value)
            if self.draft == value and not self.recalled_inputs:
                self.draft, self.cursor = "", 0
        elif command == "set_session_name":
            self.session_name = fields["name"]
        elif command == "abort":
            self.busy = False

    def append_message(self, message):
        role = message.get("role")
        if role in {"user", "assistant"}:
            value = message_text(message)
            if value:
                self.messages.append((role, value))
                if role == "user":
                    self.remember_input(value)
        if message.get("errorMessage"):
            self.notice = str(message["errorMessage"])

    def event(self, event):
        kind = event.get("type")
        if kind == "response":
            self.response(event)
        elif kind == "closed":
            self.busy = False
            self.notice = self.notice or self.tr("Pi 已退出")
            self.stop()
            self.page = "chat"
        elif kind == "stderr":
            self.notice = event["message"]
        elif kind == "agent_start":
            self.busy = True
        elif kind == "agent_end" and not event.get("willRetry"):
            # Releases before agent_settled still report agent_end.
            self.busy = False
            self.send("get_session_stats")
        elif kind == "agent_settled":
            self.busy = False
            self.send("get_state")
        elif kind == "message_start" and event.get("message", {}).get("role") == "assistant":
            self.stream.clear()
        elif kind == "message_update":
            update = event.get("assistantMessageEvent", {})
            if update.get("type") == "text_delta":
                index = update.get("contentIndex", 0)
                self.stream[index] = (self.stream.get(index, "") + update.get("delta", ""))[-6000:]
        elif kind == "message_end":
            self.stream.clear()
            self.append_message(event.get("message", {}))
        elif kind == "tool_execution_start":
            name = event.get("toolName", "")
            self.messages.append(("tool", f"· {name}"))
        elif kind == "tool_execution_end":
            self.tools.append((event.get("toolName", ""), message_text(event.get("result", {}))[-3000:]))
        elif kind == "extension_ui_request":
            if event.get("method") == "notify":
                self.notice = str(event.get("message", ""))
            elif event.get("method") in {"confirm", "select", "input", "editor"}:
                self.dialogs.append(event)
                if not self.dialog:
                    self.next_dialog()

    def next_dialog(self):
        self.dialog = self.dialogs.popleft() if self.dialogs else None
        if not self.dialog:
            self.page = "chat"
            return
        title = str(self.dialog.get("title", ""))
        method = self.dialog["method"]
        if method == "select":
            self.list_page("dialog_select", [(str(x), {"value": x})
                                             for x in self.dialog.get("options", [])])
        elif method == "confirm":
            message = str(self.dialog.get("message", ""))
            question = "\n".join(part for part in (title, message) if part)
            lines = list(self.screen.lines(question))[:4]
            self.list_page("dialog_confirm", [(line, {}) for line in lines] + [
                (self.tr("取消"), {"confirmed": False}), (self.tr("确认"), {"confirmed": True}),
            ])
            self.selected = len(lines)
        else:
            self.edit(f"dialog_{method}", str(self.dialog.get("prefill") or ""), title)

    def answer(self, **fields):
        if not self.dialog:
            return
        if self.rpc:
            try:
                self.rpc.write({"type": "extension_ui_response", "id": self.dialog["id"], **fields})
            except OSError:
                self.notice = self.tr("无法向 Pi 返回输入")
        self.next_dialog()

    def clear_messages(self):
        self.messages.clear()
        self.input_history.clear()
        self.recalled_inputs = ()
        self.wrapped_messages.clear()
        self.wrapped_count = 0
        self.wrapped_stream = None
        self.history_lines = ()

    def chat_lines(self, screen):
        layout = self.display_value("chat_font_size"), self.display_value("render_markdown")
        if self.wrapped_layout != layout:
            self.wrapped_layout = layout
            self.wrapped_messages.clear()
            self.wrapped_count = 0
        for index in range(self.wrapped_count, len(self.messages)):
            role, value = self.messages[index]
            self.wrapped_messages.extend(screen.message_lines(role, value, *layout))
        self.wrapped_count = len(self.messages)
        if not self.stream:
            self.wrapped_stream = None
            return self.wrapped_messages
        active = "".join(value for _, value in sorted(self.stream.items()))
        key = layout, active
        if self.wrapped_stream is None or self.wrapped_stream[0] != key:
            self.wrapped_stream = key, list(screen.message_lines("assistant", active, *layout))
        return self.wrapped_messages + self.wrapped_stream[1]

    def enter_history(self):
        self.history_lines = tuple(self.chat_lines(self.screen))
        self.scroll = 0
        self.page = "history"

    def scroll_history(self, key):
        if key.code in (ESC, ENTER, TAB):
            self.scroll = 0
            self.history_lines = ()
            self.page = "chat"
            return
        if not key.control and key.code in LIST_STEPS:
            rows = self.history_rows()
            step = {UP: 1, DOWN: -1, KEY_F: 1, KEY_X: -1, PAGE_UP: rows, PAGE_DOWN: -rows}[key.code]
            self.scroll = max(0, min(max(0, len(self.history_lines) - rows), self.scroll + step))
            if step < 0 and not self.scroll:
                self.history_lines = ()
                self.page = "chat"

    def repeat_scroll(self, key):
        if key.alt or key.control:
            return False
        if self.page in LIST_PAGES or self.page in {"history", "detail"}:
            return key.code in LIST_STEPS
        return key.code in CHAT_REPEAT_KEYS and self.page == "chat" and not self.composition()[0]

    def remember_input(self, value):
        if value.strip() and (not self.input_history or self.input_history[-1] != value):
            self.input_history.append(value)

    def recall_input(self, key):
        if not self.recalled_inputs:
            if key == DOWN:
                self.cursor = len(self.draft)
                return
            if self.draft and self.cursor:
                self.cursor = 0
                return
            if not self.input_history:
                return
            self.recalled_inputs = tuple(self.input_history)
            self.recall_index = len(self.recalled_inputs)
            self.recall_draft = self.draft, self.cursor
        index = self.recall_index + (-1 if key == UP else 1)
        if index == len(self.recalled_inputs):
            self.draft, self.cursor = self.recall_draft
            self.recalled_inputs = ()
        elif 0 <= index < len(self.recalled_inputs):
            self.recall_index = index
            self.draft = self.recalled_inputs[index]
            self.cursor = 0 if key == UP else len(self.draft)

    def composition(self):
        return self.ime.view() if self.ime and self.chinese else ("", [], 0)

    def input_mode(self):
        if not self.chinese or not self.ime:
            return "EN"
        options = self.ime.options
        return ("双" if options.scheme == "double" else "") + ("简" if options.script == "simplified" else "繁")

    def insert(self, value):
        if value:
            self.recalled_inputs = ()
            self.draft = self.draft[:self.cursor] + value + self.draft[self.cursor:]
            self.cursor += len(value)

    def enable_pinyin(self):
        options = self.pinyin_options()
        if self.ime and self.ime.options == options:
            self.chinese = True
            self.ime_notice = ""
            return
        if self.working:
            return
        self.chinese = True
        self.ime_notice = self.tr("正在初始化拼音…")
        if self.ime_pending:
            return
        previous, self.ime = self.ime, None
        self.ime_pending = True

        def prepare():
            if previous is None:
                return Pinyin(CONFIG / "rime", options)
            try:
                return previous.configure(options)
            except Exception:
                previous.close()
                raise

        self.background("ime", prepare)

    def chat_menu(self):
        self.list_page("chat_menu", [
            (self.tr("返回项目首页"), "home"),
            (self.tr("翻看聊天记录"), "history"), (self.tr("切换模型"), "models"),
            (self.tr("Pi 设置"), "pi_settings"), (self.tr("输入法设置"), "input"),
            (self.tr("显示设置"), "display"),
            (self.tr("Pi 原生命令 / 登录"), "native"),
            (self.tr("会话列表"), "sessions"), (self.tr("重命名会话"), "rename"),
            (self.tr("查看最近工具输出"), "tools"),
        ])

    def handle_ime(self, key, insert, preedit, *, path=False):
        if key.control and key.code == 57:
            if not key.repeat:
                if self.chinese:
                    if self.ime:
                        self.ime.clear()
                    self.chinese = False
                    self.ime_notice = ""
                else:
                    self.enable_pinyin()
            return True
        if self.ime_pending and self.chinese:
            return bool(key.text) or key.code == ENTER
        if not self.chinese or not self.ime:
            return False
        if path and preedit and key.text in PATH_PUNCTUATION:
            _, value = self.ime.key(ord(" "))
            insert(value)
            return bool(self.composition()[0])
        code = None
        if preedit:
            code = {ESC: 0xff1b, BACKSPACE: 0xff08, ENTER: ord(" "),
                    PAGE_UP: 0xff55, PAGE_DOWN: 0xff56, UP: 0xff55, DOWN: 0xff56,
                    LEFT: 0xff51, RIGHT: 0xff53}.get(key.code)
        if code is None and len(key.text) == 1 and ((key.text.isascii() and key.text.islower()) or preedit):
            code = ord(key.text)
        if code is None:
            return False
        handled, value = self.ime.key(code)
        insert(value)
        return bool(handled or preedit or value)

    def edit_chat(self, key):
        preedit, _, _ = self.composition()
        if (key.text == "/" and not self.draft and not preedit and not key.repeat
                and not (self.chinese and self.ime_pending)):
            self.start_native(from_chat=True)
            return
        if self.handle_ime(key, self.insert, preedit):
            return
        if key.code == ENTER and not key.repeat:
            self.prompt()
        elif key.code == ESC and not key.repeat and self.busy:
            self.send("abort")
        elif key.code == TAB and not key.repeat:
            self.chat_menu()
        elif key.code == BACKSPACE and self.cursor:
            self.recalled_inputs = ()
            self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
            self.cursor -= 1
        elif key.code == LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key.code == RIGHT:
            self.cursor = min(len(self.draft), self.cursor + 1)
        elif not key.control and key.code in {UP, DOWN}:
            self.recall_input(key.code)
        elif not key.control and key.code == PAGE_UP:
            self.enter_history()
        elif key.text and len(self.draft) < 8000:
            text = self.ime.literal(key.text) if self.ime and self.chinese else key.text
            self.insert(text)

    def handle(self, key):
        if key.alt and self.page != "native":
            return
        if self.page == "native":
            if key.control and key.code == CTRL_Q:
                if not key.repeat:
                    self.finish_native()
            else:
                self.native.key(key)
        elif self.page == "core_update":
            if self.update_finished and key.code == ESC:
                self.updater.close()
                self.updater = None
                self.home()
            elif not self.update_finished and not self.working and key.code != ESC:
                self.updater.key(key)
        elif self.page == "history":
            self.scroll_history(key)
        elif self.page == "chat":
            self.edit_chat(key)
        elif self.page == "install":
            if self.working and key.code == ESC:
                self.install_cancel.set()
                self.notice = self.tr("正在取消安装…")
            elif not self.working:
                if (key.code == ENTER or key.text == "6") and not key.repeat:
                    self.notice = self.tr("正在准备安装…")
                    self.install_cancel.clear()
                    self.install_thread = self.background("install", lambda: install(
                        lambda status: self.results.put(("progress", status, "")),
                        self.install_cancel))
                elif key.text == "7":
                    self.edit("binary", str(HOME) + "/", self.tr("Pi 可执行文件的绝对路径"))
                elif key.text == "8":
                    self.check()
                elif key.code == ESC:
                    self.active = False
        elif self.page == "check":
            if key.code == ESC:
                self.active = False
        elif self.page == "detail":
            if key.code == ESC:
                if self.detail_back == "workspace":
                    self.workspace()
                elif self.detail_back == "sessions":
                    self.sessions()
                elif self.detail_back == "trash":
                    self.trash()
                elif self.detail_back == "settings":
                    self.preferences()
                elif self.detail_back == "pi_settings":
                    self.pi_settings(self.pi_settings_from_chat, focus="core")
                elif self.detail_back == "home":
                    self.home()
                else:
                    self.page = self.detail_back
            elif not key.control and key.code in LIST_STEPS:
                step = LIST_STEPS[key.code]
                self.offset = max(0, min(self.offset + step, len(self.detail_lines(self.screen)) - 7))
        elif self.page in TEXT_FIELDS:
            self.handle_field(key)
        else:
            self.handle_list(key)

    def handle_field(self, key):
        page = self.page
        path = page in PATH_FIELDS
        if self.handle_ime(key, self.insert_field, self.composition()[0], path=path):
            return
        if key.code == ESC:
            if page.startswith("dialog_"):
                self.answer(cancelled=True)
            elif page == "path":
                self.workspace()
            else:
                self.page = "install" if page == "binary" else "chat_menu"
        elif key.code == BACKSPACE and self.field_cursor:
            cursor = self.field_cursor
            self.field = self.field[:cursor - 1] + self.field[cursor:]
            self.field_cursor -= 1
        elif key.code == LEFT:
            self.field_cursor = max(0, self.field_cursor - 1)
        elif key.code == RIGHT:
            self.field_cursor = min(len(self.field), self.field_cursor + 1)
        elif key.code == ENTER and not key.repeat:
            if page.startswith("dialog_"):
                self.answer(value=self.field)
            elif self.field.strip():
                if page == "path":
                    self.project(Path(self.field.strip()))
                elif page == "binary":
                    self.check(self.field.strip())
                elif page == "rename":
                    self.send("set_session_name", name=self.field.strip())
                    self.page = "chat"
        elif key.text:
            value = self.ime.literal(key.text) if self.ime and self.chinese and not path else key.text
            self.insert_field(value)

    def insert_field(self, value):
        value = value[:max(0, 4096 - len(self.field))]
        cursor = self.field_cursor
        self.field = self.field[:cursor] + value + self.field[cursor:]
        self.field_cursor += len(value)

    def handle_list(self, key):
        page = self.page
        if key.code == ESC:
            if page.startswith("dialog_"):
                self.answer(cancelled=True)
            elif page == "home":
                self.active = False
            elif page == "chat_menu":
                self.page = "chat"
                if self.chinese:
                    self.enable_pinyin()
            elif page == "models":
                if self.models_back == "pi_settings":
                    self.pi_settings(True, focus="/model")
                else:
                    self.chat_menu()
            elif page == "pi_settings":
                if self.pi_settings_from_chat:
                    self.chat_menu()
                else:
                    self.preferences()
            elif page == "input_choice":
                self.input_settings(focus=self.input_setting_key)
            elif page == "input_settings":
                if self.input_settings_back == "chat_menu":
                    self.chat_menu()
                else:
                    self.preferences()
            elif page == "browse":
                self.workspace()
            elif page == "workspace" and self.workspace_target == "default":
                self.workspace_target = "open"
                self.preferences()
            elif page == "language":
                self.preferences()
            elif page == "display_settings":
                self.chat_menu() if self.display_from_chat else self.preferences()
            elif page == "display_choice":
                self.display_preferences(self.display_from_chat, focus=self.display_setting_key)
            elif page == "purge_session":
                self.trash()
            elif page in {"trash", "delete_session"}:
                self.sessions()
            else:
                self.home()
            return
        step = LIST_STEPS.get(key.code) if not key.control else None
        if step and self.rows:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + step))
            return
        if page == "workspace" and key.text == "7":
            self.edit("path", "", self.tr("绝对路径或 ~/…"))
            return
        if not self.rows or key.repeat:
            return
        entry = self.rows[self.selected][1]
        if page in {"sessions", "trash"} and key.text == "8" and isinstance(entry, Path):
            self.confirm_delete(entry, permanent=page == "trash")
            return
        if page == "sessions" and key.text == "7" and isinstance(entry, Path):
            self.start(entry)
            if self.rpc:
                self.edit("rename", "", self.tr("输入该会话的新名称"))
            return
        if key.code != ENTER:
            return
        if page.startswith("dialog_"):
            if entry:
                self.answer(**entry)
        elif page == "home":
            self.open_home_menu(entry)
        elif page == "workspace":
            self.project(entry) if entry else self.browse(HOME)
        elif page == "browse":
            self.project(entry) if self.selected == 0 else self.browse(entry)
        elif page == "sessions":
            self.trash() if entry == "trash" else self.start(entry)
        elif page == "trash":
            self.change_session_file(entry, restore=True)
        elif page == "purge_session":
            if isinstance(entry, Path):
                self.change_session_file(entry, permanent=True)
            elif entry == "cancel":
                self.trash()
        elif page == "delete_session":
            if isinstance(entry, Path):
                self.change_session_file(entry)
            elif entry == "cancel":
                self.sessions()
        elif page == "settings":
            self.open_preference(entry)
        elif page == "pi_settings":
            self.open_pi_setting(entry)
        elif page == "input_settings":
            self.input_choices(entry)
        elif page == "input_choice":
            self.save_input_choice(entry)
        elif page == "language":
            self.settings["language"] = entry
            self.store()
            self.preferences()
        elif page == "display_settings":
            self.display_choices(entry)
        elif page == "display_choice":
            self.settings[self.display_setting_key] = entry
            self.display_preferences(self.display_from_chat, focus=self.display_setting_key)
            self.store()
        elif page == "chat_menu":
            self.open_chat_menu(entry)
        elif page == "models" and not any(command == "set_model" for command, _ in self.pending.values()):
            self.send("set_model", provider=entry["provider"], modelId=entry["id"])

    def open_home_menu(self, action):
        self.workspace_target = "open"
        if action in {"workspace", "manage"}:
            self.workspace()
        elif action == "default" and self.settings.get("default_directory"):
            self.project(Path(self.settings["default_directory"]))
        elif action == "recent" and self.settings.get("recent"):
            self.project(Path(self.settings["recent"][0]))
        elif action in {"default", "recent"}:
            self.workspace()
        elif action == "native":
            self.start_native(from_chat=False)
        elif action == "settings":
            self.preferences()
        elif action == "core_update":
            self.start_core_update()

    def open_preference(self, action):
        if action == "language":
            self.list_page("language", [(self.tr("简体中文"), "zh"), ("English", "en")])
            self.selected = int(self.settings.get("language") == "en")
        elif action == "input":
            self.input_settings_back = "settings"
            self.input_settings()
        elif action == "pi_settings":
            self.pi_settings(False)
        elif action == "display":
            self.display_preferences(False)
        elif action == "directory":
            self.workspace_target = "default"
            self.workspace()
        elif action == "clear":
            self.settings.pop("default_directory", None)
            self.store()
            self.preferences()

    def open_chat_menu(self, action):
        if action == "history":
            self.enter_history()
        elif action == "models":
            self.models()
        elif action == "pi_settings":
            self.pi_settings(True)
        elif action == "input":
            self.input_settings_back = "chat_menu"
            self.input_settings()
        elif action == "display":
            self.display_preferences(True)
        elif action == "native":
            self.start_native(from_chat=True)
        elif action == "rename" and self.rpc:
            self.edit("rename", self.session_name if self.session_file else "", self.tr("输入新的会话名称"))
        elif action == "tools":
            details = "\n\n".join(f"{name}\n{value}" for name, value in self.tools)
            self.detail(details or self.tr("当前没有工具输出"), "chat_menu")
        elif action in {"sessions", "home"}:
            if self.busy:
                self.page = "chat"
                self.notice = self.tr("请先按 Esc 停止当前任务")
                return
            self.stop()
            self.sessions() if action == "sessions" else self.home()


def detect_only(path):
    try:
        return validate_pi(path), ""
    except Exception as exc:
        return None, str(exc)


def main():
    if os.geteuid() == 0:
        raise SystemExit("Card Pi requires a non-root account")
    app = App()

    def stop_signal(_signum, _frame):
        app.active = False

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    held_escape = {}
    modifiers = {}
    held_scroll = {}
    try:
        with ExitStack() as stack, open_display() as (display, pixels):
            devices, cardputer = keyboard_fds(stack)
            dirty = True
            ticker_start = monotonic()
            ticker_frame = None
            while app.active:
                app.ticker_time = monotonic() - ticker_start
                cycle, elapsed = divmod(app.ticker_time, TICKER_PERIOD)
                frame = (cycle, max(0, int((elapsed - TICKER_PERIOD + TICKER_SLIDE) / 0.035)))
                if frame != ticker_frame:
                    ticker_frame = frame
                    if app.page not in {"native", "core_update", "history"}:
                        dirty = True
                if app.update():
                    dirty = True
                if dirty:
                    display.show(pixels, app.screen.render(app))
                    dirty = False
                for device, key in read_keys(devices, 0.035, modifiers, cardputer):
                    identity = device, key.code
                    if key.code == ESC:
                        if key.released and device in held_escape:
                            pressed_at = held_escape.pop(device)
                            if monotonic() - pressed_at >= 1.0:
                                app.active = False
                            else:
                                app.handle(key)
                        elif not key.released and not key.repeat:
                            held_escape[device] = monotonic()
                    elif key.released:
                        held_scroll.pop(identity, None)
                    elif key.repeat and identity in held_scroll:
                        continue
                    elif app.repeat_scroll(key):
                        if not key.repeat:
                            app.handle(key)
                            held_scroll[identity] = monotonic() + 0.30, app.page
                    else:
                        app.handle(key)
                    dirty = True
                now = monotonic()
                for identity, (deadline, page) in list(held_scroll.items()):
                    device, code = identity
                    if device not in devices:
                        del held_scroll[identity]
                    elif app.page == page and now >= deadline and app.repeat_scroll(KeyPress(code)):
                        held_scroll[identity] = now + 0.09, page
                        app.handle(KeyPress(code, repeat=True))
                        dirty = True
                if any(monotonic() - when > 1.0 for when in held_escape.values()):
                    app.active = False
    finally:
        if app.native:
            app.native.close()
        if app.updater:
            app.updater.close()
        app.install_cancel.set()
        if app.install_thread:
            app.install_thread.join(timeout=0.8)
        app.stop()
        if app.ime:
            app.ime.close()
