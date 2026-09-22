"""Plain-curses widgets: framed panes, popups and a file browser.

No third-party dependency on purpose -- the tool has to run on the same bare
box as xray. Layout follows nmtui: a framed body, a highlighted cursor line and
a key-hint footer.
"""

import curses
import os
from pathlib import Path

# Colour pairs
NORMAL = 0
HEADER = 1
SELECT = 2
OK = 3
ERROR = 4
DIM = 5
KEYHINT = 6
WARN = 7
TITLE = 8


def init_colors() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    curses.init_pair(HEADER, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(OK, curses.COLOR_GREEN, background)
    curses.init_pair(ERROR, curses.COLOR_RED, background)
    curses.init_pair(DIM, curses.COLOR_CYAN, background)
    curses.init_pair(KEYHINT, curses.COLOR_YELLOW, background)
    curses.init_pair(WARN, curses.COLOR_YELLOW, background)
    curses.init_pair(TITLE, curses.COLOR_CYAN, background)


def attr(pair: int, bold: bool = False) -> int:
    value = curses.color_pair(pair)
    return value | curses.A_BOLD if bold else value


def getkey(window):
    """Next key as str/int, or None when the read timed out."""
    try:
        key = window.get_wch()
    except curses.error:
        return None
    except KeyboardInterrupt:
        return "q"
    return key


def is_enter(key) -> bool:
    return key in ("\n", "\r", curses.KEY_ENTER, 10, 13)


def is_escape(key) -> bool:
    return key == "\x1b" or key == 27


def clip(text: str, width: int) -> str:
    text = str(text).replace("\t", "    ")
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def addstr(window, y: int, x: int, text: str, style: int = 0) -> None:
    """Write clipped to the window, never raising at the bottom-right cell."""
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    text = clip(text, width - x)
    if not text:
        return
    try:
        if x + len(text) >= width:
            # Filling the last column with addstr() wraps the cursor and raises;
            # insstr() writes it without moving the cursor past the edge.
            window.insstr(y, x, text, style)
        else:
            window.addstr(y, x, text, style)
    except curses.error:
        pass


def fill_line(window, y: int, x: int, width: int, style: int) -> None:
    addstr(window, y, x, " " * width, style)


def draw_frame(window, y: int, x: int, height: int, width: int,
               title: str = "", focused: bool = False) -> None:
    """Box with an inline title; the focused frame is bold."""
    style = attr(TITLE, True) if focused else attr(DIM)
    horizontal = "─" * max(0, width - 2)
    addstr(window, y, x, "┌" + horizontal + "┐", style)
    for row in range(y + 1, y + height - 1):
        addstr(window, row, x, "│", style)
        addstr(window, row, x + width - 1, "│", style)
    addstr(window, y + height - 1, x, "└" + horizontal + "┘", style)
    if title:
        label = clip(f" {title} ", max(0, width - 4))
        addstr(window, y, x + 2, label, attr(TITLE, True) if focused else attr(DIM, True))


def center_window(height: int, width: int):
    """A new window centred on screen, clamped to the terminal size."""
    screen_height, screen_width = curses.LINES, curses.COLS
    height = min(height, screen_height)
    width = min(width, screen_width)
    top = max(0, (screen_height - height) // 2)
    left = max(0, (screen_width - width) // 2)
    window = curses.newwin(height, width, top, left)
    window.keypad(True)
    return window


def wrap(text: str, width: int) -> list:
    """Word wrap that also honours embedded newlines."""
    lines = []
    for paragraph in str(text).split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if len(candidate) <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                while len(word) > width:
                    lines.append(word[:width])
                    word = word[width:]
                current = word
        lines.append(current)
    return lines


# -- popups ---------------------------------------------------------------

def popup_message(stdscr, title: str, body: str, style: int = NORMAL) -> None:
    width = min(curses.COLS - 4, max(40, min(90, max(len(l) for l in str(body).split("\n")) + 6)))
    lines = wrap(body, width - 4)
    height = min(curses.LINES - 2, len(lines) + 4)
    window = center_window(height, width)
    window.erase()
    draw_frame(window, 0, 0, height, width, title, focused=True)
    for index, line in enumerate(lines[: height - 4]):
        addstr(window, 1 + index, 2, line, attr(style))
    addstr(window, height - 2, 2, "Press any key", attr(KEYHINT))
    window.refresh()
    window.timeout(-1)
    getkey(window)
    stdscr.touchwin()
    stdscr.refresh()


def popup_confirm(stdscr, title: str, body: str, default_yes: bool = True) -> bool:
    width = min(curses.COLS - 4, max(40, min(80, len(body) + 8)))
    lines = wrap(body, width - 4)
    height = min(curses.LINES - 2, len(lines) + 5)
    window = center_window(height, width)
    window.timeout(-1)
    choice = default_yes

    while True:
        window.erase()
        draw_frame(window, 0, 0, height, width, title, focused=True)
        for index, line in enumerate(lines[: height - 5]):
            addstr(window, 1 + index, 2, line)
        row = height - 2
        yes = " Yes " if choice else "[Yes]"
        no = "[No] " if choice else " No  "
        addstr(window, row, 2, yes, attr(SELECT) if choice else attr(NORMAL))
        addstr(window, row, 10, no, attr(SELECT) if not choice else attr(NORMAL))
        addstr(window, row, 20, "←/→, Enter, Esc", attr(KEYHINT))
        window.refresh()

        key = getkey(window)
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t", "h", "l"):
            choice = not choice
        elif key in ("y", "Y"):
            choice = True
            break
        elif key in ("n", "N"):
            choice = False
            break
        elif is_enter(key):
            break
        elif is_escape(key):
            choice = False
            break

    stdscr.touchwin()
    stdscr.refresh()
    return choice


def popup_input(stdscr, title: str, prompt: str, value: str = "",
                secret: bool = False) -> str:
    """Single-line editor. Returns None when cancelled."""
    width = min(curses.COLS - 4, max(46, min(96, len(prompt) + 20, curses.COLS - 4)))
    height = 7
    window = center_window(height, width)
    window.timeout(-1)
    text = str(value)
    cursor = len(text)
    view = 0
    field_width = width - 4

    curses.curs_set(1)
    while True:
        window.erase()
        draw_frame(window, 0, 0, height, width, title, focused=True)
        addstr(window, 1, 2, clip(prompt, field_width), attr(DIM))
        if cursor < view:
            view = cursor
        if cursor - view >= field_width - 1:
            view = cursor - field_width + 2
        shown = ("*" * len(text) if secret else text)[view: view + field_width - 1]
        fill_line(window, 3, 2, field_width, attr(SELECT))
        addstr(window, 3, 2, shown, attr(SELECT))
        addstr(window, height - 2, 2, "Enter save · Esc cancel", attr(KEYHINT))
        try:
            window.move(3, 2 + min(cursor - view, field_width - 1))
        except curses.error:
            pass
        window.refresh()

        key = getkey(window)
        if key is None:
            continue
        if is_enter(key):
            break
        if is_escape(key):
            text = None
            break
        if key in (curses.KEY_BACKSPACE, "\b", "\x7f", 127):
            if cursor > 0:
                text = text[: cursor - 1] + text[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            text = text[:cursor] + text[cursor + 1:]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(text), cursor + 1)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = len(text)
        elif key == "\x15":              # Ctrl-U clears the line
            text, cursor = "", 0
        elif isinstance(key, str) and key.isprintable():
            text = text[:cursor] + key + text[cursor:]
            cursor += 1

    curses.curs_set(0)
    stdscr.touchwin()
    stdscr.refresh()
    return text


def popup_paste(stdscr, title: str, prompt: str, counter=None, initial: str = "") -> str:
    """Multi-line box for pasted text. Returns the text, or None when cancelled.

    A terminal paste arrives as ordinary keystrokes, newlines included, so the
    only thing needed to accept forty share links at once is an Enter that adds
    a line instead of submitting. Ctrl-D (or F2) finishes.

    `counter(text)` may return a one-line summary shown while typing -- the
    links screen uses it to say how many links it can see so far.
    """
    width = min(curses.COLS - 4, max(56, curses.COLS - 10))
    height = min(curses.LINES - 2, 18)
    window = center_window(height, width)
    window.timeout(-1)
    lines = str(initial).split("\n") if initial else [""]
    field_width = width - 4
    rows = height - 6
    # The summary is recomputed per line, not per keystroke: a pasted
    # subscription is thousands of keystrokes, and re-parsing all of it on each
    # one would turn the paste into a crawl.
    summary, summarised = "", -1

    curses.curs_set(1)
    while True:
        window.erase()
        draw_frame(window, 0, 0, height, width, title, focused=True)
        addstr(window, 1, 2, clip(prompt, field_width), attr(DIM))

        top = max(0, len(lines) - rows)
        for offset, line in enumerate(lines[top: top + rows]):
            shown = line[-(field_width - 1):] if len(line) >= field_width else line
            addstr(window, 3 + offset, 2, shown, attr(NORMAL))

        if len(lines) != summarised:
            summarised = len(lines)
            summary = (counter("\n".join(lines)) if counter
                       else f"{summarised} line{'s' if summarised > 1 else ''}")
        addstr(window, height - 3, 2, clip(summary, field_width), attr(OK))
        addstr(window, height - 2, 2, "Ctrl-D done · Esc cancel", attr(KEYHINT))
        cursor_row = 3 + min(len(lines) - 1 - top, rows - 1)
        try:
            window.move(cursor_row, 2 + min(len(lines[-1]), field_width - 1))
        except curses.error:
            pass
        window.refresh()

        key = getkey(window)
        if key is None:
            continue
        if key in ("\x04", curses.KEY_F2):        # Ctrl-D
            break
        if is_escape(key):
            lines = None
            break
        if is_enter(key):
            lines.append("")
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f", 127):
            if lines[-1]:
                lines[-1] = lines[-1][:-1]
            elif len(lines) > 1:
                lines.pop()
        elif key == "\x15":                       # Ctrl-U drops the current line
            lines[-1] = ""
        elif isinstance(key, str) and key.isprintable():
            lines[-1] += key

    curses.curs_set(0)
    stdscr.touchwin()
    stdscr.refresh()
    return None if lines is None else "\n".join(lines).strip()


def popup_list(stdscr, title: str, items: list, current: int = 0,
               hint: str = "↑/↓ move · Enter select · Esc cancel") -> int:
    """Pick one of `items` (list of str or (label, description)). None = cancel."""
    labels, notes = [], []
    for item in items:
        if isinstance(item, (tuple, list)):
            labels.append(str(item[0]))
            notes.append(str(item[1]) if len(item) > 1 else "")
        else:
            labels.append(str(item))
            notes.append("")

    widest = max([len(l) for l in labels] + [len(n) for n in notes] + [len(hint), len(title)])
    width = min(curses.COLS - 4, max(40, widest + 8))
    visible = min(len(labels), max(3, curses.LINES - 8))
    height = min(curses.LINES - 2, visible + 4 + (1 if any(notes) else 0))
    window = center_window(height, width)
    window.timeout(-1)
    index = max(0, min(current, len(labels) - 1))
    top = 0

    while True:
        rows = height - 4 - (1 if any(notes) else 0)
        top = max(0, min(top, max(0, len(labels) - rows)))
        if index < top:
            top = index
        if index >= top + rows:
            top = index - rows + 1

        window.erase()
        draw_frame(window, 0, 0, height, width, title, focused=True)
        for offset in range(rows):
            position = top + offset
            if position >= len(labels):
                break
            selected = position == index
            style = attr(SELECT) if selected else attr(NORMAL)
            fill_line(window, 1 + offset, 1, width - 2, style)
            addstr(window, 1 + offset, 2, clip(labels[position], width - 4), style)
        if any(notes):
            addstr(window, height - 3, 2, clip(notes[index], width - 4), attr(DIM))
        addstr(window, height - 2, 2, clip(hint, width - 4), attr(KEYHINT))
        window.refresh()

        key = getkey(window)
        if key in (curses.KEY_UP, "k"):
            index = (index - 1) % len(labels)
        elif key in (curses.KEY_DOWN, "j"):
            index = (index + 1) % len(labels)
        elif key == curses.KEY_HOME:
            index = 0
        elif key == curses.KEY_END:
            index = len(labels) - 1
        elif key in (curses.KEY_NPAGE,):
            index = min(len(labels) - 1, index + rows)
        elif key in (curses.KEY_PPAGE,):
            index = max(0, index - rows)
        elif is_enter(key):
            break
        elif is_escape(key):
            index = None
            break

    stdscr.touchwin()
    stdscr.refresh()
    return index


def file_browser(stdscr, start: str, title: str = "Select file",
                 extensions=(".json",), pick_dir: bool = False) -> str:
    """Browse the filesystem; returns a path or None.

    `extensions` filters the files shown (None = show everything);
    `pick_dir` adds "d" for taking the directory itself, with files still
    listed and selectable -- a source can be either.
    """
    current = Path(start or Path.cwd()).expanduser()
    if current.is_file():
        current = current.parent
    if not current.is_dir():
        current = Path.home()

    width = min(curses.COLS - 4, max(60, curses.COLS - 10))
    height = min(curses.LINES - 2, max(12, curses.LINES - 6))
    window = center_window(height, width)
    window.timeout(-1)
    index, top, show_hidden, filter_text = 0, 0, False, ""

    def entries():
        items = [("..", current.parent, True)] if current.parent != current else []
        try:
            children = sorted(current.iterdir(),
                              key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            children = []
        for child in children:
            if not show_hidden and child.name.startswith("."):
                continue
            if filter_text and filter_text.lower() not in child.name.lower():
                continue
            if child.is_dir():
                items.append((child.name + "/", child, True))
            else:
                if extensions and child.suffix.lower() not in extensions:
                    continue
                items.append((child.name, child, False))
        return items

    while True:
        listing = entries()
        rows = height - 5
        index = max(0, min(index, max(0, len(listing) - 1)))
        if index < top:
            top = index
        if index >= top + rows:
            top = index - rows + 1
        top = max(0, min(top, max(0, len(listing) - rows)))

        window.erase()
        draw_frame(window, 0, 0, height, width, title, focused=True)
        addstr(window, 1, 2, clip(str(current), width - 4), attr(DIM, True))
        for offset in range(rows):
            position = top + offset
            if position >= len(listing):
                break
            label, path, is_dir = listing[position]
            selected = position == index
            style = attr(SELECT) if selected else (attr(TITLE) if is_dir else attr(NORMAL))
            fill_line(window, 2 + offset, 1, width - 2, style)
            suffix = ""
            if not is_dir:
                try:
                    suffix = f"{path.stat().st_size / 1024:.0f} KB"
                except OSError:
                    suffix = ""
            addstr(window, 2 + offset, 2, clip(label, width - 14), style)
            if suffix:
                addstr(window, 2 + offset, width - len(suffix) - 3, suffix, style)

        hint = ("Enter select · d use this dir · " if pick_dir else "Enter select · ")
        hint += "h up · / filter · . hidden · ~ home · Esc cancel"
        if filter_text:
            hint = f"filter: {filter_text}  (Esc clears)"
        addstr(window, height - 2, 2, clip(hint, width - 4), attr(KEYHINT))
        window.refresh()

        key = getkey(window)
        if key in (curses.KEY_UP, "k"):
            index -= 1
        elif key in (curses.KEY_DOWN, "j"):
            index += 1
        elif key == curses.KEY_NPAGE:
            index += rows
        elif key == curses.KEY_PPAGE:
            index -= rows
        elif key == curses.KEY_HOME:
            index = 0
        elif key == curses.KEY_END:
            index = len(listing) - 1
        elif key in (curses.KEY_LEFT, "h"):
            current, index, top, filter_text = current.parent, 0, 0, ""
        elif key == "~":
            current, index, top, filter_text = Path.home(), 0, 0, ""
        elif key == ".":
            show_hidden = not show_hidden
        elif key == "/":
            typed = popup_input(stdscr, "Filter", "Show entries containing:", filter_text)
            filter_text = typed or ""
            index, top = 0, 0
            window.touchwin()
        elif key in ("d", "D") and pick_dir:
            result = str(current)
            break
        elif is_enter(key) or key in (curses.KEY_RIGHT, "l"):
            if not listing:
                continue
            label, path, is_dir = listing[index]
            if is_dir:
                current, index, top, filter_text = path, 0, 0, ""
            else:
                result = str(path)
                break
        elif is_escape(key):
            if filter_text:
                filter_text, index, top = "", 0, 0
                continue
            result = None
            break

    stdscr.touchwin()
    stdscr.refresh()
    return result


def human_bytes(size) -> str:
    """A byte count in the largest unit that still reads as a small number."""
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return ""


def human_rate(per_second) -> str:
    return f"{human_bytes(per_second)}/s"


def human_size(path) -> str:
    try:
        return human_bytes(os.path.getsize(path))
    except OSError:
        return "missing"
