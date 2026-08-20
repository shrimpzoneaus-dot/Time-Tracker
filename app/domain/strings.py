"""Every user-facing string, in one table.

The legacy bot mixed English UI with Vietnamese error messages, so there was no
way to change the language without hunting through 2,000 lines. Staff-facing
text carries Vietnamese because that is what the bot already spoke to them;
the admin console is English only, because the admins are the people who read
the payroll reports.

Set APP_LANGUAGE=vi to serve Vietnamese, or call t(key, lang="vi") directly.
"""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    # --- clock screen ---
    "clock_in": {"en": "Clock In", "vi": "Bắt đầu ca"},
    "clock_out": {"en": "Clock Out", "vi": "Kết thúc ca"},
    "start_break": {"en": "Start Break", "vi": "Nghỉ giải lao"},
    "end_break": {"en": "End Break", "vi": "Quay lại làm"},
    "on_shift": {"en": "on shift", "vi": "đang làm"},
    "on_break": {"en": "on break", "vi": "đang nghỉ"},
    "not_working": {"en": "not clocked in", "vi": "chưa vào ca"},
    "started_at": {"en": "Started", "vi": "Vào ca"},
    "break_total": {"en": "Break", "vi": "Giải lao"},
    "today": {"en": "Today", "vi": "Hôm nay"},
    "this_month": {"en": "This month", "vi": "Tháng này"},
    "hours": {"en": "Hours", "vi": "Số giờ"},
    "gross": {"en": "Gross", "vi": "Lương gộp"},
    "advances": {"en": "Advances", "vi": "Đã ứng"},
    "net": {"en": "Net", "vi": "Còn lại"},
    "my_shifts": {"en": "My shifts", "vi": "Ca của tôi"},
    "no_shifts": {"en": "No shifts yet.", "vi": "Chưa có ca nào."},
    "sign_out": {"en": "Sign out", "vi": "Đăng xuất"},
    # --- messages ---
    "clocked_in_at": {"en": "Clocked in at {time}.", "vi": "Đã vào ca lúc {time}."},
    "clocked_out_at": {"en": "Clocked out at {time}.", "vi": "Đã kết thúc ca lúc {time}."},
    "break_started": {"en": "Break started.", "vi": "Bắt đầu giải lao."},
    "break_ended": {"en": "Break ended.", "vi": "Đã quay lại làm việc."},
    "shift_total": {"en": "Total worked: {duration}", "vi": "Tổng giờ làm: {duration}"},
    # --- errors ---
    "error_generic": {
        "en": "Something went wrong. Please try again.",
        "vi": "Có lỗi xảy ra. Vui lòng thử lại.",
    },
    "error_database": {
        "en": "Database error. Please try again shortly.",
        "vi": "Lỗi cơ sở dữ liệu. Vui lòng thử lại sau.",
    },
    "sign_in_prompt": {
        "en": "Open Telegram and send /start to get your sign-in link.",
        "vi": "Mở Telegram và gửi /start để nhận liên kết đăng nhập.",
    },
    "link_expired": {
        "en": "That sign-in link has expired. Send /start in Telegram for a new one.",
        "vi": "Liên kết đã hết hạn. Gửi /start trong Telegram để lấy liên kết mới.",
    },
    "open_timesheet": {"en": "Open my timesheet", "vi": "Mở bảng công của tôi"},
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry["en"]
    return text.format(**kwargs) if kwargs else text


def bilingual(key: str, **kwargs) -> str:
    """English with the Vietnamese underneath. Used by the bot, which talks to
    staff directly and where a wrong guess about the reader is costly."""
    english = t(key, "en", **kwargs)
    vietnamese = t(key, "vi", **kwargs)
    return english if english == vietnamese else f"{english}\n{vietnamese}"
